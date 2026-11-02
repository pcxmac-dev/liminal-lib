"""What a provider plugin gives the live recorder (live.py; Provider.live, plugin API 1.5): each live
feed it has, a stream (a WebSocket's messages: an exchange's trades) or a poller (REST, on its key's
limits: Tiingo's Blue Ocean nights), declared by the plugin with its URL, messages and parsing. The
recorder connects, follows the symbols open on a chart, keeps alive, stores and kills the same way
for each.

A stream object gives: `source` (its feed's name), `url`, `asset` (crypto | stock), `keepalive` (a
message, PING for WebSocket pings, or None), `dynamic` (its symbols follow the charts: `change` while
connected), `authed` (False until the server has taken its keys), `confirmed` (the symbols the server
says it streams), `subscribe(symbols)` (the first messages on a connection), `change(add, drop)`, and
`parse(message)` -> [(symbol, trade)], raising Reconnect, Refused or Notice. Optionally `method` (how
its bars are built, for their provenance) and `tape_rules` (bars by the consolidated tape's rules per
trade condition: stocks). A poller finds the session it works, of a kind the app's calendar knows
("overnight"), with `session_at` (plugin API 1.10)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..config import Settings

PING = "ping"  # keepalive by WebSocket ping frames rather than a message


@dataclass(frozen=True)
class Live:
    """A live feed of a plugin: its stream's class, or a poller."""
    name: str  # the live feed's name: its bars, status and kills (coinbase; iex; tiingoboats)
    asset: str  # crypto | stock
    # its stream's class, made with stream(*credentials, dynamic=...) (crypto) or stream(*credentials) (stocks)
    stream: Callable[..., object] | None = None
    # what its stream signs in with, from the settings: None when it is not set up here (no keys,
    # LIVE_STOCKS=off, offline); None itself when it needs none
    credentials: Callable[["Settings"], tuple | None] | None = None
    # a poller instead of a stream: made from the settings, None when it is not polled here
    poller: Callable[["Settings"], object | None] | None = None
    label: str = ""  # how the page names it when its name is not its plugin's: "alpaca (iex)"
    # one venue's share of the market (IEX's trades, Blue Ocean's nights): a chart of all feeds uses
    # it only where no feed of the whole market has the span, never for days
    partial: bool = False


class Reconnect(ConnectionError):
    """The exchange asked for a new connection."""


class Refused(ConnectionError):
    """The server ended the session (keys, plan, or another connection on the account): tried
    again after `wait` seconds at the earliest."""

    def __init__(self, message: str, wait: float):
        super().__init__(message)
        self.wait = wait


class Notice(Exception):
    """The server turned down a request but kept the connection (shown, not retried)."""


def ns(iso: str) -> int:
    """An RFC 3339 time with up to nanosecond digits as ns since the epoch, exactly."""
    whole, _, frac = iso.rstrip("Z").partition(".")
    return int(datetime.fromisoformat(whole + "+00:00").timestamp()) * 10**9 + int((frac + "000000000")[:9])


def trade(ts_ns: int, seq: int, price, size, trade_id) -> dict:
    """A trade as the recorder stores it."""
    return {"sip_timestamp": ts_ns, "sequence_number": seq, "price": float(price), "size": float(size),
            "exchange": None, "conditions": [], "id": str(trade_id), "tape": None}


def session_at(kind: str, t: datetime):
    """The market session of this kind ("overnight": Blue Ocean's nights, 20:00-04:00 ET; the app's
    sessions.py) open at `t`, or None: for a poller that works a session no stream has (Tiingo's
    Blue Ocean nights; plugin API 1.10). The app's calendar, read when asked."""
    from ..freshness import CALENDAR
    from ..sessions import ET

    d = t.astimezone(ET).date()
    return next((s for s in CALENDAR.sessions(kind, d, d + timedelta(days=1)) if s.open <= t < s.close), None)
