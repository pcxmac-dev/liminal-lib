"""A plugin's own fetching (plugin API 1.9): what it wants downloaded, and how it downloads one.

The user, 2026-10-31: "each plugin will manage it's own fetching". A plugin that fetches on its own
gives a `Fetcher` (Provider.fetcher, Publisher.fetcher): `plan(ctx)` the resources it wants now
(`Job`s, put on the shared queue: storage/queue.py), and `fetch(ctx, job)` one of them, stored in
the databases it points to (layout.Database), its requests counted with `ctx.count`. The runner
(runner.py) gives each such plugin a worker of its own that takes only its own jobs, between which
it stops for the fetching pause, the plugin's kill or pause; a plugin's failure stops its worker
alone. A plugin without one is fetched by the app's paths not yet moved into plugins (the bars sync,
the news reader, the datasets' files), which keep the same queue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable


@dataclass(frozen=True)
class Job:
    """A resource a plugin downloads: one row of the queue."""
    kind: str  # bars, news, document, dataset file ...
    key: str  # unique among the plugin's part's of this kind: a ticker, a document's address
    title: str = ""  # how the Fetcher names it (its key when left out)
    part: str = ""  # its feed or publisher in the plugin (the plugin's own name when left out)
    detail: str = ""  # what it fetches
    priority: int = 0  # lower first
    not_before: datetime | None = None  # not asked before then (its release, a wait)


@dataclass(frozen=True)
class Fetched:
    """What a job's fetch took and got. `again`: asked again then (a release still to come)."""
    requests: int = 0
    rows: int = 0
    bytes: int = 0
    detail: str = ""
    again: datetime | None = None


@dataclass
class FetchContext:
    """What a plugin's plan and fetch get: its name, the settings, the main store, the time, the log,
    `count(n)` for its requests (its daily ledger), and `stopping()`: true once it should stop
    between two requests (the fetching pause, its kill or pause)."""
    plugin: str
    settings: object
    store: object
    now: datetime
    log: Callable[[str], None]
    count: Callable[[int], None] = field(default=lambda n=1: None)
    stopping: Callable[[], bool] = field(default=lambda: False)


@dataclass(frozen=True)
class Fetcher:
    """A plugin's own fetching (see the module's docstring)."""
    plan: Callable[[FetchContext], Iterable[Job]]
    fetch: Callable[[FetchContext, Job], Fetched]
    kinds: tuple[str, ...] = ()  # what it fetches, for the views
