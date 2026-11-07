"""The apps bound together (the user, 2026-11-04: "objects that I can bind together, like the login
app and the integrator app ... I can still link to an existing integrator and get its functions tied
into the chart app ... seamlessly like it is local"; "the library basically facilitates the
interprocess communication, the way the apps can bind to each other"): what a super app does with
another app's URL. Reach it (`Binding.reachable`), start it when nothing answers (`start_app`: the
app's module run as `serve --no-browser --port N --until-quiet`, detached and windowless, so it ends
again once no page has been in touch for a while), read its tray (`tray_of`), and pass its pages'
requests on (`forward`: the super app's front proxies `/<name>/...` to the bound app's `/...`, the
answers and the cookies back, a cookie's path narrowed to the prefix). The charts app's front did
this for the integrator by hand since 2026-11-03 (gui/front.py); from liminal-libs 1.9.0 it does it
through here, for the integrator and the login app alike."""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from urllib.parse import urlsplit

from .. import web
from .login import SESSION_HEADER

# The request headers passed on to a bound app: who the page is (the charts profile cookie and
# header, the login session), that it is the app's own page (the write header), what it accepts. Not
# where it comes from (Origin, Sec-Fetch-Site: the front checked them; the bound app takes a request
# saying neither)
FORWARDED = ("Cookie", "Content-Type", "Accept", "Last-Event-ID", web.WRITE_HEADER, web.PROFILE_HEADER, SESSION_HEADER)
PASSED_BACK = ("Content-Type", "Cache-Control", "X-Content-Type-Options")
START_SECONDS = 30.0  # how long a front waits for an app it started to answer


@dataclass(frozen=True)
class Binding:
    """A bound app: its name (the prefix the super app serves it under: `/login/...`), its URL, and
    the module to start when nothing answers there ("" for one never started here)."""

    name: str
    url: str
    module: str = ""

    @property
    def port(self) -> int:
        return urlsplit(self.url).port or 80

    @property
    def prefix(self) -> str:
        return f"/{self.name}"

    def reachable(self) -> bool:
        return web.listening(self.port)

    def tray(self) -> dict | None:
        return tray_of(self.url)


def tray_of(url: str, timeout: float = 5.0) -> dict | None:
    """A bound app's tray (`GET /api/tray`), read by the front itself (no page's origin travels);
    None when nothing answers or the answer is not a tray."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(urllib.request.Request(url.rstrip("/") + "/api/tray", headers={"Accept": "application/json"}),
                         timeout=timeout) as resp:
            tray = json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return tray if isinstance(tray, dict) and "items" in tray else None


def start_app(module: str, port: int, env: dict | None = None, log=print, until_quiet: bool = True) -> bool:
    """Start an app's server for a front (`python -m <module> serve --no-browser --port PORT
    [--until-quiet]`): no window, outliving this process, with `env` added to its environment; it
    ends again once no page has been in touch for a while. False: the module is not installed."""
    try:
        __import__(module)
    except ImportError:
        log(f"{module} is not installed: nothing to start at port {port}")
        return False
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe") if os.name == "nt" and exe.with_name("pythonw.exe").exists() else exe
    flags = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0  # DETACHED | NEW_GROUP | NO_WINDOW
    args = [str(pythonw), "-m", module, "serve", "--no-browser", "--port", str(port)] + (["--until-quiet"] if until_quiet else [])
    subprocess.Popen(args, creationflags=flags, close_fds=True, env={**os.environ, **(env or {})},
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log(f"{module} started at port {port}")
    return True


def wait_for(port: int, seconds: float = START_SECONDS, step: float = 0.25) -> bool:
    """Whether something answers at `port` within `seconds`."""
    end = time.monotonic() + seconds
    while True:
        if web.listening(port):
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(step)


def forward(handler, method: str, url: str, path: str, *, cookie_path: str | None = None,
            unavailable: str = "the app does not answer; ask again shortly") -> None:
    """The request passed on to the bound app at `url` as `path` (its body read here, within
    MAX_BODY), its answer passed back as it comes (an event stream chunk by chunk), its cookies too
    (their path narrowed to `cookie_path`, the prefix the super app serves the bound app under, so a
    bound app's cookie stays its own)."""
    raw_length = handler.headers.get("Content-Length") or "0"
    length = int(raw_length) if raw_length.isdecimal() else -1
    if length < 0:
        return web.send_json(handler, {"error": "bad Content-Length"}, HTTPStatus.BAD_REQUEST)
    if length > web.MAX_BODY:
        return web.send_json(handler, {"error": "request too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
    body = handler.rfile.read(length) if length else None
    target = urlsplit(url)
    headers = {name: value for name in FORWARDED if (value := handler.headers.get(name)) is not None}
    headers["Host"] = target.netloc
    conn = http.client.HTTPConnection(target.hostname, target.port, timeout=None)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
    except OSError:
        conn.close()
        return web.send_json(handler, {"error": unavailable}, HTTPStatus.SERVICE_UNAVAILABLE)
    try:
        ctype = resp.getheader("Content-Type") or "application/octet-stream"
        passed = [(name, value) for name in PASSED_BACK if (value := resp.getheader(name)) is not None]
        passed += [("Set-Cookie", _repath(c, cookie_path)) for c in resp.msg.get_all("Set-Cookie") or []]
        if ctype.startswith("text/event-stream"):
            handler.send_response(resp.status)
            for name, value in passed:
                handler.send_header(name, value)
            handler.end_headers()
            try:
                while chunk := resp.read1(65536):
                    handler.wfile.write(chunk)
                    handler.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the page went away
            return
        data = resp.read()
        web.send(handler, resp.status, data, ctype, passed)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass  # the page went away
    finally:
        conn.close()


def _repath(cookie: str, prefix: str | None) -> str:
    """A bound app's cookie with its path under the prefix it is served at (`Path=/` -> `Path=/login`)."""
    if not prefix:
        return cookie
    parts = [p.strip() for p in cookie.split(";")]
    out = [p for p in parts if not p.lower().startswith("path=")]
    out.insert(1, f"Path={prefix}")
    return "; ".join(out)
