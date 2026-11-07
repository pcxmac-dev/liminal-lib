"""What the machine's own resolver says about a name (nslookup, windowless): the addresses of a host, the
mail exchangers of a domain, the text records of a name. For the login's checks of its own mail host (the
user, 2026-11-06: an admin account "would need to be valid in the DNS record"). Nothing here is cached:
a check wants the answer now."""
from __future__ import annotations

import os
import re
import subprocess

NAME_FORM = r"[a-z0-9_-]+(\.[a-z0-9_-]+)+"
IP_FORM = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9a-f:]+:[0-9a-f:]*)$", re.I)


def valid_name(name: str) -> str:
    """The name lowercased without a trailing dot, or "" when it is no DNS name."""
    name = name.strip().strip(".").lower()
    return name if re.fullmatch(NAME_FORM, name) else ""


def query(name: str, rtype: str) -> str | None:
    """nslookup's answer for `name` of `rtype` (a, aaaa, mx, txt), None when it could not run."""
    try:
        return subprocess.run(["nslookup", f"-type={rtype}", name], capture_output=True, text=True, timeout=15,
                              **_no_window()).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None


def addresses(host: str) -> list[str] | None:
    """The addresses (A and AAAA) of `host`: [] when it has none, None when it is no name or the lookup
    could not run."""
    host = valid_name(host)
    if not host:
        return None
    found: list[str] = []
    for rtype in ("a", "aaaa"):
        out = query(host, rtype)
        if out is None:
            return None
        found += _addresses_in(out)
    return list(dict.fromkeys(found))


def _addresses_in(out: str) -> list[str]:
    """The addresses after the first "Name:" line (the ones before are the resolver's own)."""
    seen_name = False
    found = []
    for line in out.splitlines():
        s = line.strip()
        if s.lower().startswith("name:"):
            seen_name = True
            continue
        if not seen_name:
            continue
        if s.lower().startswith(("address:", "addresses:")):
            s = s.split(":", 1)[1].strip()
        if s and IP_FORM.match(s):
            found.append(s.lower())
    return found


def mx_records(domain: str) -> list[str] | None:
    """The mail exchangers in `domain`'s mail record by preference: [] when it has none, None when it is no
    name or the lookup could not run."""
    domain = valid_name(domain)
    if not domain:
        return None
    out = query(domain, "mx")
    if out is None:
        return None
    found = []
    for line in out.splitlines():  # Windows: "MX preference = 10, mail exchanger = h"; Linux: "mail exchanger = 10 h."
        m = re.search(r"MX preference = (\d+), mail exchanger = (\S+)", line) or re.search(r"mail exchanger = (\d+) (\S+)", line)
        if m:
            found.append((int(m.group(1)), m.group(2).rstrip(".").lower()))
    return list(dict.fromkeys(h for _, h in sorted(found)))


def txt_records(name: str) -> list[str] | None:
    """The text records of `name`, each one's quoted strings joined: [] when it has none, None when it is
    no name or the lookup could not run."""
    name = valid_name(name)
    if not name:
        return None
    out = query(name, "txt")
    if out is None:
        return None
    return ["".join(re.findall(r'"([^"]*)"', record)) for record in re.findall(r'((?:"[^"]*"\s*)+)', out)]


def spf_record(domain: str) -> str | None:
    """The SPF record of `domain` ("" when it has none; None when the lookup could not run)."""
    records = txt_records(domain)
    if records is None:
        return None
    return next((r for r in records if r.replace(" ", "").lower().startswith("v=spf1")), "")


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if os.name == "nt" else {}  # CREATE_NO_WINDOW: no console pops up
