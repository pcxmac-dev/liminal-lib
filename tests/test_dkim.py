"""DKIM for the user's own domain (liminal_libs.dkim): the key and its DNS record, a message signed and
verified the way a receiving server does, what breaks the signature, the canonical forms, the record
looked up; the mailer signing when set to."""
from __future__ import annotations

from email import message_from_bytes
from types import SimpleNamespace

import pytest

pytest.importorskip("cryptography")

from liminal_libs import dkim, mail  # noqa: E402
from liminal_libs.mail import Mailer, MailSettings  # noqa: E402


@pytest.fixture(scope="module")
def key():
    pem, public = dkim.generate_key()
    return pem, public


def test_the_key_and_its_dns_record(key):
    pem, public = key
    assert pem.startswith(b"-----BEGIN PRIVATE KEY-----") and dkim.public_of(pem) == public
    name, value = dkim.dns_record("hypokrites.me", "liminal", public)
    assert name == "liminal._domainkey.hypokrites.me" and value == f"v=DKIM1; k=rsa; p={public}" and len(public) > 300


RAW = (b"From: Liminal <liminal@hypokrites.me>\r\nTo: ada@example.test\r\nSubject: Your code,\r\n folded here\r\n"
       b"Date: Thu, 06 Nov 2026 21:00:00 -0800\r\nMessage-ID: <1@hypokrites.me>\r\nContent-Type: text/plain\r\n\r\n"
       b"Your code is  \r\n\r\n    123456\r\n\r\nType it.   \r\n\r\n\r\n")


def test_a_message_is_signed_and_verifies_until_it_is_changed(key):
    pem, public = key
    value = dkim.sign(RAW, "hypokrites.me", "liminal", pem, now=1800000000)
    assert value.startswith("v=1; a=rsa-sha256; c=relaxed/relaxed; d=hypokrites.me; s=liminal; t=1800000000; h=from:to:subject:date:message-id; bh=")
    signed = b"DKIM-Signature: " + value.encode() + b"\r\n" + RAW
    assert dkim.verify(signed, public)
    # the receiving side may refold and respace: relaxed canonicalization forgives it
    refolded = signed.replace(b"Subject: Your code,\r\n folded here", b"Subject:  Your code,   folded\r\n\there").replace(b"Type it.   ", b"Type   it.")
    assert dkim.verify(refolded, public)
    # a changed body or a changed signed header: no longer the message signed
    assert not dkim.verify(signed.replace(b"123456", b"654321"), public)
    assert not dkim.verify(signed.replace(b"To: ada@", b"To: eve@"), public)
    assert not dkim.verify(signed, dkim.generate_key()[1])  # another key
    assert not dkim.verify(RAW, public)  # unsigned


def test_the_canonical_forms_follow_the_rfc():
    assert dkim._relaxed_header(" Subject ", "  Hello   there \r\n  friend \t") == "subject:Hello there friend\r\n"
    assert dkim._relaxed_body("a  b \t\r\n\r\nc\r\n\r\n\r\n") == "a b\r\n\r\nc\r\n" and dkim._relaxed_body("") == "" and dkim._relaxed_body("\r\n\r\n") == ""
    headers, body = dkim._split(b"A: 1\nB: 2\n more\n\nbody\n")
    assert headers == [("A", " 1"), ("B", " 2\r\n more")] and body == "body\r\n"
    assert dkim._split(b"A: 1\n") == ([("A", " 1")], "")


def test_the_mailer_signs_for_the_senders_domain_when_a_key_is_set(key, tmp_path):
    from tests.test_mail import FakeServer  # the stand-in server records the message data

    pem, public = key
    path = tmp_path / "hypokrites.me.liminal.pem"
    path.write_bytes(pem)
    s = MailSettings.from_env({"LIMINAL_SMTP_HOST": "direct", "LIMINAL_SMTP_FROM": "Liminal <liminal@hypokrites.me>",
                               "LIMINAL_SMTP_DKIM_KEY": str(path), "LIMINAL_SMTP_DKIM_SELECTOR": "Liminal"})
    assert s.signing and s.signing_domain == "hypokrites.me" and s.dkim_record == "liminal._domainkey.hypokrites.me" and s.describe()["dkim"] == "liminal._domainkey.hypokrites.me"
    lines: list[str] = []
    FakeServer.made = []
    sent = Mailer(s, log=lines.append, smtp=FakeServer, resolve=lambda d: [f"mx.{d}"]).send("ada@example.test", "Your code", "123456\n")
    raw = FakeServer.made[0].data
    msg = message_from_bytes(raw)
    assert msg["DKIM-Signature"] and "d=hypokrites.me" in msg["DKIM-Signature"] and dkim.verify(raw, public)
    assert sent.signed == "liminal._domainkey.hypokrites.me" and sent.as_dict()["signed"] == sent.signed and lines[-1].endswith("; signed under liminal._domainkey.hypokrites.me")
    assert not dkim.verify(raw.replace(b"123456", b"000000"), public)
    # the domain said outright wins; a key file that is not there is said
    assert MailSettings.from_env({"LIMINAL_SMTP_FROM": "me@proton.me", "LIMINAL_SMTP_DKIM_KEY": "x", "LIMINAL_SMTP_DKIM_SELECTOR": "s",
                                  "LIMINAL_SMTP_DKIM_DOMAIN": "hypokrites.me"}).signing_domain == "hypokrites.me"
    with pytest.raises(mail.MailError) as e:
        Mailer(MailSettings.from_env({"LIMINAL_SMTP_HOST": "direct", "LIMINAL_SMTP_FROM": "me@hypokrites.me",
                                      "LIMINAL_SMTP_DKIM_KEY": str(tmp_path / "missing.pem"), "LIMINAL_SMTP_DKIM_SELECTOR": "liminal"}),
               smtp=FakeServer, resolve=lambda d: [d]).send("ada@example.test", "s", "t")
    assert "DKIM key file cannot be read" in str(e.value)


def test_the_published_record_is_read_through_the_machines_resolver(monkeypatch):
    windows = ("Server:  UnKnown\nAddress:  192.168.1.1\n\nNon-authoritative answer:\n"
               "liminal._domainkey.hypokrites.me\ttext =\n\n\t\"v=DKIM1; k=rsa; \" \"p=MIIBIjANBgkq\" \"hkiG9w0BAQEFAAOCAQ8A\"\n")
    linux = ("Server:\t\t127.0.0.53\n\nNon-authoritative answer:\n"
             "liminal._domainkey.hypokrites.me\ttext = \"v=DKIM1; k=rsa; p=MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A\"\n")
    for out in (windows, linux):
        monkeypatch.setattr(dkim.subprocess, "run", lambda args, _o=out, **kw: SimpleNamespace(stdout=_o))
        assert dkim.published("liminal._domainkey.hypokrites.me") == "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A"
    monkeypatch.setattr(dkim.subprocess, "run", lambda args, **kw: SimpleNamespace(stdout="*** liminal._domainkey.hypokrites.me: Non-existent domain\n"))
    assert dkim.published("liminal._domainkey.hypokrites.me") is None and dkim.published("not a name") is None
