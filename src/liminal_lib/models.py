"""Domain model: an instrument trades on an exchange, which is in a country.

These are "belongs to" relationships, so they are composed rather than
inherited. Inheritance is used where behaviour genuinely differs: kinds of
instrument here, and data sources in `sources/`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import ClassVar


@dataclass(frozen=True)
class Country:
    code: str  # ISO 3166-1 alpha-2, e.g. "US"
    name: str
    timezone: str  # IANA zone of its main market, e.g. "America/New_York"


COUNTRIES: dict[str, Country] = {
    "US": Country("US", "United States", "America/New_York"),
    "CA": Country("CA", "Canada", "America/Toronto"),
    "GB": Country("GB", "United Kingdom", "Europe/London"),
    "DE": Country("DE", "Germany", "Europe/Berlin"),
    "JP": Country("JP", "Japan", "Asia/Tokyo"),
    "AU": Country("AU", "Australia", "Australia/Sydney"),
    "FR": Country("FR", "France", "Europe/Paris"),
    "NL": Country("NL", "Netherlands", "Europe/Amsterdam"),
    "BE": Country("BE", "Belgium", "Europe/Brussels"),
    "PT": Country("PT", "Portugal", "Europe/Lisbon"),
    "IT": Country("IT", "Italy", "Europe/Rome"),
    "CH": Country("CH", "Switzerland", "Europe/Zurich"),
    "ES": Country("ES", "Spain", "Europe/Madrid"),
    "HK": Country("HK", "Hong Kong", "Asia/Hong_Kong"),
    "CN": Country("CN", "China", "Asia/Shanghai"),
    "TW": Country("TW", "Taiwan", "Asia/Taipei"),
    "KR": Country("KR", "South Korea", "Asia/Seoul"),
    "IN": Country("IN", "India", "Asia/Kolkata"),
    "BR": Country("BR", "Brazil", "America/Sao_Paulo"),
    "ZA": Country("ZA", "South Africa", "Africa/Johannesburg"),
    "SG": Country("SG", "Singapore", "Asia/Singapore"),
}


def country(code: str | None) -> Country | None:
    return COUNTRIES.get(code.upper()) if code else None


@dataclass(frozen=True)
class Venue:
    """A stock exchange the app knows by name (the user, 2026-11-02: stocks on foreign exchanges):
    its MIC, its country (the timezone), the suffix a listing there carries in its ticker
    (`7203.T`, Reuters-style; none for the US, whose tickers stay bare) and the names people type
    for it (`TSE:7203`)."""

    mic: str
    name: str
    country: str
    suffix: str | None = None
    aliases: tuple[str, ...] = ()


VENUES: tuple[Venue, ...] = (
    Venue("XNYS", "New York Stock Exchange", "US", aliases=("NYSE",)),
    Venue("XNAS", "Nasdaq", "US", aliases=("NASDAQ",)),
    Venue("ARCX", "NYSE Arca", "US", aliases=("ARCA", "NYSEARCA")),
    Venue("XASE", "NYSE American", "US", aliases=("AMEX", "NYSEAMERICAN")),
    Venue("BATS", "Cboe BZX", "US", aliases=("CBOE", "BATS")),
    Venue("IEXG", "IEX", "US", aliases=("IEX",)),
    Venue("XTSE", "Toronto Stock Exchange", "CA", "TO", ("TSX",)),
    Venue("XTSX", "TSX Venture Exchange", "CA", "V", ("TSXV",)),
    Venue("XLON", "London Stock Exchange", "GB", "L", ("LSE",)),
    Venue("XETR", "Xetra", "DE", "DE", ("XETRA",)),
    Venue("XFRA", "Frankfurt Stock Exchange", "DE", "F", ("FRANKFURT",)),
    Venue("XPAR", "Euronext Paris", "FR", "PA", ("EURONEXTPARIS", "PARIS")),
    Venue("XAMS", "Euronext Amsterdam", "NL", "AS", ("EURONEXTAMSTERDAM", "AMSTERDAM")),
    Venue("XBRU", "Euronext Brussels", "BE", "BR", ("EURONEXTBRUSSELS", "BRUSSELS")),
    Venue("XLIS", "Euronext Lisbon", "PT", "LS", ("EURONEXTLISBON", "LISBON")),
    Venue("XMIL", "Euronext Milan", "IT", "MI", ("EURONEXTMILAN", "BORSAITALIANA", "MILAN")),
    Venue("XSWX", "SIX Swiss Exchange", "CH", "SW", ("SIX",)),
    Venue("XMAD", "Bolsa de Madrid", "ES", "MC", ("BME", "MADRID")),
    Venue("XTKS", "Tokyo Stock Exchange", "JP", "T", ("TSE", "JPX", "TYO", "TOKYO")),
    Venue("XHKG", "Hong Kong Exchanges", "HK", "HK", ("HKEX",)),
    Venue("XSHG", "Shanghai Stock Exchange", "CN", "SS", ("SSE", "SHANGHAI")),
    Venue("XSHE", "Shenzhen Stock Exchange", "CN", "SZ", ("SZSE", "SHENZHEN")),
    Venue("XTAI", "Taiwan Stock Exchange", "TW", "TW", ("TWSE",)),
    Venue("ROCO", "Taipei Exchange", "TW", "TWO", ("TPEX",)),
    Venue("XKRX", "Korea Exchange", "KR", "KS", ("KRX", "KOSPI")),
    Venue("XKOS", "KOSDAQ", "KR", "KQ", ("KOSDAQ",)),
    Venue("XNSE", "National Stock Exchange of India", "IN", "NS", ("NSE",)),
    Venue("XBOM", "BSE", "IN", "BO", ("BSE", "BOMBAY")),
    Venue("XASX", "Australian Securities Exchange", "AU", "AX", ("ASX",)),
    Venue("BVMF", "B3", "BR", "SA", ("B3", "BOVESPA")),
    Venue("XJSE", "Johannesburg Stock Exchange", "ZA", "JO", ("JSE",)),
    Venue("XSES", "Singapore Exchange", "SG", "SI", ("SGX",)),
)
_VENUES = {v.mic: v for v in VENUES}
SUFFIX_MICS: dict[str, str] = {v.suffix: v.mic for v in VENUES if v.suffix}
# Friendly exchange names -> ISO 10383 MIC
EXCHANGE_ALIASES: dict[str, str] = {alias: v.mic for v in VENUES for alias in v.aliases}


def exchange_mic(name: str) -> str:
    """'NASDAQ' -> 'XNAS'; a MIC is returned unchanged."""
    key = name.strip().upper().replace(" ", "")
    return EXCHANGE_ALIASES.get(key, key)


def venue(mic: str | None) -> Venue | None:
    return _VENUES.get(mic.strip().upper()) if mic else None


def is_foreign(mic: str | None) -> bool:
    """Whether a MIC is an exchange outside the US that the app knows. None, a US MIC and an
    unknown code read as the US: every row from before keeps its ways."""
    v = venue(mic)
    return v is not None and v.country != "US"


def mic_suffix(mic: str | None) -> str | None:
    v = venue(mic)
    return v.suffix if v else None


def suffix_mic(suffix: str | None) -> str | None:
    return SUFFIX_MICS.get(suffix.strip().upper()) if suffix else None


def exchange_timezone(mic: str | None) -> str:
    """The IANA zone of an exchange's days (New York for the US and for anything unknown)."""
    v = venue(mic)
    return COUNTRIES[v.country].timezone if v else "America/New_York"


def exchange_name(mic: str | None) -> str | None:
    v = venue(mic)
    return v.name if v else None


def listing_ticker(ticker: str, mic: str | None) -> str:
    """The stored form of a listing: `7203.T` for 7203 on XTKS (the same again for `7203.T`), the
    bare ticker for the US or an exchange without a suffix."""
    t = ticker.strip().upper()
    suffix = mic_suffix(mic)
    if not suffix or t.endswith("." + suffix):
        return t
    return f"{t}.{suffix}"


def parse_symbol(text: str) -> tuple[str, str | None]:
    """'SPCX::NASDAQ' or 'NASDAQ:SPCX' -> ('SPCX', 'XNAS'); 'SPCX' -> ('SPCX', None); 'TSE:7203',
    '7203::XTKS' and '7203.T' -> ('7203.T', 'XTKS'): the one grammar of symbols.stock_spec."""
    from .symbols import stock_spec  # symbols imports this module: a lazy import

    text = text.strip()
    if "::" in text:
        ticker, exch = text.split("::", 1)
        spec = stock_spec(ticker, exchange_mic(exch))
    elif ":" in text:
        exch, ticker = text.split(":", 1)
        spec = stock_spec(ticker, exchange_mic(exch))
    else:
        spec = stock_spec(text)
    return spec.ticker, spec.exchange


@dataclass(frozen=True)
class Exchange:
    source: str
    id: int  # the source's own exchange id (appears on every trade)
    name: str
    country: Country | None
    mic: str | None = None  # ISO 10383 market identifier, e.g. "XNAS"
    acronym: str | None = None
    type: str | None = None  # exchange | TRF | SIP ...

    @property
    def timezone(self) -> str:
        return self.country.timezone if self.country else "UTC"


@dataclass(frozen=True)
class Instrument:
    """Something that trades. Subclasses describe specific kinds."""

    kind: ClassVar[str] = "instrument"
    ticker: str
    name: str | None = None
    primary_exchange: Exchange | None = None
    currency: str = "USD"
    # Symbols get reused: the listing date and FIGI identify *this* instrument.
    list_date: date | None = None
    figi: str | None = None
    active: bool = True
    # The source's reference record as received (description, market cap, address, ...).
    details: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def country(self) -> Country | None:
        return self.primary_exchange.country if self.primary_exchange else None

    def describe(self) -> str:
        where = self.primary_exchange.name if self.primary_exchange else "unknown exchange"
        return f"{self.ticker} ({self.kind}, {self.name or 'unnamed'}) on {where}"


@dataclass(frozen=True)
class Stock(Instrument):
    kind: ClassVar[str] = "stock"


@dataclass(frozen=True)
class ETF(Instrument):
    kind: ClassVar[str] = "etf"
    holdings: tuple[str, ...] = field(default=())


# Source type codes -> Instrument subclass
_KINDS: dict[str, type[Instrument]] = {
    "CS": Stock, "ADRC": Stock, "PFD": Stock,
    "ETF": ETF, "ETN": ETF, "ETV": ETF, "ETS": ETF,
}


def instrument_class(type_code: str | None) -> type[Instrument]:
    return _KINDS.get((type_code or "").upper(), Instrument)
