"""What a login is tied to, and what every app reads to know which login it binds to (the user,
2026-11-06: "have like a setup.cfg for the login, and then like other web apps use that cfg file to decide
the domain/scope of the login"; "lets have login apps tied to domains (with email server) or local
(file://project/folder)"; "use a url regardless, file if local, domain w/ email server in mail record
(lookup) or specified if none in the domain lookup"): `liminal-login.cfg`, an INI file whose [login] scope
is a URL. `file:///...` is a project folder: a local login, no email and no sign-up (the first user is made on
the page or at the command line, the next by a signed-in user). `https://<domain>` is the domain's users:
their codes go through the domain's own mail server, the one its mail record (MX) names, unless the [mail]
section names a server to send through. `Scope.find` reads the file every app looks for the same way:
LIMINAL_LOGIN_CFG, else `liminal-login.cfg` in the workspace, in the working folder or one above it, else in
the data folder's login/."""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from .. import layout

FILE_NAME = "liminal-login.cfg"
CFG_VAR = "LIMINAL_LOGIN_CFG"
MAIL_KEYS = ("host", "port", "security", "user", "from", "helo", "dkim_key", "dkim_selector", "dkim_domain")
MEMBERS = ("domain", "any")
_DOMAIN = r"[a-z0-9-]+(\.[a-z0-9-]+)+"


@dataclass(frozen=True)
class Scope:
    url: str
    title: str = ""
    serves: tuple[str, ...] = ("charts",)  # the apps (their users' namespaces) this login serves
    port: int | None = None
    members: str = "domain"  # a domain's login: "domain" (addresses at the domain only) or "any"
    data: Path | None = None  # where the users' folders live; None: by the kind (data_dir)
    mail: dict = field(default_factory=dict)  # the [mail] section: host, port, security, user, from, helo, dkim_*
    path: Path | None = None  # the file read, when one was

    @property
    def kind(self) -> str:
        return "local" if self.url.lower().startswith("file:") else "domain"

    @property
    def root(self) -> Path | None:
        """A local login's project folder (the file URL as a path)."""
        if self.kind != "local":
            return None
        parts = urlparse(self.url)
        raw = parts.path if not parts.netloc or parts.netloc == "localhost" else f"//{parts.netloc}{parts.path}"
        return Path(url2pathname(raw))

    @property
    def domain(self) -> str:
        if self.kind != "domain":
            return ""
        return (urlparse(self.url).hostname or "").lower().strip(".")

    @property
    def name(self) -> str:
        """The scope's short name: the domain, or the project folder's name."""
        if self.kind == "domain":
            return self.domain
        return self.root.name if self.root is not None and self.root.name else "local"

    @property
    def label(self) -> str:
        """For a page: `@hypokrites.me`, or `local to C:\\...\\liminal`."""
        return f"@{self.domain}" if self.kind == "domain" else f"local to {self.root}"

    def data_dir(self, default: Path | None = None) -> Path:
        """Where the users' folders live: `data` when set; a local login's project folder (its
        store/data/login when it has a store/data, else .liminal/login inside it); a domain's `default` (the
        data folder's login/), where the app keeps a folder per domain."""
        if self.data is not None:
            return self.data
        if self.kind == "local" and self.root is not None:
            store = self.root / "store" / "data"
            return store / "login" if store.is_dir() else self.root / ".liminal" / "login"
        return default if default is not None else layout.data_dir() / "login"

    def mail_variables(self) -> dict[str, str]:
        """The [mail] section as the mailer's variables (LIMINAL_SMTP_*). A domain's login with no server
        named sends straight to each address's own mail server (direct), from login@<domain>."""
        out = {f"LIMINAL_SMTP_{k.upper()}": str(v).strip() for k, v in self.mail.items() if k in MAIL_KEYS and str(v).strip()}
        if self.kind == "domain":
            out.setdefault("LIMINAL_SMTP_HOST", "direct")
            out.setdefault("LIMINAL_SMTP_FROM", f"login@{self.domain}")
        return out

    def describe(self) -> dict:
        return {"url": self.url, "kind": self.kind, "name": self.name, "label": self.label, "title": self.title or self.name,
                "serves": list(self.serves), "members": self.members if self.kind == "domain" else "local",
                "root": str(self.root) if self.root is not None else None, "domain": self.domain,
                "mailServer": (self.mail.get("host") or "the domain's own mail record") if self.kind == "domain" else "",
                "path": str(self.path) if self.path is not None else None}

    @classmethod
    def local(cls, root: Path, **kw) -> "Scope":
        return cls(Path(root).resolve().as_uri(), **kw)

    @classmethod
    def for_domain(cls, domain: str, **kw) -> "Scope":
        return cls(f"https://{domain.strip().lower().strip('.')}", **kw)

    @classmethod
    def parse(cls, text: str, path: Path | None = None) -> "Scope":
        cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=(";", "#"))
        cfg.read_string(text)
        where = path or FILE_NAME
        if "login" not in cfg or not cfg["login"].get("scope", "").strip():
            raise ValueError(f"{where}: a [login] section with `scope = file:///... or https://<domain>` is needed")
        login = cfg["login"]
        url = login["scope"].strip()
        if "://" not in url and not url.lower().startswith("file:"):
            url = f"https://{url}"
        if not url.lower().startswith("file:"):  # a domain: its name alone, lowercased
            url = f"https://{(urlparse(url).hostname or '').lower().strip('.')}"
        serves = tuple(s.strip().lower() for s in login.get("serves", "charts").replace(";", ",").split(",") if s.strip()) or ("charts",)
        port = login.get("port", "").strip()
        data = login.get("data", "").strip()
        members = (login.get("members", "domain").strip().lower()) or "domain"
        if members not in MEMBERS:
            raise ValueError(f"{where}: members is domain or any, not {members!r}")
        mail = {k.strip().lower(): v.strip() for k, v in cfg["mail"].items()} if "mail" in cfg else {}
        scope = cls(url, title=login.get("title", "").strip(), serves=serves, port=int(port) if port.isdecimal() else None,
                    members=members, data=Path(data).expanduser() if data else None, mail=mail, path=Path(path) if path else None)
        if scope.kind == "domain" and not scope.domain:
            raise ValueError(f"{where}: the scope names no domain: {url}")
        return scope

    @classmethod
    def read(cls, path: Path) -> "Scope":
        path = Path(path)
        return cls.parse(path.read_text(encoding="utf-8"), path)

    @classmethod
    def find(cls, start: Path | None = None, env=None) -> "Scope | None":
        """The file every app reads: LIMINAL_LOGIN_CFG, else liminal-login.cfg in the workspace, in `start`
        (the working folder) or one above it, else in the data folder's login/; None when there is none."""
        env = os.environ if env is None else env
        named = (env.get(CFG_VAR) or "").strip()
        if named:
            return cls.read(Path(named).expanduser())
        places: list[Path] = []
        home = layout.home()
        if home is not None:
            places.append(home / FILE_NAME)
        here = Path(start) if start is not None else Path.cwd()
        places.extend(p / FILE_NAME for p in (here, *here.parents))
        places.append(layout.data_dir() / "login" / FILE_NAME)
        for p in places:
            if p.is_file():
                return cls.read(p)
        return None

    def text(self) -> str:
        """The file's text."""
        lines = ["[login]", f"; what this login is tied to: file:///a/project/folder (local: no email, no sign-up) or https://<your-domain>",
                 f"scope = {self.url}"]
        if self.title:
            lines.append(f"title = {self.title}")
        lines.append(f"serves = {', '.join(self.serves)}")
        if self.port:
            lines.append(f"port = {self.port}")
        if self.kind == "domain":
            lines.append("; members: domain (addresses at the domain only) or any (everyone)")
            lines.append(f"members = {self.members}")
        if self.data is not None:
            lines.append(f"data = {self.data}")
        lines += ["", "[mail]"]
        if self.kind == "domain":
            lines.append("; the email server the codes go through; nothing here: the domain's own mail record (MX) is looked up")
        else:
            lines.append("; a local login sends no email")
        for k in MAIL_KEYS:
            v = self.mail.get(k, "")
            lines.append(f"{k} = {v}" if v else f"; {k} =")
        lines.append("; the password: `liminal-login mail setup` keeps it in Windows Credential Manager, never here")
        return "\n".join(lines) + "\n"

    def write(self, path: Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.text(), encoding="utf-8")
        return path
