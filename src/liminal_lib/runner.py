"""The fetcher's workers: each plugin that fetches on its own (plugin API 1.9: sources/fetching.py)
gets a worker of its own, taking only its own jobs from the shared queue (storage/queue.py).

A worker asks its plugin's parts what they want (Fetcher.plan) and puts it on the queue, then takes
its jobs one at a time (Fetcher.fetch), each ending done, failed or held. Between two jobs it stops
for the fetching pause (the Fetcher's Pause and Halt), the plugin's kill or pause, or a part's; a
StopRun from a fetch stops its worker for the run. One plugin's failure, a fetch's or its plan's,
stops its worker alone: the others go on (each in a thread of its own: their waits are requests').
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable

from . import collector, killswitch
from .collector import StopRun
from .sources.fetching import FetchContext, Fetcher, Job
from .sources.registry import plugin_fetchers


def stopped(store, plugin: str, part: str | None = None, now: datetime | None = None) -> str | None:
    """Why a plugin's worker stops now: the fetching pause or halt, the plugin's kill or pause, or
    its part's. None when it may fetch."""
    now = now or datetime.now(timezone.utc)
    p = collector.pause_state(store, now)
    if p:
        return "fetching halted" if p.get("halt") else "fetching paused"
    for name in dict.fromkeys((plugin, part or plugin)):
        k = killswitch.killed(store, name, "fetch", now)
        if k:
            return f"{name}: {k.get('why') or 'stopped'}"
    return None


def run_plugin(plugin: str, parts: list[tuple[str, Fetcher]], store, settings, log: Callable[[str], None] = print,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc), limit: int | None = None) -> dict:
    """One worker's pass: its parts' plans on the queue, then its jobs until none is due, it is
    stopped, or `limit` jobs are done. Returns {"done", "failed", "held", "stopped"}."""
    out = {"done": 0, "failed": 0, "held": 0, "stopped": None}
    store.queue_reset(plugin)  # left running by a pass that ended before them
    fetchers = {part: f for part, f in parts}
    sent = [0]

    def count(n: int = 1) -> None:
        sent[0] += n
        store.count_request(plugin, n=n, now=clock())

    def ctx(part: str) -> FetchContext:
        return FetchContext(plugin, settings, store, clock(), log, count,
                            lambda: stopped(store, plugin, part, clock()) is not None)

    why = stopped(store, plugin, now=clock())
    if why:
        out["stopped"] = why
        return out
    for part, fetcher in parts:
        try:
            jobs = list(fetcher.plan(ctx(part)))
        except Exception as e:  # noqa: BLE001 - a plugin's surprise: its part plans nothing this pass
            log(f"{plugin}: {part}'s plan failed: {type(e).__name__}: {e}")
            continue
        by: dict[tuple[str, str], list[dict]] = {}
        for j in jobs:
            by.setdefault((j.part or part, j.kind), []).append(
                {"key": j.key, "title": j.title or j.key, "detail": j.detail or None, "priority": j.priority,
                 "not_before": j.not_before})
        for (p, kind), items in by.items():
            store.queue_put(plugin, p, kind, items, now=clock())
    done = 0
    while limit is None or done < limit:
        why = stopped(store, plugin, now=clock())
        if why:
            out["stopped"] = why
            break
        row = store.queue_claim(plugin, now=clock())
        if row is None:
            break
        fetcher = fetchers.get(row["part"]) or next(iter(fetchers.values()))
        job = Job(row["kind"], row["key"], row["title"], row["part"], row["detail"] or "", row["priority"])
        sent[0] = 0
        try:
            got = fetcher.fetch(ctx(row["part"]), job)
        except StopRun as e:
            store.queue_end(plugin, row["part"], row["kind"], row["key"], "waiting", detail=f"stopped: {e}",
                            requests=sent[0], now=clock())
            out["stopped"] = str(e)
            log(f"{plugin}: stopped ({e})")
            break
        except Exception as e:  # noqa: BLE001 - this job failed; the plugin's next goes on
            store.queue_end(plugin, row["part"], row["kind"], row["key"], "failed", error=f"{type(e).__name__}: {e}"[:300],
                            requests=sent[0], now=clock())
            out["failed"] += 1
            log(f"{plugin}: {row['title']}: {type(e).__name__}: {e}")
            continue
        state = "held" if got.again and got.again > clock() else "done"
        store.queue_end(plugin, row["part"], row["kind"], row["key"], state, detail=got.detail or None,
                        requests=got.requests or sent[0], rows=got.rows, bytes_=got.bytes, not_before=got.again,
                        now=clock())
        out[state] += 1
        done += 1
    return out


def run_all(store_factory: Callable[[], object], settings, log: Callable[[str], None] = print,
            only: str | None = None) -> dict[str, dict]:
    """Every plugin that fetches on its own (`only`: that one), each in a worker thread of its own
    with a store of its own; returns each one's pass. A worker's crash is its own."""
    results: dict[str, dict] = {}

    def work(plugin: str, parts) -> None:
        try:
            store = store_factory()
            try:
                results[plugin] = run_plugin(plugin, parts, store, settings, log)
            finally:
                store.close()
        except Exception as e:  # noqa: BLE001 - this worker's crash; the others go on
            results[plugin] = {"error": f"{type(e).__name__}: {e}"}
            log(f"{plugin}: worker failed: {type(e).__name__}: {e}")

    threads = [threading.Thread(target=work, args=(p, parts), name=f"fetch-{p}", daemon=True)
               for p, parts in plugin_fetchers().items() if only in (None, p)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results
