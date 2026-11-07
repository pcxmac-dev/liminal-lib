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
import re
import smtplib
import socket
import ssl
import subprocess
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parseaddr

HOST_VAR, PORT_VAR, SECURITY_VAR = "LIMINAL_SMTP_HOST", "LIMINAL_SMTP_PORT", "LIMINAL_SMTP_SECURITY"
USER_VAR, PASSWORD_VAR, FROM_VAR = "LIMINAL_SMTP_USER", "LIMINAL_SMTP_PASSWORD", "LIMINAL_SMTP_FROM"
VARS = (HOST_VAR, PORT_VAR, SECURITY_VAR, USER_VAR, PASSWORD_VAR, FROM_VAR)
SECURITIES = ("starttls", "ssl", "none")
HELO_VAR = "LIMINAL_SMTP_HELO"  # what this machine calls itself to a mail server (the local domain, yours to scope)
DIRECT = "direct"  # LIMINAL_SMTP_HOST=direct: straight to each recipient's own mail exchangers, no account
DIRECT_PORT = 25
DKIM_KEY_VAR, DKIM_SELECTOR_VAR, DKIM_DOMAIN_VAR = "LIMINAL_SMTP_DKIM_KEY", "LIMINAL_SMTP_DKIM_SELECTOR", "LIMINAL_SMTP_DKIM_DOMAIN"
ALL_VARS = (*VARS, HELO_VAR, DKIM_KEY_VAR, DKIM_SELECTOR_VAR, DKIM_DOMAIN_VAR)
LOOPBACK = ("127.0.0.1", "localhost", "::1")  # a server on this machine: Proton Bridge
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
    helo: str = ""
    dkim_key: str = ""  # the private key's file (dkim.py): the message signed for the user's domain
    dkim_selector: str = ""
    dkim_domain: str = ""  # the sender's domain unless said

    @classmethod
    def from_env(cls, env=None, cfg=None) -> "MailSettings":
        """The settings from `env`, or from the process's environment over the user's saved variables (the
        Windows registry's user Environment key, read now: `liminal-login mail setup` while the app runs
        counts), the password from the secret store (secrets.py: Windows' Credential Manager) when it is
        in neither; `cfg`, a login's own settings (apps.scope: the file's [mail] section), between the saved
        variables and the environment."""
        if env is None:
            env = {**_saved(), **{k: str(v) for k, v in (cfg or {}).items() if k in ALL_VARS},
                   **{k: v for k, v in os.environ.items() if k in ALL_VARS}}
            if not env.get(PASSWORD_VAR):
                secret = _secret(PASSWORD_VAR)
                if secret:
                    env[PASSWORD_VAR] = secret
        raw_port = (env.get(PORT_VAR) or "").strip()
        security = (env.get(SECURITY_VAR) or "starttls").strip().lower()
        return cls(host=(env.get(HOST_VAR) or "").strip(), port=int(raw_port) if raw_port.isdecimal() else 587,
                   security=security if security in SECURITIES else "starttls",
                   user=(env.get(USER_VAR) or "").strip(), password=env.get(PASSWORD_VAR) or "",
                   sender=(env.get(FROM_VAR) or env.get(USER_VAR) or "").strip(), helo=(env.get(HELO_VAR) or "").strip(),
                   dkim_key=(env.get(DKIM_KEY_VAR) or "").strip(), dkim_selector=(env.get(DKIM_SELECTOR_VAR) or "").strip().lower(),
                   dkim_domain=(env.get(DKIM_DOMAIN_VAR) or "").strip().lower())

    @property
    def direct(self) -> bool:
        """Straight to each recipient's own mail exchangers (LIMINAL_SMTP_HOST=direct; the user, 2026-11-06:
        \"you can use a terminal to send email if to the correct port with right protocol\"): port 25 with
        STARTTLS, no account and no password; the message comes from `sender`, yours."""
        return self.host.strip().lower() == DIRECT

    @property
    def through_server(self) -> bool:
        """Whether messages go through a mail server of the user's (an account at a host, or a server on
        this machine), not straight to each address's own (the user, 2026-11-06: "only allow signup if the
        login app is tied to an smtp server")."""
        return self.configured and not self.direct

    @property
    def configured(self) -> bool:
        """Direct: a sender. Through an account: a host and a sender; a user with a password, or none
        (Proton Bridge on this machine)."""
        if self.direct:
            return bool(self.sender)
        return bool(self.host and self.sender and (bool(self.user) == bool(self.password)))

    @property
    def signing(self) -> bool:
        """Whether messages are signed for the user's domain (a key file and a selector set)."""
        return bool(self.dkim_key and self.dkim_selector)

    @property
    def signing_domain(self) -> str:
        """The domain signed for: LIMINAL_SMTP_DKIM_DOMAIN, else the sender's."""
        return self.dkim_domain or parseaddr(self.sender)[1].rpartition("@")[2].lower()

    @property
    def dkim_record(self) -> str:
        """The DNS name of the signing key's record (`<selector>._domainkey.<domain>`), or ""."""
        return f"{self.dkim_selector}._domainkey.{self.signing_domain}" if self.signing else ""

    def helo_name(self) -> str:
        """What this machine calls itself to a mail server: LIMINAL_SMTP_HELO, else its full name when it
        has one, else liminal.local (a mail exchanger wants a name with a dot in it)."""
        if self.helo:
            return self.helo
        name = socket.getfqdn().strip(".")
        return name if "." in name else "liminal.local"

    def describe(self) -> dict:
        """What the page may know: the provider (or direct) and the sender, never the password."""
        return {"configured": self.configured, "host": self.host, "port": DIRECT_PORT if self.direct else self.port,
                "security": self.security, "sender": parseaddr(self.sender)[1] or self.sender, "user": self.user,
                "direct": self.direct, "dkim": self.dkim_record, "throughServer": self.through_server}

    def problems(self) -> list[str]:
        out = []
        if self.direct:
            return [] if self.sender else [f"{FROM_VAR} is not set (the address the message comes from: yours)"]
        if not self.host:
            out.append(f"{HOST_VAR} is not set (127.0.0.1 for Proton Bridge on this machine, smtp.gmail.com, "
                       "smtp.protonmail.ch; direct: straight to each address's own mail server; `liminal-login mail setup`)")
        if not self.sender:
            out.append(f"{FROM_VAR} or {USER_VAR} is not set (the address the message comes from)")
        if bool(self.user) != bool(self.password):
            out.append(f"{USER_VAR} and {PASSWORD_VAR} go together (an app password for Gmail, an SMTP token for Proton)")
        return out


@dataclass(frozen=True)
class Sent:
    """The mail server's word that it took a message (the user, 2026-11-06: "a way to check the server
    response to see if it received the email"): its reply to the message's data, with the code (250: queued
    for delivery; what happens after is the server's), when, and the message id to find it by."""

    to: str
    host: str
    port: int
    code: int
    reply: str
    at: str
    message_id: str
    signed: str = ""  # the DKIM record the message is signed under, when it is

    def as_dict(self) -> dict:
        """For a page or a client: the address masked."""
        return {"to": _mask(self.to), "host": self.host, "port": self.port, "code": self.code, "reply": self.reply,
                "at": self.at, "messageId": self.message_id, "signed": self.signed}

    def __str__(self) -> str:
        return f"{self.host}:{self.port} answered {self.code} {self.reply}".rstrip()


class Mailer:
    """Sends through the account's server. `smtp` and `smtp_ssl` are the transports (smtplib's),
    replaceable by a test's stand-ins."""

    def __init__(self, settings: MailSettings | None = None, log=None, smtp=None, smtp_ssl=None, resolve=None, cfg=None):
        self._settings = settings
        self._cfg = dict(cfg or {})  # a login's own settings (its cfg's [mail]), read with the environment
        self.log = log or (lambda m: None)
        self._smtp = smtp or smtplib.SMTP
        self._smtp_ssl = smtp_ssl or smtplib.SMTP_SSL
        self._resolve = resolve or exchangers  # a domain's mail exchangers (direct delivery)

    @property
    def settings(self) -> MailSettings:
        """The settings given, or the user's as they are now (the environment, the saved variables, the
        secret store), read each time: an account set up while the app runs is used at once."""
        return self._settings if self._settings is not None else MailSettings.from_env(cfg=self._cfg)

    @settings.setter
    def settings(self, value: MailSettings | None) -> None:
        self._settings = value

    @property
    def configured(self) -> bool:
        return self.settings.configured

    def message(self, to: str, subject: str, text: str) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.settings.sender
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=parseaddr(self.settings.sender)[1].rpartition("@")[2] or "liminal.local")
        msg.set_content(text)
        s = self.settings
        if s.signing:  # signed for the user's domain (dkim.py): the DNS record at the domain proves it
            from . import dkim

            try:
                pem = Path(s.dkim_key).read_bytes()
            except OSError as e:
                raise MailError(f"the DKIM key file cannot be read ({s.dkim_key}: {e}); `liminal-login mail dkim init` makes one") from None
            msg["DKIM-Signature"] = dkim.sign(msg.as_bytes(), s.signing_domain, s.dkim_selector, pem)
        return msg

    def send(self, to: str, subject: str, text: str) -> "Sent":
        """One message to `to`: the server's word that it took it (Sent); MailError when the account is
        not set up, the server is not reached or it refuses the sender, the address or the message."""
        s = self.settings
        if not s.configured:
            raise MailError("email sending is not set up: " + "; ".join(s.problems()))
        if not parseaddr(to)[1] or "@" not in parseaddr(to)[1]:
            raise MailError("the address to send to is not an email address")
        msg = self.message(to, subject, text)
        sender = parseaddr(s.sender)[1] or s.sender
        recipient = parseaddr(to)[1]
        context = _context("" if s.direct else s.host)
        helo = s.helo_name()
        if s.direct:  # the recipient's own exchangers by preference, the first reached takes it
            hosts, port = self._resolve(recipient.rpartition("@")[2]), DIRECT_PORT
            if not hosts:
                raise MailError(f"no mail server is known for {_mask(to)}")
        else:
            hosts, port = [s.host], s.port
        tried: list[str] = []
        for host in hosts:
            try:
                if not s.direct and s.security == "ssl":
                    server = self._smtp_ssl(host, port, timeout=TIMEOUT_S, context=context, local_hostname=helo)
                else:
                    server = self._smtp(host, port, timeout=TIMEOUT_S, local_hostname=helo)
                break
            except (smtplib.SMTPException, OSError) as e:
                tried.append(_scrub(e, s.password))
        else:
            where = f"{hosts[0]}:{port}" if len(hosts) == 1 else f"{', '.join(hosts)} (port {port})"
            raise MailError(f"the mail server at {where} did not take the message: " + "; ".join(tried))
        try:
            try:
                server.ehlo()
                if s.direct:  # TLS when the exchanger offers it, which is nearly always; never a code in the clear
                    if server.has_extn("starttls"):
                        server.starttls(context=context)
                        server.ehlo()
                    elif s.security != "none":
                        raise MailError(f"the mail server at {host} offers no TLS: not sending in the clear ({SECURITY_VAR}=none would)")
                elif s.security == "starttls":
                    server.starttls(context=context)
                    server.ehlo()
                if s.user:
                    server.login(s.user, s.password)
                # The envelope step by step, so the server's answer to the message itself is kept (the user,
                # 2026-11-06: "check the server response to see if it received the email"): 250 to the data is
                # its word that the message is queued for delivery; anything else is a refusal.
                code, reply = _reply(*server.mail(sender))
                if code != 250:
                    raise MailError(f"the mail server at {host}:{port} refused the sender {sender}: {code} {reply}")
                code, reply = _reply(*server.rcpt(recipient))
                if code not in (250, 251):
                    raise MailError(f"the mail server at {host}:{port} refused the address {_mask(to)}: {code} {reply}")
                code, reply = _reply(*server.data(msg.as_bytes()))
                if code != 250:
                    raise MailError(f"the mail server at {host}:{port} did not take the message: {code} {reply}")
            finally:
                try:
                    server.quit()
                except Exception:  # noqa: BLE001 - the answer is in hand; a quit refused is nothing
                    pass
        except smtplib.SMTPAuthenticationError as e:
            raise MailError(f"the mail server refused the credentials ({host}: {_scrub(e, s.password)}); "
                            "Gmail wants an app password, Proton an SMTP token or the Bridge's password") from None
        except (smtplib.SMTPException, OSError) as e:
            raise MailError(f"the mail server at {host}:{port} did not take the message: {_scrub(e, s.password)}") from None
        sent = Sent(to, host, port, code, reply, datetime.now(timezone.utc).isoformat(timespec="seconds"), msg["Message-ID"],
                    s.dkim_record)
        self.log(f"login: a message went to {_mask(to)} through {host} ({subject}): the server answered {code} {reply}"
                 + (f"; signed under {s.dkim_record}" if s.signing else ""))
        return sent


DOMAIN_FORM = r"[a-z0-9-]+(\.[a-z0-9-]+)+"


def mx_records(domain: str) -> list[str] | None:
    """The mail exchangers in `domain`'s mail record (MX) by preference (dns.mx_records): [] when the domain
    has none, None when it is no domain or the lookup could not run."""
    from . import dns

    return dns.mx_records(domain)


def exchangers(domain: str) -> list[str]:
    """Where to deliver for `domain`: its mail exchangers (mx_records), else the domain itself (the implicit
    MX of RFC 5321); nothing for what is not a domain."""
    domain = domain.strip().strip(".").lower()
    if not re.fullmatch(DOMAIN_FORM, domain):
        return []
    return mx_records(domain) or [domain]


def _context(host: str) -> ssl.SSLContext:
    """TLS to the server, verified; not for a server on this machine (Proton Bridge's own certificate is
    self-signed, and the connection never leaves the machine)."""
    context = ssl.create_default_context()
    if host.strip().lower() in LOOPBACK:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def _saved() -> dict[str, str]:
    """The mail variables among the user's saved ones (Windows; {} elsewhere)."""
    try:
        from .config import UserEnvironment

        return UserEnvironment(names=ALL_VARS).saved()
    except Exception:  # noqa: BLE001 - nothing saved to read
        return {}


def _secret(name: str) -> str | None:
    """A secret from the machine's store (secrets.py), None without one."""
    try:
        from . import secrets

        return secrets.get(name)
    except Exception:  # noqa: BLE001 - no store, or one that cannot be read: not set
        return None


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW: no console pops up


def _reply(code, text) -> tuple[int, str]:
    """An SMTP reply as (code, its words): smtplib gives the words as bytes, sometimes over lines."""
    words = text.decode("utf-8", "replace") if isinstance(text, (bytes, bytearray)) else str(text)
    return int(code), " ".join(words.split())


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
