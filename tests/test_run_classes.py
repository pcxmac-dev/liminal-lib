"""Phase 5a (1.5.0): the library's run orchestration as classes (collector.Collector, audit.Auditor,
initializer.Initializer), the hot paths and the module's functions as they were. Each class owns a
run's state and its decisions, calls the module's functions through the module (a stand-in a test
sets is the one used) and reads the module's constants when a stage runs, never when it is made;
the module keeps a wrapper under every name a caller or a test uses (sync, run_audits, random_span,
initialize, _Queue)."""
from __future__ import annotations

import inspect
import random
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from liminal_libs import audit, collector, initializer
from liminal_libs.collector import ET, Collector, RunQueue, SyncResult, sync
from liminal_libs.config import Tier
from liminal_libs.coverage import FeedProfile, day_start_ms, ms
from liminal_libs.errors import NotEntitled
from liminal_libs.models import Instrument
from liminal_libs.sources.base import DataSource
from liminal_libs.storage import Store

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)  # a Wednesday, midday
PLAN = Tier("free", requests_per_minute=5, history_years=2)
QUIET = lambda *_: None  # noqa: E731


class FakeSource(DataSource):
    """A feed answering every bar request with the bars it was given, counting its requests."""

    name = "fake"

    def __init__(self, bars: dict | None = None, entitled=("minute", "day")):
        self.bars = bars or {}
        self.entitled = set(entitled)
        self._requests = 0
        self.calls: list[tuple[str, str]] = []

    @property
    def requests_made(self) -> int:
        return self._requests

    def is_entitled(self, resolution, ticker, asset="stock"):
        self._requests += 1
        return resolution in self.entitled

    def bar_pages(self, ticker, start, end, timespan, multiplier=1, descending=False, asset="stock"):
        self._requests += 1
        self.calls.append((ticker, timespan))
        if timespan not in self.entitled:
            raise NotEntitled(403, "no " + timespan)
        yield list(self.bars.get(timespan, []))

    def trade_pages(self, *a, **k):
        raise AssertionError("no ticks are fetched for history")

    def exchanges(self):
        self._requests += 1
        return []

    def instrument(self, ticker, resolve_exchange=True):
        self._requests += 1
        return Instrument(ticker)


def _bar(t: int, price: float = 10.0) -> dict:
    return {"t": t, "o": price, "h": price + 1, "l": price - 1, "c": price, "v": 100}


def _collector(store, source=None, **kw) -> Collector:
    return Collector(source or FakeSource(), store, PLAN, now=NOW, log=QUIET, **kw)


@pytest.fixture
def store():
    with Store(":memory:") as s:
        yield s


@pytest.fixture
def no_requests_before_the_symbols(monkeypatch):
    """The stage before the symbols stands in: a run's exchanges, calendars and market days are not
    what these tests look at."""
    for name in ("sync_exchanges", "refresh_calendars", "market_days", "fetch_market_moves"):
        monkeypatch.setattr(collector, name, lambda *a, **k: 0)


# -- the Collector ---------------------------------------------------------------------------------
def test_the_stage_before_the_symbols_calls_the_module_in_order(store, monkeypatch):
    calls: list[tuple[str, tuple]] = []
    for name in ("sync_exchanges", "refresh_calendars", "market_days", "fetch_market_moves"):
        monkeypatch.setattr(collector, name, lambda *a, _n=name, **k: calls.append((_n, a)) or 0)
    c = _collector(store, proxies=["SPY"])
    c._before_symbols()
    assert [n for n, _ in calls] == ["sync_exchanges", "refresh_calendars", "market_days", "fetch_market_moves"]
    assert calls[2][1][2] is c.me and calls[2][1][6] == ["SPY"] and calls[3][1][3] == ["SPY"]  # the app's proxies
    assert c.me.name == "fake" and c.feeds == {"fake": c.me}  # a profile made for a feed given none
    calls.clear()
    c.only = {"AAA"}  # a run for chosen symbols spends nothing on the rest
    c._before_symbols()
    assert calls == []
    # the contract the charts app's tests read from the run's source: the stages' order, in the class
    body = inspect.getsource(sync)
    assert body.index("market_days(source, store, me, feeds, store.watchlist(source.name), now, proxies or [], log)") \
        < body.index("fetch_market_moves(source, store, now, proxies or [], log)")
    assert 'if source.serves("stock") and gives_days(source, me):' in body
    assert sync.__wrapped__ is Collector and list(inspect.signature(sync).parameters)[:4] == ["source", "store", "tier", "now"]


def test_the_runs_constants_and_clock_are_read_when_it_runs(store, monkeypatch, no_requests_before_the_symbols):
    seen: dict = {}

    def daily_first(source, store, me, feeds, watched, floor, order, refusals, now, log=print, limit=0, served=None):
        seen["limit"], seen["at"] = limit, now
        return 0, None

    monkeypatch.setattr(collector, "daily_first", daily_first)
    store.watch("fake", ["AAA"], "day")
    c = _collector(store)  # made before the constants change: a run still reads them when it plans
    monkeypatch.setattr(collector, "DAILY_FIRST_PER_RUN", 3)
    monkeypatch.setattr(collector, "FUTURES_PER_RUN", 7)
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(collector, "monotonic", lambda: clock.now)
    result = c.run()
    assert seen["limit"] == 3 and seen["at"] == NOW and c.futures_per_run == 7 and c.sweep_left == 0
    assert result is c.result and result.stopped is None
    clock.now = 160.0
    assert c.clock() == NOW + timedelta(seconds=60)  # the run's clock: `now` is when it began
    assert c.floors["stock"] == collector.history_floor(PLAN, NOW.date(), "stock")


def test_a_run_fetches_a_watched_symbols_bars_and_the_next_run_leaves_it_alone(store, no_requests_before_the_symbols):
    day = ms(datetime(2026, 10, 20, tzinfo=ET))  # Tuesday's session, over by now
    minute = ms(datetime(2026, 10, 20, 10, 0, tzinfo=ET))
    src = FakeSource(bars={"day": [_bar(day)], "minute": [_bar(minute)]})
    store.watch("fake", ["AAA"], "minute")
    result = sync(src, store, PLAN, NOW, log=QUIET)
    assert isinstance(result, SyncResult) and result.rows == 1 and result.stopped is None and result.skipped == 0
    assert store.bar_range("fake", "AAA", "minute") == (minute, minute)
    assert store.bar_range("fake", "AAA", "day") == (day, day)  # daily bars first (the sweep), then its minutes
    rows = {r["key"]: r for r in store.queue_rows("fake")}
    assert rows["AAA"]["state"] == "done" and rows["AAA"]["rows"] == 1
    assert store.watchlist("fake")[0]["synced_at"] == NOW
    # the same run through the class: nothing new published since, so the symbol is left alone
    c = _collector(store, FakeSource(bars=src.bars))
    again = c.run()
    assert again is c.result and again.skipped == 1 and again.rows == 0 and c.source.calls == []
    assert c.pending == [] and c.queue is not None and c.queue.current is None


def test_a_refused_feed_stops_the_run_with_a_strike(store, no_requests_before_the_symbols):
    from liminal_libs import killswitch
    from liminal_libs.errors import RateLimited

    class Refusing(FakeSource):
        def bar_pages(self, *a, **k):
            self._requests += 1
            raise RateLimited(429, "slow down")
            yield  # noqa: unreachable - a generator

    store.watch("fake", ["AAA"], "minute")
    c = _collector(store, Refusing())
    result = c.run()
    assert result.stopped is not None and "rate limited" in result.stopped
    assert killswitch.killed(store, "fake", "fetch", NOW) is not None  # the strike


def test_the_run_queue_keeps_the_rows_and_a_failing_store_is_logged_once(store):
    log: list[str] = []
    q = RunQueue(store, "fake", log.append)
    q.plan([{"ticker": "AAA", "asset": "stock"}, {"ticker": "BBB", "asset": "stock"}], lambda w: w["ticker"] == "AAA",
           settle=True)
    q.start("AAA")
    rows = {r["key"]: r for r in store.queue_rows("fake")}
    assert rows["AAA"]["state"] == "running" and rows["BBB"]["state"] == "waiting"
    assert rows["AAA"]["detail"] == "stock: history to fill" and rows["BBB"]["detail"] == "stock: new data"
    q.end("AAA", rows=5)
    q.start("BBB")
    q.skip()  # taken, left without an end of its own: done
    rows = {r["key"]: r for r in store.queue_rows("fake")}
    assert rows["AAA"]["state"] == "done" and rows["BBB"]["state"] == "done" and q.current is None and log == []
    assert collector._Queue is RunQueue  # its name before 1.5.0

    class Broken:
        def __getattr__(self, name):
            def fail(*a, **k):
                raise RuntimeError("locked")
            return fail

    broken = RunQueue(Broken(), "fake", log.append)
    broken.plan([{"ticker": "AAA", "asset": "stock"}], lambda w: False, settle=False)
    broken.start("AAA")
    broken.end("AAA")
    assert broken.broken and log == [log[0]] and "the fetch queue was not kept this run" in log[0]


def test_the_monthly_symbol_caps_are_the_plugins_read_when_asked(store):
    from liminal_libs.sources import providers

    declared = {n: p.symbols_per_month for n, p in providers().items() if p.symbols_per_month}
    assert dict(collector.SYMBOLS_PER_MONTH) == declared
    assert "fake" not in collector.SYMBOLS_PER_MONTH and collector.monthly_cap(FakeSource()) is None
    with pytest.MonkeyPatch.context() as mp:
        mp.setitem(collector.SYMBOLS_PER_MONTH, "fake", 5)
        assert collector.SYMBOLS_PER_MONTH["fake"] == 5 and collector.monthly_cap(FakeSource()) == 5
        assert "fake" in collector.SYMBOLS_PER_MONTH and len(collector.SYMBOLS_PER_MONTH) == len(declared) + 1
        now = datetime(2026, 10, 21, tzinfo=timezone.utc)
        assert [collector._monthly_symbol(store, "fake", t, now) for t in "ABCDEF"] == [True] * 5 + [False]
        assert collector.monthly_cap(SimpleNamespace(name="fake", symbols_per_month=450)) == 450  # the source's own
    assert "fake" not in collector.SYMBOLS_PER_MONTH  # the stand-in went with the patch
    for name, cap in declared.items():
        collector.SYMBOLS_PER_MONTH[name] = cap  # the plugin's own: nothing stands in for it
    assert collector.SYMBOLS_PER_MONTH._set == {}


# -- the Auditor -----------------------------------------------------------------------------------
def test_the_auditor_reads_its_watch_rows_once_and_audits_through_the_module(store, monkeypatch):
    audited: list[tuple] = []
    monkeypatch.setattr(audit, "audit_span", lambda *a, **k: audited.append(a) or {"verdict": "pass"})
    feeds = {"massive": FeedProfile("massive", PLAN, frozenset({"stock"}), 0, venues={"stock": "sip"}),
             "fake": FeedProfile("fake", PLAN, frozenset({"stock"}), 1, venues={"stock": "sip"})}
    store.watch("massive", ["AAA"], "minute")
    store.watch("fake", ["AAA"], "minute")
    a, b = ms(datetime(2026, 10, 13, tzinfo=ET)), ms(datetime(2026, 10, 18, tzinfo=ET))  # a week of the owner's minutes
    store.add_coverage("massive", "AAA", "minute", a, b)
    store.queue_audit("AAA", "minute", a, b, "massive")
    auditor = audit.Auditor(FakeSource(), store, feeds["fake"], feeds, NOW, log=QUIET, rng=random.Random(1))
    assert auditor.window("stock", "minute")[0] == day_start_ms(collector.history_floor(PLAN, NOW.date(), "stock"))
    assert auditor.window("stock", "minute") == audit._window(feeds["fake"], "stock", "minute", NOW)
    assert auditor.run(queued=2, random_count=0) == 1
    (args,) = audited
    assert args[0] is auditor.source and args[1] is store and args[2:4] == ("AAA", "minute")
    assert a <= args[4] < args[5] <= b and args[5] - args[4] == audit.SPOT_MS  # one random hour of the span
    assert args[6:8] == ("massive", "after-download") and store.audit_queue() == []
    assert auditor.mine is auditor.mine and set(auditor.mine) == {"AAA"}  # read once
    pick = auditor.random_span()
    assert pick is not None and pick[0] == "AAA" and pick[1] == "minute" and pick[4] == "massive"
    assert a <= pick[2] < pick[3] <= b and pick[3] - pick[2] >= 3_600_000
    assert auditor.run(queued=0, random_count=1) == 1 and audited[-1][7] == "random"
    # within the requests allowed: none left, nothing audited
    assert auditor.run(queued=2, random_count=2, requests=0) == 0
    # the wrappers
    assert audit.run_audits(FakeSource(), store, feeds["fake"], feeds, NOW, queued=0, random_count=1, log=QUIET) == 1
    assert audit.random_span(store, FakeSource(), feeds["fake"], NOW, random.Random(2), feeds=feeds)[0] == "AAA"
    # another market's copy is not audited (same_market), nor a feed's own data
    other = {"massive": FeedProfile("massive", PLAN, frozenset({"stock"}), 0, venues={"stock": "sip"}),
             "fake": FeedProfile("fake", PLAN, frozenset({"stock"}), 1, venues={"stock": "iex"})}
    assert audit.Auditor(FakeSource(), store, other["fake"], other, NOW, rng=random.Random(1)).random_span() is None


# -- the Initializer -------------------------------------------------------------------------------
def test_the_initializer_runs_as_initialize_does(store):
    today = date(2026, 10, 24)
    rows = [{"ticker": t, "name": t, "exchange": "XNAS", "type": "CS", "kind": "stock"} for t in ("AAPL", "MSFT")]
    store.update_directory("massive", "stock", rows, today)
    store.save_market_snapshot("massive", date(2026, 10, 23), [{"T": "AAPL", "c": 250.0, "v": 50e6, "vw": 250.0},
                                                               {"T": "MSFT", "c": 400.0, "v": 10e6, "vw": 400.0}])
    init = initializer.Initializer(store, ["massive"], now=NOW)
    dry = init.run(dry_run=True)
    assert dry["added"] == {"massive": 2} and dry["top"] == ["AAPL", "MSFT"] and store.watchlist() == []
    assert init.have == set() and init.watched == set()  # what was watched already, read for the run
    assert initializer.initialize(store, ["massive"], dry_run=True, now=NOW) == dry  # the wrapper
    out = init.run()
    assert out["listed"] == 2 and out["ranked"] == 2 and {w["ticker"] for w in store.watchlist("massive")} == {"AAPL", "MSFT"}
    assert initializer.load_order(store) == {"AAPL": 0, "MSFT": 1}
    assert store.kv_json(initializer.DONE_KEY)["at"] == NOW.isoformat(timespec="seconds")
    assert set(initializer.load_bulk(store)) == {"AAPL", "MSFT"}
    assert initializer.Initializer(store, ["massive"]).run(dry_run=True)["added"] == {"massive": 0}  # watched already
