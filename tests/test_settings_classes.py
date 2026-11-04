"""Phase 5c (1.5.0): the settings and the services as classes (config.Settings.load and
with_detected, config.UserEnvironment, datasets.DatasetFetcher and FileQueue, publications.Reader's
for_view, view, run_due and recheck_now, killswitch.KillSwitch, runner.Runner, live.Recorder.for_settings,
host.Attachment.to and Host.start). Each calls the module's functions through the module (a stand-in
a test sets is the one used); the module keeps a wrapper under every name a caller or a test uses
(load_settings, with_detected, saved_user_keys, save_user_variable, fetch, fetch_git, _FileQueue,
view, run_due, recheck_now, killed, strike, release, kills, run_plugin, run_all, attach, start_host)."""
from __future__ import annotations

import os
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from liminal_libs import config, datasets, host, killswitch, layout, live, publications, runner
from liminal_libs.config import Settings, UserEnvironment, load_settings
from liminal_libs.gov import GovDB
from liminal_libs.sources import registry
from liminal_libs.sources.dataset import Dataset, Parsed
from liminal_libs.sources.fetching import Fetched, Fetcher, Job
from liminal_libs.sources.keys import KeyVar
from liminal_libs.sources.plugin import FeedPlugin
from liminal_libs.sources.publisher import Feed, Policy, PublisherPlugin
from liminal_libs.storage import Store

NOW = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)
TODAY = date(2026, 10, 21)


class AcmeFeed(FeedPlugin):
    name = "acme"
    title = "Acme"
    about = "a test feed"
    assets = frozenset({"stock"})
    keys = (KeyVar("ACME_API_KEY"),)
    plan_vars = ("ACME_PLAN",)
    plan_choices = {"ACME_PLAN": ("free", "pro")}
    provides = {"stock": ("day", "minute")}

    def build(self, settings, request_budget):
        return None


class AcmeNews(PublisherPlugin):
    name = "acme-news"
    title = "Acme News"
    about = "what Acme announces"
    feeds = (Feed("acme-press", "Acme", "Press releases", "https://news.acme.example/rss"),)
    policy = Policy(hosts=("news.acme.example",))


class AcmeData(PublisherPlugin):
    name = "acme-data"
    title = "Acme Data"
    about = "its research files"
    policy = Policy(hosts=("data.acme.example",))
    datasets = (Dataset("acme-set", "Acme set", "", lambda t: [("https://data.acme.example/x.csv", False)],
                        lambda u, b: Parsed(values=[("X", 0, 1.0)]), 7, "acme", "research"),)


class AcmeDocs(PublisherPlugin):
    name = "acme-docs"
    title = "Acme Docs"
    about = "its documents, fetched on its own"
    policy = Policy(hosts=("docs.acme.example",))
    fetcher = Fetcher(lambda ctx: [Job("document", "x")], lambda ctx, job: Fetched(rows=1), ("document",))


@pytest.fixture
def installed(monkeypatch):
    """The plugins given the ones installed (entry points' instances), the plugin directory out of the way."""
    monkeypatch.setattr(layout, "plugins_dir", lambda: None)

    def install(*plugins):
        eps = [SimpleNamespace(name=p.name, value=f"fake:{p.name}", load=lambda p=p: p) for p in plugins]
        monkeypatch.setattr(registry, "_entry_points", lambda: eps)
        return registry.reload()

    yield install
    registry._providers = None


@pytest.fixture(autouse=True)
def nothing_of_yours(monkeypatch):
    """Tests never read the user's keys or settings: no saved variables unless a test gives some, the
    plugins' variables and the app's database out of the process's environment."""
    monkeypatch.setattr(config, "saved_user_keys", lambda: {})
    for name in (*registry.variables(), "LIMINAL_DB", "ACME_API_KEY", "ACME_PLAN"):
        monkeypatch.delenv(name, raising=False)


# -- the settings ------------------------------------------------------------------------------------
def test_settings_load_reads_the_file_the_saved_variables_and_the_environment_through_the_module(tmp_path, monkeypatch, installed):
    installed(AcmeFeed())
    monkeypatch.setattr(config, "read_env_file", lambda path: {"LIMINAL_DB": str(tmp_path / "file.db"), "ACME_PLAN": "free"})
    monkeypatch.setattr(config, "saved_user_keys", lambda: {"ACME_API_KEY": "saved", "ACME_PLAN": "pro"})
    s = Settings.load(env_file=Path("anything.env"))
    assert isinstance(s, Settings) and s.db_path == tmp_path / "file.db"
    assert s.env == {"ACME_API_KEY": "saved", "ACME_PLAN": "pro"}  # the saved variables over the file's
    assert s.variables["LIMINAL_DB"] == str(tmp_path / "file.db") and s.plugin("acme") is None  # no settings of its own
    monkeypatch.setenv("ACME_PLAN", "mine")
    assert Settings.load(env_file=Path("anything.env")).env["ACME_PLAN"] == "mine"  # the process's environment wins
    given = Settings.load(env={"LIMINAL_DB": str(tmp_path / "given.db")}, env_file=None)
    assert given.db_path == tmp_path / "given.db" and given.env == {}  # with `env`, nothing saved is read
    assert load_settings(env={"LIMINAL_DB": str(tmp_path / "given.db")}, env_file=None) == given  # the wrapper
    with pytest.raises(config.ConfigError):
        Settings.load(env={"LIMINAL_DB": str(tmp_path / "m.db"), "AUDITS_PER_RUN": "many"}, env_file=None)


def test_with_detected_applies_the_plans_found_where_you_set_none(tmp_path, installed, monkeypatch):
    installed(AcmeFeed())
    base = Settings.load(env={"LIMINAL_DB": str(tmp_path / "m.db")}, env_file=None)
    out = base.with_detected({"ACME_PLAN": "pro"})
    assert out.detected == {"ACME_PLAN": "pro"} and out.env["ACME_PLAN"] == "pro" and out.variables == base.variables
    assert out.with_detected({"ACME_PLAN": "pro"}) is out  # the same plans: as it is
    assert config.with_detected(base, {"ACME_PLAN": "pro"}).detected == out.detected  # the wrapper
    mine = Settings.load(env={"LIMINAL_DB": str(tmp_path / "m.db"), "ACME_PLAN": "free"}, env_file=None)
    assert mine.with_detected({"ACME_PLAN": "pro"}) is mine  # your own setting wins
    direct = Settings(db_path=tmp_path / "m.db")
    assert direct.with_detected({"ACME_PLAN": "pro"}) is direct  # made directly: no variables to apply to
    calls: list[dict] = []
    monkeypatch.setattr(config, "load_settings", lambda env, env_file: calls.append(env) or base)  # through the module
    assert base.with_detected({"ACME_PLAN": "plus"}).detected == {"ACME_PLAN": "plus"} and calls[0]["ACME_PLAN"] == "plus"


class FakeRegistry:
    """A stand-in for winreg: the user Environment key as a dict."""

    HKEY_CURRENT_USER, KEY_SET_VALUE, REG_SZ = "HKCU", 2, 1

    def __init__(self, values=None):
        self.values = dict(values or {})

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, path, reserved=0, access=0):
        assert (root, path) == ("HKCU", "Environment")
        return self._Key()

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


def test_the_user_environment_reads_and_saves_the_plugins_variables(installed):
    installed(AcmeFeed())
    reg = FakeRegistry({"ACME_API_KEY": "k1", "OTHER": "x", "ACME_PLAN": ""})
    env = UserEnvironment(registry=reg)
    assert env.names == registry.variables() and "ACME_API_KEY" in env.names and not env.real
    assert env.saved() == {"ACME_API_KEY": "k1"}  # the plugins' variables, the set ones
    assert UserEnvironment(registry=reg, names=("OTHER",)).saved() == {"OTHER": "x"}
    env.save("ACME_API_KEY", "k2")
    assert reg.values["ACME_API_KEY"] == "k2" and os.environ["ACME_API_KEY"] == "k2"  # saved, and this process's
    env.save("ACME_API_KEY", None)
    assert "ACME_API_KEY" not in reg.values and "ACME_API_KEY" not in os.environ
    env.save("ACME_API_KEY", None)  # removing what is not there: nothing
    config.save_user_variable("ACME_API_KEY", "k3", registry=reg)  # the wrapper
    assert reg.values["ACME_API_KEY"] == "k3" and os.environ["ACME_API_KEY"] == "k3"
    if os.name != "nt":  # without Windows' registry: nothing to read, nothing saved
        assert UserEnvironment().saved() == {}
        with pytest.raises(config.ConfigError):
            UserEnvironment().save("ACME_API_KEY", "k")


# -- the datasets ----------------------------------------------------------------------------------
def test_the_dataset_fetcher_fetches_a_declared_file_once_and_keeps_its_queue(tmp_path, installed):
    installed(AcmeData())

    class Resp:
        status_code, content, headers = 200, b"x,1", {"Last-Modified": "Tue, 20 Oct 2026 12:00:00 GMT"}

        def raise_for_status(self):
            pass

    class Session:
        def __init__(self):
            self.calls: list[str] = []

        def get(self, url, headers=None, timeout=None):
            self.calls.append(url)
            return Resp()

    session, log = Session(), []
    with Store(tmp_path / "m.db") as store:
        fetcher = datasets.DatasetFetcher(store, today=TODAY, session=session, log=log.append)
        assert fetcher.session is session and fetcher.today == TODAY
        assert fetcher.fetch(["acme-set"]) == {"acme-set": 1} and session.calls == ["https://data.acme.example/x.csv"]
        assert fetcher.fetch(["acme-set"]) == {"acme-set": 0} and len(session.calls) == 1  # fetched today: not due
        assert fetcher.fetch(["acme-set"], force=True) == {"acme-set": 0} and len(session.calls) == 2  # unchanged
        rows = {r["key"]: r for r in store.queue_rows("acme-data")}
        assert rows["https://data.acme.example/x.csv"]["state"] == "done" and rows["https://data.acme.example/x.csv"]["detail"] == "unchanged"
        assert datasets.fetch(store, ["acme-set"], today=TODAY, session=session, log=log.append, force=True) == {"acme-set": 0}
        assert fetcher.fetch(["acme-set"], only_started=True) == {"acme-set": 0}
        assert any("acme-set: x.csv (0.00 MB, published 2026-10-20)" in m for m in log)
        queue = datasets.FileQueue(store, datasets.declared()["acme-set"], log.append)  # as collected: its publisher's
        assert queue.plugin == queue.part == "acme-data" and queue.title("https://data.acme.example/y.csv") == "acme-set: y.csv"
    assert datasets._FileQueue is datasets.FileQueue  # its name before 1.5.0
    assert datasets.DatasetFetcher(object(), today=TODAY)._session is None  # a session only when a file is fetched


# -- the publications' reader ----------------------------------------------------------------------
def test_the_reader_is_made_for_a_view_a_pass_and_a_recheck(tmp_path, installed):
    news = AcmeNews()
    installed(news)
    reader = publications.Reader.for_view(GovDB(None))
    assert isinstance(reader.session, publications._NoSession) and reader.store is None
    rows = publications.Reader.view(GovDB(None), None, None, now=NOW, publishers={"acme-news": news})
    assert [p["name"] for p in rows] == ["acme-news"] and rows[0]["state"] == "ready"
    assert rows[0]["feeds"][0]["state"] == "not asked yet" and rows[0]["policy"]["hosts"] == ["news.acme.example"]
    assert publications.view(GovDB(None), None, None, now=NOW, publishers={"acme-news": news}) == rows  # the wrapper
    offline = SimpleNamespace(offline=True, db_path=tmp_path / "m.db")
    assert publications.Reader.run_due(offline, print) is None and publications.run_due(offline, print) is None
    assert publications.Reader.recheck_now(offline, "acme-news", print) is None
    online = SimpleNamespace(offline=False, db_path=tmp_path / "m.db")
    assert publications.recheck_now(online, "nobody", print) is None  # unknown: nothing sent, no lock taken
    assert not publications.lock_path(tmp_path / "m.db").exists()


# -- the kill switch ---------------------------------------------------------------------------------
def test_the_kill_switch_strikes_releases_and_lists_through_the_store(monkeypatch):
    with Store(":memory:") as store:
        ks = killswitch.KillSwitch(store)
        k = ks.strike("fake", "live", "refused", now=NOW)
        assert k["strikes"] == 1 and k["by"] == "policy"
        assert ks.killed("fake", "live", NOW + timedelta(minutes=29)) and ks.killed("fake", "live", NOW + timedelta(minutes=31)) is None
        assert killswitch.killed(store, "fake", "live", NOW) == k  # the wrapper
        again = ks.strike("fake", "live", "still refused", now=NOW + timedelta(minutes=5))
        assert again["strikes"] == 1 and again["until"] == k["until"]  # the same kill
        second = ks.strike("fake", "live", "refused", now=NOW + timedelta(hours=1))
        assert second["strikes"] == 2 and datetime.fromisoformat(second["until"]) == NOW + timedelta(hours=2)  # the ladder
        at = NOW + timedelta(hours=1)
        assert [x["feed"] for x in ks.kills(["fake", "other"], at)] == ["fake"] == [x["feed"] for x in killswitch.kills(store, ["fake", "other"], at)]
        monkeypatch.setattr(killswitch, "sharing", lambda feed: ["twin"] if feed == "fake" else [])  # through the module
        ks.strike("fake", "fetch", "429", now=NOW, at_least_s=7200)
        twin = ks.killed("twin", "fetch", NOW)
        assert twin and twin["why"].startswith("the same account as fake") and datetime.fromisoformat(twin["until"]) == NOW + timedelta(hours=2)
        assert killswitch.strike(store, "other", "fetch", "paused by you", now=NOW, minutes=5)["by"] == "user"
        assert ks.release("fake") == ["live", "fetch"] and killswitch.release(store, "twin") == ["fetch"]
        assert ks.kills(["fake", "twin"], NOW) == [] and ks.release("fake") == []


# -- the fetcher's workers ---------------------------------------------------------------------------
def test_the_runner_runs_each_plugins_worker_on_its_own_store(tmp_path, installed):
    installed(AcmeDocs())
    path = tmp_path / "m.db"
    Store(path).close()
    log: list[str] = []
    r = runner.Runner(lambda: Store(path), None, log.append)
    out = r.run()
    assert out == {"acme-docs": {"done": 1, "failed": 0, "held": 0, "stopped": None}}
    parts = registry.plugin_fetchers()["acme-docs"]
    with Store(path) as store:
        assert {x["key"]: x["state"] for x in store.queue_rows("acme-docs")} == {"x": "done"}
        assert r.plugin("acme-docs", parts, store, clock=lambda: NOW) == out["acme-docs"]  # planned and fetched again
        assert runner.run_plugin("acme-docs", parts, store, None, log.append, clock=lambda: NOW)["done"] == 1  # the wrapper
        assert runner.run_plugin("acme-docs", parts, store, None, log.append, clock=lambda: NOW, limit=0)["done"] == 0
    assert runner.run_all(lambda: Store(path), None, log.append) == out
    assert runner.Runner(lambda: Store(path), None, log.append).run(only="nobody") == {}


# -- the recorder for the settings ---------------------------------------------------------------
def test_the_recorder_for_settings_is_the_apps_make_recorder(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMINAL_LIVE_LOCK", str(tmp_path / "live.lock"))
    settings = load_settings(env={"LIMINAL_DB": str(tmp_path / "m.db"), "LIVE_CRYPTO": "off", "LIVE_STOCKS": "off"},
                             env_file=None)
    stop = threading.Event()
    rec = live.Recorder.for_settings(settings, locks=[], stop=stop)
    assert rec.db_path == settings.db_path and rec.coins == () and rec.streams == [] and rec.stock_keys is None
    assert rec.publications is settings and rec.stop is stop and rec.selected_only is settings.live_selected_only
    assert rec.boats is None and rec.locks == []
    assert rec.log.path == tmp_path / "logs" / "liminal.log"  # the database's log (logs.log_to)
    quiet = live.Recorder.for_settings(settings, locks=[], log=print)
    assert quiet.log is print and quiet.stop is not stop


# -- the resident hosts ----------------------------------------------------------------------------
def test_an_attachment_is_made_by_the_class_and_a_host_started_by_it(tmp_path, monkeypatch):
    monkeypatch.setenv("LIMINAL_IPC_DIR", str(tmp_path / "ipc"))
    db = tmp_path / "m.db"
    calls: list[tuple] = []

    class Popen:
        def __init__(self, args, **kw):
            calls.append((args, kw))

    monkeypatch.setattr(host.subprocess, "Popen", Popen)
    host.Host.start("live", db)
    host.start_host("live", db)  # the wrapper
    assert len(calls) == 2
    assert all(a[1:] == ["-m", host.APP_MODULE, "host", "live"] and kw["env"]["LIMINAL_DB"] == str(db) for a, kw in calls)
    assert host.Attachment.to("t", db, wait=0.2) is None  # no host answers, none started
    started: list[int] = []
    assert host.attach("t", db, start=lambda: started.append(1), wait=0.3) is None and started == [1]  # started, never answered
    served = host.Host("t", db, idle=0.3, start=5, log=lambda m: None)
    assert served.start_wait == 5  # how long a new host waits for its first client (`start` is the class's: a process)
    reasons: list[str] = []
    thread = threading.Thread(target=lambda: reasons.append(served.serve(lambda stop: stop.wait())), daemon=True)
    thread.start()
    attached, deadline = None, time.monotonic() + 5
    while attached is None and time.monotonic() < deadline:
        attached = host.Attachment.to("t", db, wait=1.0)
    assert isinstance(attached, host.Attachment) and attached.host_pid == os.getpid() and attached.alive()
    assert served.clients() == [os.getpid()]
    attached.detach()
    thread.join(5)
    assert reasons and "no client attached" in reasons[0]
