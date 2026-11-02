"""Closed history: a feed's closed months of bars in DuckDB, a file a month (history/<feed>/<YYYY-MM>.<n>.duckdb).

The user, 2026-10-31: "lets roll out mongo and duck support", "each module needs to support sqlite
or mongo or duck db based on data type", "lets try to use the optimal data store for the data
type". A feed's bars are kept in SQLite while their month is open (the sync and the live recorder
write them a batch at a time, with revisions and the change log) and in DuckDB once it is closed:
read by column, a month of Massive's minute and second bars (September 2026, 180 million) took 22%
of SQLite's space and its market-wide scans ran over 100 times faster (results-massive-2026-09).

Moved gradually (the user's choice, the same day): the hourly sync starts `history run` in a process
of its own, which copies closed months newest first within a time budget. Each month is read a
series at a time (short reads: the feed's writes and checkpoints are never held up), written to a
new file, checked against what was read (rows and checksums) and only then entered in the feed's
manifest (`history`, in its SQLite file). The SQLite rows stay ("we should always keep all source
data"): DuckDB is where the month is read from, not the only copy.

A write to an archived month in SQLite (a correction, a rebuild) marks it stale at once: triggers in
the feed's file, so every writer keeps it whatever its code version. A stale month is read
from SQLite until it is copied again, once quiet (QUIET_DAYS without a change). What is archived:
`bars` and `built_bars` up to the day; the weeks, months and years built from the days stay in SQLite
(derive.py rebuilds them whole from the days, so a symbol's 2003 year bar is written again every hour:
every month of every feed read as written lately, none ever closed), as do the pruned list and the
revisions (small, and changed), so the filtered view and the as-of reads come out the same.
"""
from __future__ import annotations

import functools
import os
import sqlite3
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

import pandas as pd

from ..layout import HISTORY  # the folder beside the feeds
from .common import _distinct, write_transaction

CLOSED_AFTER_DAYS = 7  # a month is closed this long after its end (late corrections come within days)
QUIET_DAYS = 2  # copied (again) once nothing in it changed for this long
BUDGET_S = 20 * 60  # a run's time for copying (the hourly sync starts one an hour)
MONTHS_PER_FEED = 3  # at most this many months of a feed a run
CHUNK_ROWS = 100_000  # rows held in memory between SQLite and DuckDB (some 40 MB as Python rows)
WRITE_MEMORY = "512MB"  # DuckDB's own limit while writing a month, and its threads
WRITE_THREADS = 2
READ_MEMORY = "256MB"  # a reading store's DuckDB, and how many months it keeps attached
READ_THREADS = 2
ATTACHED = 12
CALENDAR = ("week", "month", "year")  # built whole from the days (derive.py): kept in SQLite, never archived
COLUMNS = ("ticker", "timespan", "multiplier", "ts", "open", "high", "low", "close", "volume", "vwap",
           "transactions", "stored_at")
TABLES = {"bars": COLUMNS, "built_bars": (*COLUMNS[:-1], "derivation", "stored_at")}

# The manifest, in the feed's SQLite file: each archived month, its file and what was copied. The
# triggers mark a month stale when a bar in it (not a week's, month's or year's: never archived) is
# written or removed in SQLite, by the bar's own time (the change log merges a minute's writes of a
# series into one span); the WHEN keeps a write after the newest archived month (nearly every
# write) to an index seek. A trigger whose text changed is made again (ensure: TRIGGER_MARK).
SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    month       TEXT PRIMARY KEY,   -- YYYY-MM (UTC)
    lo_ms       INTEGER NOT NULL,   -- its first ms
    hi_ms       INTEGER NOT NULL,   -- the next month's first ms
    file        TEXT NOT NULL,      -- in history/<feed>/
    bars        INTEGER NOT NULL,   -- rows copied from bars
    built       INTEGER NOT NULL,   -- from built_bars
    checksum    TEXT NOT NULL,      -- per table: rows, the sum of ts % 1000003, the volume
    bytes       INTEGER NOT NULL,
    archived_at INTEGER NOT NULL,   -- ms: when its copy began (a write logged since kept it out)
    seconds     REAL,               -- how long the copy took
    stale       INTEGER NOT NULL DEFAULT 0  -- 1: written in SQLite since: read there until copied again
);
CREATE INDEX IF NOT EXISTS history_current ON history (stale, hi_ms);
CREATE INDEX IF NOT EXISTS changes_by_minute ON changes (minute);
""" + "".join(f"""
CREATE TRIGGER IF NOT EXISTS history_stale_{table}_{event} AFTER {event.upper()} ON {table}
WHEN {row}.timespan NOT IN ('week', 'month', 'year') AND {row}.ts < (SELECT MAX(hi_ms) FROM history WHERE stale=0)
BEGIN
    UPDATE history SET stale=1 WHERE stale=0 AND lo_ms <= {row}.ts AND hi_ms > {row}.ts;
END;
""" for table in ("bars", "built_bars") for event, row in (("insert", "new"), ("update", "new"), ("delete", "old")))
TRIGGER_MARK = "timespan NOT IN"  # in the current triggers' text; one without it is from before


@functools.cache
def available() -> bool:
    """Whether duckdb is installed here (a project dependency; without it every bar is read from SQLite)."""
    try:
        import duckdb  # noqa: F401
    except ImportError:
        return False
    return True


def month_of(ms: int) -> str:
    d = datetime.fromtimestamp(ms / 1000, timezone.utc)
    return f"{d.year:04d}-{d.month:02d}"


def bounds(month: str) -> tuple[int, int]:
    """A month's [first ms, the next month's first ms) in UTC."""
    y, m = (int(x) for x in month.split("-"))
    lo = datetime(y, m, 1, tzinfo=timezone.utc)
    hi = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
    return int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)


def _months(lo_ms: int, hi_ms: int) -> list[str]:
    """The months from lo's to hi's (inclusive), oldest first."""
    out, m = [], month_of(lo_ms)
    last = month_of(hi_ms)
    while m <= last:
        out.append(m)
        m = month_of(bounds(m)[1])
    return out


def has_manifest(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='history'").fetchone() is not None


def ensure(conn: sqlite3.Connection) -> None:
    """The manifest and its triggers in a feed's file (once; the change log must be there); triggers
    made by an older version are made again."""
    found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'history_stale_%'"))
    if has_manifest(conn) and len(found) == 6 and all(TRIGGER_MARK in sql for sql in found.values()):
        return
    with write_transaction(conn):
        for name in found:  # all six in one transaction: no write runs between without one
            conn.execute(f"DROP TRIGGER {name}")
    conn.executescript(SCHEMA)  # each statement IF NOT EXISTS: processes doing it at once are fine


def current(conn: sqlite3.Connection, lo_ms: int | None, hi_ms: int | None) -> list[tuple[int, int, str]]:
    """The archived months read from DuckDB in [lo, hi): (lo_ms, hi_ms, file) of each current
    (not stale) one, oldest first. None for an edge: open on that side."""
    if not has_manifest(conn):
        return []
    return conn.execute("SELECT lo_ms, hi_ms, file FROM history WHERE stale=0 AND hi_ms > ? AND lo_ms < ? ORDER BY lo_ms",
                        (-(2**62) if lo_ms is None else int(lo_ms), 2**62 if hi_ms is None else int(hi_ms))).fetchall()


def manifest(conn: sqlite3.Connection) -> list[dict]:
    if not has_manifest(conn):
        return []
    cur = conn.execute("SELECT * FROM history ORDER BY month DESC")
    names = [c[0] for c in cur.description]
    return [dict(zip(names, r)) for r in cur]


def outside(months: list[tuple[int, int, str]]) -> tuple[str, list[int]]:
    """SQL (with its arguments) leaving out the archived months' bar times, for the part read from SQLite."""
    return "".join(" AND NOT (ts >= ? AND ts < ?)" for _ in months), [x for lo, hi, _ in months for x in (lo, hi)]


def plain(df: pd.DataFrame) -> pd.DataFrame:
    """DuckDB's nullable integers as SQLite's reads give them: int64, or float64 where one is missing."""
    for c in df.columns:
        if str(df[c].dtype) in ("Int32", "Int64"):
            df[c] = df[c].astype("float64") if df[c].isna().any() else df[c].astype("int64")
    return df


class Reader:
    """A store's DuckDB for reading archived months: one in-memory database (one memory limit), each
    month's file attached read-only on first use, the least recently used detached past ATTACHED.
    Files are never changed once in the manifest (a month copied again gets a new file), so any
    number of processes read them at once."""

    def __init__(self, folder: Path | None):
        self.folder = folder
        self._db = None
        self._attached: OrderedDict[str, str] = OrderedDict()  # path -> alias
        self._n = 0
        self._lock = threading.Lock()

    def _connect(self):
        if self._db is None:
            import duckdb

            self._db = duckdb.connect(":memory:", config={"memory_limit": READ_MEMORY, "threads": READ_THREADS})
        return self._db

    def _alias(self, path: Path) -> str:
        key = str(path)
        alias = self._attached.get(key)
        if alias is not None:
            self._attached.move_to_end(key)
            return alias
        db = self._connect()
        self._n += 1
        alias = f"m{self._n}"
        db.execute(f"ATTACH '{key.replace(chr(39), chr(39) * 2)}' AS {alias} (READ_ONLY)")
        self._attached[key] = alias
        while len(self._attached) > ATTACHED:
            _, old = self._attached.popitem(last=False)
            db.execute(f"DETACH {old}")
        return alias

    def frame(self, feed: str, file: str, sql: str, params: Iterable) -> pd.DataFrame:
        """`sql` with {t_bars} / {t_built_bars} for the month's tables, as a frame."""
        with self._lock:
            alias = self._alias(self.folder / feed / file)
            q = sql.format(**{f"t_{t}": f"{alias}.{t}" for t in TABLES})
            return plain(self._connect().execute(q, list(params)).df())

    def rows(self, feed: str, file: str, sql: str, params: Iterable) -> list[tuple]:
        with self._lock:
            alias = self._alias(self.folder / feed / file)
            q = sql.format(**{f"t_{t}": f"{alias}.{t}" for t in TABLES})
            return [tuple(r) for r in self._connect().execute(q, list(params)).fetchall()]

    def forget(self, path: Path) -> None:
        """Detach a file (one the archiver replaced), so it can be removed."""
        with self._lock:
            alias = self._attached.pop(str(path), None)
            if alias is not None and self._db is not None:
                self._db.execute(f"DETACH {alias}")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
            self._db, self._attached = None, OrderedDict()


# -- archiving --------------------------------------------------------------------------------

@dataclass
class Copied:
    month: str
    file: str
    bars: int
    built: int
    bytes: int
    seconds: float
    kept: bool  # entered in the manifest (False: written in SQLite while copied; tried again later)


def _now_ms() -> int:
    return int(time.time() * 1000)


def due(conn: sqlite3.Connection, now_ms: int | None = None,
        spans: list[tuple[str, str, str, int, int, int]] | None = None) -> list[str]:
    """The feed's closed months to copy, newest first: ones some series has bars in (`spans`, see
    series_spans), closed CLOSED_AFTER_DAYS since, not current in the manifest, and nothing written
    in them for QUIET_DAYS (the change log; while it does not reach that far back, none)."""
    now_ms = _now_ms() if now_ms is None else now_ms
    spans = series_spans(conn) if spans is None else spans
    if not spans:
        return []
    since = conn.execute("SELECT MIN(ms) FROM changes_since").fetchone()[0]
    quiet_from = now_ms - QUIET_DAYS * 86_400_000
    if since is None or since > quiet_from:
        return []
    have = {m for (m,) in conn.execute("SELECT month FROM history WHERE stale=0")} if has_manifest(conn) else set()
    busy = _merged(conn.execute(f"SELECT lo, hi FROM changes WHERE minute >= ? AND timespan NOT IN {CALENDAR}",
                                (quiet_from // 60_000,)).fetchall())
    held = _merged([(s[4], s[5]) for s in spans])
    out = []
    for m in reversed(_months(held[0][0], min(held[-1][1], now_ms))):
        lo, hi = bounds(m)
        if hi + CLOSED_AFTER_DAYS * 86_400_000 > now_ms or m in have:
            continue
        if not any(a < hi and b >= lo for a, b in held) or any(a < hi and b >= lo for a, b in busy):
            continue
        out.append(m)
    return out


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Overlapping [lo, hi] spans as one (a day of the recorder's writes: some 100,000 entries, a few spans)."""
    out: list[list[int]] = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1] + 1:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def series_spans(conn: sqlite3.Connection) -> list[tuple[str, str, str, int, int, int]]:
    """(table, ticker, timespan, multiplier, first ts, last ts) of every series in bars and
    built_bars but the calendar frames (CALENDAR): a primary-key seek per series and two more for
    its edges."""
    out = []
    for table in TABLES:
        for t, span, mult in _distinct(conn, table, ("ticker", "timespan", "multiplier"), ("ts",)):
            if span in CALENDAR:
                continue
            lo = conn.execute(f"SELECT MIN(ts) FROM {table} WHERE ticker=? AND timespan=? AND multiplier=?",
                              (t, span, mult)).fetchone()[0]
            hi = conn.execute(f"SELECT MAX(ts) FROM {table} WHERE ticker=? AND timespan=? AND multiplier=?",
                              (t, span, mult)).fetchone()[0]
            if lo is not None:
                out.append((table, t, span, mult, lo, hi))
    return out


def _checksum(n: int, tsmod: int, volume: float) -> str:
    return f"{n}:{tsmod}:{volume:.6g}"


def archive_month(conn: sqlite3.Connection, folder: Path, feed: str, month: str,
                  spans: list[tuple[str, str, str, int, int, int]] | None = None, now_ms: int | None = None,
                  code: str = "", stop: Callable[[], bool] | None = None) -> Copied | None:
    """Copy one month of a feed's bars into a new DuckDB file and enter it in the manifest, if
    nothing was written in it meanwhile (else the file is removed and it is tried again later).
    None if `stop` asked to end before it was done (nothing kept)."""
    import duckdb

    ensure(conn)
    started = _now_ms() if now_ms is None else now_ms
    t0 = time.monotonic()
    lo, hi = bounds(month)
    spans = series_spans(conn) if spans is None else spans
    dest = folder / feed
    dest.mkdir(parents=True, exist_ok=True)
    versions = [p.name.split(".")[1] for p in dest.glob(f"{month}.*.duckdb*")]
    name = f"{month}.{1 + max([int(v) for v in versions if v.isdigit()] or [0])}.duckdb"
    part = dest / (name + ".part")
    sums: dict[str, tuple[int, int, float]] = {}
    written = False
    db = duckdb.connect(str(part), config={"memory_limit": WRITE_MEMORY, "threads": WRITE_THREADS})
    try:
        for table, cols in TABLES.items():
            db.execute(f"CREATE TABLE {table} (ticker VARCHAR, timespan VARCHAR, multiplier INTEGER, ts BIGINT, "
                       "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, vwap DOUBLE, transactions BIGINT, "
                       + ("derivation BIGINT, " if table == "built_bars" else "") + "stored_at BIGINT)")
            count, tsmod, volume = 0, 0, 0.0
            buf: list[tuple] = []
            for _, t, span, mult, first, last in sorted(s for s in spans if s[0] == table):
                if last < lo or first >= hi:
                    continue
                if stop is not None and stop():
                    return None
                cur = conn.execute(f"SELECT {', '.join(cols)} FROM {table} WHERE ticker=? AND timespan=? AND "
                                   "multiplier=? AND ts >= ? AND ts < ? ORDER BY ts", (t, span, mult, lo, hi))
                while rows := cur.fetchmany(CHUNK_ROWS):  # a busy stock's month of seconds: over half a million
                    for r in rows:
                        tsmod += r[3] % 1_000_003
                        volume += r[8] or 0.0
                    count += len(rows)
                    buf.extend(rows)
                    if len(buf) >= CHUNK_ROWS:
                        _insert(db, table, cols, buf)
            _insert(db, table, cols, buf)
            sums[table] = (count, tsmod, volume)
        db.execute("CREATE TABLE about AS SELECT ? AS feed, ? AS month, ?::BIGINT AS lo_ms, ?::BIGINT AS hi_ms, "
                   "?::BIGINT AS archived_at, ? AS code", [feed, month, lo, hi, started, code])
        for table, (count, tsmod, volume) in sums.items():  # what was written is what was read
            got = db.execute(f"SELECT COUNT(*), COALESCE(SUM(ts % 1000003), 0), COALESCE(SUM(volume), 0) FROM {table}"
                             ).fetchone()
            if got[0] != count or got[1] != tsmod or abs(got[2] - volume) > 1e-6 * max(1.0, abs(volume)):
                raise RuntimeError(f"{feed} {month} {table}: wrote {got}, read {(count, tsmod, volume)}")
        db.execute("CHECKPOINT")
        written = True
    finally:
        db.close()
        if not written:
            _remove(part)
    seconds = round(time.monotonic() - t0, 1)
    checksum = ";".join(f"{t}={_checksum(*sums[t])}" for t in TABLES)
    size = part.stat().st_size
    with write_transaction(conn):  # nothing written in the month since the copy began: entered at once
        changed = conn.execute("SELECT 1 FROM changes WHERE minute >= ? AND lo < ? AND hi >= ? LIMIT 1",
                               (started // 60_000, hi, lo)).fetchone() is not None
        if not changed:
            os.replace(part, dest / name)
            conn.execute("INSERT OR REPLACE INTO history (month, lo_ms, hi_ms, file, bars, built, checksum, bytes, "
                         "archived_at, seconds, stale) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                         (month, lo, hi, name, sums["bars"][0], sums["built_bars"][0], checksum, size, started, seconds))
    if changed:
        _remove(part)
    return Copied(month, name, sums["bars"][0], sums["built_bars"][0], size, seconds, not changed)


def _insert(db, table: str, cols: tuple[str, ...], buf: list[tuple]) -> None:
    if buf:
        chunk = pd.DataFrame(buf, columns=list(cols))
        db.register("chunk", chunk)
        db.execute(f"INSERT INTO {table} SELECT * FROM chunk")
        db.unregister("chunk")
        buf.clear()


def _remove(path: Path) -> None:
    for p in (path, path.with_name(path.name + ".wal")):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def tidy(conn: sqlite3.Connection, folder: Path, feed: str) -> int:
    """Remove the feed's month files the manifest no longer names (a month copied again) and parts
    left by a run that ended: a file still open in a reader is left for the next run. Returns how many."""
    dest = folder / feed
    if not dest.is_dir():
        return 0
    keep = {f for (f,) in conn.execute("SELECT file FROM history")} if has_manifest(conn) else set()
    gone = 0
    for p in dest.iterdir():
        if p.name not in keep and (p.suffix == ".duckdb" or p.name.endswith(".duckdb.part") or p.name.endswith(".wal")):
            if p.name.endswith(".wal") and p.name[:-4] in keep:
                continue
            try:
                p.unlink()
                gone += 1
            except OSError:
                pass
    return gone


def run(feeds: dict[str, sqlite3.Connection], folder: Path, log: Callable[[str], None], budget_s: float = BUDGET_S,
        months_per_feed: int = MONTHS_PER_FEED, now_ms: int | None = None, code: str = "",
        stop: Callable[[], bool] | None = None) -> list[tuple[str, Copied]]:
    """Copy due months of each feed (by name: its SQLite connection), newest first, a feed's at most
    `months_per_feed`, until the budget is spent (a month begun is finished). Returns what was copied."""
    t0 = time.monotonic()
    done: list[tuple[str, Copied]] = []
    for feed, conn in feeds.items():
        ensure(conn)
        tidy(conn, folder, feed)
        if time.monotonic() - t0 > budget_s or (stop is not None and stop()):
            break
        spans = series_spans(conn)
        months = due(conn, now_ms, spans)[:months_per_feed]
        for m in months:
            if time.monotonic() - t0 > budget_s or (stop is not None and stop()):
                break
            got = archive_month(conn, folder, feed, m, spans, code=code, stop=stop)
            if got is None:
                break
            done.append((feed, got))
            log(f"history: {feed} {m} " + (f"copied to DuckDB: {got.bars:,} bars, {got.built:,} built, "
                                           f"{got.bytes / 1e6:,.1f} MB in {got.seconds:,.0f} s" if got.kept else
                                           "written in SQLite while copied; tried again once quiet"))
    return done
