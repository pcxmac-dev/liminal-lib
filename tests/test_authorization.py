"""The authorization interface (apps.authorization.Login) and the plugins' IPC surface: a plugin asks
its login, and nothing else, for its keys; an app enumerates the plugins attached to it and their API,
and calls a hook by name; the login client and the environment are realizations of the one interface."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from liminal_libs.apps import App, EnvironmentLogin, Login, LoginClient, LoginRefused, LoginUnreachable, Unauthorized
from liminal_libs.sources.conformance import problems
from liminal_libs.sources.plugin import FeedPlugin
from liminal_libs.sources.registry import Provider
from liminal_libs.sources.publisher import PublisherPlugin


class FakeLogin(Login):
    NAMESPACE = "charts"

    def __init__(self, pairs: dict | None = None, name: str | None = "ada"):
        self.pairs, self.name = dict(pairs or {}), name

    def user(self) -> dict:
        if self.name is None:
            raise Unauthorized("not signed in")
        return {"name": self.name, "email": "ada@example.test", "verified": True}

    def vector(self, name: str) -> str:
        self.user()
        if name not in self.pairs:
            raise Unauthorized(f"no such pair: {name}", 404)
        return self.pairs[name]


def test_the_login_interface_names_the_pairs_and_says_who_is_signed_in():
    login = FakeLogin({"massive-account-api-key": "k", "massive-account-name": "ada@example.test", "claude-account-password": "p"})
    assert login.key("massive") == "k" and login.account("massive") == "ada@example.test" and login.password("claude") == "p"
    assert login.has("massive-account-api-key") and not login.has("tiingo-account-api-key")
    assert login.signed_in and login.may("massive", "fetch") and login.describe() == {"namespace": "charts", "signedIn": True, "kind": "FakeLogin"}
    with pytest.raises(Unauthorized) as e:
        login.key("tiingo")
    assert e.value.status == 404
    nobody = FakeLogin(name=None)
    assert not nobody.signed_in and not nobody.may("massive", "fetch") and not nobody.has("massive-account-api-key")
    with pytest.raises(Unauthorized) as e:
        nobody.user()
    assert e.value.status == 401
    with pytest.raises(TypeError):
        Login()  # abstract


def test_the_environment_is_a_login_until_every_plugin_is_attached_through_an_app():
    env = {"MASSIVE_API_KEY": "k1", "TIINGO_ACCOUNT_API_KEY": "k2"}
    login = EnvironmentLogin({"massive-account-api-key": "MASSIVE_API_KEY"}, env)
    assert login.key("massive") == "k1" and login.key("tiingo") == "k2"  # mapped, else the variable named after the pair
    assert login.user()["name"] and login.signed_in and login.NAMESPACE == "environment"
    with pytest.raises(Unauthorized) as e:
        login.account("massive")
    assert e.value.status == 404 and "MASSIVE_ACCOUNT_NAME" in e.value.reason
    assert isinstance(LoginClient("http://127.0.0.1:1"), Login) and issubclass(LoginRefused, Unauthorized)
    assert issubclass(LoginUnreachable, Unauthorized)
    away = LoginClient("http://127.0.0.1:9", timeout=0.5)
    assert away.describe()["reachable"] is False and not away.signed_in


class AcmeSource:
    def __init__(self, budget):
        self.budget = budget


class AcmePlugin(FeedPlugin):
    name, title, api = "acme", "Acme", "2.0"
    assets = frozenset({"stock"})
    provides = {"stock": ("day",)}

    def build(self, settings, request_budget):
        return AcmeSource(request_budget)

    def serves(self, settings):
        return frozenset({"stock"}) if self.login is not None and self.login.has("acme-account-api-key") else frozenset()


class DemoApp(App):
    NAME, TITLE, ICON = "demo", "Demo", "plug"


def test_a_plugin_enumerates_the_hooks_it_offers_and_asks_its_login_for_its_key(tmp_path):
    p = AcmePlugin()
    face = p.interface()
    assert face["name"] == "acme" and face["kind"] == "feed" and face["system"] == "integrator" and face["attached"] is False
    assert set(face["hooks"]) == {"build", "serves"} and face["hooks"]["build"] == "(settings, request_budget)"
    assert p.ask("build", None, 7).budget == 7
    with pytest.raises(LookupError):
        p.ask("rate", None)  # the contract's default is not offered as the plugin's own
    with pytest.raises(LookupError):
        p.ask("interface")
    assert p.login is None and p.app is None and p.ask("serves", None) == frozenset()
    # attached by an app: its login furnishes the key, and nothing else does
    DemoApp.STATIC = tmp_path
    app = DemoApp(log=lambda m: None, login=FakeLogin({"acme-account-api-key": "k"}))
    assert app.attach(p)["attached"] is True and p.app is app and p.login is app.login
    assert p.login.key("acme") == "k" and p.ask("serves", None) == frozenset({"stock"})
    assert app.plugins() == {"acme": p.interface()} and app.ask("acme", "build", None, 3).budget == 3
    with pytest.raises(LookupError):
        app.ask("nobody", "build")
    face = app.interface()
    assert face["name"] == "demo" and face["plugins"] == ["acme"] and face["login"]["namespace"] == "charts"
    assert set(face["methods"]) == {"describe", "tray", "plugins", "attach", "detach", "ask", "interface"}
    assert problems(p) == []
    # served: the app's plugins and interface
    app.start(0)
    try:
        port = int(app.served.rstrip("/").rsplit(":", 1)[1])
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/plugins")
        assert json.loads(conn.getresponse().read()) == {"plugins": {"acme": p.interface()}}
        conn.request("GET", "/api/interface")
        assert json.loads(conn.getresponse().read())["plugins"] == ["acme"]
    finally:
        app.stop()
    assert app.detach("acme") is True and p.app is None and p.login is None and app.plugins() == {} and not app.detach("acme")


def test_a_record_and_a_publisher_offer_their_hooks_the_same_way():
    record = Provider(name="acme-rec", title="Acme", build=lambda s, b: AcmeSource(b), assets=frozenset({"stock"}))
    face = record.interface()
    assert face["kind"] == "feed" and set(face["hooks"]) == {"build"} and face["hooks"]["build"] == "(s, b)"
    assert record.ask("build", None, 9).budget == 9
    login = FakeLogin({"acme-rec-account-api-key": "k"})
    record.attach("the app", login)  # frozen, still attachable
    assert record.login is login and record.app == "the app" and record.login.key("acme-rec") == "k"
    record.detach()
    assert record.login is None

    class Agency(PublisherPlugin):
        name, title, api, kind = "agency", "Agency", "2.0", "official"

        def moments(self, day):
            return ()

    pub = Agency()
    assert pub.interface()["hooks"] == {"moments": "(day)"} and pub.HOOKS[0] == "moments"
    assert pub.ask("moments", None) == ()


def test_the_pairs_are_named_by_the_variables_and_a_login_fills_the_settings(monkeypatch):
    from liminal_libs import config
    from liminal_libs.apps import attach_registry, pair_name, variable_name
    from liminal_libs.sources.keys import KeyVar

    assert pair_name("MASSIVE_API_KEY") == "massive-api-key" and pair_name("ALPACA_API_KEY_ID") == "alpaca-api-key-id"
    assert variable_name("massive-api-key") == "MASSIVE_API_KEY" and KeyVar("TIINGO_API_KEY").pair == "tiingo-api-key"
    # a login furnishes the variables it has pairs for, listing them once when it can
    login = FakeLogin({"massive-api-key": "k1", "acme-api-key": "k2"})
    assert login.variables(["MASSIVE_API_KEY", "ACME_API_KEY", "TIINGO_API_KEY"]) == {"MASSIVE_API_KEY": "k1", "ACME_API_KEY": "k2"}
    assert FakeLogin(name=None).variables(["MASSIVE_API_KEY"]) == {} and login.variables([]) == {}

    class Listing(FakeLogin):
        asked: list = []

        def names(self):
            return set(self.pairs)

        def vector(self, name):
            self.asked.append(name)
            return super().vector(name)
    listing = Listing({"massive-api-key": "k1"})
    assert listing.variables(["MASSIVE_API_KEY", "TIINGO_API_KEY"]) == {"MASSIVE_API_KEY": "k1"} and listing.asked == ["massive-api-key"]
    env_login = EnvironmentLogin(env={"MASSIVE_API_KEY": "e", "EMPTY": ""})
    assert env_login.names() == {"massive-api-key"} and env_login.variables(["MASSIVE_API_KEY", "EMPTY"]) == {"MASSIVE_API_KEY": "e"}
    # the settings: the login's pairs win over the environment (the root authorization); without a login, as before
    monkeypatch.setattr(config, "saved_user_keys", lambda: {})
    plain = config.load_settings(env={"MASSIVE_API_KEY": "env-k"}, env_file=None)
    signed = config.load_settings(env={"MASSIVE_API_KEY": "env-k"}, env_file=None, login=FakeLogin({"massive-api-key": "login-k"}))
    assert plain.env.get("MASSIVE_API_KEY") == "env-k" and signed.env.get("MASSIVE_API_KEY") == "login-k"
    assert config.load_settings(env={}, env_file=None, login=FakeLogin(name=None)).env.get("MASSIVE_API_KEY") is None
    # every plugin the registry loaded, attached to an app (none twice, none another app's)
    app = DemoApp(log=lambda m: None, login=login)
    other = DemoApp(log=lambda m: None)
    names = attach_registry(app, only="integrator")
    from liminal_libs.sources.registry import providers

    assert all(providers()[n].app is app and providers()[n].login is login for n in names if n in providers())
    assert attach_registry(other, only="integrator") == []  # held by `app` already
    assert set(app.plugins()) == set(names)
    for n in names:
        app.detach(n)
