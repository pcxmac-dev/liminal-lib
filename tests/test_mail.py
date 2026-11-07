"""Outgoing mail through one account of the user's (liminal_libs.mail): the settings from the
environment, a message through a stand-in server (STARTTLS, SSL, none), what is refused, and that the
password reaches no error, log or description. Nothing is sent anywhere."""
from __future__ import annotations

import smtplib
from email import message_from_bytes, message_from_string
from types import SimpleNamespace

import pytest

from liminal_libs import mail
from liminal_libs.mail import MailError, Mailer, MailSettings


class FakeServer:
    """What smtplib's SMTP does, as far as the mailer uses it; it records the calls."""

    made: list = []
    extensions = ("starttls",)

    def __init__(self, host, port, timeout=None, context=None, local_hostname=None):
        self.calls: list = [("connect", host, port, timeout, context is not None, local_hostname)]
        self.context = context
        self.made.append(self)

    def ehlo(self):
        self.calls.append(("ehlo",))

    def has_extn(self, name):
        return name.lower() in self.extensions

    def starttls(self, context=None):
        self.context = context
        self.calls.append(("starttls", context is not None))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def mail(self, sender, options=()):
        self.calls.append(("mail", sender))
        return 250, b"2.1.0 OK"

    def rcpt(self, recip, options=()):
        self.calls.append(("rcpt", recip))
        return 250, b"2.1.5 OK"

    def data(self, msg):
        self.data = msg if isinstance(msg, (bytes, bytearray)) else msg.encode()
        m = message_from_bytes(msg) if isinstance(msg, (bytes, bytearray)) else message_from_string(msg)
        self.calls.append(("data", m["From"], m["To"], m["Subject"], m.get_payload(), m["Message-ID"], m["Date"]))
        return 250, b"2.0.0 OK  1699 - gsmtp"

    def quit(self):
        self.calls.append(("quit",))


class RefusingServer(FakeServer):
    def login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, f"bad credentials for {user} with {password}".encode())


class RejectingServer(FakeServer):
    def rcpt(self, recip, options=()):
        return 550, b"5.1.1 no such user"


class DownServer(FakeServer):
    def __init__(self, host, port, timeout=None, context=None, local_hostname=None):
        raise OSError("connection refused")


class PlainServer(FakeServer):
    extensions = ()  # no STARTTLS offered


ENV = {"LIMINAL_SMTP_HOST": "smtp.gmail.com", "LIMINAL_SMTP_USER": "me@gmail.com", "LIMINAL_SMTP_PASSWORD": "app-pw-1234",
       "LIMINAL_SMTP_FROM": "Liminal <me@gmail.com>", "LIMINAL_SMTP_HELO": "neptune.example.test"}


def test_the_settings_come_from_the_environment_and_say_what_is_missing():
    s = MailSettings.from_env({})
    assert not s.configured and s.port == 587 and s.security == "starttls"
    assert any("LIMINAL_SMTP_HOST" in p for p in s.problems())
    s = MailSettings.from_env({**ENV, "LIMINAL_SMTP_PORT": "465", "LIMINAL_SMTP_SECURITY": "SSL"})
    assert s.configured and s.port == 465 and s.security == "ssl" and s.sender == "Liminal <me@gmail.com>"
    assert s.describe() == {"configured": True, "host": "smtp.gmail.com", "port": 465, "security": "ssl",
                            "sender": "me@gmail.com", "user": "me@gmail.com", "direct": False, "dkim": ""}
    assert "app-pw" not in repr(s) and "app-pw" not in str(s.describe())
    half = MailSettings.from_env({"LIMINAL_SMTP_HOST": "smtp.gmail.com", "LIMINAL_SMTP_USER": "me@gmail.com"})
    assert not half.configured and any("go together" in p for p in half.problems())
    bridge = MailSettings.from_env({"LIMINAL_SMTP_HOST": "127.0.0.1", "LIMINAL_SMTP_PORT": "1025",
                                    "LIMINAL_SMTP_SECURITY": "none", "LIMINAL_SMTP_FROM": "me@proton.me"})
    assert bridge.configured and bridge.security == "none" and not bridge.user
    assert MailSettings.from_env({**ENV, "LIMINAL_SMTP_SECURITY": "odd", "LIMINAL_SMTP_PORT": "x"}).port == 587


def test_a_message_goes_through_the_account_with_starttls_and_the_log_names_no_secret():
    FakeServer.made = []
    lines: list[str] = []
    m = Mailer(MailSettings.from_env(ENV), log=lines.append, smtp=FakeServer, smtp_ssl=FakeServer)
    sent = m.send("ada@example.test", "Your passkey", "the text")
    calls = FakeServer.made[0].calls
    assert calls[0] == ("connect", "smtp.gmail.com", 587, mail.TIMEOUT_S, False, "neptune.example.test")
    assert [c[0] for c in calls] == ["connect", "ehlo", "starttls", "ehlo", "login", "mail", "rcpt", "data", "quit"]
    assert ("login", "me@gmail.com", "app-pw-1234") in calls and ("mail", "me@gmail.com") in calls and ("rcpt", "ada@example.test") in calls
    assert calls[7][1:5] == ("Liminal <me@gmail.com>", "ada@example.test", "Your passkey", "the text\n")
    assert calls[7][5].startswith("<") and calls[7][5].endswith("@gmail.com>") and calls[7][6]  # a message id and a date
    # the server's word, kept: its answer to the message, when, and the id to find it by
    assert (sent.to, sent.host, sent.port, sent.code, sent.reply) == ("ada@example.test", "smtp.gmail.com", 587, 250, "2.0.0 OK 1699 - gsmtp")
    assert sent.message_id == calls[7][5] and sent.at.startswith("20") and str(sent) == "smtp.gmail.com:587 answered 250 2.0.0 OK 1699 - gsmtp"
    assert sent.as_dict() == {"to": "a***@example.test", "host": "smtp.gmail.com", "port": 587, "code": 250, "reply": "2.0.0 OK 1699 - gsmtp",
                              "at": sent.at, "messageId": sent.message_id, "signed": ""}
    assert lines == ["login: a message went to a***@example.test through smtp.gmail.com (Your passkey): the server answered 250 2.0.0 OK 1699 - gsmtp"]
    # SSL from the start, and no login for a bridge on this machine
    FakeServer.made = []
    Mailer(MailSettings.from_env({**ENV, "LIMINAL_SMTP_PORT": "465", "LIMINAL_SMTP_SECURITY": "ssl"}),
           smtp=FakeServer, smtp_ssl=FakeServer).send("ada@example.test", "s", "t")
    calls = FakeServer.made[0].calls
    assert calls[0][2] == 465 and calls[0][4] is True and [c[0] for c in calls] == ["connect", "ehlo", "login", "mail", "rcpt", "data", "quit"]
    FakeServer.made = []
    Mailer(MailSettings.from_env({"LIMINAL_SMTP_HOST": "127.0.0.1", "LIMINAL_SMTP_PORT": "1025", "LIMINAL_SMTP_SECURITY": "none",
                                  "LIMINAL_SMTP_FROM": "me@proton.me"}), smtp=FakeServer).send("ada@example.test", "s", "t")
    assert [c[0] for c in FakeServer.made[0].calls] == ["connect", "ehlo", "mail", "rcpt", "data", "quit"]


def test_what_is_refused_and_that_the_password_never_leaves():
    with pytest.raises(MailError) as e:
        Mailer(MailSettings.from_env({}), smtp=FakeServer).send("ada@example.test", "s", "t")
    assert "not set up" in str(e.value) and "LIMINAL_SMTP_HOST" in str(e.value)
    with pytest.raises(MailError) as e:
        Mailer(MailSettings.from_env(ENV), smtp=FakeServer).send("not an address", "s", "t")
    assert "not an email address" in str(e.value)
    with pytest.raises(MailError) as e:
        Mailer(MailSettings.from_env(ENV), smtp=RefusingServer).send("ada@example.test", "s", "t")
    assert "refused the credentials" in str(e.value) and "app-pw-1234" not in str(e.value) and "***" in str(e.value)
    with pytest.raises(MailError) as e:
        Mailer(MailSettings.from_env(ENV), smtp=DownServer).send("ada@example.test", "s", "t")
    assert "did not take the message" in str(e.value) and "connection refused" in str(e.value)
    with pytest.raises(MailError) as e:  # the server's own words when it refuses the address
        Mailer(MailSettings.from_env(ENV), smtp=RejectingServer).send("ada@example.test", "s", "t")
    assert str(e.value) == "the mail server at smtp.gmail.com:587 refused the address a***@example.test: 550 5.1.1 no such user"
    assert mail.mask("ada.lovelace@example.test") == "a***@example.test" and mail.mask("nothing") == "***"


def test_direct_delivery_goes_to_the_recipients_own_exchangers_with_tls_and_no_account():
    # the user, 2026-11-06: "you can use a terminal to send email if to the correct port with right protocol":
    # LIMINAL_SMTP_HOST=direct, the sender yours; port 25 of the address's own exchangers, TLS, no password
    env = {"LIMINAL_SMTP_HOST": "direct", "LIMINAL_SMTP_FROM": "me@proton.me", "LIMINAL_SMTP_HELO": "neptune.example.test"}
    s = MailSettings.from_env(env)
    assert s.direct and s.configured and s.problems() == [] and s.helo_name() == "neptune.example.test"
    assert s.describe() == {"configured": True, "host": "direct", "port": 25, "security": "starttls", "sender": "me@proton.me",
                            "user": "", "direct": True, "dkim": ""}
    bare = MailSettings.from_env({"LIMINAL_SMTP_HOST": "direct"})
    assert not bare.configured and bare.problems() == ["LIMINAL_SMTP_FROM is not set (the address the message comes from: yours)"]
    assert "." in MailSettings().helo_name()  # a name with a dot, whatever the machine is called
    FakeServer.made = []
    lines: list[str] = []
    asked: list[str] = []
    m = Mailer(s, log=lines.append, smtp=FakeServer, resolve=lambda d: asked.append(d) or [f"mx1.{d}", f"mx2.{d}"])
    sent = m.send("ada@example.test", "s", "t")
    calls = FakeServer.made[0].calls
    assert asked == ["example.test"] and calls[0] == ("connect", "mx1.example.test", 25, mail.TIMEOUT_S, False, "neptune.example.test")
    assert [c[0] for c in calls] == ["connect", "ehlo", "starttls", "ehlo", "mail", "rcpt", "data", "quit"]  # no login
    assert ("mail", "me@proton.me") in calls and (sent.host, sent.port, sent.code) == ("mx1.example.test", 25, 250)
    assert lines == ["login: a message went to a***@example.test through mx1.example.test (s): the server answered 250 2.0.0 OK 1699 - gsmtp"]

    class FirstDown(FakeServer):
        def __init__(self, host, port, timeout=None, context=None, local_hostname=None):
            if host.startswith("mx1."):
                raise OSError("connection refused")
            super().__init__(host, port, timeout, context, local_hostname)
    # the first exchanger down: the second takes it; none reached: said with both; no TLS: not in the clear
    FakeServer.made = []
    assert Mailer(s, smtp=FirstDown, resolve=lambda d: [f"mx1.{d}", f"mx2.{d}"]).send("ada@example.test", "s", "t").host == "mx2.example.test"
    with pytest.raises(MailError) as e:
        Mailer(s, smtp=DownServer, resolve=lambda d: [f"mx1.{d}", f"mx2.{d}"]).send("ada@example.test", "s", "t")
    assert str(e.value).startswith("the mail server at mx1.example.test, mx2.example.test (port 25) did not take the message: OSError: connection refused")
    with pytest.raises(MailError) as e:
        Mailer(s, smtp=PlainServer, resolve=lambda d: [d]).send("ada@example.test", "s", "t")
    assert "offers no TLS" in str(e.value)
    plain = MailSettings.from_env({**env, "LIMINAL_SMTP_SECURITY": "none"})
    assert Mailer(plain, smtp=PlainServer, resolve=lambda d: [d]).send("ada@example.test", "s", "t").host == "example.test"
    with pytest.raises(MailError) as e:
        Mailer(s, smtp=FakeServer, resolve=lambda d: []).send("ada@example.test", "s", "t")
    assert str(e.value) == "no mail server is known for a***@example.test"


def test_the_settings_are_read_from_the_environment_the_saved_variables_and_the_secret_store(monkeypatch):
    # `liminal-login mail setup` saves the account as user variables and the password in the secret store:
    # a mailer built without settings sees them at once, the process's own environment winning
    for v in mail.ALL_VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(mail, "_saved", lambda: {"LIMINAL_SMTP_HOST": "127.0.0.1", "LIMINAL_SMTP_PORT": "1025", "LIMINAL_SMTP_USER": "me@proton.me"})
    monkeypatch.setattr(mail, "_secret", lambda name: "bridge-pw-1" if name == "LIMINAL_SMTP_PASSWORD" else None)
    s = MailSettings.from_env()
    assert (s.host, s.port, s.user, s.password, s.sender) == ("127.0.0.1", 1025, "me@proton.me", "bridge-pw-1", "me@proton.me") and s.configured
    monkeypatch.setenv("LIMINAL_SMTP_HOST", "smtp.gmail.com")
    monkeypatch.setenv("LIMINAL_SMTP_PASSWORD", "env-pw")
    assert (MailSettings.from_env().host, MailSettings.from_env().password) == ("smtp.gmail.com", "env-pw")
    m = Mailer(smtp=FakeServer)
    assert m.settings.host == "smtp.gmail.com" and m.configured
    monkeypatch.setenv("LIMINAL_SMTP_HOST", "127.0.0.1")
    assert m.settings.host == "127.0.0.1"  # read anew
    FakeServer.made = []
    assert m.send("ada@example.test", "s", "t").host == "127.0.0.1"
    server = FakeServer.made[0]
    assert server.calls[0][1:3] == ("127.0.0.1", 1025) and ("starttls", True) in server.calls
    assert server.context.check_hostname is False  # the Bridge's own certificate, on this machine
    FakeServer.made = []
    Mailer(MailSettings.from_env(ENV), smtp=FakeServer).send("ada@example.test", "s", "t")
    assert FakeServer.made[0].context.check_hostname is True  # elsewhere: verified
    given = Mailer(MailSettings.from_env(ENV), smtp=FakeServer)
    monkeypatch.setenv("LIMINAL_SMTP_HOST", "elsewhere.test")
    assert given.settings.host == "smtp.gmail.com"  # settings given stay as given
    given.settings = None
    assert given.settings.host == "elsewhere.test"


def test_the_exchangers_come_from_the_machines_own_resolver(monkeypatch):
    windows = ("Server:  UnKnown\nAddress:  192.168.1.1\n\nNon-authoritative answer:\n"
               "proton.me\tMX preference = 20, mail exchanger = mailsec.protonmail.ch\n"
               "proton.me\tMX preference = 10, mail exchanger = mail.protonmail.ch\n")
    linux = ("Server:\t\t127.0.0.53\nAddress:\t127.0.0.53#53\n\nNon-authoritative answer:\n"
             "proton.me\tmail exchanger = 10 mail.protonmail.ch.\nproton.me\tmail exchanger = 20 mailsec.protonmail.ch.\n")
    ran: list = []
    for out in (windows, linux):
        monkeypatch.setattr(mail.subprocess, "run", lambda args, _o=out, **kw: ran.append(args) or SimpleNamespace(stdout=_o))
        assert mail.exchangers("Proton.me.") == ["mail.protonmail.ch", "mailsec.protonmail.ch"]
    assert ran[0] == ["nslookup", "-type=mx", "proton.me"]
    monkeypatch.setattr(mail.subprocess, "run", lambda args, **kw: SimpleNamespace(stdout="*** nowhere.test: Non-existent domain\n"))
    assert mail.exchangers("nowhere.test") == ["nowhere.test"]  # the implicit MX: the domain itself
    assert mail.exchangers("not a domain") == [] and mail.exchangers("") == []
