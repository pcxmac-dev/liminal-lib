"""What the resolver says (liminal_libs.dns), parsed from nslookup's two shapes (Windows, Linux)."""
from __future__ import annotations

from types import SimpleNamespace

from liminal_libs import dns

WINDOWS_A = ("Server:  UnKnown\nAddress:  192.168.1.1\n\nNon-authoritative answer:\nName:    liminal.hypokrites.me\n"
             "Addresses:  2606:2800:21f:cb07:6820:80da:af6b:8b2c\n          93.184.216.34\n")
LINUX_A = ("Server:\t\t127.0.0.53\nAddress:\t127.0.0.53#53\n\nNon-authoritative answer:\nName:\tliminal.hypokrites.me\n"
           "Address: 93.184.216.34\nName:\tliminal.hypokrites.me\nAddress: 2606:2800:21f:cb07:6820:80da:af6b:8b2c\n")
NONE = "Server:  UnKnown\nAddress:  192.168.1.1\n\n*** UnKnown can't find nowhere.hypokrites.me: Non-existent domain\n"
TXT = ("Server:  UnKnown\nAddress:  192.168.1.1\n\nNon-authoritative answer:\nhypokrites.me\ttext =\n\n\t\"v=spf1 include:_spf.protonmail.ch\" \" mx ~all\"\n"
       "hypokrites.me\ttext =\n\n\t\"some-verification=abc\"\n")


def test_addresses_mail_record_and_text_records_are_read_from_either_shape(monkeypatch):
    asked: list = []

    def run(args, **kw):
        asked.append(args)
        rtype, name = args[1].split("=", 1)[1], args[2]
        if "nowhere" in name:
            return SimpleNamespace(stdout=NONE)
        if rtype == "a":
            return SimpleNamespace(stdout=WINDOWS_A if len(asked) % 2 else LINUX_A)
        if rtype == "aaaa":
            return SimpleNamespace(stdout=NONE)
        if rtype == "txt":
            return SimpleNamespace(stdout=TXT)
        return SimpleNamespace(stdout="hypokrites.me\tMX preference = 10, mail exchanger = mail.protonmail.ch\n")
    monkeypatch.setattr(dns.subprocess, "run", run)
    assert dns.addresses("Liminal.Hypokrites.me.") == ["2606:2800:21f:cb07:6820:80da:af6b:8b2c", "93.184.216.34"]
    assert sorted(dns.addresses("liminal.hypokrites.me")) == ["2606:2800:21f:cb07:6820:80da:af6b:8b2c", "93.184.216.34"]  # the other shape
    assert dns.addresses("nowhere.hypokrites.me") == [] and dns.addresses("not a name") is None
    assert dns.mx_records("hypokrites.me") == ["mail.protonmail.ch"] and dns.mx_records("nowhere.hypokrites.me") == []
    assert dns.txt_records("hypokrites.me") == ["v=spf1 include:_spf.protonmail.ch mx ~all", "some-verification=abc"]
    assert dns.spf_record("hypokrites.me") == "v=spf1 include:_spf.protonmail.ch mx ~all" and dns.spf_record("nowhere.hypokrites.me") == ""
    assert dns.valid_name("A.B.") == "a.b" and dns.valid_name("no") == ""
    monkeypatch.setattr(dns.subprocess, "run", lambda args, **kw: (_ for _ in ()).throw(OSError("no nslookup")))
    assert dns.addresses("a.b") is None and dns.mx_records("a.b") is None and dns.txt_records("a.b") is None and dns.spf_record("a.b") is None
