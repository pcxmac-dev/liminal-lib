"""The login app's client: what any program or plugin on this machine asks the running login app
(liminal-login at its port) over HTTP, with the session it was given at sign-in.

The user, 2026-11-03: "the authorization app, liminal-login, is kind of the root authorization for
all plugins and software integration, it provides the plugins with all the key and account
infrastructure"; "each plugin, and software component needs to inherit the liminal-login 'class'
which allows the plugin IPC with the actual login app, which stores keys and account info, the keys
or account info, is stored according to key-vector pair which interrogates the current logged in
user, for a particular account, like claude-account-name or claude-account-api-key (key name) it's
return vector would be the api key or account name or account password, that only the liminal-login
instance would be able to furnish". So: a pair is named `<plugin>-account-api-key`,
`<plugin>-account-name` or `<plugin>-account-password` (`key`, `account`, `password` here) or
anything else by name (`vector`); only the login app can open a user's store, and a client holds a
session token, never a key of the store. The transport is HTTP on 127.0.0.1 (the login app's page
and API are one server); the proxy pattern over the hosts' pipe comes with the apps' binding."""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from ..web import WRITE_HEADER

URL_VAR = "LIMINAL_LOGIN_URL"
DEFAULT_URL = "http://127.0.0.1:8795"
SESSION_HEADER = "X-Liminal-Session"  # the session a client got at sign-in, on every request after


class LoginRefused(Exception):
    """The login app refused: not signed in, a wrong name or password, no such pair, a name it
    does not take (its status, 401/403/404/400, and reason)."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


class LoginUnreachable(Exception):
    """No login app answers at the URL."""


class LoginClient:
    """A client of the login app: `sign_in` keeps the session token; `key`, `account`, `password`
    and `vector` ask for the signed-in user's pairs; `put` and `remove` change them. A program made
    by another (a plugin's host, a worker) is given the token (`token=`), never the password."""

    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 10.0):
        self.url = (url or os.environ.get(URL_VAR) or DEFAULT_URL).rstrip("/")
        self.token = token
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if method != "GET":
            headers[WRITE_HEADER] = "1"
        if self.token:
            headers[SESSION_HEADER] = self.token
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                reason = json.loads(raw).get("error") or e.reason
            except (ValueError, AttributeError):
                reason = e.reason
            raise LoginRefused(e.code, str(reason)) from None
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise LoginUnreachable(f"no login app answers at {self.url}: {e}") from None
        return json.loads(raw) if raw else {}

    @property
    def signed_in(self) -> bool:
        return self.token is not None

    def status(self) -> dict:
        """Whether the login app has users yet and whether this session is signed in (`GET /api/status`)."""
        return self._call("GET", "/api/status")

    def sign_in(self, name: str, password: str) -> dict:
        """Sign in as `name`; the session token is kept for the calls after. Returns the user."""
        out = self._call("POST", "/api/sign-in", {"name": name, "password": password})
        self.token = out.get("token")
        return out.get("user", {})

    def sign_out(self) -> None:
        try:
            if self.token:
                self._call("POST", "/api/sign-out")
        finally:
            self.token = None

    def user(self) -> dict:
        """The signed-in user (`GET /api/me`): name, created, seen, the store's fingerprints."""
        return self._call("GET", "/api/me")

    def pairs(self) -> list[dict]:
        """The user's pairs by name, kind and label, never their values."""
        return self._call("GET", "/api/pairs").get("pairs", [])

    def vector(self, name: str) -> str:
        """The value of the pair `name` (the key, the account name, the password: what only the
        login app can furnish)."""
        return self._call("GET", f"/api/pairs/{urllib.parse.quote(name, safe='')}")["value"]

    def put(self, name: str, value: str, kind: str = "key", label: str = "") -> dict:
        return self._call("PUT", f"/api/pairs/{urllib.parse.quote(name, safe='')}",
                          {"value": value, "kind": kind, "label": label})

    def remove(self, name: str) -> dict:
        return self._call("DELETE", f"/api/pairs/{urllib.parse.quote(name, safe='')}")

    def key(self, plugin: str) -> str:
        return self.vector(f"{plugin}-account-api-key")

    def account(self, plugin: str) -> str:
        return self.vector(f"{plugin}-account-name")

    def password(self, plugin: str) -> str:
        return self.vector(f"{plugin}-account-password")
