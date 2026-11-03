"""Stocks listed outside the US (the user, 2026-11-02): one stored name per listing, TICKER.SUFFIX
(models.VENUES), whichever way it is typed; US tickers and pins as before; a feed carries them only
when it says so (Provider.exchanges, plugin API 1.12)."""
from __future__ import annotations

import pytest

from liminal_lib import models
from liminal_lib.models import COUNTRIES, VENUES, exchange_mic, exchange_timezone, is_foreign, listing_ticker, parse_symbol
from liminal_lib.sources.registry import Provider
from liminal_lib.symbols import resolve


@pytest.mark.parametrize("typed, ticker, exchange", [
    ("7203.T", "7203.T", "XTKS"), ("TSE:7203", "7203.T", "XTKS"), ("7203::XTKS", "7203.T", "XTKS"),
    ("tse:7203.t", "7203.T", "XTKS"), ("2330.TW", "2330.TW", "XTAI"), ("TWSE:2330", "2330.TW", "XTAI"),
    ("PETR4.SA", "PETR4.SA", "BVMF"), ("B3:PETR4", "PETR4.SA", "BVMF"), ("600519.SS", "600519.SS", "XSHG"),
    ("000001.SZ", "000001.SZ", "XSHE"), ("VOD.L", "VOD.L", "XLON"), ("LSE:VOD", "VOD.L", "XLON"),
    ("SHOP.TO", "SHOP.TO", "XTSE"), ("SAP.DE", "SAP.DE", "XETR"), ("MC.PA", "MC.PA", "XPAR"),
    ("ASML.AS", "ASML.AS", "XAMS"), ("0700.HK", "0700.HK", "XHKG"), ("005930.KS", "005930.KS", "XKRX"),
    ("RELIANCE.NS", "RELIANCE.NS", "XNSE"), ("BHP.AX", "BHP.AX", "XASX"), ("NESN.SW", "NESN.SW", "XSWX"),
    ("ENEL.MI", "ENEL.MI", "XMIL"),
])
def test_a_listing_abroad_is_stored_as_ticker_dot_suffix_however_it_is_typed(typed, ticker, exchange):
    spec = resolve(typed)
    assert (spec.ticker, spec.asset, spec.source_ticker, spec.exchange) == (ticker, "stock", ticker, exchange)
    assert parse_symbol(typed) == (ticker, exchange)


def test_us_tickers_and_pins_are_as_before():
    assert resolve("SPCX::NASDAQ").ticker == "SPCX" and resolve("SPCX::NASDAQ").exchange == "XNAS"
    assert resolve("nasdaq:spcx").ticker == "SPCX" and resolve("SPCX").exchange is None
    for cls in ("BRK.B", "BF.B"):  # a share class, not a suffix
        assert resolve(cls).ticker == cls and resolve(cls).exchange is None
    assert resolve("7203.T::NASDAQ").exchange == "XNAS"  # an explicit US pin wins over a suffix
    assert resolve("NQ1!").asset == "future" and resolve("EURUSD").asset == "fx" and resolve("BTCUSD").asset == "crypto"
    assert parse_symbol("SPCX::NASDAQ") == ("SPCX", "XNAS") and parse_symbol("SPCX") == ("SPCX", None)


def test_the_venues_table():
    assert exchange_mic("TSE") == "XTKS" and exchange_mic("TSX") == "XTSE" and exchange_mic("EURONEXT PARIS") == "XPAR"
    for alias, mic in (("TWSE", "XTAI"), ("B3", "BVMF"), ("BOVESPA", "BVMF"), ("SSE", "XSHG"), ("SZSE", "XSHE"),
                       ("HKEX", "XHKG"), ("KRX", "XKRX"), ("KOSDAQ", "XKOS"), ("NSE", "XNSE"), ("BSE", "XBOM"),
                       ("SIX", "XSWX"), ("BME", "XMAD"), ("JSE", "XJSE"), ("SGX", "XSES"), ("NASDAQ", "XNAS")):
        assert exchange_mic(alias) == mic
    assert all(v.country in COUNTRIES for v in VENUES)
    assert not is_foreign(None) and not is_foreign("XNAS") and not is_foreign("OTC") and is_foreign("XTKS")
    assert exchange_timezone("XTKS") == "Asia/Tokyo" and exchange_timezone("XNAS") == "America/New_York"
    assert exchange_timezone(None) == "America/New_York"
    assert listing_ticker("7203", "XTKS") == "7203.T" == listing_ticker("7203.T", "XTKS")
    assert listing_ticker("AAPL", "XNAS") == "AAPL" and listing_ticker("vod", "XLON") == "VOD.L"
    assert models.exchange_name("XTAI") == "Taiwan Stock Exchange" and models.exchange_name("ZZZZ") is None
    suffixes = [v.suffix for v in VENUES if v.suffix]
    assert len(suffixes) == len(set(suffixes))  # one exchange per suffix


def _provider(**kw) -> Provider:
    return Provider(name="t", title="T", build=lambda s, b: None, assets=frozenset({"stock"}), **kw)


def test_a_feed_carries_a_listing_abroad_only_when_it_says_so():
    us_only = _provider()
    assert us_only.can_carry(resolve("AAPL")) and us_only.can_carry(resolve("SPCX::NASDAQ"))
    assert not us_only.can_carry(resolve("7203.T"))
    abroad = _provider(exchanges=frozenset({"XTKS"}))
    assert abroad.can_carry(resolve("7203.T")) and not abroad.can_carry(resolve("2330.TW"))
    assert not abroad.can_carry(resolve("AAPL"))  # it said XTKS alone
    both = _provider(exchanges=frozenset({"US", "XSHG", "XSHE"}))
    assert both.can_carry(resolve("AAPL")) and both.can_carry(resolve("600519.SS")) and not both.can_carry(resolve("7203.T"))
    told = _provider(carries=lambda spec: spec.ticker == "7203.T")
    assert told.can_carry(resolve("7203.T")) and not told.can_carry(resolve("AAPL"))  # its own list has the say
