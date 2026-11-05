"""The data specs (plugin API 2.1): what each kind of data a plugin delivers is, how complete and
fresh a frame of it is, and whether its rows are sound.

The user, 2026-11-03: "every plugin needs to furnish the correct mechanisms for assessing data, data
integrity, provide a 'spec' for the data, different source plugins like massive and Tingo can
inherit 'stock_symbol_data'. obviously the api for each source provider is different so the calls
are inherent (external calls to source) of the plugin itself." So the fetching stays in each
plugin's DataSource (sources/base.py), and what it fetches is described and judged here, by a class
per kind of data, three methods each:

- `spec()`: what a row is (the fields and their dtypes), the resolutions, the sessions and the
  calendar, the adjustments, the symbol form; for a document its parts; and where each product goes
  (`products`: the store each is kept in).
- `assess(frame, since, until)`: the frame against the spec over a window: what is held and what is
  missing (`held` and `gaps`, in the coverage ledger's form: coverage.Planner can subtract them),
  completeness (the share of the bars the calendar expects that are there), latency (how far the
  newest row trails the window's end) and, with a plan (`tier`), freshness's state (empty, behind,
  current): what the coverage planner and the integrator's assessor read.
- `integrity(frame)`: the rows judged: malformed and spiky bars by the bad-data rules (baddata.malformed,
  baddata.spikes, called, not copied), duplicates, rows outside the sessions, gaps inside them, a
  required field missing. The rule that needs the store (a bar against its own finer bars,
  baddata.inconsistent) stays in the store's scan, reached through `scan`; two feeds' copies of a
  span are compared with `compare` (audit.compare).

Two families (the user, the same evening: "current data types supported are 'documents' which have
paragraphs, pages and statements" "and time series data for stocks" "or what ever asset"):

- `TimeSeriesData`, the feeds' bars and ticks: `StockSymbolData` (the user's stock_symbol_data),
  `IndexData`, `FxData`, `CryptoSymbolData`, `FutureData`, one per asset class; a vendor whose data
  differs subclasses one (Massive's SIP tape with ticks, Tiingo's split-adjusted daily closes,
  Alpaca's IEX stream) and its feed plugin declares it (`FeedPlugin.data = {"stock": MassiveStockData}`);
  without a declaration the contract gives the asset class's (`FeedPlugin.data_for`).
- `DocumentData`, the publishers' documents, with their parts `Paragraph`, `Page` and `Statement`:
  `ReleaseData` (an agency's releases, rules and speeches), `HeadlineData` (a vendor's headlines),
  `FilingData` (a filing's pages and the statements parsed from them); a publisher declares its own
  (`PublisherPlugin.data`) or takes the default by its kind (`PublisherPlugin.document_data`).

Storage (the user: "mongo is used to store the original source document, while a different database
is used to evaluate or derive information from the original document"): each class names its
products and where each goes (`Product`). The original goes untouched to its store (a document to
MongoDB, or SQLite until it moves: stores.py; a feed's bars to its SQLite file and its closed
months to DuckDB); what is evaluated or derived from it (a statement parsed from a filing, the AI
evaluations, the bars built here, the bad-data list) is a product of its own, kept apart in
SQLite or DuckDB, keyed to the original and never written back into it (`derived`, `keyed_to`;
`check()` holds a class to it). Nothing moves today: the classes say where each product belongs.

This module imports no registry and no plugin: the contract (sources/plugin.py) reaches it when a
plugin's data class is asked for.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Iterable, Mapping
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .. import baddata, freshness
from ..audit import compare as compare_copies
from ..storage.documents import GOV_ITEMS, NEWS, Kind

ET = ZoneInfo("America/New_York")
UTC = "UTC"
NOTHING: Mapping = MappingProxyType({})
STEP_MS = baddata.STEP_MS  # second, minute, day: the base frames the feeds send (the rest are built here)
BASES = baddata.BASES
UTC_DAY_ASSETS = ("fx", "crypto")  # their day bars are UTC days (coverage.UTC_DAY_ASSETS)
# The bars as the feeds send them (DataSource.bar_pages) and as they are stored (storage/feeds.py)
WIRE = {"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap", "n": "transactions"}
COLUMNS = tuple(WIRE.values())
TICK_WIRE = {"sip_timestamp": "ts_ns", "sequence_number": "seq", "price": "price", "size": "size", "exchange": "exchange",
             "conditions": "conditions", "id": "trade_id", "tape": "tape"}
SEVERE = ("malformed", "spike", "duplicate", "out_of_session", "missing")  # a series' findings that make it not ok


# -- what a spec is made of ------------------------------------------------------------------------

@dataclass(frozen=True)
class Field:
    """One field of a row: its name as stored, its dtype, its unit, whether a row must have it."""
    name: str
    dtype: str  # int64 | float64 | str | bool
    unit: str = ""
    required: bool = True
    about: str = ""

    def as_dict(self) -> dict:
        return {"name": self.name, "dtype": self.dtype, "unit": self.unit, "required": self.required, "about": self.about}


@dataclass(frozen=True)
class Product:
    """One product of a kind of data and where it goes: the original as fetched, or something
    evaluated or derived from it (`derived`), kept apart and keyed to the original (`keyed_to`), never
    written back into it. `dtype` is the Store picker's data type it is picked under (stores.TYPES:
    bars, documents, series, files), `engines` the engines it can be kept in, the optimal first."""
    name: str
    dtype: str
    engines: tuple[str, ...]
    holds: str
    where: str = ""  # the file, folder or collection it is in, in the data folder's terms
    derived: bool = False
    keyed_to: str = ""  # the original product it points to, when derived

    def as_dict(self) -> dict:
        return {"name": self.name, "dtype": self.dtype, "engines": list(self.engines), "holds": self.holds, "where": self.where,
                "derived": self.derived, "keyedTo": self.keyed_to}


@dataclass(frozen=True)
class Spec:
    """What a kind of data is: its family (time-series, document), its kind (an asset class, a
    document kind), the fields of a row, and its products with their stores."""
    family: str
    kind: str
    fields: tuple[Field, ...]
    products: tuple[Product, ...]
    notes: tuple[str, ...] = ()

    @property
    def field_names(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields)

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    def product(self, name: str) -> Product | None:
        return next((p for p in self.products if p.name == name), None)

    def as_dict(self) -> dict:
        return {"family": self.family, "kind": self.kind, "fields": [f.as_dict() for f in self.fields],
                "products": [p.as_dict() for p in self.products], "notes": list(self.notes)}


@dataclass(frozen=True)
class SeriesSpec(Spec):
    """A time series' spec: the resolutions its source delivers (the frames above are built here,
    derive.STANDARD), the sessions and the calendar they keep, the zone a day bar is stamped in, the
    adjustments its prices carry, the symbol form, what market its data is, and its tick rows."""
    resolutions: tuple[str, ...] = ()
    calendar: str = ""  # the sessions kind freshness.CALENDAR takes (an asset class, or a feed's own: overnight)
    sessions: str = ""  # the sessions in words
    day_zone: str = "America/New_York"
    adjustments: tuple[str, ...] = ()
    symbol: str = ""
    market: str = ""
    ticks: bool = False
    tick_fields: tuple[Field, ...] = ()
    wire: Mapping[str, str] = NOTHING  # the fields as the source delivers them -> as stored

    def as_dict(self) -> dict:
        return {**super().as_dict(), "resolutions": list(self.resolutions), "calendar": self.calendar, "sessions": self.sessions,
                "dayZone": self.day_zone, "adjustments": list(self.adjustments), "symbol": self.symbol, "market": self.market,
                "ticks": self.ticks, "tickFields": [f.as_dict() for f in self.tick_fields], "wire": dict(self.wire)}


@dataclass(frozen=True)
class DocumentSpec(Spec):
    """A document kind's spec: its parts, the fields its key is made of and the field it is ordered
    by (the documents store's Kind: its table and collection), what a document must have, and what
    each named statement parsed from it must hold."""
    parts: tuple[str, ...] = ("paragraph", "page", "statement")
    key: tuple[str, ...] = ("source", "id")
    order: str = "published"
    table: str = ""
    collection: str = ""
    statements: Mapping[str, tuple[str, ...]] = NOTHING

    def as_dict(self) -> dict:
        return {**super().as_dict(), "parts": list(self.parts), "key": list(self.key), "order": self.order, "table": self.table,
                "collection": self.collection, "statements": {k: list(v) for k, v in self.statements.items()}}


# -- what an assessment and an integrity report are ------------------------------------------------

@dataclass(frozen=True)
class Assessment:
    """A frame against its spec over a window [since, until): what is there, what is missing, how
    fresh it is. Spans are half-open [start, end) ms, the coverage ledger's form."""
    kind: str
    timespan: str | None
    since_ms: int
    until_ms: int
    rows: int  # rows given
    present: int  # rows (documents) inside the window, one per expected slot at most
    expected: int | None  # slots the calendar expects in the window (documents: none)
    completeness: float | None  # present / expected, or the share of complete documents
    held: tuple[tuple[int, int], ...]
    gaps: tuple[tuple[int, int], ...]
    oldest_ms: int | None
    newest_ms: int | None
    latency_s: float | None  # how far the newest row's end trails the window's end
    state: str  # empty | behind | current
    expected_through_ms: int | None = None  # freshness: the newest bar the plan should have published
    next_due_ms: int | None = None
    rule: str = ""
    notes: tuple[str, ...] = ()

    @property
    def missing(self) -> int | None:
        return None if self.expected is None else self.expected - self.present

    def as_dict(self) -> dict:
        return {"kind": self.kind, "timespan": self.timespan, "sinceMs": self.since_ms, "untilMs": self.until_ms, "rows": self.rows,
                "present": self.present, "expected": self.expected, "missing": self.missing, "completeness": self.completeness,
                "held": [list(s) for s in self.held], "gaps": [list(s) for s in self.gaps], "oldestMs": self.oldest_ms,
                "newestMs": self.newest_ms, "latencyS": self.latency_s, "state": self.state,
                "expectedThroughMs": self.expected_through_ms, "nextDueMs": self.next_due_ms, "rule": self.rule,
                "notes": list(self.notes)}


@dataclass(frozen=True)
class Finding:
    """One thing a rule caught: the rule, where (a bar's time in ms, a document's key), the evidence."""
    rule: str
    at: int | str | None
    evidence: Mapping = NOTHING

    def as_dict(self) -> dict:
        return {"rule": self.rule, "at": self.at, "evidence": _plain(self.evidence)}


@dataclass(frozen=True)
class Integrity:
    """What the rules caught in a frame. `ok` when nothing severe was (a series' gaps are reported,
    not held against it: a thin stock has minutes without trades)."""
    kind: str
    rows: int
    findings: tuple[Finding, ...]
    severe: tuple[str, ...] = SEVERE

    @property
    def problems(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.rule in self.severe)

    @property
    def ok(self) -> bool:
        return not self.problems

    def count(self, rule: str) -> int:
        return sum(1 for f in self.findings if f.rule == rule)

    def by_rule(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.rule] = out.get(f.rule, 0) + 1
        return out

    def where(self, rule: str) -> tuple:
        return tuple(f.at for f in self.findings if f.rule == rule)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "rows": self.rows, "ok": self.ok, "byRule": self.by_rule(),
                "findings": [f.as_dict() for f in self.findings]}


def _plain(value):
    """A value as JSON would take it (numpy scalars and arrays, tuples, mappings)."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_plain(v) for v in value.tolist()]
    if isinstance(value, np.generic):
        return value.item()
    return value


def _ms(moment, zone=ET) -> int:
    """A moment in ms: an int or float (ms already), an aware datetime (a naive one is taken as UTC),
    a date (midnight in `zone`), a pandas Timestamp."""
    if isinstance(moment, (int, np.integer)):
        return int(moment)
    if isinstance(moment, float):
        return int(moment)
    if isinstance(moment, pd.Timestamp):
        moment = moment.to_pydatetime()
    if isinstance(moment, datetime):
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return int(moment.timestamp() * 1000)
    if isinstance(moment, date):
        return int(datetime.combine(moment, time(0), zone).timestamp() * 1000)
    raise TypeError(f"not a moment: {moment!r}")


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _runs(slots: np.ndarray, mask: np.ndarray, step: int) -> tuple[tuple[int, int, int], ...]:
    """The runs of `slots` where `mask` holds, each as (first slot, last slot + step, slots in it)."""
    idx = np.flatnonzero(mask)
    if not len(idx):
        return ()
    breaks = np.flatnonzero(np.diff(idx) != 1) + 1
    return tuple((int(slots[run[0]]), int(slots[run[-1]]) + step, len(run)) for run in np.split(idx, breaks))


def _spans(slots: np.ndarray, mask: np.ndarray, step: int) -> tuple[tuple[int, int], ...]:
    """The runs of `slots` where `mask` holds, each as [first slot, last slot + step): the ledger's spans."""
    return tuple((a, b) for a, b, _ in _runs(slots, mask, step))


# -- the time series' rows --------------------------------------------------------------------------

BAR_FIELDS = (
    Field("ts", "int64", "ms since the epoch (UTC)", True, "the bar's start; a day bar: midnight of its date in the day zone"),
    Field("open", "float64", "price", True),
    Field("high", "float64", "price", True),
    Field("low", "float64", "price", True),
    Field("close", "float64", "price", True),
    Field("volume", "float64", "shares, coins or contracts", True),
    Field("vwap", "float64", "price", False, "volume-weighted average price, where the source sends it"),
    Field("transactions", "int64", "trades", False, "trades in the bar, where the source sends it"),
)
TICK_FIELDS = (
    Field("ts_ns", "int64", "ns since the epoch (UTC)", True, "the SIP timestamp"),
    Field("seq", "int64", "", True, "the sequence number (a feed without one: the exchange's trade id and the exchange)"),
    Field("price", "float64", "price", True),
    Field("size", "float64", "shares or coins", True),
    Field("exchange", "int64", "", False, "the exchange's id (the exchanges table)"),
    Field("conditions", "str", "", False, "the sale conditions, comma-separated (ticks.py: which trades move a bar)"),
    Field("trade_id", "str", "", False),
    Field("tape", "int64", "", False, "1 and 2: the CTA letters; 3: UTP's"),
)
SERIES_PRODUCTS = (
    Product("bars", "bars", ("duckdb", "sqlite"), "the feed's own bars as it sent them, every version kept (bar_revisions); the open "
            "months in its SQLite file, its closed months in DuckDB (storage/history.py)", "feeds/<feed>.db; history/<feed>/<YYYY-MM>.<n>.duckdb"),
    Product("ticks", "ticks", ("sqlite",), "the feed's trades as it sent them", "feeds/<feed>-ticks.db"),
    Product("built", "bars", ("duckdb", "sqlite"), "bars built here from the feed's ticks or finer bars (built_bars), apart from what it "
            "sent, each pointing to its derivation (provenance.py)", "feeds/<feed>.db", derived=True, keyed_to="bars"),
    Product("pruned", "bars", ("sqlite",), "the bad-data list: a bar a rule caught, as it was, with the rule and its evidence; the "
            "feed's own bar stays where it is (storage/pruned.py)", "feeds/<feed>.db", derived=True, keyed_to="bars"),
)


def rows(frame) -> pd.DataFrame:
    """A frame of bars as the rules read it: the stored columns (COLUMNS: ts in ms, open, high, low,
    close, volume, vwap, transactions) from a DataFrame in either shape, the feeds' (t, o, h, l, c, v,
    vw, n: DataSource.bar_pages) or the store's, with a DatetimeIndex standing in for `ts`, or from a
    list of such dicts; in time order (a stable sort: a duplicate keeps its place), each row's place in
    the frame given kept in `row`."""
    df = frame.copy() if isinstance(frame, pd.DataFrame) else pd.DataFrame(list(frame))
    df = df.rename(columns={w: s for w, s in WIRE.items() if w in df.columns and s not in df.columns})
    if "ts" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            idx = df.index.tz_convert(UTC) if df.index.tz is not None else df.index.tz_localize(UTC)
            df["ts"] = idx.as_unit("ms").asi8
        elif df.empty:
            df["ts"] = pd.Series(dtype="int64")
        else:
            raise ValueError("a frame of bars needs a ts (or t) column, or a DatetimeIndex")
    if pd.api.types.is_datetime64_any_dtype(df["ts"]):
        stamps = pd.DatetimeIndex(df["ts"])
        stamps = stamps.tz_convert(UTC) if stamps.tz is not None else stamps.tz_localize(UTC)
        df["ts"] = stamps.as_unit("ms").asi8
    try:
        df["ts"] = pd.to_numeric(df["ts"]).astype("int64")
    except (TypeError, ValueError) as e:
        raise ValueError(f"a bar without a time: {e}") from e
    for c in COLUMNS[1:]:
        if c not in df.columns:
            df[c] = np.nan
    out = df[list(COLUMNS)].copy()
    out["row"] = np.arange(len(out))
    return out.sort_values("ts", kind="mergesort").reset_index(drop=True)


def wire(bars: pd.DataFrame) -> list[dict]:
    """Rows as the feeds send them (t, o, h, l, c, v, vw, n), from the stored columns (audit.compare reads these)."""
    out = []
    for r in bars.itertuples(index=False):
        d = {w: getattr(r, s) for w, s in WIRE.items()}
        out.append({k: (None if v is None or (isinstance(v, float) and math.isnan(v)) else (int(v) if k in ("t", "n") else v))
                    for k, v in d.items()})
    return out


class TimeSeriesData:
    """A time series' data, one subclass per asset class (its declarations the class attributes), a
    vendor's subclass where its data differs (the module's docstring). `spec()`, `assess(frame, since,
    until)` and `integrity(frame)` answer for any frame of its bars; `slots` gives the bar starts the
    calendar expects, `compare` two feeds' copies of a span (audit.compare), `scan` a stored series
    by every bad-data rule (baddata.scan_series: the store's finer bars too). An instance holds what
    it declares and the feed it is asked for (`feed`), never a frame."""

    family = "time-series"
    asset: str = ""  # stock | index | fx | crypto | future (the abstract base: none)
    calendar: str = ""  # the sessions kind freshness.CALENDAR keeps for it (the asset's, or a feed's own: overnight)
    sessions: str = ""  # the sessions in words (sessions.py)
    day_zone: str = "America/New_York"  # the zone a day bar is stamped in (UTC: fx and crypto)
    resolutions: tuple[str, ...] = ("second", "minute", "day")  # what its source delivers: the frames above are built here
    adjustments: tuple[str, ...] = ()  # what its prices are adjusted for (split)
    symbol: str = ""  # the symbol form, in words (symbols.py)
    market: str = ""  # what its data is: feeds of one market are copies (sip, iex, consolidated, one exchange's)
    ticks: bool = False  # whether its source delivers tick rows (DataSource.trade_pages)
    judge_volume: bool = True  # audits: whether two copies' volumes are judged (not across crypto venues)
    fields: tuple[Field, ...] = BAR_FIELDS
    tick_fields: tuple[Field, ...] = TICK_FIELDS
    products: tuple[Product, ...] = SERIES_PRODUCTS
    notes: tuple[str, ...] = ()

    def __init__(self, feed: str = "", **attributes) -> None:
        """For the feed named (`feed`: the plugin's name, by FeedPlugin.data_for), with any of its
        attributes set on the instance (a test's StockSymbolData(resolutions=("day",))). An attribute
        the class has not is an error."""
        self.feed = feed
        for k, v in attributes.items():
            if not hasattr(type(self), k):
                raise TypeError(f"{type(self).__name__} has no attribute {k!r}")
            setattr(self, k, v)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.asset}{' of ' + self.feed if self.feed else ''}>"

    @property
    def zone(self) -> ZoneInfo | timezone:
        return timezone.utc if self.day_zone == UTC else ZoneInfo(self.day_zone)

    @property
    def required(self) -> tuple[str, ...]:
        return tuple(f.name for f in self.fields if f.required)

    # -- the spec ---------------------------------------------------------------------------------

    def spec(self) -> SeriesSpec:
        return SeriesSpec(self.family, self.asset, tuple(self.fields), tuple(self.products), tuple(self.notes),
                          tuple(self.resolutions), self.calendar or self.asset, self.sessions, self.day_zone,
                          tuple(self.adjustments), self.symbol, self.market, self.ticks, tuple(self.tick_fields) if self.ticks else (),
                          MappingProxyType(dict(WIRE)))

    def check(self) -> list[str]:
        """Every way the class falls short of a data spec, in words (conformance.py reads it): an
        asset class, a calendar, resolutions the library knows, fields with a time and a price, and
        products whose derived ones point to an original and are never written back into it."""
        out: list[str] = []
        tag = type(self).__name__
        if self.asset not in DEFAULTS:
            out.append(f"{tag}: asset {self.asset!r} is none of {sorted(DEFAULTS)}")
        if not self.resolutions or not set(self.resolutions) <= set(BASES):
            out.append(f"{tag}: resolutions {self.resolutions!r} must be among {BASES}")
        names = [f.name for f in self.fields]
        if "ts" not in names or not {"open", "high", "low", "close"} <= set(names):
            out.append(f"{tag}: fields must hold ts, open, high, low and close")
        if self.day_zone != UTC:
            try:
                ZoneInfo(self.day_zone)
            except Exception:  # noqa: BLE001 - whatever ZoneInfo raised: not a zone
                out.append(f"{tag}: day_zone {self.day_zone!r} is not a zone")
        out += _product_problems(tag, self.products)
        return out

    # -- the calendar ----------------------------------------------------------------------------

    def step(self, timespan: str) -> int:
        try:
            return STEP_MS[timespan]
        except KeyError:
            raise ValueError(f"{timespan!r} is not a base frame ({', '.join(BASES)}): the frames above are built here") from None

    def slots(self, timespan: str, since, until, *, exchange: str | None = None, product: str | None = None) -> np.ndarray:
        """The bar starts (ms, int64) a complete series of `timespan` holds in [since, until): a
        second's or minute's open intervals by the calendar (freshness.CALENDAR.open_minutes: a
        stock's extended hours, an index's regular ones, forex's and futures' sessions, crypto's
        clock), a day's one per session day, stamped at midnight of the day zone (UTC for forex and
        crypto: every UTC date a session touches). `exchange` (a MIC abroad) gives a listing its own
        days; `product` a future its own sessions."""
        lo, hi = _ms(since, self.zone), _ms(until, self.zone)
        step = self.step(timespan)
        if lo >= hi:
            return np.array([], dtype="int64")
        cal = self.calendar or self.asset
        if timespan == "day":
            d0, d1 = _dt(lo).date() - timedelta(days=2), _dt(hi).date() + timedelta(days=2)
            days: set[date] = set()
            for s in freshness.CALENDAR.sessions(cal, d0, d1, product=product, exchange=exchange):
                if self.day_zone == UTC:
                    d, end = s.open.astimezone(timezone.utc).date(), (s.close - timedelta(microseconds=1)).astimezone(timezone.utc).date()
                    while d <= end:
                        days.add(d)
                        d += timedelta(days=1)
                else:
                    days.add(s.day)
            stamps = np.array(sorted(_ms(d, self.zone) for d in days), dtype="int64")
        else:
            index = freshness.CALENDAR.open_minutes(cal, _dt(lo), _dt(hi), product=product, step_seconds=step // 1000,
                                                    exchange=exchange)
            stamps = np.asarray(index.as_unit("ms").asi8, dtype="int64")
        return stamps[(stamps >= lo) & (stamps < hi)]

    # -- the assessment ------------------------------------------------------------------------------

    def assess(self, frame, since, until, *, timespan: str = "minute", tier=None, now: datetime | None = None,
               exchange: str | None = None, product: str | None = None) -> Assessment:
        """The frame's bars of `timespan` against the calendar over [since, until): the slots held
        and missing (`held`, `gaps`: the ledger's spans), completeness, the newest and oldest bar,
        how far the newest trails the window's end (`latency_s`), and the state: with a plan (`tier`,
        and `now`: the window's end unless given) freshness.assess's (empty, behind, current: what
        the plan should have published by now); without one, behind when the window's last expected
        slot is missing. Rows outside the window are counted in `rows` and ignored otherwise."""
        lo, hi = _ms(since, self.zone), _ms(until, self.zone)
        step = self.step(timespan)
        bars = rows(frame)
        ts = bars["ts"].to_numpy(dtype="int64")
        inside = np.unique(ts[(ts >= lo) & (ts < hi)])
        slots = self.slots(timespan, lo, hi, exchange=exchange, product=product)
        has = np.isin(slots, inside)
        present = int(has.sum())
        notes = []
        if timespan not in self.resolutions:
            notes.append(f"{timespan} bars are not among the spec's resolutions ({', '.join(self.resolutions)})")
        stray = int(len(inside) - present)
        if stray:
            notes.append(f"{stray} bar time{'s' if stray > 1 else ''} inside the window at no slot of the calendar")
        newest = int(inside.max()) if len(inside) else None
        oldest = int(inside.min()) if len(inside) else None
        latency = None if newest is None else max(0.0, (hi - (newest + step)) / 1000)
        through = due = None
        rule = ""
        if tier is not None:
            moment = now or _dt(hi)
            last = None if newest is None else _dt(newest)
            got = freshness.assess(self.calendar or self.asset, tier, last, moment, exchange)
            state, rule = got["state"], got["rule"]
            through = _ms(got["expectedThrough"]) if got["expectedThrough"] else None
            due = _ms(got["nextDue"]) if got["nextDue"] else None
        elif not len(inside):
            state = "empty"
        else:
            state = "behind" if len(slots) and not has[-1] else "current"
        return Assessment(self.asset, timespan, lo, hi, len(bars), present, int(len(slots)),
                          (present / len(slots)) if len(slots) else None, _spans(slots, has, step), _spans(slots, ~has, step),
                          oldest, newest, latency, state, through, due, rule, tuple(notes))

    # -- the integrity -------------------------------------------------------------------------------

    def integrity(self, frame, *, timespan: str = "minute", now: datetime | None = None, exchange: str | None = None,
                  product: str | None = None) -> Integrity:
        """The frame's rows judged: `malformed` (baddata.malformed: a price not positive and finite,
        the high below the low, the open or close outside them, negative volume, a time in the future),
        `missing` (a required field of the spec not there), `duplicate` (a second row at a time: the
        first is kept, the rest reported), `out_of_session` (a bar at no slot of the calendar), `spike`
        (baddata.spikes: a range many times its neighbours' on little volume, the price not moving on;
        judged on the rows the other rules passed, within the session's parts as the store's scan
        does), and `gap` (slots of the calendar missing between the first and the last bar, one finding
        per run). Evidence as the rules give it; `ok` ignores the gaps."""
        bars = rows(frame)
        n = len(bars)
        findings: list[Finding] = []
        if not n:
            return Integrity(self.asset, 0, ())
        step = self.step(timespan)
        now_ms = _ms(now) if now is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
        ts = bars["ts"].to_numpy(dtype="int64")
        row = bars["row"].to_numpy()
        dup = bars["ts"].duplicated(keep="first").to_numpy()
        first = {int(t): int(r) for t, r in zip(ts[~dup], row[~dup])}
        for i in np.flatnonzero(dup):
            findings.append(Finding("duplicate", int(ts[i]), {"row": int(row[i]), "first_row": first[int(ts[i])]}))
        caught, evidence = baddata.malformed(bars, now_ms)
        for i, ev in zip(np.flatnonzero(caught), evidence):
            findings.append(Finding("malformed", int(ts[i]), {**ev, "row": int(row[i])}))
        for name in self.required:
            if name == "ts" or name not in bars.columns:
                continue
            lost = bars[name].isna().to_numpy()
            for i in np.flatnonzero(lost & ~caught):
                findings.append(Finding("missing", int(ts[i]), {"field": name, "row": int(row[i])}))
        slots = self.slots(timespan, int(ts.min()), int(ts.max()) + step, exchange=exchange, product=product)
        outside = ~np.isin(ts, slots)
        for i in np.flatnonzero(outside):
            findings.append(Finding("out_of_session", int(ts[i]), {"row": int(row[i]), "calendar": self.calendar or self.asset}))
        keep = ~(caught | dup)
        sub = bars[keep].reset_index(drop=True)
        if len(sub):
            parts = baddata._parts(sub["ts"].to_numpy(dtype="int64"), timespan, self.asset, exchange)
            hit, why = baddata.spikes(sub, parts)
            for i, ev in zip(np.flatnonzero(hit), why):
                findings.append(Finding("spike", int(sub["ts"].iloc[i]), {**ev, "row": int(sub["row"].iloc[i])}))
        if len(slots):
            for a, b, lacking in _runs(slots, ~np.isin(slots, ts), step):
                findings.append(Finding("gap", a, {"end": b, "slots": lacking}))
        order = {r: i for i, r in enumerate((*SEVERE, "gap"))}
        findings.sort(key=lambda f: (order.get(f.rule, 99), f.at if isinstance(f.at, int) else 0))
        return Integrity(self.asset, n, tuple(findings))

    def compare(self, frame, other, *, timespan: str = "minute", judge_volume: bool | None = None) -> dict:
        """Two feeds' copies of a span, bar by bar on identical times (audit.compare: timing, prices in
        basis points, volume and trade count in percent, a verdict); `judge_volume` the class's unless
        said (crypto venues cover different books)."""
        step = self.step(timespan)
        judge = self.judge_volume if judge_volume is None else judge_volume
        return compare_copies(wire(rows(frame)), wire(rows(other)), step, judge_volume=judge)

    def scan(self, store, source: str, ticker: str, timespan: str = "minute", lo_ms: int | None = None, hi_ms: int | None = None,
             now_ms: int | None = None, exchange: str | None = None) -> int:
        """A stored series judged by every bad-data rule, the one needing the store's finer bars too
        (baddata.scan_series: rules 1 to 3, what they catch put on the feed's pruned list). Returns
        entries added."""
        return baddata.scan_series(store, source, ticker, timespan, self.asset, lo_ms, hi_ms, now_ms, exchange)


def _product_problems(tag: str, products: Iterable[Product]) -> list[str]:
    """A derived product points to an original product of the same class and shares no store with
    it where that store is the original's alone: the original is never written back into."""
    out: list[str] = []
    products = tuple(products)
    if not all(isinstance(p, Product) for p in products):
        return [f"{tag}: products must be Product declarations"]
    names = [p.name for p in products]
    if len(set(names)) != len(names):
        out.append(f"{tag}: a product twice: {names}")
    by = {p.name: p for p in products}
    for p in products:
        if not p.engines:
            out.append(f"{tag}: product {p.name!r} names no engine")
        if p.derived:
            if not p.keyed_to or p.keyed_to not in by or by[p.keyed_to].derived:
                out.append(f"{tag}: derived product {p.name!r} must be keyed to an original product")
        elif p.keyed_to:
            out.append(f"{tag}: product {p.name!r} is keyed to {p.keyed_to!r} but not derived")
    originals = [p for p in products if not p.derived]
    if not originals:
        out.append(f"{tag}: no original product")
    return out


# -- the asset classes ---------------------------------------------------------------------------------

class StockSymbolData(TimeSeriesData):
    """A stock's bars and ticks (the user's stock_symbol_data): the consolidated tape's sessions, 04:00
    to 20:00 ET on NYSE trading days (the collector's checks; a listing abroad its exchange's local
    days), day bars stamped at midnight ET of the trading date, prices split-adjusted (every feed's
    bars are, so they agree: Massive's adjusted=true, Alpaca's adjustment=split, Tiingo's split
    factors applied in its plugin), never dividend-adjusted."""

    asset = "stock"
    calendar = "stock"
    sessions = ("04:00-20:00 ET on NYSE trading days (the regular session 09:30-16:00, 13:00 on early-close days); a listing "
                "outside the US: its exchange's local calendar days")
    adjustments = ("split",)
    symbol = "TICKER (SPCX; a share class with a dot: BRK.B); a listing outside the US as TICKER.SUFFIX (7203.T, models.VENUES)"


class IndexData(TimeSeriesData):
    """An index's values: regular hours only, the closing value a bar stamped at the close; no volume
    (an index trades nothing), no ticks, nothing to adjust."""

    asset = "index"
    calendar = "index"
    sessions = "09:30-16:00 ET on NYSE trading days (13:00 on early-close days), the official closing value stamped at the close"
    symbol = "I:CODE as the source names it (I:SPX, I:COMP); shown by its common name (SPX, IXIC)"
    fields = tuple(f if f.name != "volume" else Field(f.name, f.dtype, f.unit, False, "an index has none") for f in BAR_FIELDS)


class FxData(TimeSeriesData):
    """A currency pair's bars: the spot market as the sources quote it, Sunday 17:00 to Friday 17:00
    ET, day bars UTC days; volume where a source counts ticks, none otherwise; a pair shown the other
    way round is the source's inverted (symbols.SymbolSpec.invert)."""

    asset = "fx"
    calendar = "fx"
    sessions = "Sunday 17:00 to Friday 17:00 ET, each weekday's session from 17:00 ET the evening before; day bars are UTC days"
    day_zone = UTC
    symbol = "C:BASEQUOTE in the market's quoting convention (C:EURUSD; USDEUR is EURUSD inverted)"
    fields = tuple(f if f.name != "volume" else Field(f.name, f.dtype, "ticks", False, "a source's tick count, where it sends one")
                   for f in BAR_FIELDS)


class CryptoSymbolData(TimeSeriesData):
    """A crypto pair's bars and trades: around the clock, day bars UTC days; the same pair is a
    different market on each exchange (one exchange's book, or a consolidation across exchanges:
    `market`), so two copies' volumes are not judged against each other; trades carry no sale
    conditions (every trade counts: ticks.py)."""

    asset = "crypto"
    calendar = "crypto"
    sessions = "every day, around the clock; a session is one UTC day"
    day_zone = UTC
    symbol = "X:BASEQUOTE (X:BTCUSD); an exchange prefix typed (COINBASE:BTCUSD) is dropped: the feed says whose book it is"
    judge_volume = False
    tick_fields = tuple(f for f in TICK_FIELDS if f.name not in ("conditions", "tape"))


class FutureData(TimeSeriesData):
    """A futures contract's bars: CME Globex sessions, 18:00 ET the evening before to 17:00 ET on
    the settlement date, with the holiday halts of its product group; a session's bar stamped at
    midnight ET of its settlement date; the continuous contract (NQ1!) rolls, nothing is adjusted."""

    asset = "future"
    calendar = "future"
    sessions = ("18:00 ET the evening before to 17:00 ET on the settlement date, Monday to Friday, with the product group's "
                "holiday halts (equity index, or energy and other commodities)")
    resolutions = ("minute", "day")
    symbol = "PRODUCT and rank: NQ1! (the most active contract), NQ2! (the next); the source is asked for the product (NQ)"
    notes = ("a continuous contract's series rolls from one contract to the next: no adjustment is applied at a roll",)


DEFAULTS: dict[str, type[TimeSeriesData]] = {"stock": StockSymbolData, "index": IndexData, "fx": FxData,
                                             "crypto": CryptoSymbolData, "future": FutureData}


def default_series(asset: str) -> type[TimeSeriesData] | None:
    """The contract's data class for an asset class (what a feed plugin gets without a `data`
    declaration), or None for an asset class the library has not."""
    return DEFAULTS.get(asset)


# -- the documents and their parts --------------------------------------------------------------------

@dataclass(frozen=True)
class Paragraph:
    """A paragraph of a document: its text, its place (from 0, in its page or the document), its heading."""
    text: str
    index: int = 0
    heading: str = ""

    @property
    def empty(self) -> bool:
        return not self.text.strip()

    @property
    def words(self) -> int:
        return len(self.text.split())

    def as_dict(self) -> dict:
        return {"index": self.index, "heading": self.heading, "text": self.text}


@dataclass(frozen=True)
class Page:
    """A page of a document: its number (from 1), its paragraphs, or its text whole when not split."""
    number: int
    paragraphs: tuple[Paragraph, ...] = ()
    text: str = ""

    @property
    def empty(self) -> bool:
        return not self.text.strip() and all(p.empty for p in self.paragraphs)

    @property
    def body(self) -> str:
        return self.text if self.text else "\n\n".join(p.text for p in self.paragraphs)

    def as_dict(self) -> dict:
        return {"number": self.number, "paragraphs": [p.as_dict() for p in self.paragraphs], "text": self.text}


@dataclass(frozen=True)
class Statement:
    """A statement parsed from a document (a filing's balance sheet, a release's figures, a
    decision's target range): its name, the period it covers, its values by field (None: not
    parsed), their unit, and where in the document it was read (a page, a paragraph). Derived from
    the original: kept apart and keyed to it, never written back (DocumentData's products)."""
    name: str
    period: str = ""
    values: Mapping[str, float | None] = NOTHING
    unit: str = ""
    source: str = ""

    def missing(self, fields: Iterable[str] = ()) -> tuple[str, ...]:
        """The fields not parsed: those of `fields` (what a statement of this name must hold) absent
        or None, and any value None or not a finite number."""
        out = [f for f in fields if self.values.get(f) is None]
        for k, v in self.values.items():
            if k in out:
                continue
            if v is None or not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v):
                out.append(k)
        return tuple(out)

    @property
    def complete(self) -> bool:
        return not self.missing()

    def as_dict(self) -> dict:
        return {"name": self.name, "period": self.period, "values": dict(self.values), "unit": self.unit, "source": self.source}


@dataclass(frozen=True)
class Document:
    """A document as the publishers' stores hold it (storage/documents.py: an agency's item, a
    vendor's headline) and, as they come, with its parts: its key (source, id), what the publisher
    said of it (title, url, when published, its kind, who it is by, its tags), its pages and the
    statements parsed from it, and the record as fetched (`raw`: what the original store keeps,
    untouched)."""
    source: str
    id: str
    title: str = ""
    url: str | None = None
    published_ms: int | None = None
    kind: str = ""
    by: str = ""  # the agency or the publisher
    tags: tuple[str, ...] = ()
    fetched_at: str | None = None
    pages: tuple[Page, ...] = ()
    statements: tuple[Statement, ...] = ()
    raw: Mapping = NOTHING

    @property
    def key(self) -> tuple[str, str]:
        return self.source, self.id

    @property
    def published(self) -> str | None:
        return None if self.published_ms is None else _dt(self.published_ms).isoformat(timespec="seconds")

    def page(self, number: int) -> Page | None:
        return next((p for p in self.pages if p.number == number), None)

    def missing_pages(self) -> tuple[int, ...]:
        """The page numbers a run from 1 to the highest would have and the document has not."""
        if not self.pages:
            return ()
        have = {p.number for p in self.pages}
        return tuple(n for n in range(1, max(have) + 1) if n not in have)

    def missing(self, required: Iterable[str] = (), statements: Mapping[str, Iterable[str]] = NOTHING) -> tuple[str, ...]:
        """What the document lacks, in words: a required field empty, no time, a page missing or
        empty, a statement not parsed through."""
        out = []
        for name in required:
            value = getattr(self, name, None) if hasattr(self, name) else self.raw.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                out.append(f"no {name}")
        if self.published_ms is None:
            out.append("no time")
        gone = self.missing_pages()
        if gone:
            out.append("page" + ("s " if len(gone) > 1 else " ") + ", ".join(str(n) for n in gone) + " missing")
        out += [f"page {p.number} empty" for p in self.pages if p.empty]
        for s in self.statements:
            lost = s.missing(statements.get(s.name, ()))
            if lost:
                out.append(f"statement {s.name}: {', '.join(lost)} not parsed")
        return tuple(out)

    def as_dict(self) -> dict:
        return {"source": self.source, "id": self.id, "title": self.title, "url": self.url, "published": self.published,
                "publishedMs": self.published_ms, "kind": self.kind, "by": self.by, "tags": list(self.tags),
                "fetchedAt": self.fetched_at, "pages": [p.as_dict() for p in self.pages],
                "statements": [s.as_dict() for s in self.statements]}


def _time_ms(value) -> int | None:
    """A document's time in ms: an int (ms), an ISO string, a datetime; None when there is none or it
    cannot be read."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        return None if isinstance(value, float) and not math.isfinite(value) else int(value)
    if isinstance(value, datetime):
        return _ms(value)
    if isinstance(value, str):
        try:
            t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return _ms(t)
    return None


def _paragraph(p, index: int) -> Paragraph:
    if isinstance(p, Paragraph):
        return p
    if isinstance(p, str):
        return Paragraph(p, index)
    return Paragraph(str(p.get("text") or ""), int(p.get("index", index)), str(p.get("heading") or ""))


def _page(p, number: int) -> Page:
    if isinstance(p, Page):
        return p
    if isinstance(p, str):
        return Page(number, (), p)
    paragraphs = tuple(_paragraph(q, i) for i, q in enumerate(p.get("paragraphs") or ()))
    return Page(int(p.get("number", number)), paragraphs, str(p.get("text") or ""))


def _statement(s) -> Statement:
    if isinstance(s, Statement):
        return s
    return Statement(str(s.get("name") or ""), str(s.get("period") or ""), MappingProxyType(dict(s.get("values") or {})),
                     str(s.get("unit") or ""), str(s.get("source") or ""))


# A filing (the SEC's EDGAR, business records, tax filings to come: the user, 2026-10-31, "i think im
# gonna need mongo for downloading business records/tax filings etc"): the original with its pages,
# in MongoDB; registered in storage/documents.KINDS when filings are fetched (nothing is stored yet).
FILINGS = Kind("filings", "filings", "filings", ("source", "id"), "published",
               ("source", "id", "agency", "title", "url", "published", "kind", "tags", "fetched_at", "pages"), ("tags", "pages"))


def _document_products(kind: Kind | None) -> tuple[Product, ...]:
    """A document kind's products: the original where its store keeps it (MongoDB, or SQLite until it
    moves), and what is derived from it, apart and keyed to it."""
    from .. import layout  # the data folder's names
    from ..mongo import DATABASE

    table = kind.table if kind else "documents"
    collection = kind.collection if kind else "documents"
    file = layout.MAIN if kind is NEWS else layout.GOV
    return (
        Product("original", "documents", ("mongodb", "sqlite"), "the document as fetched, untouched: its record, its pages, its "
                "attachments as they come", f"mongodb://127.0.0.1:27017/{DATABASE}.{collection}; {file} ({table})"),
        Product("statements", "series", ("sqlite", "duckdb"), "what is parsed from the original (its statements' values by field "
                "and period), keyed to it by (source, id), never written back into it", "", derived=True, keyed_to="original"),
        Product("evaluations", "series", ("sqlite",), "the AI assistants' evaluations of it (the main file's evaluations table), "
                "keyed to it", layout.MAIN + " (evaluations)", derived=True, keyed_to="original"),
    )


class DocumentData:
    """A kind of document a publisher delivers, with its parts (Paragraph, Page, Statement): its
    declarations the class attributes (the store's Kind: table, collection, key, order and fields;
    what a document must have; what each named statement must hold). `spec()`, `assess(docs, since,
    until)` and `integrity(docs)` answer for any documents of the kind (the stores' dicts, or
    `Document`s); `document(item)` reads one. An instance holds what it declares and the publisher it
    is asked for (`publisher`)."""

    family = "document"
    kind: str = ""  # release | headline | filing
    store: Kind | None = None  # the documents store's kind (storage/documents.py)
    required: tuple[str, ...] = ("title",)  # what a document must have besides its key and its time
    parts: tuple[str, ...] = ("paragraph", "page", "statement")
    statements: Mapping[str, tuple[str, ...]] = NOTHING  # a statement's name -> the fields its values must hold
    notes: tuple[str, ...] = ()

    def __init__(self, publisher: str = "", **attributes) -> None:
        self.publisher = publisher
        for k, v in attributes.items():
            if not hasattr(type(self), k):
                raise TypeError(f"{type(self).__name__} has no attribute {k!r}")
            setattr(self, k, v)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.kind}{' of ' + self.publisher if self.publisher else ''}>"

    @property
    def products(self) -> tuple[Product, ...]:
        return _document_products(self.store)

    @property
    def fields(self) -> tuple[Field, ...]:
        names = self.store.fields if self.store else ("source", "id", "title", "url", "published", "kind", "tags", "fetched_at")
        lists = self.store.lists if self.store else ("tags",)
        order = self.store.order if self.store else "published"
        key = self.store.key if self.store else ("source", "id")
        out = []
        for n in names:
            dtype = "list" if n in lists else "int64" if n.endswith("_ms") else "str"
            required = n in key or n == order or n in self.required
            about = ("the key" if n in key else "ms since the epoch (UTC)" if n.endswith("_ms") else "ISO time (UTC)"
                     if n == order else "")
            out.append(Field(n, dtype, "", required, about))
        return tuple(out)

    def spec(self) -> DocumentSpec:
        k = self.store
        return DocumentSpec(self.family, self.kind, self.fields, self.products, tuple(self.notes), tuple(self.parts),
                            tuple(k.key) if k else ("source", "id"), k.order if k else "published", k.table if k else "",
                            k.collection if k else "", MappingProxyType({n: tuple(f) for n, f in self.statements.items()}))

    def check(self) -> list[str]:
        """Every way the class falls short of a document spec (conformance.py reads it)."""
        out: list[str] = []
        tag = type(self).__name__
        if not self.kind:
            out.append(f"{tag}: no kind")
        if self.store is not None and not isinstance(self.store, Kind):
            out.append(f"{tag}: store must be a storage.documents.Kind")
        if tuple(self.parts) != ("paragraph", "page", "statement"):
            out.append(f"{tag}: parts must be paragraph, page and statement")
        if not isinstance(self.statements, Mapping) or not all(isinstance(n, str) and isinstance(f, (tuple, list))
                                                              for n, f in self.statements.items()):
            out.append(f"{tag}: statements must map a statement's name to the fields it holds")
        out += _product_problems(tag, self.products)
        return out

    # -- a document -----------------------------------------------------------------------------------

    def document(self, item) -> Document:
        """A stored item (a dict of the store's fields, with `pages` and `statements` where they are)
        as a Document; a Document is given back as it is."""
        if isinstance(item, Document):
            return item
        d = dict(item)
        order = self.store.order if self.store else "published"
        when = d.get(order)
        if when is None and order != "published":
            when = d.get("published")
        if when is None and order != "published_ms":
            when = d.get("published_ms")
        tags = d.get("tags") if d.get("tags") is not None else d.get("tickers")
        if isinstance(tags, str):  # a SQLite row's JSON list, or names with commas between
            try:
                tags = json.loads(tags) if tags.lstrip().startswith("[") else [t.strip() for t in tags.split(",") if t.strip()]
            except ValueError:
                tags = [t.strip() for t in tags.split(",") if t.strip()]
        return Document(str(d.get("source") or ""), str(d.get("id") or ""), str(d.get("title") or ""), d.get("url"),
                        _time_ms(when), str(d.get("kind") or ""), str(d.get("agency") or d.get("publisher") or ""),
                        tuple(str(t) for t in (tags or ())), d.get("fetched_at"),
                        tuple(_page(p, i + 1) for i, p in enumerate(d.get("pages") or ())),
                        tuple(_statement(s) for s in d.get("statements") or ()), MappingProxyType(d))

    def documents(self, items) -> list[Document]:
        return [self.document(x) for x in items]

    # -- the assessment -------------------------------------------------------------------------------

    def assess(self, items, since, until, *, due=None) -> Assessment:
        """The documents published in [since, until): how many, how many complete (every required
        field, every page, every statement: `completeness` their share), the oldest and newest, how far
        the newest trails the window's end (`latency_s`), and the state: empty, current, or behind
        when something was due (`due`: a publisher's calendar moment at or before the window's end)
        and nothing published since it is there. Documents have no calendar of slots: `expected`,
        `held` and `gaps` are not given."""
        lo, hi = _ms(since), _ms(until)
        docs = self.documents(items)
        inside: list[Document] = []
        seen: set[tuple[str, str]] = set()
        twice = 0
        for d in docs:
            if d.published_ms is None or not lo <= d.published_ms < hi:
                continue
            if d.key in seen:
                twice += 1  # a document given twice is one document (integrity names it)
                continue
            seen.add(d.key)
            inside.append(d)
        times = [d.published_ms for d in inside]
        newest, oldest = (max(times), min(times)) if times else (None, None)
        complete = sum(1 for d in inside if not d.missing(self.required, self.statements))
        latency = None if newest is None else max(0.0, (hi - newest) / 1000)
        due_ms = _ms(due) if due is not None else None
        if not inside:
            state = "empty"
        elif due_ms is not None and due_ms <= hi and newest < due_ms:
            state = "behind"
        else:
            state = "current"
        notes = []
        undated = sum(1 for d in docs if d.published_ms is None)
        if undated:
            notes.append(f"{undated} document{'s' if undated > 1 else ''} without a time, left out of the window")
        if twice:
            notes.append(f"{twice} document{'s' if twice > 1 else ''} given twice, counted once")
        return Assessment(self.kind, None, lo, hi, len(docs), len(inside), None, (complete / len(inside)) if inside else None,
                          (), (), oldest, newest, latency, state, None, due_ms, "due" if due_ms is not None else "", tuple(notes))

    # -- the integrity ----------------------------------------------------------------------------------

    def integrity(self, items, *, now: datetime | None = None) -> Integrity:
        """The documents judged: `duplicate` (a key twice: the first is kept), `missing_field` (a
        required field empty), `bad_time` (no time that can be read, or one in the future), `bad_url`
        (not HTTPS: the publishers' policy), `page_missing` and `page_empty` (a filing's pages: a
        number skipped, a page with nothing on it), `statement_incomplete` (a statement with a field
        not parsed: what its name must hold, or a value not a finite number)."""
        now_ms = _ms(now) if now is not None else int(datetime.now(timezone.utc).timestamp() * 1000)
        docs = self.documents(items)
        findings: list[Finding] = []
        seen: dict[tuple[str, str], int] = {}
        for i, d in enumerate(docs):
            at = f"{d.source}:{d.id}"
            if d.key in seen:
                findings.append(Finding("duplicate", at, {"row": i, "first_row": seen[d.key]}))
                continue
            seen[d.key] = i
            for name in self.required:
                value = getattr(d, name, None) if hasattr(d, name) else d.raw.get(name)
                if value is None or (isinstance(value, str) and not value.strip()):
                    findings.append(Finding("missing_field", at, {"field": name, "row": i}))
            if d.published_ms is None:
                order = self.store.order if self.store else "published"
                findings.append(Finding("bad_time", at, {"row": i, "value": d.raw.get(order), "why": "no time that can be read"}))
            elif d.published_ms > now_ms + 60_000:
                findings.append(Finding("bad_time", at, {"row": i, "value": d.published, "why": "in the future"}))
            if d.url and not str(d.url).lower().startswith("https://"):
                findings.append(Finding("bad_url", at, {"row": i, "url": d.url, "why": "not HTTPS"}))
            gone = d.missing_pages()
            if gone:
                findings.append(Finding("page_missing", at, {"row": i, "pages": list(gone), "have": sorted(p.number for p in d.pages)}))
            for p in d.pages:
                if p.empty:
                    findings.append(Finding("page_empty", at, {"row": i, "page": p.number}))
            for s in d.statements:
                lost = s.missing(self.statements.get(s.name, ()))
                if lost:
                    findings.append(Finding("statement_incomplete", at, {"row": i, "statement": s.name, "period": s.period,
                                                                         "fields": list(lost)}))
        return Integrity(self.kind, len(docs), tuple(findings), severe=DOCUMENT_SEVERE)


DOCUMENT_SEVERE = ("duplicate", "missing_field", "bad_time", "bad_url", "page_missing", "page_empty", "statement_incomplete")


class ReleaseData(DocumentData):
    """An agency's documents (gov.py: what the publishers' feeds say they published): releases, rules,
    proposed rules, notices, speeches, data releases, trading suspensions; kept as items (GOV_ITEMS)
    by the feed's key and the item's id."""

    kind = "release"
    store = GOV_ITEMS
    required = ("title", "by")


class HeadlineData(DocumentData):
    """A vendor's headlines (the main file's news: the News view and the globe's ticker), kept by the
    plugin's name and the headline's id, with the tickers it names."""

    kind = "headline"
    store = NEWS
    required = ("title", "by")


class FilingData(DocumentData):
    """A filing (a company's with the SEC, a tax filing, a business record): the original with its
    pages in MongoDB, the statements parsed from it (a balance sheet's or an income statement's
    figures by period) in SQLite or DuckDB, keyed to the filing and never written back into it."""

    kind = "filing"
    store = FILINGS
    required = ("title", "by")
    statements = MappingProxyType({
        "balance sheet": ("total_assets", "total_liabilities", "equity"),
        "income statement": ("revenue", "net_income"),
        "cash flow": ("operating", "investing", "financing"),
    })


DOCUMENT_DEFAULTS: dict[str, type[DocumentData]] = {"official": ReleaseData, "vendor": HeadlineData, "research": ReleaseData,
                                                    "mirror": ReleaseData}


def default_document(kind: str) -> type[DocumentData]:
    """The contract's document class for a publisher's kind (what a publisher plugin gets without a
    `data` declaration): a vendor's headlines, every other's releases."""
    return DOCUMENT_DEFAULTS.get(kind, ReleaseData)
