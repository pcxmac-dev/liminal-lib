"""Outgoing mail of the Liminal apps (the user, 2026-11-05: "send an email out, to an email server with
correct credentials so that its not blocked, like proton or gmail in order to obtain a passkey to
create the account"; "this operation can be part lib, and part login app"): the sending is here, one
authenticated SMTP account of yours, Gmail's or Proton's, read from your user environment variables,
never from a file of ours; what is sent (the login app's passkey) is each app's own:

    LIMINAL_SMTP_HOST      smtp.gmail.com; smtp.protonmail.ch (Proton Business, an SMTP token);
                           127.0.0.1 for Proton Bridge (a Proton account on this machine)
    LIMINAL_SMTP_PORT      587 (STARTTLS, the default); 465 (SSL); Proton Bridge's 1025
    LIMINAL_SMTP_SECURITY  starttls (default), ssl, or none (Proton Bridge on this machine only)
    LIMINAL_SMTP_USER      the account (Gmail: the address; its app password below)
    LIMINAL_SMTP_PASSWORD  Gmail: an app password (2-step verification on); Proton: the SMTP token
                           or the Bridge's password
    LIMINAL_SMTP_FROM      the From address (LIMINAL_SMTP_USER unless said); a display name is fine:
                           Liminal <you@example.com>

A message goes out through that account alone, so the receiving server sees a sender it knows
(SPF, DKIM: the provider's), not a machine of ours. The password never reaches a log or an error."""
from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import parseaddr

HOST_VAR, PORT_VAR, SECURITY_VAR = "LIMINAL_SMTP_HOST", "LIMINAL_SMTP_PORT", "LIMINAL_SMTP_SECURITY"
USER_VAR, PASSWORD_VAR, FROM_VAR = "LIMINAL_SMTP_USER", "LIMINAL_SMTP_PASSWORD", "LIMINAL_SMTP_FROM"
VARS = (HOST_VAR, PORT_VAR, SECURITY_VAR, USER_VAR, PASSWORD_VAR, FROM_VAR)
SECURITIES = ("starttls", "ssl", "none")
TIMEOUT_S = 30


class MailError(Exception):
    """The message could not be sent: what went wrong, without the credentials."""


@dataclass
class MailSettings:
    host: str = ""
    port: int = 587
    security: str = "starttls"
    user: str = ""
    password: str = field(default="", repr=False)
    sender: str = ""

    @classmethod
    def from_env(cls, env=None) -> "MailSettings":
        env = os.environ if env is None else env
        raw_port = (env.get(PORT_VAR) or "").strip()
        security = (env.get(SECURITY_VAR) or "starttls").strip().lower()
        return cls(host=(env.get(HOST_VAR) or "").strip(), port=int(raw_port) if raw_port.isdecimal() else 587,
                   security=security if security in SECURITIES else "starttls",
                   user=(env.get(USER_VAR) or "").strip(), password=env.get(PASSWORD_VAR) or "",
                   sender=(env.get(FROM_VAR) or env.get(USER_VAR) or "").strip())

    @property
    def configured(self) -> bool:
        """A host and a sender; a user with a password, or none (Proton Bridge on this machine)."""
        return bool(self.host and self.sender and (bool(self.user) == bool(self.password)))

    def describe(self) -> dict:
        """What the page may know: the provider and the sender, never the password."""
        return {"configured": self.configured, "host": self.host, "port": self.port, "security": self.security,
                "sender": parseaddr(self.sender)[1] or self.sender, "user": self.user}

    def problems(self) -> list[str]:
        out = []
        if not self.host:
            out.append(f"{HOST_VAR} is not set (smtp.gmail.com, smtp.protonmail.ch, or 127.0.0.1 for Proton Bridge)")
        if not self.sender:
            out.append(f"{FROM_VAR} or {USER_VAR} is not set (the address the message comes from)")
        if bool(self.user) != bool(self.password):
            out.append(f"{USER_VAR} and {PASSWORD_VAR} go together (an app password for Gmail, an SMTP token for Proton)")
        return out


class Mailer:
    """Sends through the account's server. `smtp` and `smtp_ssl` are the transports (smtplib's),
    replaceable by a test's stand-ins."""

    def __init__(self, settings: MailSettings | None = None, log=None, smtp=None, smtp_ssl=None):
        self.settings = settings or MailSettings.from_env()
        self.log = log or (lambda m: None)
        self._smtp = smtp or smtplib.SMTP
        self._smtp_ssl = smtp_ssl or smtplib.SMTP_SSL

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def message(self, to: str, subject: str, text: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.settings.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(text)
        return msg

    def send(self, to: str, subject: str, text: str) -> None:
        """One message to `to`; MailError when the account is not set up or the server refuses."""
        s = self.settings
        if not s.configured:
            raise MailError("email sending is not set up: " + "; ".join(s.problems()))
        if not parseaddr(to)[1] or "@" not in parseaddr(to)[1]:
            raise MailError("the address to send to is not an email address")
        msg = self.message(to, subject, text)
        context = ssl.create_default_context()
        try:
            if s.security == "ssl":
                server = self._smtp_ssl(s.host, s.port, timeout=TIMEOUT_S, context=context)
            else:
                server = self._smtp(s.host, s.port, timeout=TIMEOUT_S)
            try:
                server.ehlo()
                if s.security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                if s.user:
                    server.login(s.user, s.password)
                server.send_message(msg)
            finally:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001 - the message went; a quit refused is nothing
                    pass
        except smtplib.SMTPAuthenticationError as e:
            raise MailError(f"the mail server refused the credentials ({s.host}: {_scrub(e, s.password)}); "
                            "Gmail wants an app password, Proton an SMTP token or the Bridge's password") from None
        except (smtplib.SMTPException, OSError) as e:
            raise MailError(f"the mail server at {s.host}:{s.port} did not take the message: {_scrub(e, s.password)}") from None
        self.log(f"login: a message went to {_mask(to)} through {s.host} ({subject})")


def _scrub(e: BaseException, *secrets: str) -> str:
    text = f"{type(e).__name__}: {e}"
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


def _mask(address: str) -> str:
    """An address for the page and the log: a***@example.com."""
    local, _, domain = parseaddr(address)[1].partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


mask = _mask
