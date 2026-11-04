"""What a publisher plugin declares for a dataset it puts out (Publisher.datasets, plugin API 1.4):
research and reference files downloaded whole and kept locally, fetched, versioned and shown by the
app (datasets.py; `datasets fetch`, then on their own cadence). A dataset's files come only from its
publisher's hosts (Policy.hosts: a GitHub submodule's from github.com), never a blocked site, and
pausing its plugin stops them.

The helpers below are for a dataset's parser and its publication rule: days are US/Eastern
midnights, times milliseconds since the epoch."""
from __future__ import annotations

import io
import math
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# Times before 1970 (Shiller's data starts in 1871, French's in 1926) are reached by arithmetic from
# here: datetime.fromtimestamp refuses a negative time on Windows up to Python 3.14 (OSError 22).
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class Parsed:
    """What a dataset's parser reads from one file: values of its series, and index members."""
    values: list[tuple[str, int, float]] = field(default_factory=list)  # (series, ts ms, value)
    series: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # series -> (title, unit, freq)
    constituents: list[dict] = field(default_factory=list)


@dataclass(frozen=True)
class Dataset:
    name: str  # unique among every plugin's datasets: its state and versions are kept by it
    title: str
    licence: str
    files: Callable[[date], list[tuple[str, bool]]]  # today -> [(url, final: closes once imported)]
    parse: Callable[[str, bytes], Parsed]  # (url or path, the file's bytes) -> what it holds
    refresh_days: int  # how often the files that are not final are fetched again
    provider: str      # whose data it is: names its file, datasets/<provider>.db (one plugin's only)
    kind: str          # provenance for the quality prior (composite.PRIORS): official | research | mirror
    # When a value of the period starting at ts (ms) was first published, by the source's schedule:
    # dates the history in a file's first import. None: the file's own time (or members' 'date added').
    published: Callable[[int], int] | None = None
    git: tuple[str, str] | None = None  # (owner/repo on GitHub, path of the file in it): a git submodule
    index: str = ""  # the index whose members it lists (S&P 500), when it lists members
    publisher: str = ""  # the publisher plugin declaring it: set when the app collects it


def ms(d: date) -> int:
    """A day's US/Eastern midnight, in ms."""
    return int(datetime(d.year, d.month, d.day, tzinfo=ET).timestamp() * 1000)


def day_of(ts: int) -> date:
    """The US/Eastern day of a time in ms (any year: EPOCH)."""
    return (EPOCH + timedelta(milliseconds=ts)).astimezone(ET).date()


def at(d: date, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(d.year, d.month, d.day, hour, minute, tzinfo=ET).timestamp() * 1000)


def months_after(months: int, day: int) -> Callable[[int], int]:
    """A publication rule: on `day` of the month `months` after the period's month (US/Eastern midnight)."""
    def rule(ts: int) -> int:
        d = day_of(ts)
        m = d.month - 1 + months
        return at(date(d.year + m // 12, m % 12 + 1, day))
    return rule


def num(cell) -> float | None:
    """A finite number from a cell, else None (blank, 'nan', text)."""
    try:
        v = float(cell)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def text(blob: bytes, url: str) -> str:
    """A file's text: a .zip's first member (Latin-1), or the file itself (UTF-8, its BOM dropped)."""
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as z:
            return z.read(z.namelist()[0]).decode("latin-1")
    return blob.decode("utf-8-sig", errors="replace")
