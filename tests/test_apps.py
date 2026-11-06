"""The apps' frame (liminal_libs.apps): an app served at a free port answers its own pages alone,
serves its files and the library's (the icons, the theme), its tray and its routes; the login
client's calls and errors against a stand-in login app. No process starts: servers run in threads
on port 0."""
from __future__ import annotations

import json
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from liminal_libs import ui, web
from liminal_libs.apps import (App, LoginClient, LoginRefused, LoginUnreachable, Refused, TrayItem,
                               SESSION_HEADER)


class DemoApp(App):
    NAME, TITLE, ICON = "demo", "Demo", "plug"
    PORT_VAR = "LIMINAL_DEMO_PORT"

    def items(self):
        return (TrayItem("things", "key", "Things: the demo's plugin", app="demo"),)

    def api(self, handler, method, path, query, body):
        if path == "/api/echo" and method == "GET":
            return 200, {"q": query, "cookie": web.cookie(handler, "demo")}
        if path == "/api/echo" and method == "POST":
            return 201, {"got": body}, [("Set-Cookie", "demo=1; Path=/")]
        if path == "/api/refuse":
            raise Refused(418, "no")
        if path == "/api/boom":
            raise RuntimeError("a secret the page must not see")
        return super().api(handler, method, path, query, body)


def _port(url: str) -> int:
    return int(url.split("127.0.0.1:", 1)[1].split("/", 1)[0])


def _get(port, path, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    return r.status, r.read(), r


def _send(port, method, path, body=None, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    conn.request(method, path, body=data, headers=headers or {})
    r = conn.getresponse()
    return r.status, r.read(), r


@pytest.fixture
def demo(tmp_path):
    (tmp_path / "index.html").write_text("<html><title>Demo</title><nav id=\"rail\"></nav></html>", encoding="utf-8")
    DemoApp.STATIC = tmp_path
    lines: list[str] = []
    app = DemoApp(log=lines.append)
    app.start(0)
    yield app, _port(app.served), lines
    app.stop()


def test_an_app_serves_its_page_the_librarys_files_its_tray_and_its_routes(demo):
    app, port, lines = demo
    assert any(m.startswith("demo: its page at http://127.0.0.1:") for m in lines)
    status, body, r = _get(port, "/")
    assert status == 200 and b"<title>Demo</title>" in body and r.getheader("Cache-Control") == "no-store"
    status, body, r = _get(port, "/icons.js")  # the library's, beside the app's own files
    assert status == 200 and b"window.LiminalIcons" in body and b"user:" in body and "javascript" in r.getheader("Content-Type")
    assert b"#rail" in _get(port, "/liminal.css")[1]
    assert _get(port, "/../pyproject.toml")[0] == 404 and _get(port, "/nothing.js")[0] == 404
    status, body, _ = _get(port, "/api/tray")
    tray = json.loads(body)
    assert status == 200 and tray["app"] == "demo" and tray["icon"] == "plug"
    assert [(i["id"], i["kind"]) for i in tray["items"]] == [("demo", "app"), ("things", "plugin")]
    assert json.loads(_get(port, "/api/app")[1])["url"] == app.served
    status, body, _ = _get(port, "/api/echo?x=1&y=2")
    assert status == 200 and json.loads(body) == {"q": {"x": "1", "y": "2"}, "cookie": None}
    assert json.loads(_get(port, "/api/echo", {"Cookie": "demo=7"})[1])["cookie"] == "7"
    assert _get(port, "/api/missing")[0] == 404
    # another site's page is refused, before any route runs
    assert _get(port, "/api/echo", {"Origin": "http://evil.example"})[0] == 403
    assert _get(port, "/api/echo", {"Host": "evil.example"})[0] == 403
    # a write needs the app's own header and a JSON object; its answer's headers go through
    assert _send(port, "POST", "/api/echo", {"a": 1}, {"Content-Type": "application/json"})[0] == 403
    status, body, r = _send(port, "POST", "/api/echo", {"a": 1}, {"Content-Type": "application/json", web.WRITE_HEADER: "1"})
    assert status == 201 and json.loads(body) == {"got": {"a": 1}} and r.getheader("Set-Cookie") == "demo=1; Path=/"
    assert _send(port, "POST", "/api/echo", b"[1]", {web.WRITE_HEADER: "1"})[0] == 400
    assert _send(port, "POST", "/api/echo", b"{not json", {web.WRITE_HEADER: "1"})[0] == 400
    assert _send(port, "POST", "/api/echo", None, {web.WRITE_HEADER: "1", "Content-Length": str(web.MAX_BODY + 1)})[0] == 413
    assert _send(port, "POST", "/other", None, {web.WRITE_HEADER: "1"})[0] == 404
    status, body, _ = _get(port, "/api/refuse")
    assert status == 418 and json.loads(body) == {"error": "no"}
    status, body, _ = _get(port, "/api/boom")
    assert status == 500 and b"secret" not in body and any("GET /api/boom failed: RuntimeError" in m for m in lines)


def test_an_app_takes_its_port_from_its_variable_and_stops_when_told(tmp_path, monkeypatch):
    DemoApp.STATIC = tmp_path
    monkeypatch.setenv("LIMINAL_DEMO_PORT", "0")
    app = DemoApp(log=lambda m: None)
    assert app.port() == 0 and app.url(8795) == "http://127.0.0.1:8795/"
    monkeypatch.setenv("LIMINAL_DEMO_PORT", "8795")
    assert app.port() == 8795
    monkeypatch.setenv("LIMINAL_DEMO_PORT", "none")
    assert app.port() == 0
    stop = threading.Event()
    done: list[str] = []
    threading.Thread(target=lambda: done.append(app.serve(0, stop=stop)), daemon=True).start()
    end = threading.Event()
    for _ in range(100):
        if app.served:
            break
        end.wait(0.05)
    port = _port(app.served)
    assert web.listening(port) and _get(port, "/api/tray")[0] == 200
    stop.set()
    for _ in range(100):
        if done:
            break
        end.wait(0.05)
    assert done == [app.served] and not web.listening(port)


# -- the login client against a stand-in login app ------------------------------------------------

class FakeLogin(BaseHTTPRequestHandler):
    """What the login app answers, as far as the client is concerned; it records what it got."""

    seen: list = []

    def log_message(self, *a):
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, body=None):
        self.seen.append((self.command, self.path, self.headers.get(SESSION_HEADER), self.headers.get(web.WRITE_HEADER), body))

    def do_GET(self):
        self._record()
        if self.headers.get(SESSION_HEADER) != "tok-1":
            return self._json(401, {"error": "not signed in"})
        if self.path == "/api/me":
            return self._json(200, {"name": "ada"})
        if self.path == "/api/pairs":
            return self._json(200, {"pairs": [{"name": "massive-account-api-key", "kind": "key"}]})
        if self.path == "/api/pairs/massive-account-api-key":
            return self._json(200, {"name": "massive-account-api-key", "value": "the-key"})
        if self.path == "/api/pairs/odd%20name":
            return self._json(200, {"name": "odd name", "value": "v"})
        self._json(404, {"error": "no such pair"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self._record(body)
        if self.path == "/api/sign-in":
            if body == {"name": "ada", "password": "pw"}:
                return self._json(200, {"token": "tok-1", "user": {"name": "ada"}})
            return self._json(401, {"error": "wrong name or password"})
        if self.path == "/api/sign-out":
            return self._json(200, {"signedOut": True})
        self._json(404, {"error": "not found"})

    def do_PUT(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._record(json.loads(self.rfile.read(n)))
        self._json(200, {"name": "x", "kind": "account"})

    def do_DELETE(self):
        self._record()
        self._json(200, {"removed": True})


@pytest.fixture
def fake_login():
    FakeLogin.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLogin)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_the_login_client_signs_in_keeps_the_session_and_asks_for_vectors(fake_login, monkeypatch):
    monkeypatch.setenv("LIMINAL_LOGIN_URL", fake_login + "/")
    client = LoginClient()
    assert client.url == fake_login and not client.signed_in
    with pytest.raises(LoginRefused) as e:
        client.user()
    assert e.value.status == 401 and e.value.reason == "not signed in"
    with pytest.raises(LoginRefused) as e:
        client.sign_in("ada", "nope")
    assert e.value.status == 401 and "wrong name" in e.value.reason and not client.signed_in
    assert client.sign_in("ada", "pw") == {"name": "ada"} and client.token == "tok-1"
    assert client.user() == {"name": "ada"} and client.pairs() == [{"name": "massive-account-api-key", "kind": "key"}]
    assert client.key("massive") == "the-key" and client.vector("odd name") == "v"
    with pytest.raises(LoginRefused) as e:
        client.account("massive")
    assert e.value.status == 404
    assert client.put("x", "v", "account", "Acme") == {"name": "x", "kind": "account"} and client.remove("x") == {"removed": True}
    client.sign_out()
    assert not client.signed_in
    # what travelled: the session on every call after sign-in, the write header on every write, never on a GET
    methods = [(m, p, tok, w) for m, p, tok, w, _ in FakeLogin.seen]
    assert methods[0] == ("GET", "/api/me", None, None)
    assert ("POST", "/api/sign-in", None, "1") in methods
    assert ("GET", "/api/pairs/massive-account-api-key", "tok-1", None) in methods
    assert ("GET", "/api/pairs/odd%20name", "tok-1", None) in methods
    assert ("PUT", "/api/pairs/x", "tok-1", "1") in methods and ("DELETE", "/api/pairs/x", "tok-1", "1") in methods
    assert ("POST", "/api/sign-out", "tok-1", "1") in methods
    assert [b for m, p, _, _, b in FakeLogin.seen if p == "/api/pairs/x" and m == "PUT"] == [{"value": "v", "kind": "account", "label": "Acme"}]
    # a token given to another program stands in for its sign-in
    other = LoginClient(fake_login, token="tok-1")
    assert other.signed_in and other.user() == {"name": "ada"}


def test_the_login_client_says_when_no_login_app_answers():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeLogin)
    port = srv.server_address[1]
    srv.server_close()  # a port nothing answers at
    client = LoginClient(f"http://127.0.0.1:{port}", timeout=1)
    with pytest.raises(LoginUnreachable):
        client.status()
    assert ui.STATIC.joinpath("icons.js").is_file() and ui.STATIC.joinpath("liminal.css").is_file()
