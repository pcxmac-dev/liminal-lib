"""The class contract (plugin API 2.0, sources/plugin.py): a plugin is an instance of a FeedPlugin or
PublisherPlugin subclass, what it declares class attributes, what it does methods; the record form
(Provider, Publisher: plugin API 1.x) is a subclass of the same classes, so the registry, the menu,
the settings, the readers and the freshness rules ask both the same way."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from liminal_lib import freshness, gov, layout
from liminal_lib.config import Tier, load_settings
from liminal_lib.freshness import Delay
from liminal_lib.sources import feed_profiles, registry
from liminal_lib.sources.archive import CryptoVenuePlugin, PublicFeedPlugin
from liminal_lib.sources.http import PublicClient
from liminal_lib.sources.keys import KeyVar
from liminal_lib.sources.publisher import Feed, Policy, Publisher, PublisherPlugin
from liminal_lib.sources.registry import (PLUGIN_API, RECORD_API, FeedPlugin, Provider, Scanned, api_refusal, menu,
                                          plugin_databases, plugin_keys, publisher_menu, variables)
from liminal_lib.sources.stream import Live


@pytest.fixture
def installed(monkeypatch):
    """Plugins loaded as installed packages' entry points: each an instance, a class, or a module
    (by keyword) holding either; the plugin directory out of the way. Returns the providers found."""
    monkeypatch.setattr(layout, "plugins_dir", lambda: None)

    def install(*plugins, **modules):
        eps = [SimpleNamespace(name=getattr(p, "name", "") or "cls", value=f"fake_{i}:PLUGIN", load=(lambda p=p: p))
               for i, p in enumerate(plugins)]
        eps += [SimpleNamespace(name=n, value=f"{n}:PLUGIN", load=(lambda m=m: m)) for n, m in modules.items()]
        monkeypatch.setattr(registry, "_entry_points", lambda: eps)
        return registry.reload()

    yield install
    registry._providers = None


@pytest.fixture
def settings_for(tmp_path):
    def make(**env):
        return load_settings(env={"LIMINAL_DB": str(tmp_path / "m.db"), **env}, env_file=None)
    return make


class AcmeSource:
    def __init__(self, name, budget):
        self.name, self.budget = name, budget
        self.client = SimpleNamespace(limits_seen={})


def _view(base):
    plan = base.env.get("ACME_PLAN") or "free"
    return replace(base, tier=Tier(f"acme-{plan}", requests_per_minute=5, history_years=2), origin=base)


class AcmeFeed(FeedPlugin):
    """A feed as a class: the record's fields as attributes, its callbacks as methods."""
    name = "acme"
    title = "Acme"
    about = "a test feed"
    order = 90
    assets = frozenset({"stock", "crypto"})
    keyless = frozenset({"crypto"})
    keys = (KeyVar("ACME_API_KEY"),)
    plan_vars = ("ACME_PLAN",)
    plan_choices = {"ACME_PLAN": ("free", "pro")}
    options = ("ACME_YEARS",)
    provides = {"stock": ("day",), "crypto": ("minute", "day")}
    delays = {"acme-free": {"stock": Delay("eod"), "crypto": Delay("utc_day")}, "acme-pro": {"stock": Delay("delay")}}
    hours = {"stock": "overnight"}

    def build(self, settings, request_budget):
        return AcmeSource(self.name, request_budget)

    def view(self, base):
        return _view(base)

    def scan(self, source):
        return Scanned((("days", True, "all"),), {"ACME_PLAN": "pro"})

    def settings(self, variables):
        return {"plan": variables.get("ACME_PLAN", "free"), "has_key": bool(variables.get("ACME_API_KEY"))}


def acme_record(name="acme", **kw) -> Provider:
    """The same feed in the record form."""
    return Provider(name=name, title="Acme", about="a test feed", order=90, assets=frozenset({"stock", "crypto"}),
                    keyless=frozenset({"crypto"}), keys=(KeyVar("ACME_API_KEY"),), plan_vars=("ACME_PLAN",),
                    plan_choices={"ACME_PLAN": ("free", "pro")}, options=("ACME_YEARS",),
                    provides={"stock": ("day",), "crypto": ("minute", "day")},
                    build=lambda s, b: AcmeSource(name, b), view=_view,
                    scan=lambda source: Scanned((("days", True, "all"),), {"ACME_PLAN": "pro"}),
                    settings=lambda v: {"plan": v.get("ACME_PLAN", "free"), "has_key": bool(v.get("ACME_API_KEY"))},
                    **{"api": RECORD_API, **kw})


class AcmeNews(PublisherPlugin):
    name = "acme-news"
    title = "Acme News"
    about = "what Acme announces"
    feeds = (Feed("acme-press", "Acme", "Press releases", "https://news.acme.example/rss"),)
    policy = Policy(hosts=("news.acme.example",))
    keys = (KeyVar("ACME_CONTACT", label="your name and email", secret=False),)

    def calendar(self, today):
        return [{"when": f"{today}T08:30:00-05:00", "what": "earnings"}]

    def settings(self, variables):
        return {"contact": variables.get("ACME_CONTACT")}


def test_what_a_plugin_is():
    from liminal_lib.sources.registry import IntegratorPlugin, PlatformPlugin

    assert AcmeFeed.KIND == "feed" == Provider.KIND and AcmeNews.KIND == "publisher" == Publisher.KIND
    assert AcmeFeed.SYSTEM == "integrator" == AcmeNews.SYSTEM and issubclass(AcmeFeed, IntegratorPlugin)
    assert PlatformPlugin.SYSTEM == "platform" and not issubclass(PlatformPlugin, IntegratorPlugin)
    assert not AcmeFeed().is_record and acme_record().is_record
    assert FeedPlugin.CONTRACT and "CONTRACT" not in AcmeFeed.__dict__  # the contract's own classes carry the defaults
    assert repr(AcmeFeed()) == "<AcmeFeed 'acme': built in>"
    assert FeedPlugin().refusal() == "it has no name" and AcmeFeed().refusal() is None
    assert AcmeFeed().key_names == ("ACME_API_KEY",) and AcmeNews().key_hint == "ACME_CONTACT"


def test_a_class_feed_and_its_record_twin_give_the_same_answers(installed, settings_for):
    installed(AcmeFeed(), acme_record("acmerec"))
    assert registry.errors() == []
    settings = settings_for(ACME_API_KEY="k", ACME_PLAN="pro")
    rows = {r["name"]: r for r in menu(settings)}
    a, b = rows["acme"], rows["acmerec"]

    def same(r):
        return {k: v for k, v in r.items() if k not in ("name", "api", "origin", "databases")}
    assert same(a) == same(b)
    assert a["api"] == "2.0" and b["api"] == RECORD_API and a["scan"] and a["keysSet"] and a["serves"] == ["crypto", "stock"]
    assert a["plan"] == "acme-pro" == b["plan"]  # each one's own view of the settings
    assert a["planVars"] == [{"name": "ACME_PLAN", "value": "pro", "origin": "you", "choices": ["free", "pro"]}]
    assert [d.holds.replace("acmerec", "acme") for d in plugin_databases("acmerec")] == [d.holds for d in plugin_databases("acme")]
    assert [k.name for k in plugin_keys("acme")] == ["ACME_API_KEY"] == [k.name for k in plugin_keys("acmerec")]
    assert set(variables()) >= {"ACME_API_KEY", "ACME_PLAN", "ACME_YEARS"}
    profiles = feed_profiles(settings)
    assert profiles["acme"].assets == profiles["acmerec"].assets == frozenset({"stock", "crypto"})
    assert profiles["acme"].tier.name == "acme-pro" and profiles["acme"].capacity == profiles["acmerec"].capacity
    assert settings.plugin("acme") == {"plan": "pro", "has_key": True} == settings.plugin("acmerec")
    assert registry.get("acme").build(settings, 7).budget == 7 and registry.get("acmerec").build(settings, 7).budget == 7
    without = settings_for()
    assert registry.get("acme").served(without) == frozenset({"crypto"}) == registry.get("acmerec").served(without)


def test_the_hooks_defaults_and_what_a_plugin_gives(installed, settings_for):
    plain = FeedPlugin(name="plain", title="Plain", assets=frozenset({"stock"}), build=lambda s, b: None,
                       tier=Tier("fixed", requests_per_minute=12, history_years=1), per_run=9)
    installed(plain, AcmeFeed())
    assert not plain.has_scan and not plain.has_settings and plain.settings_of({}) is None
    assert plain.gives("build") and not plain.gives("view") and not plain.gives("rate")
    base = settings_for()
    view = plain.settings_view(base)
    assert view.tier.name == "fixed" and view.max_requests_per_run == 9 and view.origin is base
    assert plain.rate_window(base) == (12, 60.0)
    assert plain.keys_set(base) is None and plain.served(base) == frozenset({"stock"}) and plain.market_of(base) == {"stock": "plain"}
    assert plain.resolutions_for(view.tier, "stock") == ("day", "minute") and plain.resolutions_for(view.tier, "fx") == ()
    assert plain.request_bars("minute") == 1000 and plain.kind_for("stock") == "vendor"
    # a class overriding a hook gives it; a subclass setting one to None drops it
    acme = AcmeFeed()
    assert acme.gives("scan") and acme.gives("view") and acme.gives("settings") and not acme.gives("rate")

    class Quiet(AcmeFeed):
        scan = None
    assert not Quiet().has_scan and Quiet().gives("view")
    quiet = Quiet().run_scan(AcmeSource("quiet", None))
    assert [w for w, _, _ in quiet.results] == ["its server's rate limit"] and quiet.plan == {}
    full = acme.run_scan(AcmeSource("acme", None))
    assert full.plan == {"ACME_PLAN": "pro"} and full.results[0] == ("days", True, "all")
    # the record's: a callback field set, or None for the contract's default
    rec = acme_record()
    assert rec.gives("scan") and rec.gives("view") and not rec.gives("rate") and rec.has_settings
    assert Provider(name="p", title="P", build=lambda s, b: None, assets=frozenset()).gives("build")
    with pytest.raises(TypeError):
        Provider(name="p", title="P")  # a record's required fields are required still


def test_with_makes_a_changed_copy_of_a_class_instance_or_a_record():
    acme = AcmeFeed()
    moved = acme.with_(origin="package: x", version="9.9")
    assert type(moved) is AcmeFeed and moved.origin == "package: x" and moved.version == "9.9" and acme.origin == "built in"
    assert moved.has_scan and moved.build(None, 3).budget == 3
    assert not acme.with_(scan=None).has_scan and acme.has_scan
    with pytest.raises(TypeError, match="no attribute 'scam'"):
        acme.with_(scam=None)
    with pytest.raises(TypeError, match="no attribute 'nam'"):
        FeedPlugin(nam="x")
    rec = acme_record().with_(title="Acme 2")
    assert isinstance(rec, Provider) and rec.title == "Acme 2" and rec.is_record


def test_the_contract_version_of_a_class_and_of_a_record():
    assert PLUGIN_API == "2.0" and RECORD_API == "1.12"
    assert api_refusal("2.0") is None and api_refusal("2") is None
    assert "needs a newer app" in api_refusal("2.1") and "needs a newer app" in api_refusal("3.0")
    assert "needs a release for 2.x" in api_refusal("1.12")
    for api in ("1.0", "1", "1.12"):
        assert api_refusal(api, record=True) is None
    assert "needs a newer app" in api_refusal("1.13", record=True)
    assert "record" in api_refusal("2.0", record=True) and "FeedPlugin" in api_refusal("2.0", record=True)
    assert "not a contract version" in api_refusal("x", record=True)
    assert api_refusal("1.9", "1.9", record=True) is None and api_refusal("1.0", "1.0") is None
    assert "needs a release for 2.x" in api_refusal("1.9", "2.0")


def test_the_registry_takes_an_instance_a_class_or_a_module_of_either(installed):
    class Pack:  # a module: instances and classes among its plugins
        PLUGINS = (AcmeFeed, acme_record("acmerec"))
        PUBLISHERS = (AcmeNews,)

    class Empty:
        PLUGIN = object()

    class Early(AcmeFeed):
        name = "early"
        api = "1.12"  # a class claiming the record form's contract
    late = acme_record("late", api="2.0")  # a record claiming the class contract
    found = installed(Early, late, pack=Pack(), empty=Empty())
    assert list(found) == ["acme", "acmerec"] and list(registry.publishers()) == ["acme-news"]
    assert type(found["acme"]) is AcmeFeed and found["acme"].origin == "package: pack:PLUGIN"
    why = dict(registry.errors())
    assert why["early (fake_0:PLUGIN)"] == "early: written for plugin API 1.12; this app offers 2.0: it needs a release for 2.x"
    assert why["late (fake_1:PLUGIN)"].startswith("late: written for plugin API 2.0 as a record")
    assert "defines no PLUGIN (" in why["empty (empty:PLUGIN)"]


def test_a_feed_declares_its_publication_delays_and_hours(installed):
    installed(AcmeFeed())
    assert freshness.plan_delay(Tier("acme-free", 5, 2), "crypto").kind == "utc_day"
    assert freshness.plan_delay(Tier("acme-pro", 5, 2), "stock").kind == "delay"
    assert freshness.plan_delay(Tier("acme-pro", 5, 2), "crypto").kind == "eod"  # not declared: end of day
    assert freshness.hours("acme", "stock") == "overnight" and freshness.hours("acme", "crypto") == "crypto"
    assert freshness.DELAYS.declare(AcmeFeed()) is False  # declared already: nothing changes, no cache cleared
    assert freshness.plan_delay(Tier("free", 5, 2), "stock").kind == "eod"  # the library's own rows still


def test_a_publisher_as_a_class_and_as_a_record(installed, settings_for):
    wire = Publisher(name="acme-wire", title="Acme Wire", about="", feeds=(), policy=Policy(), kind="vendor",
                     account="acme", read=lambda ctx: 0, api=RECORD_API)
    installed(AcmeNews(), wire)
    news, wire = registry.publishers()["acme-news"], registry.publishers()["acme-wire"]
    assert news.has_calendar and not news.has_moments and not news.reads_itself and news.moments(date(2026, 11, 3)) == ()
    assert wire.reads_itself and wire.is_record and not wire.has_calendar and not wire.has_moments
    assert gov.upcoming(registry.publishers(), date(2026, 11, 3)) == [{"when": "2026-11-03T08:30:00-05:00", "what": "earnings",
                                                                       "publisher": "Acme News"}]
    holds = [d.holds for d in plugin_databases("acme-news")]
    assert any(h.startswith("what Acme News read") for h in holds)
    assert [k.name for k in plugin_keys("acme-news")] == ["ACME_CONTACT"] and "ACME_CONTACT" in variables()
    row = next(r for r in publisher_menu() if r["name"] == "acme-news")
    assert row["api"] == "2.0" and row["keys"][0]["name"] == "ACME_CONTACT" and not row["keys"][0]["secret"]
    settings = settings_for(ACME_CONTACT="me@example.com")
    assert settings.plugin("acme-news") == {"contact": "me@example.com"}  # a publisher's own settings (2.0: every kind)
    assert settings.plugin("acme-wire") is None


def test_a_live_feed_as_a_property_and_a_settings_hook_that_fails(installed, settings_for):
    class Streamy(AcmeFeed):
        name = "streamy"

        @property
        def live(self):
            return (Live(f"{self.name}iex", "stock", stream=object),)

    class Broken(AcmeFeed):
        name = "broken"

        def settings(self, variables):
            raise RuntimeError("no")
    installed(Streamy(), Broken())
    assert set(registry.live_feeds()) == {"streamyiex"}
    assert "streamyiex: trades" in [d.holds for d in plugin_databases("streamy")]
    settings = settings_for()
    assert "broken" not in registry.providers() and settings.plugin("broken") is None and "streamy" in registry.providers()
    assert any(why == "broken: its settings failed (RuntimeError: no)" for _, why in registry.errors())


def test_a_public_feed_and_a_crypto_venue_as_classes(installed, settings_for):
    class FakeSource:
        def __init__(self, client):
            self.client = client

    class AcmePublic(PublicFeedPlugin):
        name = "acmepub"
        title = "Acme Public"
        assets = frozenset({"stock"})
        base_url = "https://data.acme.example"
        source_class = FakeSource
        tier = Tier("acme-public", requests_per_minute=30, history_years=None)
        per_run, per_day = 50, 500

    class AcmeCoin(CryptoVenuePlugin):
        name = "acmecoin"
        title = "Acme Coin"
        base_url = "https://api.acmecoin.example"
        source_class = FakeSource
        tier = Tier("acmecoin-public", requests_per_minute=10, history_years=None)
        per_run, per_day = 100, 1000
    installed(AcmePublic(), AcmeCoin())
    settings = settings_for()
    src = registry.get("acmepub").build(settings, 5)
    assert isinstance(src, FakeSource) and isinstance(src.client, PublicClient)
    view = settings.for_source("acmepub")
    assert view.tier.name == "acme-public" and view.max_requests_per_run == 50 and view.max_requests_per_day == 500
    coin = registry.get("acmecoin")
    assert coin.assets == frozenset({"crypto"}) and coin.kind == "exchange"
    assert coin.gives("build") and coin.gives("view") and not coin.gives("scan")  # its base's build, its own view
    limited = coin.settings_view(replace(settings, crypto_venue_days=30))
    assert limited.tier.history_days == 30 and limited.max_requests_per_day == 1000
    assert coin.settings_view(settings).tier.history_days is None
    with pytest.raises(NotImplementedError, match="no source_class"):
        PublicFeedPlugin(name="bare").build(settings, 1)


def test_a_process_loads_every_plugin_found_the_ones_named_or_none(installed, monkeypatch):
    """The user, 2026-11-03: "the default case is to load all plugins, however the integrator can be
    loaded with select plugins or none". LIMINAL_PLUGINS_LOADED (registry.select) names the plugins
    kept, each with its parts (a part's name keeps its whole plugin); `none` keeps none; the plugins
    found but not named are left out quietly, a name no plugin answers to is unknown; a process
    started by this one inherits the selection (its environment)."""
    class AcmeForex(AcmeFeed):
        name = "acme-fx"
        part_of = "acme"
        assets = frozenset({"forex"})
        keyless = frozenset()
        provides = {"forex": ("day",)}
        delays = {}
        hours = {}

    class AcmePress(AcmeNews):
        part_of = "acme"

    monkeypatch.setenv(registry.SELECTION_VAR, "")  # every plugin found (restored after)
    found = installed(AcmeFeed(), AcmeForex(), AcmePress(), acme_record("zenith"))
    assert set(found) == {"acme", "acme-fx", "zenith"} and registry.selection().everything
    assert registry.selection().text == "all" and registry.plugins_loaded() == ["acme", "zenith"]
    chosen = registry.select(["acme-fx", "nobody"])  # a part's name keeps its whole plugin, its publishers too
    assert chosen.names == ("acme-fx", "nobody") and chosen.left_out == ("zenith",) and chosen.unknown == ("nobody",)
    assert set(registry.providers()) == {"acme", "acme-fx"} and set(registry.publishers()) == {"acme-news"}
    assert registry.plugins_loaded() == ["acme"] and registry.errors() == []  # left out quietly, not broken
    assert os.environ[registry.SELECTION_VAR] == "acme-fx,nobody"  # what a process started now inherits
    assert chosen.text == "acme-fx,nobody" and [r["name"] for r in menu()] == ["acme"]  # the menu lists plugins, their feeds under them
    none = registry.select("none")
    assert none.names == () and none.text == "none" and registry.providers() == {} and registry.publishers() == {}
    assert none.left_out == ("acme", "zenith") and registry.plugins_loaded() == [] and variables() == ()
    assert registry.select(None).everything and set(registry.providers()) == {"acme", "acme-fx", "zenith"}
    assert registry.SELECTION_VAR not in os.environ
    monkeypatch.setenv(registry.SELECTION_VAR, "ZENITH, acme-news")  # as typed: any case, commas or spaces
    registry.reload()
    assert registry.selection().names == ("zenith", "acme-news") and set(registry.providers()) == {"acme", "acme-fx", "zenith"}
    assert registry.selection().left_out == () and registry.Selection.parse(" All ").everything
