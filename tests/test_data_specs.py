"""The data specs (plugin API 2.1, sources/data.py): what each kind of data a plugin delivers is
(`spec`), how complete and fresh a frame of it is (`assess`), whether its rows are sound
(`integrity`); the contract's default class per asset class and per publisher kind, a plugin
declaring its own, and the conformance check reading the declarations. The user, 2026-11-03: "every
plugin needs to furnish the correct mechanisms for assessing data, data integrity, provide a 'spec'
for the data, different source plugins like massive and Tingo can inherit 'stock_symbol_data'"."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from liminal_libs import baddata, layout
from liminal_libs.config import Tier
from liminal_libs.sources import data, registry
from liminal_libs.sources.conformance import problems
from liminal_libs.sources.data import (DEFAULTS, CryptoSymbolData, Document, DocumentData, FilingData, FutureData, FxData,
                                       HeadlineData, IndexData, Page, Paragraph, ReleaseData, Statement, StockSymbolData,
                                       TimeSeriesData, rows, wire)
from liminal_libs.sources.registry import PLUGIN_API, api_refusal

from test_plugin_classes import AcmeFeed, AcmeNews, acme_record

ET = ZoneInfo("America/New_York")
DAY = date(2026, 10, 20)  # a Tuesday with the NYSE open, on daylight time


def at(h: int, m: int, s: int = 0, d: date = DAY) -> int:
    """A moment of the day in New York, in ms."""
    return int(datetime(d.year, d.month, d.day, h, m, s, tzinfo=ET).timestamp() * 1000)


def utc_day(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def utc_at(*parts: int) -> int:
    return int(datetime(*parts, tzinfo=timezone.utc).timestamp() * 1000)


def bar(t: int, **changes) -> dict:
    """A bar as a feed sends it: quiet, at 100."""
    return {"t": t, "o": 100.0, "h": 100.1, "l": 99.9, "c": 100.0, "v": 1000.0, "vw": 100.0, "n": 10, **changes}


def session() -> list[dict]:
    """The regular session's 390 minutes of DAY."""
    return [bar(at(9, 30) + i * 60_000) for i in range(390)]


class SipStock(StockSymbolData):
    """A vendor's stock data: the consolidated tape with its ticks (what Massive's would declare)."""
    market = "sip"
    ticks = True


# -- the specs ---------------------------------------------------------------------------------------

def test_each_asset_class_has_its_spec():
    specs = {asset: cls().spec() for asset, cls in DEFAULTS.items()}
    assert set(specs) == {"stock", "index", "fx", "crypto", "future"}
    assert all(s.family == "time-series" and s.kind == asset and s.calendar == asset for asset, s in specs.items())
    assert specs["stock"].resolutions == ("second", "minute", "day") and specs["future"].resolutions == ("minute", "day")
    assert specs["stock"].adjustments == ("split",) and specs["index"].adjustments == () and specs["future"].adjustments == ()
    assert specs["stock"].day_zone == "America/New_York" and specs["fx"].day_zone == specs["crypto"].day_zone == "UTC"
    assert specs["stock"].required == ("ts", "open", "high", "low", "close", "volume") == specs["crypto"].required
    assert specs["index"].required == ("ts", "open", "high", "low", "close") == specs["fx"].required  # no volume to speak of
    assert specs["stock"].wire == {"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume", "vw": "vwap",
                                   "n": "transactions"}
    assert "TICKER.SUFFIX" in specs["stock"].symbol and specs["fx"].symbol.startswith("C:") and specs["crypto"].symbol.startswith("X:")
    assert "NQ1!" in specs["future"].symbol and "17:00 ET" in specs["future"].sessions and "UTC day" in specs["crypto"].sessions
    # where each product goes: the feed's own bars (SQLite, closed months in DuckDB), its ticks, and what is derived from them
    names = [p.name for p in specs["stock"].products]
    assert names == ["bars", "ticks", "built", "pruned"]
    bars, built = specs["stock"].product("bars"), specs["stock"].product("built")
    assert bars.engines == ("duckdb", "sqlite") and "feeds/<feed>.db" in bars.where and not bars.derived
    assert built.derived and built.keyed_to == "bars" and specs["stock"].product("pruned").keyed_to == "bars"
    assert specs["stock"].product("ticks").engines == ("sqlite",)
    # the spec is plain data for the integrator's page
    assert json.loads(json.dumps(specs["crypto"].as_dict()))["kind"] == "crypto"
    assert all(cls().check() == [] for cls in DEFAULTS.values())
    assert TimeSeriesData().check() == ["TimeSeriesData: asset '' is none of ['crypto', 'future', 'fx', 'index', 'stock']"]
    # crypto: a market per exchange, so two copies' volumes are not judged; its trades carry no conditions
    crypto = CryptoSymbolData()
    assert crypto.judge_volume is False and StockSymbolData().judge_volume is True
    assert specs["crypto"].ticks is False and specs["crypto"].tick_fields == ()  # the base says its source sends none
    assert [f.name for f in CryptoSymbolData(ticks=True).spec().tick_fields] == ["ts_ns", "seq", "price", "size", "exchange", "trade_id"]
    # a vendor's subclass: what differs, declared; an instance may set an attribute of the class, no other
    sip = SipStock(feed="massive")
    assert sip.spec().market == "sip" and sip.spec().ticks and len(sip.spec().tick_fields) == 8 and repr(sip) == "<SipStock stock of massive>"
    assert sip.spec().product("ticks").where == "feeds/<feed>-ticks.db"
    assert StockSymbolData(resolutions=("day",)).spec().resolutions == ("day",)
    with pytest.raises(TypeError, match="no attribute 'resolution'"):
        StockSymbolData(resolution=("day",))


def test_the_calendar_gives_the_slots_a_complete_series_holds():
    stock = StockSymbolData()
    assert len(stock.slots("minute", at(9, 30), at(16, 0))) == 390  # the regular session
    assert len(stock.slots("minute", at(0, 0), at(0, 0, d=date(2026, 10, 21)))) == 960  # 04:00-20:00: the extended hours
    assert len(stock.slots("second", at(9, 30), at(9, 31))) == 60
    monday, next_monday = date(2026, 10, 12), date(2026, 10, 19)
    days = stock.slots("day", monday, next_monday)
    assert [datetime.fromtimestamp(t / 1000, ET).strftime("%a %H:%M") for t in days] == ["Mon 00:00", "Tue 00:00", "Wed 00:00",
                                                                                           "Thu 00:00", "Fri 00:00"]
    assert len(IndexData().slots("minute", at(0, 0), at(0, 0, d=date(2026, 10, 21)))) == 391  # 09:30-16:00 and the closing value
    # fx: UTC days, every UTC date a session touches (the Sunday evening's open counts for Sunday)
    fx_days = FxData().slots("day", utc_day(monday), utc_day(next_monday))
    assert [datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%a") for t in fx_days] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sun"]
    assert all(t % 86_400_000 == 0 for t in fx_days)  # stamped at midnight UTC
    assert len(CryptoSymbolData().slots("day", utc_day(monday), utc_day(next_monday))) == 7  # every day
    future = FutureData()
    assert len(future.slots("minute", at(0, 0), at(0, 0, d=date(2026, 10, 21)))) == 1380  # 18:00 the evening before to 17:00
    assert len(future.slots("day", monday, next_monday)) == 5
    assert len(stock.slots("minute", at(16, 0), at(9, 30))) == 0  # an empty window
    with pytest.raises(ValueError, match="not a base frame"):
        stock.slots("hour", at(9, 30), at(16, 0))


# -- the assessment ------------------------------------------------------------------------------------

def _assessed_frame() -> pd.DataFrame:
    """Minutes 09:30 to 15:19 without 10:00 to 10:09; a pre-market row, and a row at a half minute."""
    times = [at(9, 30) + i * 60_000 for i in range(350) if not at(10, 0) <= at(9, 30) + i * 60_000 < at(10, 10)]
    frame = pd.DataFrame([bar(t) for t in times] + [bar(at(8, 0), v=5.0, vw=None, n=1), bar(at(9, 30, 30), v=5.0)])
    return frame


def test_an_assessment_finds_the_gaps_and_the_latency():
    stock = StockSymbolData(feed="acme")
    a = stock.assess(_assessed_frame(), at(9, 30), at(16, 0))
    assert (a.kind, a.timespan, a.since_ms, a.until_ms) == ("stock", "minute", at(9, 30), at(16, 0))
    assert a.rows == 342 and a.present == 340 and a.expected == 390 and a.missing == 50 and round(a.completeness, 4) == 0.8718
    assert a.held == ((at(9, 30), at(10, 0)), (at(10, 10), at(15, 20)))  # the ledger's spans: what a planner subtracts
    assert a.gaps == ((at(10, 0), at(10, 10)), (at(15, 20), at(16, 0)))
    assert a.oldest_ms == at(9, 30) and a.newest_ms == at(15, 19) and a.latency_s == 2400.0  # the newest bar ends 15:20
    assert a.state == "behind" and a.rule == "" and a.expected_through_ms is None
    assert a.notes == ("1 bar time inside the window at no slot of the calendar",)
    d = json.loads(json.dumps(a.as_dict()))
    assert d["held"] == [[at(9, 30), at(10, 0)], [at(10, 10), at(15, 20)]] and d["latencyS"] == 2400.0 and d["state"] == "behind"
    # with a plan, freshness says what the plan should have published by now: real time, behind; end of day, current
    now = datetime(2026, 10, 20, 16, 0, tzinfo=ET)
    real_time = stock.assess(_assessed_frame(), at(9, 30), at(16, 0), tier=Tier("advanced", None, None), now=now)
    assert real_time.state == "behind" and real_time.rule == "real time"
    assert real_time.expected_through_ms == at(15, 59) and real_time.next_due_ms == at(16, 1)
    eod = stock.assess(_assessed_frame(), at(9, 30), at(16, 0), tier=Tier("free", 5, 2), now=now)
    assert eod.state == "current" and eod.rule == "end of day (published overnight)"
    # a complete session: current, nothing missing; a frame of another resolution than the spec's is noted
    whole = stock.assess(session(), at(9, 30), at(16, 0))
    assert whole.state == "current" and whole.completeness == 1.0 and whole.gaps == () and whole.held == ((at(9, 30), at(16, 0)),)
    assert whole.latency_s == 0.0 and whole.missing == 0
    daily = StockSymbolData(resolutions=("day",)).assess(session(), at(9, 30), at(16, 0))
    assert daily.notes == ("minute bars are not among the spec's resolutions (day)",)
    # nothing at all
    empty = stock.assess([], at(9, 30), at(16, 0))
    assert empty.state == "empty" and empty.rows == 0 and empty.completeness == 0.0 and empty.newest_ms is None and empty.latency_s is None
    assert empty.gaps == ((at(9, 30), at(16, 0)),) and empty.held == ()
    # a day frame: UTC days for crypto, New York midnights for stocks
    monday = date(2026, 10, 12)
    week = [bar(utc_day(date(2026, 10, 12 + i))) for i in range(7) if i != 2]
    crypto = CryptoSymbolData().assess(week, utc_day(monday), utc_day(date(2026, 10, 19)), timespan="day")
    assert crypto.expected == 7 and crypto.present == 6 and crypto.gaps == ((utc_day(date(2026, 10, 14)), utc_day(date(2026, 10, 15))),)
    stock_days = StockSymbolData().assess([bar(at(0, 0, d=date(2026, 10, 12 + i))) for i in range(5)], monday, date(2026, 10, 19),
                                          timespan="day")
    assert stock_days.completeness == 1.0 and stock_days.state == "current"


# -- the integrity ------------------------------------------------------------------------------------

def _flawed_session() -> list[dict]:
    """A session with one of each flaw: a high below its low, a minute twice, two rows outside the
    sessions, a spike on ordinary volume, a bar without volume."""
    rows_ = session()
    rows_[60] = {**rows_[60], "h": 99.0}  # 10:30: high below low (and the open and close outside them)
    rows_.append({**rows_[90], "v": 5.0})  # 11:00 twice
    rows_.append(bar(at(2, 0)))  # 02:00: before the extended session opens at 04:00
    rows_.append(bar(at(12, 0, d=date(2026, 10, 18))))  # a Sunday
    rows_[210] = {**rows_[210], "h": 101.5, "l": 98.5}  # 13:00: a range fifteen times its neighbours', on their volume, the price not moving on
    rows_[270] = {**rows_[270], "v": None}  # 14:00: no volume
    return rows_


def test_an_integrity_report_names_the_malformed_duplicate_out_of_session_and_spiky_rows():
    stock = StockSymbolData()
    now = datetime(2026, 10, 21, tzinfo=ET)
    report = stock.integrity(_flawed_session(), now=now)
    assert report.rows == 393 and not report.ok
    assert report.by_rule() == {"malformed": 1, "spike": 1, "duplicate": 1, "out_of_session": 2, "missing": 1, "gap": 1}
    assert report.where("malformed") == (at(10, 30),) and report.where("spike") == (at(13, 0),) and report.where("missing") == (at(14, 0),)
    assert report.where("duplicate") == (at(11, 0),) and report.where("out_of_session") == (at(12, 0, d=date(2026, 10, 18)), at(2, 0))
    assert len(report.problems) == 6 and report.count("gap") == 1
    malformed = next(f for f in report.findings if f.rule == "malformed")
    assert malformed.evidence["failed"] == ["high below low", "open or close outside high and low"] and malformed.evidence["row"] == 60
    assert malformed.evidence["bar"]["high"] == 99.0  # baddata.malformed's evidence, as it gives it
    spike = next(f for f in report.findings if f.rule == "spike")
    assert spike.evidence["range_x"] == 15.0 and spike.evidence["part"] == "regular" and spike.evidence["thresholds"]["range_x"] == baddata.SPIKE_RANGE
    duplicate = next(f for f in report.findings if f.rule == "duplicate")
    assert duplicate.evidence == {"row": 390, "first_row": 90}
    assert next(f for f in report.findings if f.rule == "missing").evidence == {"field": "volume", "row": 270}
    assert all(f.evidence["calendar"] == "stock" for f in report.findings if f.rule == "out_of_session")
    gap = next(f for f in report.findings if f.rule == "gap")  # the Sunday row stretches the calendar: Monday's hours, Tuesday's pre-market
    assert gap.at == at(4, 0, d=date(2026, 10, 19)) and gap.evidence == {"end": at(9, 30), "slots": 960 + 330}
    assert report.findings == tuple(sorted(report.findings, key=lambda f: ((*data.SEVERE, "gap").index(f.rule), f.at)))
    out = json.loads(json.dumps(report.as_dict()))
    assert out["ok"] is False and out["byRule"]["spike"] == 1 and out["findings"][0]["rule"] == "malformed"
    # a clean session: nothing caught, not a gap
    clean = stock.integrity(session(), now=now)
    assert clean.ok and clean.by_rule() == {} and clean.findings == ()
    assert stock.integrity([], now=now).ok and stock.integrity([], now=now).rows == 0
    # gaps alone are reported, not held against a thin stock
    thin = stock.integrity(session()[::2], now=now)
    assert thin.ok and thin.count("gap") == 194 and thin.by_rule() == {"gap": 194}  # the odd minutes up to the last bar
    # a time in the future is malformed (baddata's rule 1), judged against `now`
    late = stock.integrity(session(), now=datetime(2026, 10, 20, 12, 0, tzinfo=ET))
    assert late.count("malformed") == sum(1 for b in session() if b["t"] > at(12, 1)) == 238
    assert late.findings[0].evidence["failed"] == ["time in the future"]


def test_day_bars_are_judged_in_their_zone_and_a_series_is_compared_and_scanned_through_the_modules(monkeypatch):
    monday = date(2026, 10, 12)
    stock_days = [bar(at(0, 0, d=date(2026, 10, 12 + i))) for i in range(5)]
    assert StockSymbolData().integrity(stock_days, timespan="day").ok
    wrong_zone = StockSymbolData().integrity([bar(utc_day(monday))], timespan="day")  # a stock's day stamped at midnight UTC
    assert wrong_zone.where("out_of_session") == (utc_day(monday),)
    saturday = StockSymbolData().integrity(stock_days + [bar(at(0, 0, d=date(2026, 10, 17)))], timespan="day")
    assert saturday.where("out_of_session") == (at(0, 0, d=date(2026, 10, 17)),)
    crypto = CryptoSymbolData().integrity([bar(utc_day(date(2026, 10, 12 + i))) for i in range(7)], timespan="day")
    assert crypto.ok and crypto.findings == ()  # Saturday and Sunday are trading days of a coin
    # two feeds' copies of a span: audit.compare's verdict
    same = StockSymbolData().compare(session(), session())
    assert same["verdict"] == "pass" and same["matched"] == 390 and same["volumeJudged"] is True
    shifted = StockSymbolData().compare(session(), [{**r, "c": r["c"] * 1.01} for r in session()])
    assert shifted["verdict"] == "fail"
    assert CryptoSymbolData().compare(session(), session())["volumeJudged"] is False  # venues: other books
    # the store's rules (a bar against its own finer bars too) through baddata.scan_series, with the asset class
    calls = []
    monkeypatch.setattr(baddata, "scan_series", lambda *a, **k: calls.append(a) or 3)
    assert CryptoSymbolData().scan("store", "coinbase", "BTCUSD", "minute", 1, 2, exchange=None) == 3
    assert calls == [("store", "coinbase", "BTCUSD", "minute", "crypto", 1, 2, None, None)]


def test_a_frame_is_read_in_either_shape():
    wired = rows(session()[:3])
    assert list(wired.columns) == ["ts", "open", "high", "low", "close", "volume", "vwap", "transactions", "row"]
    assert wired["ts"].tolist() == [at(9, 30), at(9, 31), at(9, 32)] and wired["row"].tolist() == [0, 1, 2]
    stored = rows(pd.DataFrame({"ts": [at(9, 31), at(9, 30), at(9, 30)], "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3.0}))
    assert stored["ts"].tolist() == [at(9, 30), at(9, 30), at(9, 31)] and stored["row"].tolist() == [1, 2, 0]  # a stable sort
    assert stored["vwap"].isna().all() and stored["transactions"].isna().all()
    indexed = pd.DataFrame({"open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [3.0]},
                           index=pd.DatetimeIndex([pd.Timestamp(at(9, 30), unit="ms", tz="UTC")]))
    assert rows(indexed)["ts"].tolist() == [at(9, 30)]
    assert rows([]).empty and list(rows([]).columns)[0] == "ts"
    with pytest.raises(ValueError, match="needs a ts"):
        rows(pd.DataFrame({"open": [1.0]}))
    back = wire(rows([bar(at(9, 30), vw=None, n=None)]))
    assert back == [{"t": at(9, 30), "o": 100.0, "h": 100.1, "l": 99.9, "c": 100.0, "v": 1000.0, "vw": None, "n": None}]


# -- the documents and their parts ---------------------------------------------------------------------

FILING = {"source": "edgar", "id": "10-K-1", "agency": "SEC", "title": "Acme 10-K", "url": "https://www.sec.gov/x",
          "published": "2026-03-01T00:00:00+00:00", "kind": "filing", "tags": ["10-K"],
          "pages": [{"number": 1, "paragraphs": ["Acme Corp annual report", {"text": "Item 1. Business", "heading": "Item 1"}]},
                    {"number": 2, "text": ""}, {"number": 4, "text": "Balance sheet"}],
          "statements": [{"name": "balance sheet", "period": "FY2025", "values": {"total_assets": 10.0, "total_liabilities": 4.0, "equity": 6.0},
                          "unit": "USD millions", "source": "page 4"},
                         {"name": "income statement", "period": "FY2025", "values": {"revenue": 3.0, "net_income": None}}]}


def test_a_document_and_its_parts():
    filing = FilingData(publisher="sec")
    doc = filing.document(FILING)
    assert isinstance(doc, Document) and doc.key == ("edgar", "10-K-1") and doc.by == "SEC" and doc.tags == ("10-K",)
    assert doc.published == "2026-03-01T00:00:00+00:00" and doc.published_ms == utc_day(date(2026, 3, 1))
    assert [p.number for p in doc.pages] == [1, 2, 4] and doc.missing_pages() == (3,) and doc.page(3) is None
    first = doc.page(1)
    assert isinstance(first, Page) and [p.text for p in first.paragraphs] == ["Acme Corp annual report", "Item 1. Business"]
    assert first.paragraphs[1] == Paragraph("Item 1. Business", 1, "Item 1") and first.paragraphs[0].words == 4
    assert first.body == "Acme Corp annual report\n\nItem 1. Business" and not first.empty and doc.page(2).empty
    sheet, income = doc.statements
    assert isinstance(sheet, Statement) and sheet.complete and sheet.missing(("total_assets", "equity")) == ()
    assert sheet.missing(("goodwill",)) == ("goodwill",)  # a field the statement must hold, not parsed
    assert not income.complete and income.missing() == ("net_income",) and income.missing(("revenue", "ebit")) == ("ebit", "net_income")
    assert Statement("x", values={"a": float("nan"), "b": 1.0}).missing() == ("a",)
    assert doc.missing(filing.required, filing.statements) == ("page 3 missing", "page 2 empty",
                                                                "statement income statement: net_income not parsed")
    whole = filing.document({**FILING, "pages": FILING["pages"][:1], "statements": FILING["statements"][:1]})
    assert whole.missing(filing.required, filing.statements) == ()
    assert filing.document(doc) is doc  # a Document is given back as it is
    out = json.loads(json.dumps(doc.as_dict()))
    assert out["pages"][0]["paragraphs"][1]["heading"] == "Item 1" and out["statements"][0]["values"]["equity"] == 6.0
    # what the stores hold today: an agency's item, a vendor's headline (its tickers as its tags, a JSON list or names)
    item = ReleaseData().document({"source": "fed-press", "id": "a", "agency": "Fed", "title": "FOMC statement", "url": "https://x",
                                   "published": "2026-10-28T18:00:00+00:00", "kind": "press release", "tags": '["FINRA"]'})
    assert item.by == "Fed" and item.tags == ("FINRA",) and item.published_ms == utc_at(2026, 10, 28, 18) and item.pages == ()
    head = HeadlineData().document({"source": "massive", "id": "1", "published_ms": 1_700_000_000_000, "title": "T", "publisher": "Massive",
                                    "url": "https://m", "tickers": "AAPL,MSFT"})
    assert head.by == "Massive" and head.tags == ("AAPL", "MSFT") and head.published_ms == 1_700_000_000_000
    assert ReleaseData().document({"source": "s", "id": "x", "published": "yesterday"}).published_ms is None


ITEMS = [
    {"source": "fed-press", "id": "a", "agency": "Fed", "title": "FOMC statement", "url": "https://www.federalreserve.gov/a",
     "published": "2026-10-28T18:00:00+00:00", "kind": "press release", "tags": "[]", "fetched_at": "x"},
    {"source": "fed-press", "id": "b", "agency": "Fed", "title": "", "url": "http://www.federalreserve.gov/b",
     "published": "2026-10-28T19:00:00+00:00", "kind": "press release", "tags": ["FINRA"], "fetched_at": "x"},
    {"source": "fed-press", "id": "a", "agency": "Fed", "title": "FOMC statement again", "url": "https://x", "published": "2026-10-28T18:00:00+00:00"},
    {"source": "fed-press", "id": "c", "agency": "Fed", "title": "Later", "url": "https://x", "published": "2027-01-01T00:00:00+00:00"},
    {"source": "fed-press", "id": "d", "agency": "Fed", "title": "No time", "url": "https://x", "published": "yesterday"},
]


def test_the_document_kinds_their_specs_assessment_and_integrity():
    release, headline, filing = ReleaseData(publisher="fed"), HeadlineData(publisher="massive-news"), FilingData(publisher="sec")
    spec = release.spec()
    assert (spec.family, spec.kind, spec.parts) == ("document", "release", ("paragraph", "page", "statement"))
    assert (spec.table, spec.collection, spec.key, spec.order) == ("items", "gov_items", ("source", "id"), "published")
    assert spec.field_names == ("source", "id", "agency", "title", "url", "published", "kind", "tags", "fetched_at")
    assert next(f for f in spec.fields if f.name == "tags").dtype == "list" and spec.required == ("source", "id", "title", "published")
    # the original untouched in MongoDB (or SQLite until it moves), what is derived from it apart and keyed to it
    assert [p.name for p in spec.products] == ["original", "statements", "evaluations"]
    original, statements, evaluations = spec.products
    assert original.engines == ("mongodb", "sqlite") and "liminal.gov_items" in original.where and f"{layout.GOV} (items)" in original.where
    assert statements.derived and statements.keyed_to == "original" and statements.engines == ("sqlite", "duckdb") and "never written back" in statements.holds
    assert evaluations.derived and evaluations.keyed_to == "original" and evaluations.where == f"{layout.MAIN} (evaluations)"
    head = headline.spec()
    assert (head.kind, head.table, head.collection, head.order) == ("headline", "news", "news", "published_ms")
    assert next(f for f in head.fields if f.name == "published_ms").dtype == "int64" and f"{layout.MAIN} (news)" in head.products[0].where
    assert filing.spec().collection == "filings" and dict(filing.spec().statements)["balance sheet"] == ("total_assets", "total_liabilities", "equity")
    assert all(d.check() == [] for d in (release, headline, filing)) and DocumentData().check() == ["DocumentData: no kind"]
    assert json.loads(json.dumps(filing.spec().as_dict()))["statements"]["income statement"] == ["revenue", "net_income"]
    # integrity: a key twice, an empty title, a plain-HTTP link, a time in the future, a time that cannot be read
    now = datetime(2026, 10, 29, tzinfo=ET)
    report = release.integrity(ITEMS, now=now)
    assert report.rows == 5 and not report.ok
    assert [(f.rule, f.at) for f in report.findings] == [("missing_field", "fed-press:b"), ("bad_url", "fed-press:b"),
                                                          ("duplicate", "fed-press:a"), ("bad_time", "fed-press:c"), ("bad_time", "fed-press:d")]
    assert report.findings[0].evidence == {"field": "title", "row": 1} and report.findings[2].evidence == {"row": 2, "first_row": 0}
    assert report.findings[3].evidence["why"] == "in the future" and report.findings[4].evidence == {"row": 4, "value": "yesterday",
                                                                                                        "why": "no time that can be read"}
    assert release.integrity(ITEMS[:1], now=now).ok
    pages = filing.integrity([FILING], now=now)
    assert pages.by_rule() == {"page_missing": 1, "page_empty": 1, "statement_incomplete": 1}
    assert pages.findings[0].evidence == {"row": 0, "pages": [3], "have": [1, 2, 4]} and pages.findings[1].evidence == {"row": 0, "page": 2}
    assert pages.findings[2].evidence == {"row": 0, "statement": "income statement", "period": "FY2025", "fields": ["net_income"]}
    # the assessment: the documents published in the window (one given twice counted once), their completeness, how far
    # the newest trails the window's end, and behind only when something due did not come
    since, until = datetime(2026, 10, 28, tzinfo=ET), datetime(2026, 10, 29, tzinfo=ET)
    a = release.assess(ITEMS, since, until)
    assert (a.kind, a.timespan, a.rows, a.present, a.expected, a.completeness) == ("release", None, 5, 2, None, 0.5)
    assert a.held == () and a.gaps == () and a.missing is None
    assert a.newest_ms == utc_at(2026, 10, 28, 19) and a.latency_s == 9 * 3600.0 and a.state == "current"
    assert a.notes == ("1 document without a time, left out of the window", "1 document given twice, counted once")
    assert release.assess(ITEMS, since, until, due=datetime(2026, 10, 28, 20, tzinfo=ET)).state == "behind"
    due_met = release.assess(ITEMS, since, until, due=datetime(2026, 10, 28, 14, tzinfo=ET))
    assert due_met.state == "current" and due_met.rule == "due" and due_met.next_due_ms == at(14, 0, d=date(2026, 10, 28))
    assert release.assess([], since, until).state == "empty" and release.assess(ITEMS[3:], since, until).completeness is None
    march = filing.assess([FILING], datetime(2026, 3, 1, tzinfo=timezone.utc), datetime(2026, 3, 2, tzinfo=timezone.utc))
    assert march.present == 1 and march.completeness == 0.0  # a page missing: not complete


# -- the contract: a default per asset class and per kind, a plugin's own, the conformance check ---------------

class SipFeed(AcmeFeed):
    """A feed declaring its own stock data: the consolidated tape (plugin API 2.1)."""
    api = "2.1"
    data = {"stock": SipStock}


def test_the_contract_gives_a_default_data_class_and_a_plugin_declares_its_own():
    acme = AcmeFeed()
    stock = acme.data_for("stock")
    assert type(stock) is StockSymbolData and stock.feed == "acme" and acme.data == {} and acme.api == "2.0"  # the default stays 2.0
    assert type(acme.data_for("crypto")) is CryptoSymbolData and acme.data_for("crypto").spec().kind == "crypto"
    with pytest.raises(ValueError, match="no data class for 'bonds'"):
        acme.data_for("bonds")
    record = acme_record()
    assert type(record.data_for("stock")) is StockSymbolData and record.data_for("stock").feed == "acme"  # a record: the defaults
    sip = SipFeed()
    assert type(sip.data_for("stock")) is SipStock and sip.data_for("stock").market == "sip" and sip.data_for("stock").feed == "acme"
    assert type(sip.data_for("crypto")) is CryptoSymbolData  # not declared: the contract's
    assert type(acme.with_(data={"stock": SipStock}).data_for("stock")) is SipStock and type(acme.data_for("stock")) is StockSymbolData
    news = AcmeNews()
    assert type(news.document_data()) is ReleaseData and news.document_data().publisher == "acme-news" and news.data is None

    class Wire(AcmeNews):
        kind = "vendor"

    class Filings(AcmeNews):
        api = "2.1"
        data = FilingData
    assert type(Wire().document_data()) is HeadlineData and type(Filings().document_data()) is FilingData
    assert data.default_series("stock") is StockSymbolData and data.default_series("bonds") is None
    assert data.default_document("vendor") is HeadlineData and data.default_document("research") is ReleaseData
    assert data.default_document("anything") is ReleaseData


def test_the_conformance_check_reads_the_data_declarations():
    assert problems(SipFeed()) == [] and problems(AcmeFeed()) == [] and problems(acme_record()) == [] and problems(AcmeNews()) == []

    class WrongClass(SipFeed):
        data = {"crypto": SipStock}

    class NotAClass(SipFeed):
        data = {"stock": object}

    class NotItsAsset(SipFeed):
        data = {"fx": FxData}

    class Early(AcmeFeed):
        data = {"stock": SipStock}  # declared, but written for 2.0

    class Odd(StockSymbolData):
        resolutions = ("hour",)

    class OddFeed(SipFeed):
        data = {"stock": Odd}

    class Loose(SipFeed):
        data = ("stock", SipStock)
    assert problems(WrongClass()) == ["acme: data['crypto'] is SipStock, stock's data"]
    assert problems(NotAClass()) == ["acme: data['stock'] must be a TimeSeriesData subclass"]
    assert problems(NotItsAsset()) == ["acme: data 'fx', not one of its assets"]
    assert problems(Early()) == ["acme: declares data (plugin API 2.1) but is written for 2.0"]
    assert problems(OddFeed()) == ["acme: data_for('stock'): Odd: resolutions ('hour',) must be among ('second', 'minute', 'day')"]
    assert problems(Loose()) == ["acme: data must map an asset class to a TimeSeriesData subclass"]

    class BadNews(AcmeNews):
        data = object

    class EarlyNews(AcmeNews):
        data = FilingData

    class Filings(AcmeNews):
        api = "2.1"
        data = FilingData

    class Kindless(DocumentData):
        pass

    class KindlessNews(Filings):
        data = Kindless
    assert problems(BadNews()) == ["acme-news: data must be a DocumentData subclass or None",
                                   "acme-news: declares data (plugin API 2.1) but is written for 2.0"]
    assert problems(EarlyNews()) == ["acme-news: declares data (plugin API 2.1) but is written for 2.0"]
    assert problems(Filings()) == []
    assert problems(KindlessNews()) == ["acme-news: document_data(): Kindless: no kind"]


def test_a_plugin_written_for_2_0_loads_under_2_1(monkeypatch):
    assert PLUGIN_API == "2.1" and api_refusal("2.0") is None and api_refusal("2.1") is None and api_refusal("2") is None
    assert "needs a newer app" in api_refusal("2.2") and "needs a release for 2.x" in api_refusal("1.12")
    assert api_refusal("1.12", record=True) is None and "record" in api_refusal("2.1", record=True)
    monkeypatch.setattr(layout, "plugins_dir", lambda: None)

    class Later(SipFeed):
        name, api = "later", "2.2"
    plugins_ = (AcmeFeed(), SipFeed().with_(name="sip"), Later())
    eps = [SimpleNamespace(name=p.name, value=f"fake_{p.name}:PLUGIN", load=(lambda p=p: p)) for p in plugins_]
    monkeypatch.setattr(registry, "_entry_points", lambda: eps)
    try:
        found = registry.reload()
        assert list(found) == ["acme", "sip"] and found["acme"].api == "2.0" and found["sip"].api == "2.1"
        assert type(found["sip"].data_for("stock")) is SipStock and type(found["acme"].data_for("stock")) is StockSymbolData
        assert dict(registry.errors()) == {"later (fake_later:PLUGIN)": "later: written for plugin API 2.2, newer than this app's 2.1: "
                                                                        "it needs a newer app"}
    finally:
        registry._providers = None
