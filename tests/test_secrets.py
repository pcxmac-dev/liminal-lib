"""The user's secrets in the OS's own store (liminal_libs.secrets): a stand-in store anywhere, Windows'
Credential Manager where this runs on Windows (one entry written, read and removed)."""
from __future__ import annotations

import os
import uuid

import pytest

from liminal_libs import secrets


class DictBackend:
    def __init__(self):
        self.rows: dict = {}

    def write(self, target, user, blob):
        self.rows[target] = (user, blob)

    def read(self, target):
        return self.rows[target][1] if target in self.rows else None

    def delete(self, target):
        return self.rows.pop(target, None) is not None


def test_a_secret_is_kept_by_its_variables_name_and_never_in_the_clear():
    s = secrets.SecretStore(DictBackend())
    assert s.available and s.get("LIMINAL_SMTP_PASSWORD") is None
    s.put("LIMINAL_SMTP_PASSWORD", "app-pw-1234")
    assert s.get("LIMINAL_SMTP_PASSWORD") == "app-pw-1234" and list(s.backend.rows) == ["liminal:LIMINAL_SMTP_PASSWORD"]
    user, blob = s.backend.rows["liminal:LIMINAL_SMTP_PASSWORD"]
    assert user == "LIMINAL_SMTP_PASSWORD" and b"app-pw-1234" not in blob  # UTF-16, as Windows keeps passwords
    assert s.remove("LIMINAL_SMTP_PASSWORD") and not s.remove("LIMINAL_SMTP_PASSWORD") and s.get("LIMINAL_SMTP_PASSWORD") is None


@pytest.mark.skipif(os.name == "nt", reason="another OS: no store without a stand-in")
def test_without_a_store_nothing_is_kept_and_putting_says_so():
    s = secrets.SecretStore()
    assert not s.available and s.get("X") is None and not s.remove("X")
    with pytest.raises(OSError):
        s.put("X", "y")


@pytest.mark.skipif(os.name != "nt", reason="Windows' Credential Manager")
def test_windows_credential_manager_keeps_a_secret_for_this_user():
    s = secrets.SecretStore()
    name = f"TEST_{uuid.uuid4().hex[:8]}"
    try:
        assert s.available and s.get(name) is None
        s.put(name, "a secret value, with a tick \u2713")
        assert s.get(name) == "a secret value, with a tick \u2713"
        s.put(name, "changed")
        assert s.get(name) == "changed"
    finally:
        assert s.remove(name)
    assert s.get(name) is None and not s.remove(name)
