"""What every app's server shares (the user, 2026-11-01: "make sure that the library calls
contribute to code reuse"; 2026-11-04: "the library basically facilitates the interprocess
communication, the way the apps can bind to each other, or interoperate"): the chart front, the
integrator's page and data API, the login app and every app to come answer only requests addressed
to this machine from their own pages, serve their page's files from their own folder and the
library's (ui.STATIC: the icons, the theme), and send bodies the same way. Moved here from the
charts app's gui/web.py (liminal-libs 1.7.0); the apps' frame on top of it is apps/app.py.

Nothing here opens a database."""
from __future__ import annotations

import json
import mimetypes
import socket
from http import HTTPStatus
from pathlib import Path

MAX_BODY = 5 * 1024 * 1024
WRITE_HEADER = "X-Liminal-Charts"  # every write carries it: no other site's page can send it without a preflight
PROFILE_COOKIE = "mc_profile"  # the chart app's profile this browser signed in as (profiles keep things apart)
# The profile a page loaded as, sent with its writes: a tab left open as one profile while another
# tab signed in as a second (or out) wrote its lists over the second's (the cookie names the newest)
PROFILE_HEADER = "X-Liminal-Profile"
LOCAL_HOSTS = ("127.0.0.1", "localhost")
ALWAYS = (("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"))


def listening(port: int) -> bool:
    """Whether something answers on 127.0.0.1:`port` already. A server's own bind cannot tell on
    Windows: HTTPServer binds with SO_REUSEADDR, which there lets a second server take a port in
    use (the fetcher's page took the integrator's front's port and the front proxied to itself)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
        except OSError:
            return False
    return True


def send(handler, status: int, body: bytes, ctype: str, headers=None) -> None:
    """An answer with `body`: its type and length, no caching and no sniffing unless `headers`
    (pairs, or a dict) say otherwise, and whatever else they carry (a cookie, say)."""
    given = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
    names = {name.lower() for name, _ in given}
    handler.send_response(status)
    handler.send_header("Content-Type", ctype)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in ALWAYS:
        if name.lower() not in names:
            handler.send_header(name, value)
    for name, value in given:
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)


def send_json(handler, data, status: int = 200, headers=None) -> None:
    send(handler, status, json.dumps(data, allow_nan=False).encode(), "application/json", headers)


def local_only(handler) -> bool:
    """Only requests addressed to this machine are answered (blocks DNS rebinding from web pages)."""
    host = (handler.headers.get("Host") or "").split(":")[0]
    if host not in LOCAL_HOSTS:
        send(handler, HTTPStatus.FORBIDDEN, b"forbidden", "text/plain")
        return False
    return True


def from_this_app(handler) -> bool:
    """Whether an /api/ request comes from this app's own pages (or the address bar, or a program on
    this machine). A browser says where a request comes from (Sec-Fetch-Site) and, for most, the
    page's origin (Origin); a GET needs no header, so another site's page could otherwise reach the
    routes that start work or write (an <img> asking /api/inventory for one ticker after another,
    each a thread; /api/trading filling paper orders). A request saying neither (not from a browser,
    or one too old to say) passes: writes still need WRITE_HEADER, which no other page can send."""
    site = handler.headers.get("Sec-Fetch-Site")
    origin = handler.headers.get("Origin")
    if (site is None or site in ("same-origin", "none")) \
            and (origin is None or origin == f"http://{handler.headers.get('Host')}"):
        return True
    send(handler, HTTPStatus.FORBIDDEN, b"forbidden: not from this app's pages", "text/plain")
    return False


def cookie(handler, name: str) -> str | None:
    """The value of the request's cookie `name`, or None."""
    for part in (handler.headers.get("Cookie") or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key == name:
            return value
    return None


def profile_cookie(handler) -> int | None:
    """The chart app's profile this browser signed in as, by its cookie: decimal digits only, of a
    size SQLite holds ('²' passed isdigit and failed int). Whether it still exists is the data API's
    to check."""
    value = cookie(handler, PROFILE_COOKIE)
    if value is not None and value.isdecimal() and len(value) <= 9:
        return int(value)
    return None


def static_file(handler, rel: str, dirs) -> None:
    """One of a page's files (index.html, a script, a stylesheet, a vendored library) from the first
    of `dirs` holding it (an app's own folder first, then the library's ui.STATIC: icons.js,
    liminal.css), never a file outside them."""
    for folder in dirs:
        if folder is None:
            continue
        root = Path(folder).resolve()
        target = (root / rel).resolve()
        if root not in target.parents or not target.is_file():
            continue
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype == "application/javascript":
            ctype += "; charset=utf-8"
        return send(handler, HTTPStatus.OK, target.read_bytes(), ctype)
    send(handler, HTTPStatus.NOT_FOUND, b"not found", "text/plain")
