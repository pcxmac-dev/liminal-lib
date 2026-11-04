"""The vendor pages an AI plugin's tables were written from, read again (plugin API 2.0, assistants):
the plugin's weekly CI test (LIMINAL_VENDOR_PAGES=1) fetches each Source's page and looks for its
sentinel and its markers (a model's id with its prices, a plan's title with its limits); a marker gone
means the table is stale, and the test's failure names it. The library names no vendor host: the
pages are the plugin's (AssistantPlugin.sources)."""
from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from typing import Callable, Iterable

USER_AGENT = "liminal/1 (personal research; checks a vendor's published rates)"


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def text_of(page: str) -> str:
    """A page's text, tags and scripts stripped, whitespace folded (a markdown page is left as it is)."""
    if "<" in page and ">" in page and re.search(r"<\s*(html|body|div|p|table|h\d)\b", page, flags=re.IGNORECASE):
        parser = _Text()
        parser.feed(page)
        page = " ".join(parser.parts)
    return re.sub(r"\s+", " ", html.unescape(page)).strip()


def page_text(url: str, session=None, timeout: float = 30.0) -> str:
    """A public page's text (GET once; a markdown page served as text is kept as it is)."""
    if session is None:
        import requests

        session = requests.Session()
    resp = session.get(url, headers={"User-Agent": USER_AGENT, "Accept": "text/markdown, text/html;q=0.9, */*;q=0.5"},
                       timeout=timeout)
    if resp.status_code >= 400:
        raise RuntimeError(f"{url}: HTTP {resp.status_code}")
    return text_of(resp.text)


def missing(text: str, values: Iterable[str]) -> list[str]:
    """The values not in the text (whitespace folded on both sides, case kept)."""
    folded = re.sub(r"\s+", " ", text)
    return [v for v in values if re.sub(r"\s+", " ", v).strip() not in folded]


def check_sources(plugin, fetch: Callable[[str], str] = page_text) -> list[str]:
    """Each source page of a plugin read and checked: the problems found, one line each ("" when the
    tables still match the pages). A page that cannot be read is a problem too (a moved page is a
    change to look at)."""
    out = []
    for s in plugin.sources:
        try:
            text = fetch(s.url)
        except Exception as e:  # noqa: BLE001 - the page is the check's subject: its failure is the finding
            out.append(f"{s.url} ({s.holds}): could not be read: {type(e).__name__}: {e}")
            continue
        gone = missing(text, [s.sentinel])
        if gone:
            out.append(f"{s.url} ({s.holds}): the page changed: its sentinel {s.sentinel!r} is gone")
        lost = missing(text, s.markers)
        if lost:
            out.append(f"{s.url} ({s.holds}): the table is stale: {', '.join(repr(m) for m in lost)} no longer on the page")
    return out
