"""Secrets of the user's kept where the OS keeps them (the user, 2026-11-06: "is there a way to add
something secure to the python env?"): Windows' Credential Manager, the user's own vault, under the names
`liminal:<VARIABLE>` (the password of the mail account as `liminal:LIMINAL_SMTP_PASSWORD`), so an app reads
a secret from there when the variable is not in its environment and nothing secret sits in a file or in
the registry in the clear. Another OS has no store here (`available` is False) unless a stand-in is
given: the environment variable itself is the way there."""
from __future__ import annotations

import ctypes
import os

PREFIX = "liminal:"


class SecretStore:
    """`get(name)`, `put(name, value)` and `remove(name)` by the variable's name. `backend`: Windows'
    Credential Manager where this runs on Windows, a stand-in anywhere (`write(target, user, blob)`,
    `read(target) -> bytes | None`, `delete(target) -> bool`)."""

    def __init__(self, backend=None):
        self.backend = backend if backend is not None else (_CredentialManager() if os.name == "nt" else None)

    @property
    def available(self) -> bool:
        return self.backend is not None

    def get(self, name: str) -> str | None:
        if self.backend is None:
            return None
        blob = self.backend.read(PREFIX + name)
        return None if blob is None else blob.decode("utf-16-le")

    def put(self, name: str, value: str) -> None:
        if self.backend is None:
            raise OSError("no secret store on this machine (Windows' Credential Manager is the one supported): "
                          f"set {name} as an environment variable instead")
        self.backend.write(PREFIX + name, name, value.encode("utf-16-le"))  # UTF-16, as Windows keeps passwords

    def remove(self, name: str) -> bool:
        if self.backend is None:
            return False
        return self.backend.delete(PREFIX + name)


class _CredentialManager:
    """Windows' Credential Manager through advapi32: the generic credentials of this user."""

    TYPE_GENERIC = 1
    PERSIST_LOCAL_MACHINE = 2
    ERROR_NOT_FOUND = 1168

    def __init__(self):
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                        ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                        ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                        ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR)]

        self.CREDENTIAL = CREDENTIAL
        api = ctypes.WinDLL("advapi32", use_last_error=True)
        api.CredWriteW.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        api.CredWriteW.restype = wintypes.BOOL
        api.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        api.CredReadW.restype = wintypes.BOOL
        api.CredDeleteW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        api.CredDeleteW.restype = wintypes.BOOL
        api.CredFree.argtypes = [ctypes.c_void_p]
        api.CredFree.restype = None
        self.api = api

    def write(self, target: str, user: str, blob: bytes) -> None:
        buffer = ctypes.create_string_buffer(blob, len(blob))
        cred = self.CREDENTIAL()
        cred.Type = self.TYPE_GENERIC
        cred.TargetName = target
        cred.UserName = user
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = self.PERSIST_LOCAL_MACHINE
        if not self.api.CredWriteW(ctypes.byref(cred), 0):
            raise ctypes.WinError(ctypes.get_last_error())

    def read(self, target: str) -> bytes | None:
        found = ctypes.POINTER(self.CREDENTIAL)()
        if not self.api.CredReadW(target, self.TYPE_GENERIC, 0, ctypes.byref(found)):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return None
            raise ctypes.WinError(error)
        try:
            c = found.contents
            return ctypes.string_at(c.CredentialBlob, c.CredentialBlobSize) if c.CredentialBlobSize else b""
        finally:
            self.api.CredFree(found)

    def delete(self, target: str) -> bool:
        if not self.api.CredDeleteW(target, self.TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error == self.ERROR_NOT_FOUND:
                return False
            raise ctypes.WinError(error)
        return True


_STORE: SecretStore | None = None


def store() -> SecretStore:
    """The machine's store, made once."""
    global _STORE
    if _STORE is None:
        _STORE = SecretStore()
    return _STORE


def get(name: str) -> str | None:
    return store().get(name)


def put(name: str, value: str) -> None:
    store().put(name, value)


def remove(name: str) -> bool:
    return store().remove(name)
