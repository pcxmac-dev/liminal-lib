"""Phase 5b (1.5.0): the library's planning as classes (coverage.Planner, sourcing.Scheme and Scans,
derive.Builder and Lease, baddata.Scanner), the pure functions and the hot loops as they were. Each
class reads once what its decisions need and calls the module's functions through the module (a
stand-in a test sets is the one used); the module keeps a wrapper under every name a caller or a
test uses (plan, build, standing, reports, offers, report, detected_plans, scans_at, apply_detected,
build_chain, build_series, build_symbol, run, scan_symbol, repair_from_ticks, entries)."""
from __future__ import annotations

import json
import os
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from liminal_libs import baddata, coverage, derive, layout, proc, sourcing
from liminal_libs.config import Tier, load_settings
from liminal_libs.coverage import FeedProfile, Planner, day_start_ms, ms
from liminal_libs.sessions import ET
from liminal_libs.sources import registry
from liminal_libs.sources.plugin import FeedPlugin, Scanned
from liminal_libs.sourcing import Scans, Scheme
from liminal_libs.storage import Store

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=ET)  # a Wednesday, midday
PLAN = Tier("free", requests_per_minute=5, history_years=2)
# two copies of the same market; `fake` ranks first (SOURCE_PRIORITY), so massive stands back for it
FEEDS = {"fake": FeedProfile("fake", PLAN, frozenset({"stock"}), 0, venues={"stock": "sip"}),
         "massive": FeedProfile("massive", PLAN, frozenset({"stock"}), 1, venues={"stock": "sip"})}


@pytest.fixture
def store():
    with Store(":memory:") as s:
        yield s


class AcmeFeed(FeedPlugin):
    name = "acme"
    title = "Acme"
    about = "a test feed"
    assets = frozenset({"stock"})
    plan_vars = ("ACME_PLAN",)
    plan_choices = {"ACME_PLAN": ("free", "pro")}
    provides = {"stock": ("day", "minute")}

    def build(self, settings, request_budget):
        return None


@pytest.fixture
def acme(monkeypatch):
    """Acme the one plugin installed (an entry point's instance), the plugin directory out of the way."""
    monkeypatch.setattr(layout, "plugins_dir", lambda: None)
    plugin = AcmeFeed()
    eps = [SimpleNamespace(name="acme", value="fake_acme:PLUGIN", load=lambda: plugin)]
    monkeypatch.setattr(registry, "_entry_points", lambda: eps)
    registry.reload()
    yield plugin
    registry._providers = None


def _minutes(start: datetime, n: int) -> list[dict]:
    return [{"t": ms(start + timedelta(minutes=i)), "o": 10.0, "h": 11.0, "l": 9.0, "c": 10.5, "v": 100} for i in range(n)]


# -- the Planner -----------------------------------------------------------------------------------
def test_the_planner_reads_once_what_every_plan_needs_and_the_ledger_each_time(store, monkeypatch):
    store.watch("fake", ["AAA", "BBB"], "minute")
    store.watch("massive", ["AAA"], "minute")
    floor = date(2026, 10, 13)
    held = (ms(datetime(2026, 10, 14, tzinfo=ET)), ms(datetime(2026, 10, 16, tzinfo=ET)))
    store.add_coverage("massive", "AAA", "minute", *held)
    reads = SimpleNamespace(scores=0)
    scores = store.audit_scores
    monkeypatch.setattr(store, "audit_scores", lambda *a, **k: (setattr(reads, "scores", reads.scores + 1), scores(*a, **k))[1])
    planner = Planner(store, FEEDS, NOW)
    me = FEEDS["fake"]
    gaps = planner.plan(me, "AAA", "stock", "minute", floor)
    assert gaps and all(b <= held[0] or a >= held[1] for a, b in gaps)  # what the other copy holds is left to it
    assert gaps[0][0] == day_start_ms(floor) and reads.scores == 2  # the planner's read, and the scheme's own build
    assert planner.plan(me, "AAA", "stock", "day", floor) and reads.scores == 2  # the scores read once
    assert planner.scheme is planner.scheme is sourcing.standing(store, FEEDS, NOW)  # the standing scheme, once
    rows = planner.watch("AAA")
    assert rows is planner.watch("AAA") and set(rows) == {("fake", "AAA"), ("massive", "AAA")}  # a ticker's rows, once
    planner.forget("AAA")
    assert planner.watch("AAA") is not rows and planner.watch("AAA") == rows  # read again on asking
    assert gaps == coverage.plan(store, me, FEEDS, "AAA", "stock", "minute", floor, NOW)  # the wrapper
    assert reads.scores == 3  # the wrapper's own planner read them (the standing scheme kept)
    # the ledger is read for every plan: a span fetched meanwhile is not asked for again
    store.add_coverage("fake", "AAA", "minute", gaps[0][0], gaps[0][1])
    assert planner.plan(me, "AAA", "stock", "minute", floor)[0][0] >= gaps[0][1]
    # nothing to plan reads neither the scores nor the scheme
    quiet = Planner(store, FEEDS, NOW)
    store.add_coverage("fake", "BBB", "minute", day_start_ms(floor), coverage.frontier_ms("stock", PLAN, NOW, "minute"))
    assert quiet.plan(me, "BBB", "stock", "minute", floor) == [] and reads.scores == 3 and quiet._scheme is None
    # a pure function stands in through the module
    cut = ms(datetime(2026, 10, 15, tzinfo=ET))
    monkeypatch.setattr(coverage, "frontier_ms", lambda *a, **k: cut)
    assert max(b for _, b in Planner(store, FEEDS, NOW).plan(me, "AAA", "stock", "day", floor)) == cut


# -- the Scheme and the Scans ------------------------------------------------------------------------
def test_the_scheme_is_built_from_the_reports_and_the_standing_one_is_kept_by_store(store, acme):
    feeds = {"acme": FeedProfile("acme", PLAN, frozenset({"stock"}), 0, venues={"stock": "sip"}, capacity=1000.0)}
    today = NOW.date()
    reports = Scheme.reports(feeds, today)
    assert list(reports) == ["acme"] and reports == sourcing.reports(feeds, today)
    offers = Scheme.offered(feeds, today)
    assert {(o.asset, o.resolution, o.market) for o in offers} == {("stock", "day", "sip"), ("stock", "minute", "sip")}
    assert offers == sourcing.offers(feeds, today)
    counts = {("acme", "stock"): 10}
    scheme = Scheme.build(feeds, counts, today)
    assert isinstance(scheme, Scheme) and all(e.unique and e.sources == ["acme"] and e.symbols == 10 for e in scheme.eras)
    assert [e.resolution for e in scheme.eras] == ["day", "minute"] and scheme.load["acme"] > 0
    assert scheme.order("stock", "minute", "sip") == [(day_start_ms(e.start), sourcing.OPEN_END, ["acme"])
                                                      for e in scheme.eras if e.resolution == "minute"]
    assert sourcing.build(feeds, counts, today).as_dict() == scheme.as_dict()
    text = scheme.text(reports)
    assert text.startswith("#1 daily bars") and "acme only (unique)" in text and text == sourcing.report(scheme, reports)
    store.watch("acme", ["AAA"], "minute")
    standing = Scheme.standing(store, feeds, NOW)
    assert standing is Scheme.standing(store, feeds, NOW) is sourcing.standing(store, feeds, NOW)  # kept
    assert store in Scheme._cache and standing.eras[0].symbols == 1
    with Store(":memory:") as other:
        assert Scheme.standing(other, feeds, NOW) is not standing  # by store


def test_the_scans_are_kept_and_read_in_one_place(tmp_path, acme):
    path = tmp_path / "m.db"
    found = Scanned((("minute bars", True, "all"),), {"ACME_PLAN": "pro"}, {"ACME_PLAN": ("plus",)}, {"perMinute": 30})
    with Store(path) as store:
        row = Scans.keep(store, "acme", found, now=NOW)
        assert row["plan"] == {"ACME_PLAN": "pro"} and row["alike"] == {"ACME_PLAN": ["plus"]}
        assert row["results"] == [["minute bars", True, "all"]] and row["limits"] == {"perMinute": 30}
        assert row["at"] == NOW.isoformat(timespec="seconds")
        assert Scans.last(store, "acme") == row and Scans.last(store, "nobody") is None
        assert Scans.read(store) == {"acme": row} and Scans.read(store, ["acme", "nobody"]) == {"acme": row}
        assert store.kv_json(sourcing.SCAN_KEY.format("acme")) == row  # the row the app's `_scan` wrote
    assert Scans.at(path) == {"acme": row} == sourcing.scans_at(path) and Scans.at(tmp_path / "none.db") == {}
    assert Scans.plans({"acme": row}) == {"ACME_PLAN": "pro"} == sourcing.detected_plans({"acme": row})
    assert Scans.plans({"acme": {"plan": {"OTHER": "x"}}}) == {}  # not its own plan variable
    settings = load_settings(env={"LIMINAL_DB": str(path)}, env_file=None)
    applied = Scans.apply(settings)
    assert applied.detected == {"ACME_PLAN": "pro"} and applied.env.get("ACME_PLAN") == "pro"
    assert sourcing.apply_detected(settings).detected == applied.detected
    mine = load_settings(env={"LIMINAL_DB": str(path), "ACME_PLAN": "free"}, env_file=None)
    assert Scans.apply(mine) is mine  # the user's own setting wins


# -- the Builder and the Lease -------------------------------------------------------------------------
def test_the_builder_builds_a_series_frames_from_what_changed(store, monkeypatch):
    from liminal_libs.storage import feeds as stored

    start = datetime(2026, 10, 20, 9, 30, tzinfo=ET)
    store.watch("fake", ["AAA"], "minute")
    log: list[str] = []
    builder = derive.Builder(store, log.append)
    with monkeypatch.context() as m:  # stored and built a while ago (a build looks a minute back)
        m.setattr(stored, "_stored_ms", lambda: 1_000)
        store.upsert_bars("fake", "AAA", "minute", 1, _minutes(start, 10))
        assert builder.chain("fake", "AAA", "minute") == 7  # two 5-minute bars, a 30-minute, 1 h, 2 h, 4 h and the day
        assert builder.chain("fake", "AAA", "minute") == 0 and derive.build_chain(store, "fake", "AAA", "minute") == 0  # nothing changed
        builder.symbol("fake", "AAA")  # every chain: the weeks, months and years above the day built here
        assert builder.symbol("fake", "AAA") == 0 and derive.run(store, log=log.append) == 0  # built already
    assert not any("another process" in m for m in log)
    store.upsert_bars("fake", "AAA", "minute", 1, _minutes(start + timedelta(minutes=10), 1))
    assert derive.build_series(store, "fake", "AAA") > 0  # the changed window built again
    store.upsert_bars("fake", "AAA", "minute", 1, _minutes(start + timedelta(minutes=11), 1))
    monkeypatch.setattr(derive, "aggregate", lambda *a, **k: [])  # the binning stands in through the module
    assert builder.chain("fake", "AAA", "minute") == 0


def test_the_lease_is_this_process_build_and_another_live_process_keeps_its_own(store):
    key = derive.LEASE_KEY.format("fake", "AAA")
    with derive.Lease(store, "fake", "AAA") as taken:
        assert taken and store.kv_json(key)["pid"] == os.getpid()
        with derive.Lease(store, "fake", "AAA") as again:
            assert again  # this process's own: taken again
    assert store.kv_json(key, {}) == {}  # dropped on leaving
    other = os.getppid()
    store.kv_set(key, json.dumps({"pid": other, "start": proc.started(other) or 0, "until": int(time.time() * 1000) + 60_000}))
    lease = derive.Lease(store, "fake", "AAA")
    with lease as taken:
        assert not taken and not lease.taken
    assert store.kv_json(key)["pid"] == other  # another's lease is never dropped
    log: list[str] = []
    store.watch("fake", ["AAA"], "minute")
    store.upsert_bars("fake", "AAA", "minute", 1, _minutes(datetime(2026, 10, 20, 9, 30, tzinfo=ET), 3))
    assert derive.Builder(store, log.append).symbol("fake", "AAA") == 0
    assert derive.build_symbol(store, "fake", "AAA", log=log.append) == 0
    assert len(log) == 2 and all("another process is building it" in m for m in log)


# -- the Scanner -----------------------------------------------------------------------------------
def test_the_scanner_judges_what_changed_through_the_modules_rules(store, monkeypatch):
    start = datetime(2026, 10, 20, 9, 30, tzinfo=ET)
    bars = _minutes(start, 5)
    bars[2] = {**bars[2], "h": 8.0, "l": 12.0}  # the high below the low: malformed
    store.watch("fake", ["AAA"], "minute")
    store.upsert_bars("fake", "AAA", "minute", 1, bars)
    scanner = baddata.Scanner(store)
    assert scanner.symbol("fake", "AAA", "stock") == 1
    assert scanner.symbol("fake", "AAA", "stock") == 0  # nothing changed since
    entries = scanner.entries("AAA")
    assert len(entries) == 1 and entries[0]["rule"] == "malformed" and entries[0]["ts"] == bars[2]["t"]
    assert baddata.entries(store, "AAA") == entries and baddata.entries(store, "AAA", "fake") == entries
    assert derive.take_dirty(store, "fake", "AAA", "minute") == (bars[2]["t"], bars[2]["t"] + 60_000)  # rebuilt above it
    monkeypatch.setattr(baddata, "scan_series", lambda *a, **k: 7)  # the scan of one series stands in through the module
    store.upsert_bars("fake", "AAA", "minute", 1, _minutes(start + timedelta(minutes=10), 1))
    assert baddata.scan_symbol(store, "fake", "AAA") == 7
