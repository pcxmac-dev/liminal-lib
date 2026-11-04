"""When should new data be available? Market calendars and each plan's publication delay.

`expected()` says, for an asset class on a plan at a moment, the newest bar time the source
should already have published and when the next new data is due. `assess()` compares that
with what is stored. Nothing here calls the API; `collector.scan` uses it to decide which
symbols are worth a (cheap) check.

Publication delays (observed on Massive, Oct 2026; adjust PLAN_DELAYS if the plan changes):
- free plan, stocks and indices: end of day. A session's minute bars appear overnight; they
  were present by 03:27 ET the next morning and absent at 21:23 ET the same evening.
- free plan, currencies and crypto: through the end of the previous UTC day, shortly after
  00:00 UTC (BTCUSD probed 2026-10-23 04:17 UTC: newest minute 23:59 UTC the day before).
- futures (Basic): 8 hours delayed.
- paid stock plans: 15 minutes delayed (Starter, Developer) or real time (Advanced).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import Tier, asset_tier
from .sessions import MarketCalendar

ET = ZoneInfo("America/New_York")

# Session-based publication: a session's data appears this long after midnight ET following it.
EOD_READY_AFTER_MIDNIGHT = timedelta(hours=4)
# The source may lag its stated delay a little, and thin symbols skip minutes without trades.
GRACE = timedelta(minutes=30)


@dataclass(frozen=True)
class Delay:
    kind: str  # "eod" (session published overnight) | "utc_day" (previous UTC day) | "delay"
    lag: timedelta = timedelta(0)

    def describe(self) -> str:
        if self.kind == "eod":
            late = f", {self.lag.days} days late" if self.lag else ""
            return f"end of day (published overnight{late})"
        if self.kind == "utc_day":
            return "previous UTC day (after 00:00 UTC)"
        minutes = int(self.lag.total_seconds() // 60)
        return "real time" if minutes == 0 else f"{minutes // 60} h delayed" if minutes % 60 == 0 else f"{minutes} min delayed"


PLAN_DELAYS: dict[str, dict[str, Delay]] = {
    "free": {"stock": Delay("eod"), "index": Delay("eod"), "fx": Delay("utc_day"), "crypto": Delay("utc_day"),
             "future": Delay("delay", timedelta(hours=8))},
    "starter": {"stock": Delay("delay", timedelta(minutes=15)), "index": Delay("delay", timedelta(minutes=15)),
                "fx": Delay("delay", timedelta(minutes=15)), "crypto": Delay("delay", timedelta(minutes=15)), "future": Delay("delay", timedelta(hours=8))},
    "developer": {"stock": Delay("delay", timedelta(minutes=15)), "index": Delay("delay", timedelta(minutes=15)),
                  "fx": Delay("delay", timedelta(minutes=15)), "crypto": Delay("delay", timedelta(minutes=15)), "future": Delay("delay", timedelta(hours=8))},
    "advanced": {"stock": Delay("delay"), "index": Delay("delay"), "fx": Delay("delay"), "crypto": Delay("delay"),
                 "future": Delay("delay", timedelta(hours=8))},
}


# The crypto exchanges' public candles: each minute once it closes (no delay, no key).
for _venue in ("coinbase-public", "bitstamp-public", "bitfinex-public"):
    PLAN_DELAYS[_venue] = {"crypto": Delay("delay")}

# Alpaca: Basic serves everything but the latest 15 minutes; crypto is never held back.
# (16 minutes: the requests stop a minute inside the 15-minute block, sources/alpaca.RECENT_SIP_BLOCK,
# so the ledger must not count the minute between as held.)
PLAN_DELAYS["alpaca-basic"] = {"stock": Delay("delay", timedelta(minutes=16)), "crypto": Delay("delay"),
                               "overnight": Delay("delay", timedelta(minutes=16))}
PLAN_DELAYS["alpaca-plus"] = {"stock": Delay("delay"), "crypto": Delay("delay"), "overnight": Delay("delay")}

# Tiingo's forex (sources/plugins/tiingo.py, FOREX: tiingo (forex)), taken for history: whole UTC days, through the day
# before (how soon its newest minutes are final is not known; a minute asked too soon and found
# empty would be counted as held and never asked again).
PLAN_DELAYS["tiingofx"] = {"fx": Delay("utc_day")}

# The exchanges abroad (2026-11-02, the user: stocks on foreign exchanges): daily bars, end of day on
# the listing's own calendar (sessions.local_day_session). J-Quants' free plan publishes twelve weeks
# late: its days are asked for as the weeks pass (a day asked too soon and found empty would count as
# held). Twelve Data's forex: whole UTC days, like Tiingo's.
for _tier in ("twse", "tpex", "b3", "alphavantage-free", "jquants-light", "jquants-standard", "jquants-premium"):
    PLAN_DELAYS[_tier] = {"stock": Delay("eod")}
PLAN_DELAYS["jquants-free"] = {"stock": Delay("eod", timedelta(weeks=12))}
PLAN_DELAYS["twelvedata-free"] = {"fx": Delay("utc_day")}

# Cboe's VIX futures files (plugin-integrator-cboe's cboefutures feed): a contract's day comes with its
# settlement after the session, so its day bars are expected once the session is over, like a
# stock's on an end-of-day plan (the futures session dates: CME's, 18:00 to 17:00 ET).
PLAN_DELAYS["cboe-futures"] = {"future": Delay("eod")}

# A feed whose market keeps hours of its own: Blue Ocean (sources/plugins/alpaca.py, BOATS) trades US stocks
# overnight only, 20:00-04:00 ET (MarketCalendar's "overnight"). Judged by the stock day, its nights
# looked closed: it was asked only up to 20:00, so a night's bars came the next morning, and a stock
# opened in the chart at 23:00 read up to date with the night going on.
FEED_HOURS = {"boats": {"stock": "overnight"}}


class Delays:
    """The publication delays of every plan and the market hours of every feed: what the library
    holds (PLAN_DELAYS, FEED_HOURS) and what each feed plugin declares of its own when it loads
    (FeedPlugin.delays and hours, plugin API 2.0: `declare`, from the registry)."""

    def __init__(self, plans: dict[str, dict[str, Delay]], feed_hours: dict[str, dict[str, str]]):
        self.plans = plans  # tier name -> asset class -> Delay
        self.feed_hours = feed_hours  # feed -> asset class -> the calendar it keeps (MarketCalendar's kinds)

    def of(self, tier: Tier, asset: str) -> Delay:
        """A plan's publication delay for an asset class (end of day where neither says)."""
        tier = asset_tier(tier, asset)  # Massive: each asset class on its own plan
        return self.plans.get(tier.name, self.plans["free"]).get(asset, Delay("eod"))

    def hours(self, feed: str | None, asset: str) -> str:
        """The calendar a feed's asset class keeps (what `expected`, `history_horizon` and the coverage
        planner take as the asset): its market's own hours, else the asset class's."""
        return self.feed_hours.get(feed or "", {}).get(asset, asset)

    def declare(self, plugin) -> bool:
        """A feed plugin's own rows (its `delays`: tier name -> asset -> Delay; its `hours`: asset ->
        calendar), kept here under its names. Returns whether anything changed (the expectations
        cached are from before then, and cleared)."""
        changed = False
        for tier, by_asset in (getattr(plugin, "delays", None) or {}).items():
            rows = dict(by_asset)
            if self.plans.get(tier) != rows:
                self.plans[tier] = rows
                changed = True
        hours = dict(getattr(plugin, "hours", None) or {})
        if hours and self.feed_hours.get(plugin.name) != hours:
            self.feed_hours[plugin.name] = hours
            changed = True
        if changed:
            expected.cache_clear()
        return changed


DELAYS = Delays(PLAN_DELAYS, FEED_HOURS)


def hours(feed: str | None, asset: str) -> str:
    """The calendar a feed's asset class keeps (Delays.hours)."""
    return DELAYS.hours(feed, asset)


def plan_delay(tier: Tier, asset: str) -> Delay:
    return DELAYS.of(tier, asset)


# A plan that publishes as it trades may not have sent its newest minute yet, and late prints still
# change it: history counts that plan's data held only once it is this far behind real time (a
# minute recorded held the moment it ended stayed missing for good when it came late).
SETTLE = timedelta(minutes=3)


def settle(tier: Tier, asset: str) -> timedelta:
    """How much later than its stated delay a plan's newest data counts as final: what SETTLE
    exceeds a delay by (a real-time plan: SETTLE; 15 minutes delayed: nothing)."""
    d = plan_delay(tier, asset)
    return max(SETTLE - d.lag, timedelta(0)) if d.kind == "delay" else timedelta(0)


SPAN_MS = {"second": 1_000, "minute": 60_000, "hour": 3_600_000, "day": 86_400_000}


def published_at(ts_ms, timespan: str, asset: str, tier: Tier):
    """When bars starting at `ts_ms` (ms, array) became available on this plan (ms array): the
    bar's end plus the plan's delay; on an end-of-day plan 04:00 ET after the session's day; on a
    previous-UTC-day plan the UTC midnight after the bar. For point-in-time reads (as_of)."""
    import numpy as np
    import pandas as pd

    ts = np.asarray(ts_ms, dtype="int64")
    end = ts + SPAN_MS.get(timespan, 60_000)
    delay = plan_delay(tier, asset)
    if delay.kind == "delay":
        return end + int(delay.lag.total_seconds() * 1000)
    if delay.kind == "utc_day":
        day = SPAN_MS["day"]
        return (end - 1) // day * day + day
    t = pd.to_datetime(ts, unit="ms", utc=True).tz_convert(ET).normalize() + pd.DateOffset(days=1)
    return (t + pd.Timedelta(EOD_READY_AFTER_MIDNIGHT)).tz_convert("UTC").as_unit("ms").asi8


# -- calendar (one source of truth: sessions.MarketCalendar) ---------------------------

CALENDAR = MarketCalendar()


def use_official(store, source: str = "massive") -> bool:
    """Give CALENDAR the official schedules a source sent (upcoming holidays and early closes,
    futures sessions: collector.refresh_calendars), as the chart's calendar has them
    (gui/bars.BarEngine.calendar). Without them an unscheduled closure was a trading day here, and
    every symbol's newest session was asked for all day. Returns whether they changed."""
    from .sessions import overrides_from_store

    official = overrides_from_store(store, source)
    if official == CALENDAR.overrides:
        return False
    CALENDAR.overrides = official
    expected.cache_clear()  # its answers came from the calendar as it was
    return True


def is_trading_day(d: date, exchange: str | None = None) -> bool:
    return CALENDAR.is_trading_day(d, exchange)


def session_hours(asset: str, d: date, exchange: str | None = None) -> tuple[datetime, datetime] | None:
    """(first bar start, end) of the US session for stocks (extended hours) or indices; for a stock
    listed outside the US (`exchange`, its MIC), of its local calendar day."""
    s = CALENDAR.sessions(asset, d, d, exchange=exchange)
    return (s[0].open, s[0].close) if s else None


def previous_session(d: date, exchange: str | None = None) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d, exchange):
        d -= timedelta(days=1)
    return d


def next_session(d: date, exchange: str | None = None) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d, exchange):
        d += timedelta(days=1)
    return d


def history_horizon(asset: str, tier: Tier, now: datetime, exchange: str | None = None) -> datetime:
    """The moment a history fetch looks from: `now`, except for a stock or index on a plan that
    publishes as it trades (real time or delayed): the moment its last completed session was fully
    published and settled (that session's end, extended hours included, plus the plan's delay and
    `settle`). The session under way belongs to the live feeds; history takes completed sessions
    only (an end-of-day plan has only those anyway). Blue Ocean's nights ("overnight") likewise: at
    `now`, every stock on it was due again with each hourly run of the night, about 24,000 requests
    a day of Alpaca's 30,000 spent on the night under way. Other assets trade around the clock: `now`."""
    delay = plan_delay(tier, asset)
    if asset not in ("stock", "index", "overnight") or delay.kind != "delay":
        return now
    t = now.astimezone(ET)
    lag = delay.lag + settle(tier, asset)
    d = t.date() if is_trading_day(t.date(), exchange) else previous_session(t.date(), exchange)
    for _ in range(15):  # a holiday week at most
        hours = session_hours(asset, d, exchange)
        if hours and hours[1] + lag <= t:
            return hours[1] + lag
        d = previous_session(d, exchange)
    return now


def _open(asset: str, t: datetime, exchange: str | None = None) -> bool:
    return CALENDAR.is_open(asset, t, exchange=exchange)


def last_open_minute(asset: str, before: datetime, limit_days: int = 10, exchange: str | None = None) -> datetime | None:
    """Start of the newest one-minute bar that ends at or before `before` while the market is open."""
    t = before.astimezone(ET)
    sessions = CALENDAR.sessions(asset, (t - timedelta(days=limit_days)).date(), t.date() + timedelta(days=1),
                                 exchange=exchange)
    for s in reversed(sessions):
        if s.open < t:
            end = min(s.close, t)
            return (end - timedelta(minutes=1)).replace(second=0, microsecond=0)
    return None


def next_open_minute(asset: str, after: datetime, limit_days: int = 10, exchange: str | None = None) -> datetime | None:
    t = after.astimezone(ET)
    for s in CALENDAR.sessions(asset, t.date() - timedelta(days=1), t.date() + timedelta(days=limit_days),
                               exchange=exchange):
        if s.close > t:
            return max(s.open, t).replace(second=0, microsecond=0)
    return None


# -- expectations ----------------------------------------------------------------------

@dataclass(frozen=True)
class Expectation:
    latest: datetime | None  # newest bar start the source should have published (ET)
    next_due: datetime | None  # when the next new data should appear
    rule: str  # the plan rule applied, for display


def _eod_ready(session: date) -> datetime:
    return datetime.combine(session + timedelta(days=1), time(0), ET) + EOD_READY_AFTER_MIDNIGHT


@lru_cache(maxsize=512)  # the same for every symbol of an asset class (and exchange abroad) at one moment
def expected(asset: str, tier: Tier, now: datetime, exchange: str | None = None) -> Expectation:
    """What a plan should have published by `now` for an asset class; for a stock listed outside
    the US, `exchange` (its MIC) gives the days (sessions.local_day_session)."""
    now = now.astimezone(ET)
    delay = plan_delay(tier, asset)
    if delay.kind == "eod" and asset in ("stock", "index", "future"):
        cut = now - delay.lag  # a plan publishing its days late (J-Quants' free plan): the day that far back
        s = cut.date() if is_trading_day(cut.date(), exchange) else previous_session(cut.date(), exchange)
        while _eod_ready(s) > cut:
            s = previous_session(s, exchange)
        latest = session_hours(asset, s, exchange)[1] - timedelta(minutes=1)
        return Expectation(latest, _eod_ready(next_session(s, exchange)) + delay.lag, delay.describe())
    if delay.kind == "utc_day":
        midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        latest = last_open_minute(asset, midnight, exchange=exchange)
        nxt = midnight + timedelta(days=1)
        while next_open_minute(asset, nxt - timedelta(days=1), exchange=exchange) is None or \
                next_open_minute(asset, nxt - timedelta(days=1), exchange=exchange) >= nxt:  # a UTC day with no trading adds nothing
            nxt += timedelta(days=1)
        return Expectation(latest, nxt.astimezone(ET), delay.describe())
    cutoff = now - delay.lag
    latest = last_open_minute(asset, cutoff, exchange=exchange)
    if _open(asset, cutoff, exchange):
        nxt = now + timedelta(minutes=1)
    else:
        opens = next_open_minute(asset, cutoff, exchange=exchange)
        nxt = opens + delay.lag + timedelta(minutes=1) if opens else None
    return Expectation(latest, nxt, delay.describe())


def assess(asset: str, tier: Tier, last_bar: datetime | None, now: datetime, exchange: str | None = None) -> dict:
    """Compare the newest stored bar with what should exist.

    state: "empty" (nothing stored), "behind" (newer data should be available), "current".
    Session-based plans compare whole sessions, so a thinly traded stock whose last trade
    was at 18:40 is not reported behind for missing 19:59.
    """
    exp = expected(asset, tier, now, exchange)
    out = {"expectedThrough": exp.latest, "nextDue": exp.next_due, "rule": exp.rule, "behindBy": None}
    if last_bar is None:
        return out | {"state": "empty"}
    if exp.latest is None:
        return out | {"state": "current"}
    last_bar = last_bar.astimezone(ET)
    if plan_delay(tier, asset).kind == "eod" and asset in ("stock", "index", "future"):
        behind = last_bar.date() < exp.latest.date()
    else:
        behind = last_bar < exp.latest - GRACE
    if behind:
        out["behindBy"] = exp.latest - last_bar
    return out | {"state": "behind" if behind else "current"}
