"""Publisher plugins: what an agency, a vendor or a researcher publishes for programs (news,
releases, rates), read on a schedule of its own by the recorder (publications.py), never behind a
sync; and the datasets it puts out (sources/dataset.py), fetched by `datasets` (datasets.py).

A module in `sources/plugins/` defines `PUBLISHER` (a Publisher; `PUBLISHERS` for several), beside
or instead of a feed's `PLUGIN`: the federal agencies are parts of one plugin (the liminal-plugin-federal
package, a module each: fed.py, sec.py ...; `part_of`), a vendor's headlines sit in its feed's module
(massive.py), its requests on that feed's account, and a library of datasets is a publisher with no
feeds (the French and Datahub packages; the CFTC's in Federal).

Each declares its scrape checks (`Policy`), which the reader enforces before every request:
- only the hosts it names, over HTTPS, and never a blocked site (publications.BLOCKED_HOSTS: the
  sites whose terms bar programs, FINRA's, Yahoo's, Nasdaq.com's ...); a redirect elsewhere is not
  followed;
- feeds and APIs only: an answer that is not XML or JSON (a web page) is refused, never read;
- the host's robots.txt (read weekly; its crawl-delay kept), conditional requests, Retry-After;
- at most `per_day` requests a day and never a feed more often than `min_minutes`, whatever its
  schedule asks; a refusal holds it off for hours.
Its limits are verified again weekly and from the Plugins view (publications.recheck): robots.txt
read again and the rate-limit headers its servers send, the tighter of theirs and its own used.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET_XML
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

from ..layout import Database
from .dataset import Dataset
from .fetching import Fetcher
from .keys import KeyVar


@dataclass
class Read:
    """What a feed's answer holds (Feed.parse, API 1.7): items for the news and the Federal view
    ({id, agency, title, url, published, kind, tags}), and reference rates ({series, day, value,
    lo, hi}: the New York Fed's)."""
    items: list[dict] = field(default_factory=list)
    rates: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Feed:
    """One thing a publisher puts out for programs: an RSS or Atom feed, or a JSON API's answer."""
    key: str  # unique among all publishers' feeds (fed-press): its state and items are kept by it
    agency: str  # the name shown with its items (Fed, SEC ...)
    title: str
    url: str
    kind: str = "rss"  # rss | fedreg | nyfed (a JSON API's) | headlines (a vendor's, through its feed's client)
    every_minutes: float = 60.0  # read this often
    moments: bool = True  # read at the publisher's release moments too (its speeches: no)
    # its parser (API 1.7): parse(answer, feed) -> Read (or its items), the answer the HTTP response
    # (.content, .json()); None: an RSS or Atom feed (parse_rss), its items `item_kind`
    parse: "Callable[[object, Feed], Read | list[dict]] | None" = None
    item_kind: str = "press release"  # what an RSS feed's items are (speech, data release ...)


@dataclass(frozen=True)
class Policy:
    """A publisher's scrape checks (see the module's docstring)."""
    hosts: tuple[str, ...] = ()  # the only hosts it reads; empty for a vendor read through its feed's client
    min_minutes: float = 15.0  # never one feed more often than this
    per_day: int = 200  # requests a day, its feeds and robots.txt together
    contact: bool = False  # it asks automated readers to identify themselves (GOV_CONTACT, in the User-Agent)
    terms: str = ""  # what its terms say about programs, in a line
    terms_url: str = ""


@dataclass(frozen=True)
class Publisher:
    name: str
    title: str
    about: str
    feeds: tuple[Feed, ...]
    policy: Policy
    order: int = 100
    # official (an agency) | vendor (a data vendor's headlines, on its feed's account) | research (a
    # researcher's own library: Ken French's) | mirror (someone else's data republished: Datahub's)
    kind: str = "official"
    # a vendor's: the feed plugin whose key and limits its requests count against; that feed's
    # kill stops it, and it reads nothing without that feed's keys
    account: str = ""
    # extra reads at the publisher's release moments: the times (ET, aware) on a day it publishes
    # at a set time (the FOMC's statement at 2:00 p.m. on decision days)
    moments: Callable[[date], tuple[datetime, ...]] | None = None
    # its scheduled releases from a day on, for the views: [{"when": iso, "what": str}]
    calendar: Callable[[date], list[dict]] | None = None
    # a vendor's read: read(ctx) -> new items (ctx: publications.Context); official feeds are read
    # by the reader itself
    read: Callable[[object], int] | None = None
    # the plugin it is part of, when another publisher's (API 1.2; the Fed: Federal's): listed in it,
    # and stopped while that one is paused; its own scrape checks, state and pause still its own
    part_of: str = ""
    # the datasets it publishes (the CFTC's Commitments of Traders): fetched by `datasets`, shown with
    # it. API 1.4: each a Dataset (sources/dataset.py), declared here, its files only from its hosts and
    # stopped by its pause; a name (API 1.2) lists one another plugin declares
    datasets: tuple["Dataset | str", ...] = ()
    # what the user sets for it (API 1.3): a key, or a detail of theirs it sends where asked (the SEC:
    # GOV_CONTACT); user environment variables, set from the Plugins view or the key script
    keys: tuple[KeyVar, ...] = ()
    # databases its own code writes besides gov/gov.db and its datasets' files (API 1.8;
    # layout.Database): a vendor's `read` saving elsewhere (Massive's headlines: the main file's news)
    databases: tuple[Database, ...] = ()
    # its own fetching (API 1.9; sources/fetching.py); None: read by the app's news reader
    # (publications.py) and its datasets fetched by `datasets`
    fetcher: Fetcher | None = None
    # its own version and the plugin contract it was written for (sources/registry.py: PLUGIN_API)
    version: str = ""
    api: str = "1.0"
    origin: str = "built in"  # set when loaded

    def feed(self, key: str) -> Feed | None:
        return next((f for f in self.feeds if f.key == key), None)

    @property
    def dataset_names(self) -> tuple[str, ...]:
        return tuple(d if isinstance(d, str) else d.name for d in self.datasets)

    @property
    def declared(self) -> tuple["Dataset", ...]:
        """The datasets it declares itself (not those it only names)."""
        return tuple(d for d in self.datasets if not isinstance(d, str))


# -- the RSS and Atom reader every agency's feed shares (Feed.parse left out) -------------------------

def _iso(text: str | None) -> str | None:
    if not text:
        return None
    try:
        t = parsedate_to_datetime(text.strip())
    except (TypeError, ValueError):
        try:
            t = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_rss(data: bytes | str, feed: Feed) -> list[dict]:
    """An RSS 2.0 (or Atom) feed's items: id (guid or link), title, link, when published. Read
    from the bytes, so the XML's own declaration (and a byte-order mark) sets the encoding: the
    Fed's feeds start with one and their HTTP header names no charset (decoded as Latin-1, the
    first character was unreadable)."""
    root = ET_XML.fromstring(data)
    out = []
    for it in root.iter():
        tag = it.tag.rsplit("}", 1)[-1]
        if tag not in ("item", "entry"):
            continue
        get = {c.tag.rsplit("}", 1)[-1]: c for c in it}
        title = (get["title"].text or "").strip() if "title" in get else ""
        link = get.get("link")
        url = ((link.text or "").strip() or link.get("href")) if link is not None else None
        guid = (get["guid"].text or "").strip() if "guid" in get else (get["id"].text or "").strip() if "id" in get else ""
        when = next((get[k].text for k in ("pubDate", "published", "updated", "date") if k in get), None)
        if title:
            out.append({"id": guid or url or title, "agency": feed.agency, "title": " ".join(title.split()),
                        "url": url, "published": _iso(when), "kind": feed.item_kind})
    return out
