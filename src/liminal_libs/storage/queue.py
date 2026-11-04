"""The fetch queue: every resource a plugin downloads, or will (the Fetcher view shows it).

The user, 2026-10-31: "each plugin will manage it's own fetching", "the fetcher will then just show
the queue of all resources being downloaded like symbols", "the plugin status will be available in
the plugins:: plugin page". One row per resource, by plugin, its part (a feed or a publisher in it),
kind and key: a symbol's bars, a news feed's read, a dataset's file. A row is put on the queue
waiting, taken (running, with the process taking it), and ends done, failed or held until a time;
asked again, it is waiting again. The plugins' own fetching (sources/fetching.py) and the app's
paths not yet moved into a plugin (the bars sync, the news reader, the datasets' files) keep it.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Iterable

from .common import _now

STATES = ("running", "waiting", "held", "failed", "done")
_COLS = ("plugin", "part", "kind", "key", "title", "state", "priority", "detail", "not_before", "queued_at",
         "started_at", "finished_at", "requests", "rows", "bytes", "attempts", "error", "pid")
# the view's order: what is being fetched, then what waits (by priority), what is held or failed,
# and the newest done
_ORDER = ("CASE state WHEN 'running' THEN 0 WHEN 'waiting' THEN 1 WHEN 'held' THEN 2 WHEN 'failed' THEN 3 ELSE 4 END, "
          "CASE WHEN state='done' THEN '' ELSE printf('%012d', priority) END, "
          "CASE WHEN state='done' THEN finished_at END DESC, queued_at, key")


def _iso(at: datetime | str | None) -> str | None:
    if at is None or isinstance(at, str):
        return at
    return at.astimezone(timezone.utc).isoformat(timespec="seconds")


class QueueStore:
    """Store methods: the fetch queue (see the module's docstring)."""

    def queue_put(self, plugin: str, part: str, kind: str, items: Iterable[dict], now: datetime | None = None,
                  replace: bool = True) -> int:
        """Put resources on the queue, waiting (not asked before `not_before`, when given): each item
        {key, title, detail?, priority?, not_before?}. One being fetched stays running; one done,
        failed or held waits again (`replace=False`: one on the queue already stays as it is).
        Returns how many."""
        at = _iso(now) or _now()
        rows = [(plugin, part, kind, str(i["key"]), i.get("title") or str(i["key"]), "waiting",
                 int(i.get("priority") or 0), i.get("detail"), _iso(i.get("not_before")), at) for i in items]
        with self.conn:
            self.conn.executemany(
                "INSERT INTO fetch_queue (plugin, part, kind, key, title, state, priority, detail, not_before, queued_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (plugin, part, kind, key) DO "
                + ("NOTHING" if not replace else "UPDATE SET "
                "title=excluded.title, priority=excluded.priority, not_before=excluded.not_before, "
                "detail=CASE WHEN state='running' THEN detail ELSE excluded.detail END, "
                "queued_at=CASE WHEN state IN ('waiting', 'held') THEN queued_at ELSE excluded.queued_at END, "
                "state=CASE WHEN state='running' THEN state ELSE excluded.state END"), rows)
        return len(rows)

    def queue_start(self, plugin: str, part: str, kind: str, key: str, title: str | None = None,
                    detail: str | None = None, now: datetime | None = None) -> None:
        """A resource being fetched now, by this process (put on the queue if it was not)."""
        at = _iso(now) or _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO fetch_queue (plugin, part, kind, key, title, state, detail, queued_at, started_at, attempts, pid) "
                "VALUES (?, ?, ?, ?, ?, 'running', ?, ?, ?, 1, ?) ON CONFLICT (plugin, part, kind, key) DO UPDATE SET "
                "state='running', started_at=excluded.started_at, finished_at=NULL, error=NULL, pid=excluded.pid, "
                "attempts=attempts + 1, title=COALESCE(?, title), detail=COALESCE(excluded.detail, detail)",
                (plugin, part, kind, str(key), title or str(key), detail, at, at, os.getpid(), title))

    def queue_end(self, plugin: str, part: str, kind: str, key: str, state: str = "done", detail: str | None = None,
                  error: str | None = None, requests: int = 0, rows: int = 0, bytes_: int = 0,
                  not_before: datetime | str | None = None, title: str | None = None, now: datetime | None = None) -> None:
        """A resource's fetch ended: done, failed (`error`) or held (until `not_before`); what it took
        and got (requests, rows, bytes); `not_before` on a done one: when it is asked next."""
        if state not in ("done", "failed", "held", "waiting"):
            raise ValueError(f"not an end state: {state!r}")
        at = _iso(now) or _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO fetch_queue (plugin, part, kind, key, title, state, detail, not_before, queued_at, "
                "finished_at, requests, rows, bytes, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (plugin, part, kind, key) DO UPDATE SET state=excluded.state, "
                "detail=COALESCE(excluded.detail, detail), not_before=excluded.not_before, "
                "finished_at=excluded.finished_at, requests=excluded.requests, rows=excluded.rows, "
                "bytes=excluded.bytes, error=excluded.error, pid=NULL, title=COALESCE(?, title)",
                (plugin, part, kind, str(key), title or str(key), state, detail, _iso(not_before), at, at,
                 int(requests), int(rows), int(bytes_), error, title))

    def queue_claim(self, plugin: str, now: datetime | None = None) -> dict | None:
        """The plugin's next waiting resource (or held one whose time has come), taken by this
        process: running. None when it has none due."""
        at = _iso(now) or _now()
        with self.conn:
            row = self.conn.execute(
                "UPDATE fetch_queue SET state='running', started_at=?, finished_at=NULL, error=NULL, pid=?, "
                "attempts=attempts + 1 WHERE rowid = (SELECT rowid FROM fetch_queue WHERE plugin=? AND "
                "(state='waiting' OR state='held') AND (not_before IS NULL OR not_before <= ?) "
                "ORDER BY priority, queued_at, key LIMIT 1) RETURNING " + ", ".join(_COLS),
                (at, os.getpid(), plugin, at)).fetchone()
        return dict(zip(_COLS, row)) if row else None

    def queue_reset(self, plugin: str, part: str | None = None, kind: str | None = None) -> int:
        """Rows left running by a run that ended before them (its process gone): waiting again."""
        sql, args = "UPDATE fetch_queue SET state='waiting', pid=NULL, detail='its run ended before it' WHERE " \
                    "plugin=? AND state='running'", [plugin]
        if part is not None:
            sql, args = sql + " AND part=?", [*args, part]
        if kind is not None:
            sql, args = sql + " AND kind=?", [*args, kind]
        with self.conn:
            return self.conn.execute(sql, args).rowcount

    def queue_settle(self, plugin: str, part: str, kind: str, keep: Iterable[str], detail: str = "up to date",
                     now: datetime | None = None) -> int:
        """A run's queue for this part is what it put on it now (`keep`): its other waiting rows have
        nothing to fetch any more (done: up to date)."""
        at = _iso(now) or _now()
        keep = list(keep)
        with self.conn:
            self.conn.execute("CREATE TEMP TABLE IF NOT EXISTS _keep (key TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM _keep")
            self.conn.executemany("INSERT OR IGNORE INTO _keep VALUES (?)", [(str(k),) for k in keep])
            n = self.conn.execute(
                "UPDATE fetch_queue SET state='done', detail=?, finished_at=? WHERE plugin=? AND part=? AND kind=? "
                "AND state='waiting' AND key NOT IN (SELECT key FROM _keep)", (detail, at, plugin, part, kind)).rowcount
            self.conn.execute("DELETE FROM _keep")
        return n

    def queue_rows(self, plugin: str | None = None, kind: str | None = None, state: str | None = None,
                   limit: int = 300) -> list[dict]:
        """The queue as the Fetcher shows it (running, waiting, held, failed, then the newest done)."""
        where, args = [], []
        for col, v in (("plugin", plugin), ("kind", kind), ("state", state)):
            if v:
                where.append(f"{col}=?")
                args.append(v)
        sql = f"SELECT {', '.join(_COLS)} FROM fetch_queue{' WHERE ' + ' AND '.join(where) if where else ''} " \
              f"ORDER BY {_ORDER} LIMIT ?"
        return [dict(zip(_COLS, r)) for r in self.conn.execute(sql, [*args, int(limit)])]

    def queue_counts(self) -> dict[str, dict[str, int]]:
        """How many rows each plugin has in each state, and by kind ({"plugins": .., "kinds": ..})."""
        plugins: dict[str, dict[str, int]] = {}
        kinds: dict[str, dict[str, int]] = {}
        for plugin, kind, state, n in self.conn.execute(
                "SELECT plugin, kind, state, COUNT(*) FROM fetch_queue GROUP BY plugin, kind, state"):
            for d, k in ((plugins, plugin), (kinds, kind)):
                d.setdefault(k, {})
                d[k][state] = d[k].get(state, 0) + n
        return {"plugins": plugins, "kinds": kinds}
