"""The one authorization interface (the user, 2026-11-06: "the app plugins need to use the Abstract
Base Class login as the only way to present an interface for IPC, between plugins and login and the
app as far as AUTHORIZATION"; the login instance of an app "talks to the respective app plugins" with
"its associated plugin requirements like api keys or passwords or what ever").

`Login` is an abstract base class: every plugin and every app holds a realization of it (`Plugin.login`,
`App.login`, set when the plugin is attached by its app), and asks it, and nothing else, for a key, an
account or a password: `login.key("massive")` is the pair `massive-account-api-key` of the signed-in
user of the app's namespace. The realizations: `LoginClient` (apps/login.py), over HTTP to the login
app of that namespace (liminal-login), the proxy over the hosts' pipe to come with the resident
plugins; `EnvironmentLogin` here, reading the user environment variables that are the keys' store
until every plugin is attached through an app (the transition); a test's stand-in. A refusal is
`Unauthorized`."""
from __future__ import annotations

import getpass
import os
from abc import ABC, abstractmethod
from collections.abc import Mapping


class Unauthorized(Exception):
    """The login refuses: nobody signed in, no such pair, not allowed."""

    def __init__(self, reason: str, status: int = 401):
        super().__init__(reason)
        self.reason, self.status = reason, status


class Login(ABC):
    """What a plugin or an app may ask of the login of its app's namespace. `vector` and `user` are
    the realization's; `key`, `account` and `password` name the pairs by their convention
    (`<plugin>-account-api-key`, `-account-name`, `-account-password`); `may` is the authorization
    policy (everything the signed-in user has, until a policy says less)."""

    NAMESPACE = ""  # the app whose users it serves: charts, integrator (set by the realization)

    @abstractmethod
    def user(self) -> dict:
        """Who is signed in: at least `name`; `email` and `verified` when known. Unauthorized when nobody."""

    @abstractmethod
    def vector(self, name: str) -> str:
        """The value of the pair `name` for the signed-in user. Unauthorized when there is none, or nobody."""

    def key(self, plugin: str) -> str:
        return self.vector(f"{plugin}-account-api-key")

    def account(self, plugin: str) -> str:
        return self.vector(f"{plugin}-account-name")

    def password(self, plugin: str) -> str:
        return self.vector(f"{plugin}-account-password")

    def has(self, name: str) -> bool:
        """Whether the pair `name` can be furnished now (signed in, and the pair is there)."""
        try:
            self.vector(name)
        except Unauthorized:
            return False
        return True

    def may(self, plugin: str, action: str) -> bool:
        """Whether `plugin` may do `action` for the signed-in user (fetch, trade, evaluate ...): a
        policy for the login's realm to refine; everything the user has, by default."""
        return self.signed_in

    @property
    def signed_in(self) -> bool:
        try:
            self.user()
        except Unauthorized:
            return False
        return True

    def describe(self) -> dict:
        """What a page or a plugin's view may show: the namespace and whether anyone is signed in."""
        return {"namespace": self.NAMESPACE, "signedIn": self.signed_in, "kind": type(self).__name__}


class EnvironmentLogin(Login):
    """The keys' store until every plugin is attached through an app: the user environment variables
    (the machine's user is the signed-in user). `pairs` maps a pair's name to its variable
    (`massive-account-api-key` -> MASSIVE_API_KEY), built by the app from each plugin's `keys`; a
    name not mapped is read as a variable named after it (MASSIVE_ACCOUNT_API_KEY)."""

    NAMESPACE = "environment"

    def __init__(self, pairs: Mapping[str, str] | None = None, env: Mapping[str, str] | None = None):
        self.pairs = dict(pairs or {})
        self.env = env  # None: the process's environment, read when asked

    def user(self) -> dict:
        return {"name": getpass.getuser(), "email": None, "verified": False}

    def vector(self, name: str) -> str:
        env = os.environ if self.env is None else self.env
        variable = self.pairs.get(name) or name.upper().replace("-", "_").replace(".", "_")
        value = env.get(variable)
        if not value:
            raise Unauthorized(f"no {name} here ({variable} is not set)", 404)
        return value
