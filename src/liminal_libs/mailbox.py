"""A mailbox read over IMAP, for the login's own checks (the user, 2026-11-06: the admin account "could be
used to test the server so that it can send and receive with the user email"): whether a message the login
sent itself has arrived. The settings come from the login's file ([imap]) or LIMINAL_IMAP_*; the password
from the secret store when it is in neither. `imap` and `imap_ssl` are imaplib's classes, a test's stand-ins
when given."""
from __future__ import annotations

import imaplib
import os
import ssl
from dataclasses import dataclass, field

HOST_VAR, PORT_VAR, SECURITY_VAR, USER_VAR, PASSWORD_VAR = ("LIMINAL_IMAP_HOST", "LIMINAL_IMAP_PORT", "LIMINAL_IMAP_SECURITY",
                                                           "LIMINAL_IMAP_USER", "LIMINAL_IMAP_PASSWORD")
VARS = (HOST_VAR, PORT_VAR, SECURITY_VAR, USER_VAR, PASSWORD_VAR)
LOOPBACK = ("127.0.0.1", "localhost", "::1")
TIMEOUT_S = 30


class MailboxError(Exception):
    pass


@dataclass
class MailboxSettings:
    host: str = ""
    port: int = 993
    security: str = "ssl"  # ssl | starttls | none
    user: str = ""
    password: str = field(default="", repr=False)

    @classmethod
    def from_env(cls, env=None, cfg=None) -> "MailboxSettings":
        """From `env`, else the process's environment over a login's own settings (`cfg`, its file's [imap])
        over the user's saved variables, the password from the secret store when in none of them."""
        if env is None:
            env = {**_saved(), **{k: str(v) for k, v in (cfg or {}).items() if k in VARS},
                   **{k: v for k, v in os.environ.items() if k in VARS}}
            if not env.get(PASSWORD_VAR):
                secret = _secret(PASSWORD_VAR)
                if secret:
                    env[PASSWORD_VAR] = secret
        raw_port = (env.get(PORT_VAR) or "").strip()
        security = (env.get(SECURITY_VAR) or "ssl").strip().lower()
        return cls(host=(env.get(HOST_VAR) or "").strip(), port=int(raw_port) if raw_port.isdecimal() else 993,
                   security=security if security in ("ssl", "starttls", "none") else "ssl",
                   user=(env.get(USER_VAR) or "").strip(), password=env.get(PASSWORD_VAR) or "")

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password)

    def describe(self) -> dict:
        return {"configured": self.configured, "host": self.host, "port": self.port, "security": self.security, "user": self.user}

    def problems(self) -> list[str]:
        out = []
        if not self.host:
            out.append(f"{HOST_VAR} is not set (the server the admin mailbox is read from)")
        if not self.user or not self.password:
            out.append(f"{USER_VAR} and {PASSWORD_VAR} are needed (the admin mailbox and its password)")
        return out


class Mailbox:
    """The admin mailbox: `find(text)` says whether a message whose subject holds `text` is in the inbox."""

    def __init__(self, settings: MailboxSettings | None = None, log=None, imap=None, imap_ssl=None, cfg=None):
        self._settings = settings
        self._cfg = dict(cfg or {})
        self.log = log or (lambda m: None)
        self._imap = imap or imaplib.IMAP4
        self._imap_ssl = imap_ssl or imaplib.IMAP4_SSL

    @property
    def settings(self) -> MailboxSettings:
        return self._settings if self._settings is not None else MailboxSettings.from_env(cfg=self._cfg)

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def find(self, text: str, folder: str = "INBOX") -> int:
        """How many messages in `folder` have `text` in their subject (0: none yet). MailboxError when the
        mailbox is not set up, not reached or the password refused."""
        s = self.settings
        if not s.configured:
            raise MailboxError("the admin mailbox is not set up: " + "; ".join(s.problems()))
        context = ssl.create_default_context()
        if s.host.strip().lower() in LOOPBACK:  # a server on this machine: its own certificate
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        try:
            if s.security == "ssl":
                box = self._imap_ssl(s.host, s.port, timeout=TIMEOUT_S, ssl_context=context)
            else:
                box = self._imap(s.host, s.port, timeout=TIMEOUT_S)
                if s.security == "starttls":
                    box.starttls(ssl_context=context)
            try:
                box.login(s.user, s.password)
                box.select(folder, readonly=True)
                status, data = box.search(None, "SUBJECT", f'"{text}"')
                if status != "OK":
                    raise MailboxError(f"the mailbox at {s.host} did not answer the search: {status}")
                ids = (data[0] or b"").split() if data else []
                return len(ids)
            finally:
                try:
                    box.logout()
                except Exception:  # noqa: BLE001 - the answer is in hand
                    pass
        except imaplib.IMAP4.error as e:
            raise MailboxError(f"the mailbox at {s.host}:{s.port} refused: {_scrub(e, s.password)}") from None
        except OSError as e:
            raise MailboxError(f"the mailbox at {s.host}:{s.port} could not be reached: {_scrub(e, s.password)}") from None


def _scrub(e: BaseException, *secrets: str) -> str:
    text = f"{type(e).__name__}: {e}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _saved() -> dict[str, str]:
    try:
        from .config import UserEnvironment

        return UserEnvironment(names=VARS).saved()
    except Exception:  # noqa: BLE001 - nothing saved to read
        return {}


def _secret(name: str) -> str | None:
    try:
        from . import secrets

        return secrets.get(name)
    except Exception:  # noqa: BLE001 - no store, or one that cannot be read
        return None
