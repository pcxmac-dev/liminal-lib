"""Outgoing mail through one account of the user's (liminal_libs.mail): the settings from the
environment, a message through a stand-in server (STARTTLS, SSL, none), what is refused, and that the
password reaches no error, log or description. Nothing is sent anywhere."""
from __future__ import annotations

import smtplib

import pytest

from liminal_libs import mail
from liminal_libs.mail import MailError, Mailer, MailSettings


class FakeServer:
    """What smtplib's SMTP does, as far as the mailer uses it; it records the calls."""

    made: list = []

    def __init__(self, host, port, timeout=None, context=None):
        self.calls: list = [("connect", host, port, timeout, context is not None)]
        self.made.append(self)

    def ehlo(self):
        self.calls.append(("ehlo",))

    def starttls(self, context=None):
        self.calls.append(("starttls", context is not None))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def send_message(self, msg):
        self.calls.append(("send", msg["From"], msg["To"], msg["Subject"], msg.get_content()))

    def quit(self):
        self.calls.append(("quit",))


class RefusingServer(FakeServer):
    def login(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, f"bad credentials for {user} with {password}".encode())


class DownServer(FakeServer):
    def __init__(self, host, port, timeout=None, context=None):
        raise OSError("connection refused")


ENV = {"LIMINAL_SMTP_HOST": "smtp.gmail.com", "LIMINAL_SMTP_USER": "me@gmail.com", "LIMINAL_SMTP_PASSWORD": "app-pw-1234",
       "LIMINAL_SMTP_FROM": "Liminal <me@gmail.com>"}


def test_the_settings_come_from_the_environment_and_say_what_is_missing():
    s = MailSettings.from_env({})
    assert not s.configured and s.port == 587 and s.security == "starttls"
    assert any("LIMINAL_SMTP_HOST" in p for p in s.problems())
    s = MailSettings.from_env({**ENV, "LIMINAL_SMTP_PORT": "465", "LIMINAL_SMTP_SECURITY": "SSL"})
    assert s.configured and s.port == 465 and s.security == "ssl" and s.sender == "Liminal <me@gmail.com>"
    assert s.describe() == {"configured": True, "host": "smtp.gmail.com", "port": 465, "security": "ssl",
                            "sender": "me@gmail.com", "user": "me@gmail.com"}
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
    m.send("ada@example.test", "Your passkey", "the text")
    calls = FakeServer.made[0].calls
    assert calls[0] == ("connect", "smtp.gmail.com", 587, mail.TIMEOUT_S, False)
    assert [c[0] for c in calls] == ["connect", "ehlo", "starttls", "ehlo", "login", "send", "quit"]
    assert ("login", "me@gmail.com", "app-pw-1234") in calls
    assert calls[5][1:] == ("Liminal <me@gmail.com>", "ada@example.test", "Your passkey", "the text\n")
    assert lines == ["login: a message went to a***@example.test through smtp.gmail.com (Your passkey)"]
    # SSL from the start, and no login for a bridge on this machine
    FakeServer.made = []
    Mailer(MailSettings.from_env({**ENV, "LIMINAL_SMTP_PORT": "465", "LIMINAL_SMTP_SECURITY": "ssl"}),
           smtp=FakeServer, smtp_ssl=FakeServer).send("ada@example.test", "s", "t")
    calls = FakeServer.made[0].calls
    assert calls[0][2] == 465 and calls[0][4] is True and [c[0] for c in calls] == ["connect", "ehlo", "login", "send", "quit"]
    FakeServer.made = []
    Mailer(MailSettings.from_env({"LIMINAL_SMTP_HOST": "127.0.0.1", "LIMINAL_SMTP_PORT": "1025", "LIMINAL_SMTP_SECURITY": "none",
                                  "LIMINAL_SMTP_FROM": "me@proton.me"}), smtp=FakeServer).send("ada@example.test", "s", "t")
    assert [c[0] for c in FakeServer.made[0].calls] == ["connect", "ehlo", "send", "quit"]


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
    assert mail.mask("ada.lovelace@example.test") == "a***@example.test" and mail.mask("nothing") == "***"
