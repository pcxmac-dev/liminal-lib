"""The admin mailbox read for the checks (liminal_libs.mailbox): the settings, a search through a stand-in
IMAP server, what is refused, and that the password reaches no error."""
from __future__ import annotations

import imaplib

import pytest

from liminal_libs import mailbox
from liminal_libs.mailbox import Mailbox, MailboxError, MailboxSettings


class FakeBox:
    made: list = []
    inbox = [b"1", b"2"]

    def __init__(self, host, port, timeout=None, ssl_context=None):
        self.calls = [("connect", host, port, timeout, ssl_context is not None)]
        self.context = ssl_context
        self.made.append(self)

    def starttls(self, ssl_context=None):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"2"]

    def search(self, charset, *criteria):
        self.calls.append(("search", charset, *criteria))
        return "OK", [b" ".join(self.inbox) if criteria[-1] == '"liminal-check-1"' else b""]

    def logout(self):
        self.calls.append(("logout",))


class RefusingBox(FakeBox):
    def login(self, user, password):
        raise imaplib.IMAP4.error(f"[AUTHENTICATIONFAILED] bad password {password}")


class DownBox(FakeBox):
    def __init__(self, host, port, timeout=None, ssl_context=None):
        raise OSError("connection refused")


ENV = {"LIMINAL_IMAP_HOST": "mail.liminal.hypokrites.me", "LIMINAL_IMAP_USER": "admin@liminal.hypokrites.me", "LIMINAL_IMAP_PASSWORD": "imap-pw-1"}


def test_the_settings_and_a_search_through_the_stand_in(monkeypatch):
    s = MailboxSettings.from_env(ENV)
    assert s.configured and (s.host, s.port, s.security, s.user) == ("mail.liminal.hypokrites.me", 993, "ssl", "admin@liminal.hypokrites.me")
    assert "password" not in str(s.describe()) and "imap-pw" not in repr(s) and MailboxSettings.from_env({}).problems()
    FakeBox.made = []
    box = Mailbox(s, imap=FakeBox, imap_ssl=FakeBox)
    assert box.find("liminal-check-1") == 2 and box.find("liminal-check-2") == 0
    calls = FakeBox.made[0].calls
    assert calls[0] == ("connect", "mail.liminal.hypokrites.me", 993, mailbox.TIMEOUT_S, True)
    assert [c[0] for c in calls] == ["connect", "login", "select", "search", "logout"] and ("login", "admin@liminal.hypokrites.me", "imap-pw-1") in calls
    assert ("select", "INBOX", True) in calls and FakeBox.made[0].context.check_hostname is True
    # starttls on a plain port; a server on this machine with its own certificate
    FakeBox.made = []
    Mailbox(MailboxSettings.from_env({**ENV, "LIMINAL_IMAP_PORT": "143", "LIMINAL_IMAP_SECURITY": "starttls"}), imap=FakeBox).find("x")
    assert [c[0] for c in FakeBox.made[0].calls][:3] == ["connect", "starttls", "login"]
    FakeBox.made = []
    Mailbox(MailboxSettings.from_env({**ENV, "LIMINAL_IMAP_HOST": "127.0.0.1"}), imap_ssl=FakeBox).find("x")
    assert FakeBox.made[0].context.check_hostname is False
    # the settings read anew when none are given: the file's section, the environment, the secret store
    for v in mailbox.VARS:
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(mailbox, "_saved", lambda: {})
    monkeypatch.setattr(mailbox, "_secret", lambda name: "stored-pw" if name == "LIMINAL_IMAP_PASSWORD" else None)
    m = Mailbox(cfg={"LIMINAL_IMAP_HOST": "127.0.0.1", "LIMINAL_IMAP_USER": "admin@x.test"}, imap_ssl=FakeBox)
    assert m.configured and m.settings.password == "stored-pw"
    monkeypatch.setenv("LIMINAL_IMAP_HOST", "elsewhere.test")
    assert m.settings.host == "elsewhere.test"


def test_what_is_refused_and_the_password_never_leaves():
    with pytest.raises(MailboxError) as e:
        Mailbox(MailboxSettings.from_env({}), imap_ssl=FakeBox).find("x")
    assert "not set up" in str(e.value)
    with pytest.raises(MailboxError) as e:
        Mailbox(MailboxSettings.from_env(ENV), imap_ssl=RefusingBox).find("x")
    assert "refused" in str(e.value) and "imap-pw-1" not in str(e.value) and "***" in str(e.value)
    with pytest.raises(MailboxError) as e:
        Mailbox(MailboxSettings.from_env(ENV), imap_ssl=DownBox).find("x")
    assert "could not be reached" in str(e.value) and "connection refused" in str(e.value)
