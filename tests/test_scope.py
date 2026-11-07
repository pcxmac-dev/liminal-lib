"""What a login is tied to (liminal_libs.apps.scope): the file, a local folder or a domain as a URL, where
the users live, the mail settings it gives, and how every app finds the same file."""
from __future__ import annotations

from pathlib import Path

import pytest

from liminal_libs import layout
from liminal_libs.apps import Scope
from liminal_libs.apps import scope as scope_mod


def test_a_local_login_is_a_project_folder_as_a_file_url(tmp_path):
    s = Scope.local(tmp_path / "project", title="My project", serves=("charts", "integrator"), port=8801)
    assert s.kind == "local" and s.url.startswith("file:///") and s.root == (tmp_path / "project").resolve() and s.domain == ""
    assert s.name == "project" and s.label == f"local to {s.root}" and s.describe()["members"] == "local" and s.describe()["mailServer"] == ""
    assert s.mail_variables() == {}  # a local login sends no email
    # the users live inside the folder: its store/data/login when it has a store/data, else .liminal/login
    assert s.data_dir() == s.root / ".liminal" / "login"
    (tmp_path / "project" / "store" / "data").mkdir(parents=True)
    assert s.data_dir() == s.root / "store" / "data" / "login"
    assert Scope(s.url, data=tmp_path / "elsewhere").data_dir() == tmp_path / "elsewhere"
    # the file round trip keeps it all
    back = Scope.parse(s.text(), tmp_path / "liminal-login.cfg")
    assert (back.url, back.title, back.serves, back.port, back.kind, back.path) == (s.url, "My project", ("charts", "integrator"), 8801, "local", tmp_path / "liminal-login.cfg")
    assert Scope.read(s.write(tmp_path / "liminal-login.cfg")).root == s.root


def test_a_domain_login_is_the_domains_users_with_its_own_mail_record_unless_a_server_is_named(tmp_path):
    s = Scope.for_domain("Hypokrites.me.")
    assert s.kind == "domain" and s.url == "https://hypokrites.me" and s.domain == "hypokrites.me" and s.name == "hypokrites.me" and s.label == "@hypokrites.me"
    assert s.members == "domain" and s.root is None and s.data_dir(tmp_path) == tmp_path  # the app adds the domain's folder
    assert s.mail_variables() == {"LIMINAL_SMTP_HOST": "direct", "LIMINAL_SMTP_FROM": "Liminal <admin@hypokrites.me>"}
    assert s.admin_address == "admin@hypokrites.me" and s.admin_host == "hypokrites.me" and s.imap_variables() == {}
    assert s.describe()["mailServer"] == "the domain's own mail record"
    named = Scope.parse("[login]\nscope = hypokrites.me\nmembers = any\n\n[mail]\nhost = 127.0.0.1\nport = 1025\nuser = me@hypokrites.me\nfrom = Liminal <login@hypokrites.me>\n")
    assert named.url == "https://hypokrites.me" and named.members == "any" and named.describe()["mailServer"] == "127.0.0.1"
    assert named.mail_variables() == {"LIMINAL_SMTP_HOST": "127.0.0.1", "LIMINAL_SMTP_PORT": "1025", "LIMINAL_SMTP_USER": "me@hypokrites.me",
                                      "LIMINAL_SMTP_FROM": "Liminal <login@hypokrites.me>"}
    assert "members = any" in named.text() and "host = 127.0.0.1" in named.text() and "password" in named.text()
    # the login's own account: admin@ a host under the domain, valid in DNS; the admin mailbox for the checks
    own = Scope.parse("[login]\nscope = https://hypokrites.me\nadmin = Admin@liminal.hypokrites.me\n\n[imap]\nhost = mail.liminal.hypokrites.me\nport = 993\n")
    assert own.admin_address == "admin@liminal.hypokrites.me" and own.admin_host == "liminal.hypokrites.me"
    assert own.mail_variables()["LIMINAL_SMTP_FROM"] == "Liminal <admin@liminal.hypokrites.me>"
    assert own.imap_variables() == {"LIMINAL_IMAP_HOST": "mail.liminal.hypokrites.me", "LIMINAL_IMAP_PORT": "993", "LIMINAL_IMAP_USER": "admin@liminal.hypokrites.me"}
    assert own.describe()["admin"] == "admin@liminal.hypokrites.me" and own.describe()["mailbox"] == "mail.liminal.hypokrites.me"
    back = Scope.parse(own.text())
    assert back.admin == "admin@liminal.hypokrites.me" and back.imap["host"] == "mail.liminal.hypokrites.me" and "[imap]" in own.text()
    with pytest.raises(ValueError):
        Scope.parse("[login]\nscope = https://hypokrites.me\nadmin = admin@elsewhere.test\n")
    assert Scope.local(tmp_path).admin_address == "" and Scope.local(tmp_path).admin_host == ""
    with pytest.raises(ValueError):
        Scope.parse("[login]\nscope = https://hypokrites.me\nmembers = some\n")
    with pytest.raises(ValueError):
        Scope.parse("[other]\nx = 1\n")
    with pytest.raises(ValueError):
        Scope.parse("[login]\nscope = https:///\n")


def test_every_app_finds_the_same_file(tmp_path, monkeypatch):
    monkeypatch.setattr(layout, "home", lambda: None)
    monkeypatch.setattr(layout, "data_dir", lambda: tmp_path / "data")
    monkeypatch.delenv(scope_mod.CFG_VAR, raising=False)
    assert Scope.find(tmp_path / "deep" / "er") is None
    project = tmp_path / "project"
    (project / "sub" / "dir").mkdir(parents=True)
    Scope.local(project, title="P").write(project / "liminal-login.cfg")
    found = Scope.find(project / "sub" / "dir")  # from a folder inside the project: its file
    assert found is not None and found.title == "P" and found.path == project / "liminal-login.cfg"
    (tmp_path / "data" / "login").mkdir(parents=True)
    Scope.for_domain("example.test").write(tmp_path / "data" / "login" / "liminal-login.cfg")
    assert Scope.find(tmp_path / "deep").domain == "example.test"  # else the data folder's
    monkeypatch.setenv(scope_mod.CFG_VAR, str(project / "liminal-login.cfg"))
    assert Scope.find(tmp_path / "deep").title == "P"  # LIMINAL_LOGIN_CFG names it outright
    monkeypatch.setattr(layout, "home", lambda: project)
    monkeypatch.setenv(scope_mod.CFG_VAR, "")
    assert Scope.find(tmp_path / "deep").title == "P"  # the workspace's
