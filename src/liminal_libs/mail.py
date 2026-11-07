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

    @classmethod
    def from_env(cls, env=None) -> "MailSettings":
        env = os.environ if env is None else env
        raw_port = (env.get(PORT_VAR) or "").strip()
        security = (env.get(SECURITY_VAR) or "starttls").strip().lower()
        return cls(host=(env.get(HOST_VAR) or "").strip(), port=int(raw_port) if raw_port.isdecimal() else 587,
                   security=security if security in SECURITIES else "starttls",
                   user=(env.get(USER_VAR) or "").strip(), password=env.get(PASSWORD_VAR) or "",
                   sender=(env.get(FROM_VAR) or env.get(USER_VAR) or "").strip(), helo=(env.get(HELO_VAR) or "").strip())

    @property
    def direct(self) -> bool:
        """Straight to each recipient's own mail exchangers (LIMINAL_SMTP_HOST=direct; the user, 2026-11-06:
        \"you can use a terminal to send email if to the correct port with right protocol\"): port 25 with
        STARTTLS, no account and no password; the message comes from `sender`, yours."""
        return self.host.strip().lower() == DIRECT

    @property
    def configured(self) -> bool:
        """Direct: a sender. Through an account: a host and a sender; a user with a password, or none
        (Proton Bridge on this machine)."""
        if self.direct:
            return bool(self.sender)
        return bool(self.host and self.sender and (bool(self.user) == bool(self.password)))

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
                "direct": self.direct}

    def problems(self) -> list[str]:
        out = []
        if self.direct:
            return [] if self.sender else [f"{FROM_VAR} is not set (the address the message comes from: yours)"]
        if not self.host:
            out.append(f"{HOST_VAR} is not set (direct: straight to each address's own mail server; "
                       "or smtp.gmail.com, smtp.protonmail.ch, 127.0.0.1 for Proton Bridge)")
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

    def as_dict(self) -> dict:
        """For a page or a client: the address masked."""
        return {"to": _mask(self.to), "host": self.host, "port": self.port, "code": self.code, "reply": self.reply,
                "at": self.at, "messageId": self.message_id}

    def __str__(self) -> str:
        return f"{self.host}:{self.port} answered {self.code} {self.reply}".rstrip()


class Mailer:
    """Sends through the account's server. `smtp` and `smtp_ssl` are the transports (smtplib's),
    replaceable by a test's stand-ins."""

    def __init__(self, settings: MailSettings | None = None, log=None, smtp=None, smtp_ssl=None, resolve=None):
        self.settings = settings or MailSettings.from_env()
        self.log = log or (lambda m: None)
        self._smtp = smtp or smtplib.SMTP
        self._smtp_ssl = smtp_ssl or smtplib.SMTP_SSL
        self._resolve = resolve or exchangers  # a domain's mail exchangers (direct delivery)

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
        context = ssl.create_default_context()
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
        sent = Sent(to, host, port, code, reply, datetime.now(timezone.utc).isoformat(timespec="seconds"), msg["Message-ID"])
        self.log(f"login: a message went to {_mask(to)} through {host} ({subject}): the server answered {code} {reply}")
        return sent


def exchangers(domain: str) -> list[str]:
    """The mail exchangers of `domain` by preference (its MX records, through the machine's own resolver:
    nslookup, windowless), the domain itself when it has none (the implicit MX of RFC 5321); nothing for
    what is not a domain."""
    domain = domain.strip().strip(".").lower()
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", domain):
        return []
    try:
        out = subprocess.run(["nslookup", "-type=mx", domain], capture_output=True, text=True, timeout=15,
                             **_no_window()).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return [domain]
    found = []
    for line in out.splitlines():  # Windows: "MX preference = 10, mail exchanger = h"; Linux: "mail exchanger = 10 h."
        m = re.search(r"MX preference = (\d+), mail exchanger = (\S+)", line) or re.search(r"mail exchanger = (\d+) (\S+)", line)
        if m:
            found.append((int(m.group(1)), m.group(2).rstrip(".").lower()))
    return list(dict.fromkeys(h for _, h in sorted(found))) or [domain]


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
