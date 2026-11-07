"""DKIM (RFC 6376) for what the mailer sends: the app signs its own messages for the user's domain, so the
receiving server can see they are the domain's (SPF or DKIM aligned: DMARC passes) even from a home
connection, given one DNS record at the domain: `<selector>._domainkey.<domain> TXT "v=DKIM1; k=rsa;
p=<public key>"`. The user, 2026-11-06: "it looks like i need a domain/email server for this to work": the
domain is theirs, the server is this. RSA-SHA256, relaxed/relaxed canonicalization; the headers signed are
From, To, Subject, Date and Message-ID. `cryptography` does the arithmetic (imported when used)."""
from __future__ import annotations

import base64
import hashlib
import re
import subprocess
import time

SIGNED_HEADERS = ("from", "to", "subject", "date", "message-id")


def generate_key(bits: int = 2048) -> tuple[bytes, str]:
    """A new RSA key: (the private key as PEM bytes, the public key for the DNS record as base64)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=bits)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return pem, _public_b64(key.public_key())


def public_of(pem: bytes) -> str:
    """The public key (base64, for the DNS record) of a private key PEM."""
    from cryptography.hazmat.primitives import serialization

    return _public_b64(serialization.load_pem_private_key(pem, password=None).public_key())


def dns_record(domain: str, selector: str, public_b64: str) -> tuple[str, str]:
    """The TXT record to publish: (its name, its value)."""
    return f"{selector}._domainkey.{domain}", f"v=DKIM1; k=rsa; p={public_b64}"


def sign(raw: bytes, domain: str, selector: str, private_pem: bytes, now: float | None = None) -> str:
    """The DKIM-Signature header's value for `raw` (the message as bytes, without that header), signed with
    `private_pem` for `domain` under `selector`."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    headers, body = _split(raw)
    bh = base64.b64encode(hashlib.sha256(_relaxed_body(body).encode("latin-1")).digest()).decode()
    present = [h for h in SIGNED_HEADERS if any(n.lower() == h for n, _ in headers)]
    tags = (f"v=1; a=rsa-sha256; c=relaxed/relaxed; d={domain}; s={selector}; t={int(now if now is not None else time.time())}; "
            f"h={':'.join(present)}; bh={bh}; b=")
    data = _signed_data(headers, present, tags)
    key = serialization.load_pem_private_key(private_pem, password=None)
    signature = base64.b64encode(key.sign(data, padding.PKCS1v15(), hashes.SHA256())).decode()
    # the b= value in pieces with a space between (whitespace in it is ignored by every verifier), so the
    # header folds at spaces: a single long word would be folded as an encoded word, which breaks it
    return tags + " ".join(signature[i:i + 72] for i in range(0, len(signature), 72))


def verify(raw: bytes, public_b64: str) -> bool:
    """Whether `raw` (a message carrying a DKIM-Signature header) is signed by the key `public_b64` and
    unchanged since: what the receiving server does with the DNS record."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    headers, body = _split(raw)
    found = [v for n, v in headers if n.lower() == "dkim-signature"]
    if not found:
        return False
    value = found[-1]
    tags = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            tags[k.strip()] = re.sub(r"\s+", "", v)
    if tags.get("v") != "1" or tags.get("a") != "rsa-sha256" or tags.get("c", "simple/simple") != "relaxed/relaxed":
        return False
    if tags.get("bh") != base64.b64encode(hashlib.sha256(_relaxed_body(body).encode("latin-1")).digest()).decode():
        return False
    present = [h for h in tags.get("h", "").lower().split(":") if h]
    unsigned = re.sub(r"(^|;)(\s*b=)[^;]*", r"\1\2", value)  # the b= value left out, as when it was signed
    data = _signed_data(headers, present, unsigned)
    try:
        key = serialization.load_der_public_key(base64.b64decode(public_b64))
        key.verify(base64.b64decode(tags.get("b", "")), data, padding.PKCS1v15(), hashes.SHA256())
    except (InvalidSignature, ValueError):
        return False
    return True


def published(name: str) -> str | None:
    """The public key (its p= value) in the TXT record `name` as the machine's resolver sees it (nslookup,
    windowless); None when no DKIM record is found."""
    if not re.fullmatch(r"[a-z0-9_-]+(\.[a-z0-9_-]+)+", name.strip().strip(".").lower()):
        return None
    try:
        out = subprocess.run(["nslookup", "-type=txt", name], capture_output=True, text=True, timeout=15,
                             **_no_window()).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return None
    for record in re.findall(r'((?:"[^"]*"\s*)+)', out):  # the quoted strings of one record, joined
        text = "".join(re.findall(r'"([^"]*)"', record))
        if "v=DKIM1" in text.replace(" ", ""):
            m = re.search(r"p=([^;]*)", text)
            if m:
                return re.sub(r"\s+", "", m.group(1))
    return None


def _public_b64(public_key) -> str:
    from cryptography.hazmat.primitives import serialization

    return base64.b64encode(public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()


def _split(raw: bytes) -> tuple[list[tuple[str, str]], str]:
    """The headers (name, value as written, folded lines kept) and the body, lines ending CRLF."""
    text = re.sub(r"\r?\n", "\r\n", raw.decode("latin-1"))
    head, sep, body = text.partition("\r\n\r\n")
    headers: list[tuple[str, str]] = []
    for line in head.split("\r\n"):
        if line[:1] in (" ", "\t") and headers:
            name, value = headers[-1]
            headers[-1] = (name, value + "\r\n" + line)
        elif ":" in line:
            name, value = line.split(":", 1)
            headers.append((name, value))
    return headers, body if sep else ""


def _relaxed_header(name: str, value: str) -> str:
    """One header in relaxed form: the name lowercased, the value unfolded, its whitespace runs one space,
    none at either end, none around the colon; ending CRLF."""
    value = re.sub(r"[ \t]+", " ", value.replace("\r\n", "")).strip(" \t")
    return f"{name.strip().lower()}:{value}\r\n"


def _relaxed_body(body: str) -> str:
    """The body in relaxed form: no whitespace at line ends, whitespace runs one space, no empty lines at
    the end; CRLF after each line, nothing at all for an empty body."""
    lines = [re.sub(r"[ \t]+", " ", line.rstrip(" \t")) for line in body.split("\r\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "".join(line + "\r\n" for line in lines)


def _signed_data(headers: list[tuple[str, str]], present: list[str], signature_value: str) -> bytes:
    """What is signed: the listed headers in relaxed form (the last instance of each), then the
    DKIM-Signature header itself in relaxed form with its b= value empty and no CRLF after it."""
    data = ""
    for h in present:
        instances = [hv for hv in headers if hv[0].strip().lower() == h]
        if instances:
            data += _relaxed_header(*instances[-1])
    data += _relaxed_header("DKIM-Signature", signature_value).rstrip("\r\n")
    return data.encode("latin-1")


def _no_window() -> dict:
    import os

    return {"creationflags": 0x08000000} if os.name == "nt" else {}
