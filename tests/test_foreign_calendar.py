"""A stock listed outside the US keeps its exchange's days (the user, 2026-11-02): every weekday,
the local calendar day, so a Tokyo trading day that is a US holiday is asked for; its day bars stay
stamped at midnight ET of the local date."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from liminal_lib import coverage, freshness
from liminal_lib.config import Tier
from liminal_lib.sessions import ET, MarketCalendar

THANKSGIVING = date(2026, 11, 26)  # a US holiday, a Thursday: Tokyo trades
TIER = Tier("twse", requests_per_minute=10, history_years=None, history_start=date(2010, 1, 4))
JST = ZoneInfo("Asia/Tokyo")


def test_a_tokyo_listing_has_a_session_on_a_us_holiday_and_none_on_a_weekend():
    cal = MarketCalendar()
    assert not cal.is_trading_day(THANKSGIVING) and cal.is_trading_day(THANKSGIVING, "XTKS")
    assert not cal.is_trading_day(date(2026, 11, 28), "XTKS")  # Saturday
    assert cal.sessions("stock", THANKSGIVING, THANKSGIVING) == []
    (s,) = cal.sessions("stock", THANKSGIVING, THANKSGIVING, exchange="XTKS")
    assert s.day == THANKSGIVING and s.open == datetime(2026, 11, 26, 0, 0, tzinfo=JST)
    assert s.close == s.open + timedelta(days=1)
    assert cal.sessions("stock", date(2026, 11, 28), date(2026, 11, 29), exchange="XTKS") == []
    assert cal.is_open("stock", datetime(2026, 11, 26, 12, 0, tzinfo=JST), exchange="XTKS")
    assert not cal.is_open("stock", datetime(2026, 11, 26, 12, 0, tzinfo=ET))  # the US, the holiday
    assert cal.sessions("stock", THANKSGIVING, THANKSGIVING, exchange="XNAS") == []  # a US pin: the US days


def test_what_an_end_of_day_plan_has_published_follows_the_listings_days():
    now = datetime(2026, 11, 27, 12, 0, tzinfo=ET)  # the Friday after Thanksgiving, noon in New York
    us = freshness.expected("stock", TIER, now)
    tokyo = freshness.expected("stock", TIER, now, "XTKS")
    assert us.latest.date() == date(2026, 11, 25)  # Wednesday: Thursday was closed, Friday is under way
    assert tokyo.latest.astimezone(JST).date() == THANKSGIVING  # Thursday traded in Tokyo and is published
    assert tokyo.next_due > now
    a, b = coverage.ms(datetime(2026, 11, 26, 0, 0, tzinfo=ET)), coverage.ms(datetime(2026, 11, 27, 0, 0, tzinfo=ET))
    assert not coverage.has_open_time("stock", a, b) and coverage.has_open_time("stock", a, b, "XTKS")
    assert coverage.frontier_ms("stock", TIER, now, "day", "XTKS") == coverage.day_start_ms(date(2026, 11, 27))
    assert coverage.frontier_ms("stock", TIER, now, "day") == coverage.day_start_ms(date(2026, 11, 26))
    assert freshness.previous_session(date(2026, 11, 27), "XTKS") == THANKSGIVING
    assert freshness.previous_session(date(2026, 11, 27)) == date(2026, 11, 25)
    assert freshness.next_session(date(2026, 11, 27), "XTKS") == date(2026, 11, 30)
    assert freshness.history_horizon("stock", TIER, now, "XTKS") == now  # an end-of-day plan: from now


def test_a_plan_publishing_its_days_late_expects_the_day_that_far_back():
    late = Tier("jquants-free", requests_per_minute=5, history_years=2)
    now = datetime(2026, 11, 27, 12, 0, tzinfo=ET)
    exp = freshness.expected("stock", late, now, "XTKS")
    assert exp.latest.astimezone(JST).date() == date(2026, 9, 3)  # twelve weeks back: the last day published by then
    assert "84 days late" in exp.rule
    assert freshness.assess("stock", late, datetime(2026, 9, 3, 0, 0, tzinfo=ET), now, "XTKS")["state"] == "current"
    assert freshness.assess("stock", late, datetime(2026, 9, 2, 0, 0, tzinfo=ET), now, "XTKS")["state"] == "behind"
    assert coverage.frontier_ms("stock", late, now, "day", "XTKS") == coverage.day_start_ms(date(2026, 9, 4))
