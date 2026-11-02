"""The fetcher's requests: what it was asked for, symbol by symbol (the Fetcher view shows them).

The user, 2026-11-01: "the fetcher takes a symbol and uses the plugins ... retrieve all info about
the symbol"; "the fetcher can take a queue/stack of requests". One row per ask: who asked (a chart
page that opened the symbol, the request box on a page, the command line, or the fetcher itself
for its standing work), which symbol, what about it (its kinds: bars, live ...), when. Asked
interactively, the newest goes first: a stack. The fetcher's own standing work goes in order: a
queue. A symbol asked again while its request is open is one request, asked anew.

A request is taken (assessing: the fetcher asks the plugins what each can give for the symbol),
then collecting (its plan on the plugins' runs), and ends done, failed or cancelled.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .common import _now

OPEN = ("waiting", "assessing", "collecting")
KINDS = ("bars", "live")  # what a request can ask about a symbol today (the contract's verbs bring the rest)
STATUS_KEY = "fetcher:status:v1"  # the fetcher's status, as it last wrote it (every page reads it here)
STATES = OPEN + ("done", "failed", "cancelled")
STANDING = "fetcher"  # `by` of the fetcher's own standing work: a queue behind the asks (a stack)
_COLS = ("id", "symbol", "kinds", "span", "by", "priority", "state", "plan", "detail", "asked_at", "started_at",
         "finished_at", "pid")
_IS_OPEN = "state IN ('waiting', 'assessing', 'collecting')"
# The order they are taken in, and shown: what is being collected, then what waits (an ask before
# standing work, the newest ask first, the oldest standing work first), then the newest ended
_ORDER = ("CASE state WHEN 'collecting' THEN 0 WHEN 'assessing' THEN 1 WHEN 'waiting' THEN 2 ELSE 3 END, "
          f"CASE WHEN {_IS_OPEN} THEN priority END, "
          f"CASE WHEN {_IS_OPEN} AND by = '{STANDING}' THEN 1 ELSE 0 END, "
          f"CASE WHEN {_IS_OPEN} AND by <> '{STANDING}' THEN asked_at END DESC, "
          f"CASE WHEN {_IS_OPEN} AND by = '{STANDING}' THEN asked_at END, "
          f"CASE WHEN NOT ({_IS_OPEN}) THEN finished_at END DESC, id DESC")


def _iso(at: datetime | str | None) -> str | None:
    if at is None or isinstance(at, str):
        return at
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


def _row(r) -> dict:
    d = dict(zip(_COLS, r))
    d["kinds"] = tuple(k for k in (d["kinds"] or "").split(",") if k)
    d["plan"] = json.loads(d["plan"]) if d["plan"] else None
    return d


class RequestStore:
    """Store methods: the fetcher's requests (see the module's docstring)."""

    def request_put(self, symbol: str, kinds=("bars",), by: str = "chart", priority: int = 0, span: str | None = None,
                    now: datetime | None = None) -> dict:
        """Ask for a symbol: a new request, or the symbol's open one asked anew (its kinds joined,
        asked now, by this asker; a request being collected goes on). Returns the request."""
        symbol = symbol.strip().upper()
        if not 0 < len(symbol) <= 32:
            raise ValueError(f"not a symbol: {symbol!r}")
        wanted = tuple(dict.fromkeys(str(k) for k in kinds if k)) or ("bars",)
        at = _iso(now) or _now()
        with self.conn:
            open_row = self.conn.execute(
                f"SELECT {', '.join(_COLS)} FROM fetch_requests WHERE symbol=? AND {_IS_OPEN} ORDER BY id LIMIT 1",
                (symbol,)).fetchone()
            if open_row is not None:
                r = _row(open_row)
                joined = ",".join(dict.fromkeys(r["kinds"] + wanted))
                self.conn.execute("UPDATE fetch_requests SET kinds=?, by=?, priority=MIN(priority, ?), asked_at=?, "
                                  "span=COALESCE(?, span) WHERE id=?", (joined, by, int(priority), at, span, r["id"]))
                return self.request_get(r["id"])
            cur = self.conn.execute(
                "INSERT INTO fetch_requests (symbol, kinds, span, by, priority, state, asked_at) VALUES (?, ?, ?, ?, ?, "
                "'waiting', ?)", (symbol, ",".join(wanted), span, by, int(priority), at))
            return self.request_get(cur.lastrowid)

    def request_get(self, request_id: int) -> dict | None:
        r = self.conn.execute(f"SELECT {', '.join(_COLS)} FROM fetch_requests WHERE id=?", (int(request_id),)).fetchone()
        return _row(r) if r else None

    def request_next(self, now: datetime | None = None) -> dict | None:
        """The next request to work on, taken by this process (assessing): the newest ask, else the
        oldest standing work. None when nothing waits."""
        at = _iso(now) or _now()
        with self.conn:
            r = self.conn.execute(
                "UPDATE fetch_requests SET state='assessing', started_at=?, pid=? WHERE id = (SELECT id FROM fetch_requests "
                f"WHERE state='waiting' ORDER BY {_ORDER} LIMIT 1) RETURNING " + ", ".join(_COLS),
                (at, os.getpid())).fetchone()
        return _row(r) if r else None

    def request_update(self, request_id: int, state: str | None = None, plan=None, detail: str | None = None,
                       now: datetime | None = None) -> dict | None:
        """A request moved on: its state (collecting, done, failed, cancelled), its plan (the
        assessment, kept as JSON) and what is happening to it."""
        if state is not None and state not in STATES:
            raise ValueError(f"not a request state: {state!r}")
        sets, args = [], []
        if state is not None:
            sets.append("state=?")
            args.append(state)
            if state not in OPEN:
                sets.append("finished_at=?")
                args.append(_iso(now) or _now())
                sets.append("pid=NULL")
        if plan is not None:
            sets.append("plan=?")
            args.append(json.dumps(plan))
        if detail is not None:
            sets.append("detail=?")
            args.append(detail[:400])
        if sets:
            with self.conn:
                self.conn.execute(f"UPDATE fetch_requests SET {', '.join(sets)} WHERE id=?", [*args, int(request_id)])
        return self.request_get(request_id)

    def request_cancel(self, request_id: int, now: datetime | None = None) -> bool:
        """An open request taken back (cancelled). False: it was not open."""
        with self.conn:
            n = self.conn.execute("UPDATE fetch_requests SET state='cancelled', finished_at=?, pid=NULL, detail='cancelled' "
                                  f"WHERE id=? AND {_IS_OPEN}", (_iso(now) or _now(), int(request_id))).rowcount
        return n > 0

    def request_reset(self) -> int:
        """Requests left assessing or collecting by a fetcher that ended before them (its process
        gone): waiting again, for the fetcher starting now."""
        with self.conn:
            return self.conn.execute("UPDATE fetch_requests SET state='waiting', pid=NULL, started_at=NULL, "
                                     "detail='the fetcher ended before it' WHERE state IN ('assessing', 'collecting')").rowcount

    def requests_open(self) -> list[dict]:
        """The open requests (collecting, assessing, waiting), in the order they are taken."""
        return [_row(r) for r in self.conn.execute(
            f"SELECT {', '.join(_COLS)} FROM fetch_requests WHERE {_IS_OPEN} ORDER BY {_ORDER}")]

    def request_rows(self, state: str | None = None, limit: int = 200) -> list[dict]:
        """The requests as the Fetcher shows them: what is being collected, what waits in its order,
        then the newest ended."""
        where = " WHERE state=?" if state else ""
        args = [state] if state else []
        return [_row(r) for r in self.conn.execute(
            f"SELECT {', '.join(_COLS)} FROM fetch_requests{where} ORDER BY {_ORDER} LIMIT ?", [*args, int(limit)])]

    def request_counts(self) -> dict[str, int]:
        return {state: n for state, n in self.conn.execute(
            "SELECT state, COUNT(*) FROM fetch_requests GROUP BY state")}

    def requests_prune(self, keep: int = 500) -> int:
        """The oldest ended requests beyond `keep` forgotten (the stack never grows without end)."""
        with self.conn:
            return self.conn.execute(
                f"DELETE FROM fetch_requests WHERE NOT ({_IS_OPEN}) AND id NOT IN ("
                f"SELECT id FROM fetch_requests WHERE NOT ({_IS_OPEN}) ORDER BY finished_at DESC, id DESC LIMIT ?)",
                (int(keep),)).rowcount


def read_status(store) -> dict | None:
    """The fetcher's last status, as it wrote it (None: never ran here)."""
    raw = store.kv_get(STATUS_KEY)
    try:
        return json.loads(raw) if raw else None
    except (TypeError, ValueError):
        return None


def write_status(store, status: dict) -> None:
    store.kv_set(STATUS_KEY, json.dumps(status))
