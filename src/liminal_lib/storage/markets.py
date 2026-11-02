"""Reference data about markets: exchanges, trade conditions, holidays and futures schedules, news, market proxies."""

from __future__ import annotations

import json
from datetime import date
from typing import Iterable, TYPE_CHECKING

import pandas as pd

if TYPE_CHECKING:
    from ..models import Exchange


class MarketsStore:
    """Store methods: reference data about markets: exchanges, trade conditions, holidays and futures schedules, news, market proxies."""

    # -- exchanges ---------------------------------------------------------

    def upsert_exchanges(self, exchanges: Iterable["Exchange"]) -> int:
        data = [
            (e.source, e.id, e.name, e.acronym, e.mic, e.type, e.country.code if e.country else None)
            for e in exchanges
        ]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO exchanges VALUES (?, ?, ?, ?, ?, ?, ?)", data)
        return len(data)

    def exchanges(self, source: str | None = None) -> pd.DataFrame:
        """The exchanges each feed lists (mic_names: each MIC's name)."""
        sql = "SELECT source, id, name, acronym, mic, type, country FROM exchanges"
        params: list = []
        if source:
            sql += " WHERE source=?"
            params.append(source)
        return pd.read_sql_query(sql + " ORDER BY source, id", self.conn, params=params)

    # -- trade condition rules ------------------------------------------------

    def save_conditions(self, source: str, rows: list[dict]) -> int:
        with self.conn:
            self.conn.execute("DELETE FROM conditions WHERE source=?", (source,))
            self.conn.executemany("INSERT INTO conditions VALUES (?, ?, ?, ?, ?, ?, ?, ?)", [
                (source, r["id"], r.get("name"), r.get("cta"), r.get("utp"), int(bool(r.get("high_low"))),
                 int(bool(r.get("open_close"))), int(bool(r.get("volume")))) for r in rows])
        return len(rows)

    def condition_rules(self, feed: str | None = None) -> dict[tuple[str, str], tuple[bool, bool, bool]]:
        """{(tape family 'cta'|'utp', code): (updates high/low, open/close, volume)} for one feed's
        trades. A feed whose reference this is (Massive) marks its trades with the reference's
        numeric ids: each takes the rule of the letter it stands for, overrides included
        (ticks.BAR_RULES), so a Massive odd lot sets no high or low. Any other feed (Alpaca) marks
        them with the tapes' letters, the overrides folded in here too. The two are never mixed: some
        letters are digits, and with one key for both, the letter '2' overwrote Massive's 2 (Average
        Price Trade), whose prints then set the highs and lows of bars built from Massive's trades
        (NVDA after hours: $177 to $251 in one minute at $231). The rules made here come first in
        ticks.eligibility, so the letter '4' (derivatively priced) never stands for Massive's id 4."""
        from ..ticks import BAR_RULES

        rows = self.conn.execute("SELECT source, id, cta, utp, high_low, open_close, volume FROM conditions").fetchall()
        numeric = feed is not None and any(r[0] == feed for r in rows)
        out = {}
        for source, cid, cta, utp, hl, oc, vol in rows:
            flags = (bool(hl), bool(oc), bool(vol))
            for fam, letter in (("cta", cta), ("utp", utp)):
                if numeric and source == feed:
                    out[(fam, str(cid))] = BAR_RULES.get((fam, letter), flags) if letter else flags
                elif not numeric and letter:
                    out[(fam, letter)] = BAR_RULES.get((fam, letter), flags)
        return out

    # -- official calendars --------------------------------------------------

    def save_market_holidays(self, source: str, rows: Iterable[dict]) -> int:
        data = [(source, r["date"], r["exchange"], r["status"], r.get("open_ms"), r.get("close_ms"), r.get("name"))
                for r in rows]
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO market_holidays VALUES (?, ?, ?, ?, ?, ?, ?)", data)
        return len(data)

    def market_holidays(self, source: str) -> list[tuple[date, str, int | None, int | None]]:
        """NYSE closures and early closes (Nasdaq where NYSE is not listed for a day)."""
        rows = self.conn.execute(
            "SELECT day, exchange, status, open_ms, close_ms FROM market_holidays WHERE source=? "
            "AND exchange IN ('NYSE', 'NASDAQ') ORDER BY day, exchange DESC", (source,)).fetchall()
        out: dict[str, tuple] = {}
        for day, exchange, status, o, c in rows:  # NYSE sorts after NASDAQ descending: NYSE wins
            if day not in out or exchange == "NYSE":
                out[day] = (date.fromisoformat(day), status, o, c)
        return list(out.values())

    def save_futures_schedule(self, source: str, product: str, rows: Iterable[tuple[str, str, int]]) -> int:
        """rows: (session date, event, ms). Replaces what was stored for those dates."""
        rows = list(rows)
        days = sorted({d for d, _, _ in rows})
        with self.conn:
            self.conn.executemany("DELETE FROM futures_schedule WHERE source=? AND product=? AND day=?",
                                  [(source, product.upper(), d) for d in days])
            self.conn.executemany("INSERT OR REPLACE INTO futures_schedule VALUES (?, ?, ?, ?, ?)",
                                  [(source, product.upper(), d, e, ms) for d, e, ms in rows])
        return len(rows)

    def futures_schedules(self, source: str) -> list[tuple[str, date, list[tuple[str, int]]]]:
        out: dict[tuple, list] = {}
        for product, day, event, ms in self.conn.execute(
                "SELECT product, day, event, ts_ms FROM futures_schedule WHERE source=? ORDER BY product, day, ts_ms",
                (source,)):
            out.setdefault((product, day), []).append((event, ms))
        return [(p, date.fromisoformat(d), ev) for (p, d), ev in out.items()]

    # A vendor's headlines are its plugin's documents (stores.py): this file's news, or the MongoDB
    # server's once the plugin's documents store moved there (store:effect:<plugin>:documents).
    def _news_docs(self, engine: str, start: bool = False):
        from .. import stores
        from .documents import NEWS

        return stores.backend(engine, NEWS, main_conn=self.conn, start=start,
                              data_dir=None if str(self.path) == ":memory:" else self.path.parent)

    def _news_moved(self) -> list[str]:
        from .. import stores

        return stores.moved_to(self.conn, "mongodb")

    def save_news(self, source: str, items: Iterable[dict]) -> int:
        docs = [{"source": source, "id": str(i["id"]), "published_ms": int(i["published_ms"]), "title": i["title"],
                 "publisher": i.get("publisher"), "url": i.get("url"), "tickers": i.get("tickers") or []}
                for i in items if i.get("id") and i.get("title")]
        if source in self._news_moved():
            try:
                return self._news_docs("mongodb", start=True).put(docs, replace=True)
            except Exception:  # noqa: BLE001 - the server down: this file holds them, the next settle copies them over
                pass
        return self._news_docs("sqlite").put(docs, replace=True)

    def _news_here(self, sources: list[str] | None, out_of: list[str], since_ms: int, limit: int) -> list[dict]:
        sql = "SELECT source, id, published_ms, title, publisher, url, tickers FROM news WHERE published_ms >= ?"
        params: list = [since_ms]
        if sources is not None:
            sql += f" AND source IN ({', '.join('?' * len(sources))})"
            params += sources
        if out_of:
            sql += f" AND source NOT IN ({', '.join('?' * len(out_of))})"
            params += out_of
        rows = self.conn.execute(sql + " ORDER BY published_ms DESC LIMIT ?", params + [limit]).fetchall()
        return [{"source": s, "id": i, "published_ms": t, "title": ti, "publisher": p, "url": u,
                 "tickers": json.loads(tk or "[]")} for s, i, t, ti, p, u, tk in rows]

    def news(self, source: str | None = None, since_ms: int = 0, limit: int = 60) -> list[dict]:
        """The newest headlines (of one source, or all), from where each plugin keeps them (MongoDB's
        from this file's copy while it does not answer)."""
        moved = self._news_moved()
        there = [s for s in ([source] if source else moved) if s in moved]
        out = [] if source and there else self._news_here([source] if source else None, moved, since_ms, limit)
        if there:
            try:
                got = self._news_docs("mongodb").find({"source": there}, since=since_ms, limit=limit)
            except Exception:  # noqa: BLE001 - the server down: this file's copy of them
                got = self._news_here(there, [], since_ms, limit)
            out = sorted(out + got, key=lambda d: d["published_ms"], reverse=True)[:limit]
        return out

    def latest_news_ms(self, source: str) -> int | None:
        if source in self._news_moved():
            try:
                return self._news_docs("mongodb").newest({"source": source})
            except Exception:  # noqa: BLE001 - the server down: this file's copy
                pass
        return self._news_docs("sqlite").newest({"source": source})

    def save_proxy_closes(self, source: str, day: date, closes: dict[str, float]) -> int:
        with self.conn:
            self.conn.executemany("INSERT OR REPLACE INTO proxy_daily VALUES (?, ?, ?, ?)",
                                  [(source, day.isoformat(), t, c) for t, c in closes.items()])
        return len(closes)

    def proxy_closes(self, source: str, day: date) -> dict[str, float]:
        return dict(self.conn.execute("SELECT ticker, close FROM proxy_daily WHERE source=? AND day=?",
                                      (source, day.isoformat())).fetchall())

    def snapshot_closes(self, source: str, day: date, tickers: list[str]) -> dict[str, float]:
        marks = ",".join("?" * len(tickers))
        return dict(self.conn.execute(
            f"SELECT ticker, close FROM market_snapshot WHERE source=? AND day=? AND ticker IN ({marks}) AND close IS NOT NULL",
            (source, day.isoformat(), *tickers)).fetchall())

    def proxy_days(self, source: str) -> list[date]:
        return [date.fromisoformat(d) for (d,) in self.conn.execute(
            "SELECT DISTINCT day FROM proxy_daily WHERE source=? ORDER BY day", (source,))]

    def proxy_moves(self, source: str = "massive") -> dict[str, dict]:
        """Latest daily change of each proxy: {ticker: {day, close, changePct}}."""
        days = self.proxy_days(source)[-2:]
        if len(days) < 2:
            return {}
        prev, last = (dict(self.conn.execute("SELECT ticker, close FROM proxy_daily WHERE source=? AND day=?",
                                             (source, d.isoformat())).fetchall()) for d in days)
        return {t: {"day": days[1].isoformat(), "close": c, "changePct": (c / prev[t] - 1) * 100}
                for t, c in last.items() if prev.get(t)}


def mic_names(exchanges: pd.DataFrame) -> dict[str, str]:
    """Each market identifier's name: its exchange's own. A tape shares its operator's MIC (Massive lists
    the Consolidated Tape Association under XNYS and the Unlisted Trading Privileges plan under XNAS),
    and was shown for every NYSE stock (the user, 2026-10-31: "is this the right corp name ?"), so an
    entry of type exchange comes first; among those, the first source's (alphabetical)."""
    best: dict[str, tuple] = {}
    for r in exchanges.itertuples():
        if r.mic and (r.mic not in best or (r.type == "exchange" and best[r.mic][1] != "exchange")):
            best[r.mic] = (r.name, r.type)
    return {mic: name for mic, (name, _) in best.items()}
