"""An app's common frame: its page served at its own port on this machine alone, its tray, its
routes, the files of its page (its own folder first, then the library's: the icons and the theme).

The user, 2026-11-04: "every app, whether it's a login, the charts, platform, or integrator has its
own web kind of server and has a menu on the right-hand side with icons"; "the library basically
facilitates the interprocess communication, the way the apps can bind to each other, or interoperate
... As well as the framework for the graphics". An app is a subclass naming itself (NAME, TITLE, the
master ICON, its PORT and the variable overriding it, its own STATIC folder) and answering its routes
in `api`; everything a server must do alike (only this machine, only its own pages, the write
header on every write, the body's size and shape, the files, a taken port) is here, once. The chart
front (liminal_charts.gui.front) and the integrator's page do the same by hand today and move onto
this frame at 0.3.0, when a super app composes the trays of the apps it is bound to."""
from __future__ import annotations

import inspect
import json
import os
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .. import web
from ..ui import STATIC as UI_STATIC
from .tray import Tray, TrayItem


class Refused(Exception):
    """An answer the app refuses: its status and the reason, sent as {"error": reason}."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class App:
    """The frame of one app. A subclass sets the class attributes and overrides `api` (its routes),
    `items` (its tray's plugins) and, when its page depends on the request, `page`."""

    NAME = ""  # "login", "charts", "integrator", "platform", "plugins"
    TITLE = ""
    ICON = ""  # the master icon's name in icons.js (the user: charts "symbol", integrator "fetcher", login "user")
    PORT = 0  # its page's default port on 127.0.0.1 (0: a free one)
    PORT_VAR = ""  # the environment variable overriding PORT (LIMINAL_LOGIN_PORT ...)
    STATIC: Path | None = None  # the app's own page files (index.html, its scripts)
    INDEX = "index.html"
    API = "1.0"  # the app's own API version, for its clients

    def __init__(self, log=print, login=None):
        self.log = log
        self.login = login  # the authorization interface of this app's namespace (apps.authorization.Login)
        self.served: str | None = None  # the URL once started
        self.touched = time.monotonic()  # the last request (serve's until_quiet)
        self._plugins: dict[str, object] = {}  # the plugins attached, by name (attach)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- the plugins attached (the user, 2026-11-06: "the app itself (either chart or integrator) has
    # plugin ABC as well"; "the plugin ABC will enumerate attached plugins and their API") ------------
    def attach(self, plugin) -> dict:
        """A plugin attached to this app: it gets this app and its login (the one way to its keys);
        returns the interface the plugin offers (what `plugins` enumerates)."""
        interface = plugin.attach(self, self.login)
        self._plugins[plugin.name] = plugin
        self.log(f"{self.NAME}: {plugin.name} attached ({plugin.KIND}; hooks {', '.join(interface['hooks']) or 'none'})")
        return interface

    def detach(self, name: str) -> bool:
        plugin = self._plugins.pop(name, None)
        if plugin is None:
            return False
        plugin.detach()
        self.log(f"{self.NAME}: {name} detached")
        return True

    def plugin(self, name: str):
        return self._plugins.get(name)

    def plugins(self) -> dict[str, dict]:
        """The plugins attached and their API, by name (`GET /api/plugins`)."""
        return {name: p.interface() for name, p in self._plugins.items()}

    def ask(self, name: str, hook: str, *args, **kwargs):
        """One attached plugin's hook called by name (LookupError: no such plugin or hook)."""
        plugin = self._plugins.get(name)
        if plugin is None:
            raise LookupError(f"no plugin {name!r} is attached to {self.NAME}")
        return plugin.ask(hook, *args, **kwargs)

    def interface(self) -> dict:
        """What this app offers (the same shape a plugin gives of itself: `GET /api/interface`): its
        name and API version, the methods a plugin or another app may call on it."""
        methods = {}
        for name in ("describe", "tray", "plugins", "attach", "detach", "ask", "interface"):
            try:
                methods[name] = str(inspect.signature(getattr(self, name)))
            except (TypeError, ValueError):
                methods[name] = "(...)"
        return {"name": self.NAME, "kind": "app", "api": self.API, "login": self.login.describe() if self.login is not None else None,
                "plugins": sorted(self._plugins), "methods": methods}

    def touch(self) -> None:
        """A page is in touch (every request): what `serve(until_quiet=...)` waits on."""
        self.touched = time.monotonic()

    # -- what the app says about itself ----------------------------------------------------------
    def tray(self) -> Tray:
        """The app's tray: its master icon, then its plugins' (`items`)."""
        me = TrayItem(self.NAME, self.ICON, self.TITLE, "app", self.NAME)
        return Tray(self.NAME, self.ICON, self.TITLE, (me, *self.items()))

    def items(self) -> tuple[TrayItem, ...]:
        """The icons of the app's view plugins, in order (none in the frame)."""
        return ()

    def describe(self) -> dict:
        """What a client or a super app learns first (`GET /api/app`)."""
        return {"app": self.NAME, "title": self.TITLE, "icon": self.ICON, "api": self.API, "port": self.port(),
                "url": self.served or self.url()}

    def port(self) -> int:
        """The port to serve at: PORT_VAR in the environment when set to a number, else PORT."""
        raw = (os.environ.get(self.PORT_VAR) or "").strip() if self.PORT_VAR else ""
        return int(raw) if raw.isdecimal() else self.PORT

    def url(self, port: int | None = None) -> str:
        return f"http://127.0.0.1:{self.port() if port is None else port}/"

    def static_dirs(self) -> tuple[Path, ...]:
        """Where the page's files are looked for, in order: the app's own folder, then the library's."""
        return tuple(d for d in (self.STATIC, UI_STATIC) if d is not None)

    def page(self, handler) -> str:
        """The file "/" serves (INDEX; a subclass may pick by the request: a sign-in page, say)."""
        return self.INDEX

    # -- the routes ------------------------------------------------------------------------------
    def api(self, handler, method: str, path: str, query: dict, body: dict | None):
        """The app's routes: returns (status, data) or (status, data, headers) for an /api/ request,
        or raises Refused. `handler` is the request (its headers: a cookie, a session); `body` is the
        JSON object of a write (an empty dict for none), None for a GET. The frame answers
        /api/tray and /api/app; a subclass answers its own first and falls back to this."""
        if method == "GET" and path == "/api/tray":
            return 200, self.tray().as_dict()
        if method == "GET" and path == "/api/app":
            return 200, self.describe()
        if method == "GET" and path == "/api/plugins":
            return 200, {"plugins": self.plugins()}
        if method == "GET" and path == "/api/interface":
            return 200, self.interface()
        raise Refused(HTTPStatus.NOT_FOUND, "not found")

    # -- the server ------------------------------------------------------------------------------
    def start(self, port: int | None = None, open_browser: bool = False) -> str:
        """Serve in a thread of its own on `port` (the app's by default), or a free one when it is
        taken; returns the URL (`served`)."""
        port = self.port() if port is None else port
        if port and web.listening(port):  # a bind alone cannot tell on Windows (web.listening)
            self.log(f"{self.NAME}: port {port} is taken (another server answers there): a free one instead")
            port = 0
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(self))
        except OSError:
            httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self))
        self._httpd = httpd
        self.served = f"http://127.0.0.1:{httpd.server_address[1]}/"
        self._thread = threading.Thread(target=httpd.serve_forever, name=f"{self.NAME}-server", daemon=True)
        self._thread.start()
        self.log(f"{self.NAME}: its page at {self.served}")
        if open_browser:
            threading.Timer(0.5, webbrowser.open, args=(self.served,)).start()
        return self.served

    def stop(self) -> None:
        """End the server (the page stops answering)."""
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None

    def serve(self, port: int | None = None, open_browser: bool = False, stop: threading.Event | None = None,
              until_quiet: float | None = None) -> str:
        """`start`, then wait until `stop` is set, Ctrl+C, or (`until_quiet`, seconds) no request has
        come for that long (an app started by another app's front for itself ends again on its own);
        the server ends; returns the URL served."""
        url = self.start(port, open_browser)
        stop = stop or threading.Event()
        self.touch()
        try:
            while not stop.wait(0.5):
                if until_quiet is not None and time.monotonic() - self.touched > until_quiet:
                    self.log(f"{self.NAME}: no page has been in touch for {until_quiet:g} s: the server ends")
                    break
        except KeyboardInterrupt:
            pass
        finally:
            stop.set()
            self.stop()
        return url


def make_handler(app: App):
    """The requests of an app's page: the files from its folders, /api/ answered by `app.api`, only
    from this machine and the app's own pages; every write with the write header, a JSON object of
    at most MAX_BODY."""

    class Handler(BaseHTTPRequestHandler):
        server_version = f"liminal-{app.NAME}"

        def log_message(self, fmt, *args):  # keep the console quiet
            pass

        def do_GET(self) -> None:
            app.touch()
            if not web.local_only(self):
                return
            parts = urlsplit(self.path)
            if parts.path.startswith("/api/"):
                if web.from_this_app(self):
                    self._answer("GET", parts, None)
                return
            rel = app.page(self) if parts.path in ("", "/") else parts.path.lstrip("/")
            web.static_file(self, rel, app.static_dirs())

        def _write(self, method: str) -> None:
            app.touch()
            if not web.local_only(self) or not web.from_this_app(self):
                return
            if self.headers.get(web.WRITE_HEADER) != "1":
                return web.send(self, HTTPStatus.FORBIDDEN, b"missing request header", "text/plain")
            parts = urlsplit(self.path)
            if not parts.path.startswith("/api/"):
                return web.send(self, HTTPStatus.NOT_FOUND, b"not found", "text/plain")
            raw = self.headers.get("Content-Length") or "0"
            length = int(raw) if raw.isdecimal() else -1
            if length < 0:
                return web.send_json(self, {"error": "bad Content-Length"}, HTTPStatus.BAD_REQUEST)
            if length > web.MAX_BODY:
                return web.send_json(self, {"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            data = self.rfile.read(length) if length else b""
            body: dict = {}
            if data:
                try:
                    body = json.loads(data)
                except ValueError:
                    return web.send_json(self, {"error": "the body is not JSON"}, HTTPStatus.BAD_REQUEST)
                if not isinstance(body, dict):
                    return web.send_json(self, {"error": "the body must be a JSON object"}, HTTPStatus.BAD_REQUEST)
            self._answer(method, parts, body)

        def do_POST(self) -> None:
            self._write("POST")

        def do_PUT(self) -> None:
            self._write("PUT")

        def do_DELETE(self) -> None:
            self._write("DELETE")

        def _answer(self, method: str, parts, body: dict | None) -> None:
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            try:
                out = app.api(self, method, parts.path, query, body)
            except Refused as e:
                return web.send_json(self, {"error": e.reason}, e.status)
            except Exception as e:  # noqa: BLE001 - the app's fault, logged there; the page gets no detail
                app.log(f"{app.NAME}: {method} {parts.path} failed: {type(e).__name__}: {e}")
                return web.send_json(self, {"error": f"{app.NAME} could not answer; see its log"},
                                     HTTPStatus.INTERNAL_SERVER_ERROR)
            status, data, *rest = out
            web.send_json(self, data, status, rest[0] if rest else None)

    return Handler
