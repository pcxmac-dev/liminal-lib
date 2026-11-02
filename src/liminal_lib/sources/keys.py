"""What a plugin needs the user to set: its keys and the user's details it sends (KeyVar), each a user
environment variable, declared by the plugin that reads it (Provider.keys, Publisher.keys) and set
from the Plugins view (POST /api/plugins/key) or the key script (tools/set-key.ps1). A value never
leaves: the views say only whether it is set."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping

MAX_LENGTH = 512  # a key or a detail longer than this is a paste gone wrong
NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
# never a plugin's to set: Windows' and Python's own, and the app's own settings (config.py)
SYSTEM = frozenset({"PATH", "PATHEXT", "TEMP", "TMP", "HOME", "HOMEDRIVE", "HOMEPATH", "USERPROFILE", "USERNAME",
                    "USERDOMAIN", "APPDATA", "LOCALAPPDATA", "COMSPEC", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
                    "PROGRAMDATA", "ALLUSERSPROFILE", "PUBLIC", "PSMODULEPATH", "PROMPT", "OS", "DRIVERDATA",
                    "COMPUTERNAME", "LOGONSERVER", "SESSIONNAME", "ONEDRIVE", "NUMBER_OF_PROCESSORS",
                    "VIRTUAL_ENV", "CONDA_PREFIX", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"})
SYSTEM_PREFIXES = ("PYTHON", "PROCESSOR_", "PROGRAMFILES", "COMMONPROGRAMFILES", "PIP_", "UV_")
APP = frozenset({"LIMINAL_DB", "LIMINAL_OFFLINE", "LIMINAL_REFRESH_HOURS", "SOURCE_PRIORITY", "AUDITS_PER_RUN",
                 "AUDITS_RANDOM_PER_RUN", "TICK_HISTORY_DAYS", "SECOND_HISTORY_DAYS", "BACKUPS_KEEP", "TICK_MIN_FREE_GB",
                 "LIVE_CRYPTO", "LIVE_STOCKS", "LIVE_STREAMS", "BARS_ONLY", "CHART_CACHE_MB", "FUTURES_PER_RUN"})


@dataclass(frozen=True)
class KeyVar:
    """A variable a plugin reads, from the user's environment variables only: a key (`secret`: typed
    hidden), or a detail of yours it sends where asked (GOV_CONTACT: your name and email, to the SEC)."""
    name: str
    label: str = "API key"
    aliases: tuple[str, ...] = ()  # also read (Alpaca's own APCA_ names); the key script sets `name`
    secret: bool = True  # typed hidden; False: a detail of yours, typed in the open (never shown back either)
    hint: str = ""  # what to type (its form), for the Plugins view and the key script

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def value(self, env: Mapping[str, str]) -> str | None:
        return next((env[n] for n in self.names if env.get(n)), None)


def clean(value: str) -> str:
    """A value typed for a variable, trimmed; ValueError when it cannot be one (empty, too long, or
    holding a line break or another control character)."""
    out = str(value).strip()
    if not out:
        raise ValueError("nothing typed")
    if len(out) > MAX_LENGTH:
        raise ValueError(f"longer than {MAX_LENGTH} characters")
    if any(ord(c) < 32 or ord(c) == 127 for c in out):
        raise ValueError("it holds a line break or a control character")
    return out


def refusal(name: str) -> str | None:
    """Why a plugin may not declare (nor the Plugins view save) a variable of this name, or None: one
    not in capitals and underscores, Windows' or Python's own (PATH ...), or the app's own setting."""
    if not NAME.match(name or ""):
        return f"{name!r} is not a variable name for a plugin (capitals, digits and underscores)"
    if name in SYSTEM or name.startswith(SYSTEM_PREFIXES):
        return f"{name} is Windows' or Python's own: no plugin sets it"
    if name in APP:
        return f"{name} is the app's own setting: no plugin sets it"
    return None
