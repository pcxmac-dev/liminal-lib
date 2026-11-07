"""The apps bound together (liminal_libs.apps.binding): a front passes its pages' requests on to a
bound app and its answers and cookies back, reads the bound app's tray, starts the app when nothing
answers (a stand-in Popen here), waits for it; an app served until quiet ends on its own. No process
starts: servers run in threads on port 0."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from liminal_libs import web
from liminal_libs.apps import App, Binding, TrayItem, forward, start_app, tray_of, wait_for
from liminal_libs.apps import binding as binding_mod


class Upstream(BaseHTTPRequestHandler):
    """A bound app's server, as far as a front needs one: it echoes what it got, sets a cookie, streams."""

    def log_message(self, *a):
        pass

    def _json(self, status, data):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Set-Cookie", "liminal_login=tok; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for i in range(2):
                self.wfile.write(f"data: {i}\n\n".encode())
                self.wfile.flush()
                time.sleep(0.05)
            return
        self._json(200, {"path": self.path, "origin": self.headers.get("Origin"), "host": self.headers.get("Host"),
                         "cookie": self.headers.get("Cookie"), "session": self.headers.get("X-Liminal-Session")})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self._json(202, {"got": json.loads(self.rfile.read(n) or b"{}"), "write": self.headers.get(web.WRITE_HEADER)})


def _front(url: str):
    """A super app's front: /demo/... passed on to the bound app at `url`."""

    class Front(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _go(self, method):
            if not web.local_only(self) or not web.from_this_app(self):
                return
            if not self.path.startswith("/demo/"):
                return web.send(self, 404, b"not found", "text/plain")
            forward(self, method, url, self.path[len("/demo"):], cookie_path="/demo", drop_cookies=("other_",),
                    unavailable="demo is away")

        def do_GET(self):
            self._go("GET")

        def do_POST(self):
            self._go("POST")

    return Front


def _get(port, path, headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    r = conn.getresponse()
    return r.status, r.read(), r


def test_a_front_passes_its_pages_requests_on_and_the_answers_and_cookies_back():
    up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{up.server_address[1]}"
    front = ThreadingHTTPServer(("127.0.0.1", 0), _front(url))
    threading.Thread(target=front.serve_forever, daemon=True).start()
    port = front.server_address[1]
    try:
        status, body, r = _get(port, "/demo/api/echo?x=1", {"Cookie": "other_app=1; liminal_login=tok; other_two=2",
                                                           "Origin": f"http://127.0.0.1:{port}",
                                                           "Sec-Fetch-Site": "same-origin", "X-Liminal-Session": "tok"})
        j = json.loads(body)
        assert status == 200 and j["path"] == "/api/echo?x=1" and j["cookie"] == "liminal_login=tok" and j["session"] == "tok"
        assert json.loads(_get(port, "/demo/api/echo", {"Cookie": "other_app=1"})[1])["cookie"] is None  # none left: no header
        assert j["origin"] is None and j["host"] == url.split("//", 1)[1]  # where it came from stays here
        assert r.getheader("Set-Cookie") == "liminal_login=tok; Path=/demo; HttpOnly; SameSite=Strict"  # the cookie's path narrowed
        assert _get(port, "/demo/api/echo", {"Origin": "http://evil.example"})[0] == 403
        assert _get(port, "/other")[0] == 404
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/demo/api/x", body=json.dumps({"a": 1}), headers={"Content-Type": "application/json", web.WRITE_HEADER: "1"})
        r = conn.getresponse()
        assert r.status == 202 and json.loads(r.read()) == {"got": {"a": 1}, "write": "1"}
        status, body, r = _get(port, "/demo/api/stream")
        assert status == 200 and body == b"data: 0\n\ndata: 1\n\n" and r.getheader("Content-Type").startswith("text/event-stream")
        conn = HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/demo/api/x", body=b"{}", headers={"Content-Length": "-5", web.WRITE_HEADER: "1"})
        assert conn.getresponse().status == 400
        up.shutdown()
        up.server_close()
        status, body, _ = _get(port, "/demo/api/echo")
        assert status == 503 and json.loads(body) == {"error": "demo is away"}
    finally:
        front.shutdown()
        front.server_close()
    assert binding_mod._repath("a=1; Path=/; HttpOnly", "/login") == "a=1; Path=/login; HttpOnly"
    assert binding_mod._repath("a=1; HttpOnly", "/login") == "a=1; Path=/login; HttpOnly"
    assert binding_mod._repath("a=1; Path=/", None) == "a=1; Path=/"


class DemoApp(App):
    NAME, TITLE, ICON = "demo", "Demo", "plug"

    def items(self):
        return (TrayItem("things", "key", "Things", app="demo"),)


def test_a_bound_apps_tray_is_read_and_an_absent_app_is_started_and_waited_for(tmp_path, monkeypatch):
    DemoApp.STATIC = tmp_path
    app = DemoApp(log=lambda m: None)
    url = app.start(0).rstrip("/")
    port = int(url.rsplit(":", 1)[1])
    try:
        b = Binding("demo", url, "demo_module")
        assert b.port == port and b.prefix == "/demo" and b.reachable()
        tray = b.tray()
        assert tray["app"] == "demo" and [i["id"] for i in tray["items"]] == ["demo", "things"]
        assert wait_for(port, 1) is True
    finally:
        app.stop()
    assert tray_of(url) is None and not Binding("demo", url).reachable() and wait_for(port, 0.3, 0.05) is False
    # starting: the module's serve, detached, with the environment given; an unknown module is not started
    popen: list = []
    monkeypatch.setattr(binding_mod.subprocess, "Popen", lambda args, **kw: popen.append((args, kw)))
    lines: list[str] = []
    assert start_app("liminal_libs", 8795, {"LIMINAL_LOGIN_DIR": "x"}, lines.append) is True
    args, kw = popen[0]
    assert args[1:] == ["-m", "liminal_libs", "serve", "--no-browser", "--port", "8795", "--until-quiet"]
    assert kw["env"]["LIMINAL_LOGIN_DIR"] == "x" and kw["close_fds"] is True and kw["stdin"] is subprocess.DEVNULL
    assert start_app("liminal_libs", 8795, until_quiet=False) is True
    assert popen[1][0][-2:] == ["--port", "8795"]  # not until quiet: kept up
    assert start_app("no_such_module_here", 8795, log=lines.append) is False and len(popen) == 2
    assert any("not installed" in m for m in lines)


def test_an_app_served_until_quiet_ends_once_no_page_has_been_in_touch(tmp_path):
    DemoApp.STATIC = tmp_path
    lines: list[str] = []
    app = DemoApp(log=lines.append)
    done: list[str] = []
    threading.Thread(target=lambda: done.append(app.serve(0, until_quiet=0.6)), daemon=True).start()
    end = time.monotonic() + 5
    while not app.served and time.monotonic() < end:
        time.sleep(0.02)
    port = int(app.served.rstrip("/").rsplit(":", 1)[1])
    time.sleep(0.4)
    assert _get(port, "/api/tray")[0] == 200  # in touch: the clock starts again
    time.sleep(0.4)
    assert web.listening(port) and not done
    time.sleep(0.6)
    end = time.monotonic() + 5
    while not done and time.monotonic() < end:
        time.sleep(0.05)
    assert done == [app.served] and not web.listening(port) and any("no page has been in touch" in m for m in lines)
