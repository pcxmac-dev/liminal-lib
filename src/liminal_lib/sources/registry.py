"""Data providers are plugins: one `Provider` each, holding everything the app needs to know about it.

A provider plugin is a module that defines `PLUGIN` (a Provider; `PLUGINS` for several). Every one
is a package of its own now (liminal-plugin-<name>, 2026-11-01), registering under the entry point
group `liminal.providers` (pointing at the module, or at the Provider itself); a module dropped in
`sources/plugins/` is still found on the next start, as the built-ins were.

What a plugin gives:
- `build(settings, request_budget)`: its DataSource (sources/base.py), with its limiter and caps.
- its keys (`KeyVar`: user environment variables; never written to a file), and how to get one
  (`signup`): tools/set-key.ps1 offers each plugin's keys from `python -m liminal plugins --json`.
- what it serves: asset classes (`assets`; `keyless` the ones it serves without keys), the market
  each is (`markets`), whether it carries a symbol (`carries`), and whether it is a closed archive.
- its plan, rate window and caps (`view`, `rate`), and what its server publishes (`limits`).
  It refreshes its own plan info: its capability scan (`scan`) asks the provider what the key
  really gets and may answer with the plan that shows (`Scanned.plan`: values of its own
  `plan_vars`), which the app applies wherever the user set none (config.with_detected). A
  provider whose answers cannot tell its plans apart says so (`plan_note`); its plan is then
  only what the user chose (`plan_choices`, offered by the key script and the Plugins view).
- facts the rest of the app asks: its kind of data for the composite model, the exchange it is the
  reference for, the bars one request returns, a monthly symbol cap, the bulk symbols' resolution.
- where its data is (`plugin_databases`, API 1.8; layout.py): each feed's files, its datasets' and
  publishers', its rows in the main file, and any database its own code writes besides (`databases`).
- its capabilities (`provides`, `plan_resolutions`, `per_request`, `streams`), from which it reports
  in one standard form (`report`, a `Report`): its subscription, its limits, one line per job it
  can do on that plan (asset class, resolution, market, how far back, how fresh, bars a request)
  and its live streams. The delivery scheme (sourcing.py) reads only these reports; `sourcing`
  shows them. A plugin whose plan says more than its declarations amends its report (`reporter`).

Callbacks left out take the defaults below, so a plain keyed REST feed needs little more than its
build. A plugin that fails to load is left out and reported (`errors()`, `plugins`), never fatal.

The contract is versioned (`PLUGIN_API`, major.minor; docs/ARCHITECTURE.md says what it holds).
Each plugin gives its own `version` (a package's may leave it empty: its distribution's is used)
and the contract it was written for (`api`). One written for another major, or for a newer minor
than this app offers, is left out and reported like a broken one.
"""
from __future__ import annotations

import importlib
import math
import pkgutil
import threading
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from .. import layout
from ..layout import MAIN, Database  # noqa: F401 - plugins import them from here
from ..ratelimit import MachineLedger, RateLimiter  # plugins import MachineLedger from here (1.10)
from .fetching import Fetched, FetchContext, Fetcher, Job  # noqa: F401 - plugins import them from here
from .keys import KeyVar  # noqa: F401 - plugins import it from here
from .publisher import Publisher
from .stream import Live

if TYPE_CHECKING:
    from ..config import Settings, Tier
    from .base import DataSource

ENTRY_POINTS = "liminal.providers"
DEFAULT_PER_REQUEST = 1000  # bars a request returns, for a provider that does not say
# The plugin contract this app offers (major.minor): a new minor only adds (a field with a default,
# a helper), a new major changes or removes something a plugin may rely on. A plugin written
# before the contract had a number was written for 1.0 (Provider.api's default).
# 1.1 (2026-10-29): Provider.part_of, a plugin's second feed (Alpaca's Blue Ocean).
# 1.2 (2026-10-30): Publisher.part_of and Publisher.datasets, a plugin of several publishers (Federal).
# 1.3 (2026-10-31): Publisher.keys and KeyVar.secret, KeyVar.hint: every variable a plugin reads
#   declared by it, set from the Plugins view; Publisher.part_of may name a feed plugin (Massive's
#   headlines, in Massive).
# 1.4 (2026-10-31): Publisher.datasets holds the Dataset definitions themselves (sources/dataset.py:
#   files, parser, publication rule), read only from its hosts and stopped by its pause; a library of
#   datasets is a publisher with no feeds (Ken French's, Datahub's; the CFTC's in Federal).
# 1.5 (2026-10-31): Provider.live (sources/stream.py: Live), each live stream or poller declared by
#   its plugin (Coinbase's, Bitstamp's, Alpaca's IEX, Tiingo's Blue Ocean nights); Provider.partial,
#   a feed of one venue's share of the market (Alpaca's Blue Ocean).
# 1.6 (2026-10-31): sources.http (HttpClient, PublicClient: the HTTP client every plugin's builds on)
#   and sources.archive (Archive, a public feed's source, and what the exchanges share); each
#   vendor's client in its plugin (Massive's, Alpaca's, Tiingo's, the exchanges' and Cboe's).
# 1.7 (2026-10-31): Feed.parse and Feed.item_kind (sources.publisher: Read, parse_rss): each feed's
#   parser in its plugin (the Federal Register's, the New York Fed's), an RSS feed read by the
#   contract's reader.
# 1.8 (2026-10-31): the databases a plugin's data is in (layout.Database: plugin_databases), and
#   Provider.databases, Publisher.databases: any its own code writes besides the standard ones (a
#   SQLite, DuckDB or Parquet file in the data folder, or a MongoDB collection on this machine).
# 1.9 (2026-10-31): a plugin's own fetching (sources/fetching.py: Fetcher, Job, Fetched,
#   FetchContext; Provider.fetcher, Publisher.fetcher): its jobs on the shared fetch queue
#   (storage/queue.py), taken by a worker of its own (runner.py).
# 1.10 (2026-11-01): a poller's session calendar through the contract (sources/stream.py: session_at),
#   and the machine-wide ledger a plugin counts its key's uses on (MachineLedger, imported here):
#   what the Tiingo plugin reached into the app for, so it could leave as a package.
# 1.11 (2026-11-01): a plugin's own settings (Provider.settings -> Settings.plugins, Settings.plugin):
#   Alpaca's keys, plan, feed, crypto venue and caps, once config.py's AlpacaSettings.
PLUGIN_API = "1.11"


def api_version(api) -> tuple[int, int]:
    """A contract version's (major, minor): "1.2", "1" (1.0). ValueError when it is not one."""
    parts = str(api).strip().split(".")
    if not 1 <= len(parts) <= 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"api {api!r} is not a contract version (major.minor)")
    return int(parts[0]), int(parts[1]) if len(parts) == 2 else 0


def api_refusal(api, offered: str | None = None) -> str | None:
    """Why a plugin written for contract `api` cannot load in an app offering `offered` (default:
    PLUGIN_API; None: it can): its major must be the app's, its minor no newer than the app's."""
    offered = offered or PLUGIN_API
    try:
        major, minor = api_version(api)
    except ValueError as e:
        return str(e)
    have, have_minor = api_version(offered)
    if major < have:
        return f"written for plugin API {api}; this app offers {offered}: it needs a release for {have}.x"
    if major > have:
        return f"written for plugin API {api}; this app offers {offered}: it needs a newer app"
    if minor > have_minor:
        return f"written for plugin API {api}, newer than this app's {offered}: it needs a newer app"
    return None


@dataclass(frozen=True)
class Scanned:
    """A capability scan's answer: each thing asked (what, available, detail), and the plan the
    answers show ({plan variable: value}; empty when they cannot tell the plans apart). `alike`:
    the other values the same answers would come from ({variable: (values)}: Tiingo's Power and
    Commercial reach the same data), the plan found being the one with the lowest limits."""
    results: tuple[tuple[str, bool, str], ...]
    plan: Mapping[str, str] = field(default_factory=dict)
    alike: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # its limits verified (run_scan): the rate-limit headers its server sent, the limit a minute
    # they give, the window kept ({seen, perMinute, kept}); a server limit below the window kept
    # is used from then on (rate_window)
    limits: Mapping[str, object] = field(default_factory=dict)


# -- the standard report every provider gives the delivery scheme (sourcing.py) ---------------------

@dataclass(frozen=True)
class Limits:
    """A subscription's limits: what a job's cost is counted against."""
    requests_a_day: float  # what it can send in a day: its daily cap, or less where its rate is tighter
    per_day: int | None = None
    per_run: int | None = None
    rate: tuple[int | None, float] | None = None  # (requests, seconds) its limiter keeps
    symbols_a_month: int | None = None
    published: str = ""  # what its server publishes


@dataclass(frozen=True)
class Capability:
    """One job a provider can do on its plan: an asset class at one resolution."""
    asset: str
    resolution: str  # day, minute, second
    market: str  # what its data is: feeds of one market are copies of each other
    since: object  # date: how far back its plan reaches
    latency: float  # seconds its newest data trails real time (86,400: end of day)
    per_request: int | None  # bars one request returns (None: a symbol's whole history at once)


@dataclass(frozen=True)
class Report:
    """A provider's standard report: the same form for every plugin."""
    source: str
    title: str
    plan: str  # its subscription here
    keys_set: bool | None  # None: it needs none
    limits: Limits
    capabilities: tuple[Capability, ...]
    streams: tuple[tuple[str, str], ...] = ()  # (asset class, what its live stream is)
    kind: str = ""  # its kind of data (exchange, consolidated, official, vendor)
    notes: tuple[str, ...] = ()
    scanned: tuple[tuple[str, bool, str], ...] = ()  # its last capability scan: (what, available, detail)
    scanned_at: str | None = None

    def as_dict(self) -> dict:
        return {"source": self.source, "title": self.title, "plan": self.plan, "keysSet": self.keys_set,
                "kind": self.kind, "notes": list(self.notes), "scannedAt": self.scanned_at,
                "scanned": [{"what": w, "available": ok, "detail": d} for w, ok, d in self.scanned],
                "limits": {**self.limits.__dict__, "rate": list(self.limits.rate) if self.limits.rate else None},
                "capabilities": [{**c.__dict__, "since": str(c.since)} for c in self.capabilities],
                "streams": [{"asset": a, "what": w} for a, w in self.streams]}

    def lines(self) -> list[str]:
        """The report for the terminal, the same layout for every provider."""
        lim = self.limits
        rate = f", {lim.rate[0]:,} per {lim.rate[1]:g} s" if lim.rate and lim.rate[0] else ""
        caps = (f"{lim.requests_a_day:,.0f} requests a day{rate}" + (f", {lim.per_run:,} a run" if lim.per_run else "")
                + (f", {lim.symbols_a_month:,} symbols a month" if lim.symbols_a_month else ""))
        keys = "" if self.keys_set is None else "; keys set" if self.keys_set else "; keys NOT set"
        out = [f"{self.source} ({self.title}): plan {self.plan}{keys}; {caps}"
               + (f"; server: {lim.published}" if lim.published else "")]
        for c in self.capabilities:
            fresh = "real time" if c.latency < 60 else f"{c.latency / 60:.0f} min delayed" if c.latency < 86_400 \
                else "end of day"
            bars = "whole history a request" if c.per_request is None else f"{c.per_request:,} bars a request"
            out.append(f"    {c.asset:6} {c.resolution:6} {c.market:12} from {c.since}  {fresh:16} {bars}")
        out += [f"    {a:6} live   {w}" for a, w in self.streams]
        out += [f"    note: {n}" for n in self.notes]
        if self.scanned_at:
            out.append(f"    scanned {self.scanned_at[:16].replace('T', ' ')}:")
            out += [f"      {'yes' if ok else 'no ':3} {w}" + (f": {d}" if d else "") for w, ok, d in self.scanned]
        return out


@dataclass(frozen=True, eq=False)
class Provider:
    name: str  # the feed's name everywhere: its database file, the watchlist, the ledgers
    title: str
    build: Callable[["Settings", int | None], "DataSource"]
    assets: frozenset  # asset classes it can collect at all (with keys / on some plan)
    order: int = 100  # its place in lists (the built-in feeds: 0-60)
    about: str = ""  # one line for menus and lists
    keys: tuple[KeyVar, ...] = ()
    keyless: frozenset = frozenset()  # asset classes served without its keys (Alpaca: crypto)
    signup: str = ""  # where a key is made
    plan_vars: tuple[str, ...] = ()  # its plan's variables: read from the saved user variables like keys
    plan_choices: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # plan variable -> its values (the default first)
    plan_note: str = ""  # what its scan cannot tell about its plan (the user's choice is then the plan)
    options: tuple[str, ...] = ()  # its other variables (Settings.env)
    archival: bool = False  # a closed feed: each symbol filled once, then never asked again
    # the feed whose account (key and limits) its requests count against, when another's: a 429 or a
    # refused key kills both (killswitch.sharing)
    account: str = ""
    # the plugin it is a feed of, when another feed's (API 1.1; Blue Ocean: Alpaca's): listed, keyed
    # and rescanned inside that one, a feed of its own everywhere else (plugin_feeds)
    part_of: str = ""
    tier: "Tier | None" = None  # its plan, when it has one fixed plan
    per_run: int | None = None  # its request caps with that plan
    per_day: int | None = None
    limits: str = ""  # what its server publishes, beside the window kept (Fetcher, Limits)
    kind: str = "vendor"  # its data for the composite model: exchange, consolidated, official, vendor
    kind_by_asset: Mapping[str, str] = field(default_factory=dict)
    reference_for: frozenset = frozenset()  # asset classes whose gold standard exchange it is
    venue_notes: Mapping[str, str] = field(default_factory=dict)  # audits: what its data of an asset is
    symbols_per_month: int | None = None  # different symbols its plan allows a month
    # the initializer's bulk symbols start at this resolution, or one per asset class ({"stock": ...})
    bulk_resolution: "str | Mapping[str, str]" = "auto"
    missing_keys_note: str = ""  # the Fetcher's hover text when its keys are not set
    missing_keys_chip: str = "no key"
    # Capabilities, for the delivery scheme (sourcing.py): the resolutions it provides per asset class
    # (day, minute, second; default: day and minute for each of `assets`), the bars one request
    # returns per resolution (None: a symbol's whole history in one request), its live streams.
    provides: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    per_request: Mapping[str, int | None] = field(default_factory=dict)
    streams: Mapping[str, str] = field(default_factory=dict)  # asset class -> what its stream is
    # its live feeds for the recorder (API 1.5; sources/stream.py): a stream's class or a poller each,
    # with its own feed name (Coinbase's "coinbase", Alpaca's "iex")
    live: tuple[Live, ...] = ()
    # its bars are one venue's share of the market (API 1.5; Blue Ocean's nights): a chart of all
    # feeds uses them only where no feed of the whole market has the span, never for days
    partial: bool = False
    # databases its own code writes besides its feeds' files (API 1.8; layout.Database): none built in
    databases: tuple[Database, ...] = ()
    # its own fetching (API 1.9; sources/fetching.py); None: fetched by the app's bars sync
    fetcher: Fetcher | None = None
    # Callbacks (None: the default described at each method).
    view: Callable[["Settings"], "Settings"] | None = None
    rate: Callable[["Settings"], tuple[int | None, float]] | None = None
    has_keys: Callable[["Settings"], bool] | None = None
    serves: Callable[["Settings"], frozenset | None] | None = None
    markets: Callable[["Settings"], dict] | None = None
    carries: Callable[[object], bool] | None = None
    plan_resolutions: Callable[["Tier", str], tuple[str, ...]] | None = None  # what its plan gives per asset
    reporter: Callable[["Report", "Settings | None"], "Report"] | None = None  # amends its standard report
    # Its capability scan (`capabilities`; on each chart app start, weekly in the sync, Rescan in the
    # Plugins view): a few requests asking what its plan really gives, each answer (what, available,
    # detail), or a `Scanned` with the plan they show too; kept, and shown in its report.
    scan: Callable[["DataSource"], "list[tuple[str, bool, str]] | Scanned"] | None = None
    # Its own settings (plugin API 1.11): built from every variable the app read (Settings.variables)
    # when the settings load, kept as Settings.plugins[name] and reached with Settings.plugin(name) by it
    # and by the feeds that are parts of it; a ConfigError for a value it refuses stops the app as any
    # setting's does (Alpaca's keys, plan, feed, venue and caps).
    settings: Callable[[Mapping[str, str]], object] | None = None
    version: str = ""  # the plugin's own version (a package's, left empty: its distribution's)
    api: str = "1.0"  # the contract (PLUGIN_API) it was written for: another major is left out
    origin: str = "built in"  # set when loaded: built in, or the package's entry point

    @property
    def key_names(self) -> tuple[str, ...]:
        return tuple(n for k in self.keys for n in k.names)

    @property
    def key_hint(self) -> str:
        return " and ".join(k.name for k in self.keys)

    def keys_set(self, settings: "Settings") -> bool | None:
        """Whether its keys are set (None: it needs none). Default: every key's variable has a value."""
        if not self.keys:
            return None
        if self.has_keys:
            return bool(self.has_keys(settings))
        return all(k.value(settings.env) for k in self.keys)

    def served(self, settings: "Settings") -> frozenset | None:
        """The asset classes it collects on this machine; None when it is not set up here at all.
        Default: all of `assets` with its keys (or needing none), only `keyless` without."""
        if self.serves:
            return self.serves(settings)
        return self.assets if self.keys_set(settings) in (None, True) else frozenset(self.keyless)

    def settings_view(self, base: "Settings") -> "Settings":
        """The settings as this feed sees them (Settings.for_source): its plan and caps. Default:
        its fixed `tier` and caps when it has them, otherwise the settings as they are."""
        if self.view:
            return self.view(base)
        if self.tier is None:
            return base
        return replace(base, tier=self.tier, max_requests_per_run=self.per_run or base.max_requests_per_run,
                       max_requests_per_day=self.per_day or base.max_requests_per_day, origin=base)

    def rate_window(self, settings: "Settings") -> tuple[int | None, float]:
        """The window its limiter keeps: (requests, seconds). Default: its plan's rate a minute."""
        if self.rate:
            return self.rate(settings)
        rpm = settings.for_source(self.name).tier.requests_per_minute
        return rpm, minute_window(rpm)

    def market_of(self, settings: "Settings") -> dict[str, str]:
        """Which market each of its asset classes is (feeds of one market are copies of each other:
        one download per span between them). Default: each class its own market, named after the feed."""
        return self.markets(settings) if self.markets else {a: self.name for a in self.assets}

    def can_carry(self, spec) -> bool:
        """Whether it can hold a symbol at all: its asset class, then its own list (`carries`)."""
        return spec.asset in self.assets and (self.carries is None or bool(self.carries(spec)))

    def kind_for(self, asset: str) -> str:
        return self.kind_by_asset.get(asset) or self.kind

    def resolutions_for(self, tier: "Tier", asset: str) -> tuple[str, ...]:
        """The resolutions its plan (`tier`) provides for an asset class. Default: `provides`, or day
        and minute for each of its asset classes."""
        if asset not in self.assets:
            return ()
        if self.plan_resolutions:
            return tuple(self.plan_resolutions(tier, asset))
        return tuple(self.provides.get(asset, ("day", "minute")))

    def run_scan(self, source: "DataSource", settings: "Settings | None" = None) -> Scanned:
        """Its capability scan, its plan held to its own plan variables (a plugin sets no other's),
        and its limits verified: the rate-limit headers its server sent on the scan's requests,
        against the window its limiter keeps (with `settings`)."""
        out = self.scan(source) if self.scan else ()
        rows, plan, alike = (out.results, out.plan, out.alike) if isinstance(out, Scanned) else (out, {}, {})
        plan = {k: str(v) for k, v in (plan or {}).items() if k in self.plan_vars and v}
        rows = [(str(w), bool(ok), str(d)) for w, ok, d in rows]
        limits = self.limits_found(source, settings)
        rows.append(limits.pop("row"))
        return Scanned(tuple(rows), plan, {k: tuple(str(x) for x in v) for k, v in (alike or {}).items() if k in plan},
                       limits)

    def limits_found(self, source, settings: "Settings | None" = None) -> dict:
        """Its server's rate limit from the headers it sent (source.client.limits_seen), a minute's,
        beside the window kept: {seen, perMinute, kept, row} (row: the scan's line for it)."""
        seen = dict(getattr(getattr(source, "client", None), "limits_seen", None) or {})
        found = server_limits(seen)
        per_minute = server_per_minute(seen)
        kept = list(self.rate_window(settings)) if settings is not None else None
        said = "; ".join(_per(q, w) for q, w in found)
        if not found:
            row = ("its server's rate limit", True, "not sent in its answers: its own published limits are kept"
                   + (f" ({self.limits})" if self.limits else ""))
        elif kept is None or within(kept[0], kept[1], found) == tuple(kept):
            row = ("its server's rate limit", True, said + (f"; {_per(*kept)} kept, inside it" if kept and kept[0] else ""))
        else:
            row = ("its server's rate limit", False, f"{said}: " + (f"tighter than the {_per(*kept)} kept" if kept[0]
                                                                     else "its plan keeps no limit")
                   + "; its server's is used from now on (a little inside it)")
        return {"seen": seen, "perMinute": per_minute, "kept": kept, "row": row}

    def plan_state(self, settings: "Settings") -> list[dict]:
        """Each of its plan variables: its value here and where it comes from (the user's own
        setting, its scan, or the default when neither set it), its choices."""
        mine = settings.variables or {}
        found = settings.detected or {}
        out = []
        for v in self.plan_vars:
            choices = list(self.plan_choices.get(v, ()))
            out.append({"name": v, "value": mine.get(v) or found.get(v) or (choices[0] if choices else None),
                        "origin": "you" if mine.get(v) else "scan" if found.get(v) else "default", "choices": choices})
        return out

    def request_bars(self, resolution: str) -> int | None:
        """Bars one request returns (None: a symbol's whole history at once). Default: 1,000."""
        return self.per_request.get(resolution, DEFAULT_PER_REQUEST)

    def report(self, profile, today, settings: "Settings | None" = None, scanned: dict | None = None) -> Report:
        """Its standard report on this machine: `profile` is its coverage.FeedProfile (plan, asset
        classes served, markets, capacity); `settings` add its caps and keys (the scheme needs neither);
        `scanned`, its last capability scan ({at, results})."""
        from ..collector import history_floor
        from ..coverage import latency

        caps = []
        for asset in sorted(profile.assets):
            for res in self.resolutions_for(profile.tier, asset):
                since = history_floor(profile.tier, today, asset)
                if res == "second":
                    since = max(since, today - timedelta(days=profile.second_days))
                caps.append(Capability(asset, res, profile.venues.get(asset) or self.name, since,
                                       latency(profile.tier, asset), self.request_bars(res)))
        view = settings.for_source(self.name) if settings is not None else None
        limits = Limits(profile.capacity if profile.capacity is not None else float(view.max_requests_per_day if view else 0),
                        per_day=view.max_requests_per_day if view else None,
                        per_run=view.max_requests_per_run if view else None,
                        rate=rate_window(self.name, settings) if settings is not None else None,  # its server's lower one too
                        symbols_a_month=self.symbols_per_month, published=self.limits)
        out = Report(self.name, self.title, profile.tier.name, self.keys_set(settings) if settings is not None else None,
                     limits, tuple(caps), tuple((a, w) for a, w in self.streams.items() if a in profile.assets),
                     self.kind, ("a closed archive: each symbol filled once, then not asked again",) if self.archival else (),
                     tuple((str(w), bool(ok), str(d)) for w, ok, d in (scanned or {}).get("results", [])),
                     (scanned or {}).get("at"))
        return self.reporter(out, settings) if self.reporter else out


# -- helpers for building a provider's client ----------------------------------------------------

def minute_window(rpm: int | None) -> float:
    """The seconds a limiter of `rpm` requests a minute keeps them in. A few a minute (Massive's
    free classes: 5): a couple of seconds' margin, so jitter between sending and arriving never
    puts six in the server's minute."""
    return 62.0 if rpm and rpm <= 10 else 60.0


def limiter(name: str, settings: "Settings", key: str | None = None) -> RateLimiter:
    """A feed's limiter, its window kept for the whole machine (every process and database on it)
    under the feed's name and its key's fingerprint (the public feeds' limits are per address)."""
    out = RateLimiter(*rate_window(name, settings))
    out.share(MachineLedger(name, key))
    return out


def machine_caps(client, name: str, settings: "Settings", key: str | None = None):
    """The feed's daily request cap held for the machine too, not only for this database."""
    machine = MachineLedger(name, key)
    cap = settings.for_source(name).max_requests_per_day
    client.take_day = lambda: machine.take_day(cap)
    return client


def public_client(name: str, base_url: str, settings: "Settings", request_budget: int | None):
    """A keyless public feed's client (its limits are per address: the machine's)."""
    from .http import PublicClient

    client = PublicClient(base_url, rate_limiter=limiter(name, settings), request_budget=request_budget)
    return machine_caps(client, name, settings)


def rate_window(name: str, settings: "Settings") -> tuple[int | None, float]:
    """The window each feed's limiter keeps: (requests, seconds). A little inside what each
    server publishes (Provider.limits) so a limiter never meets a refusal; and inside the limits its
    server's own headers gave on the last capability scans of every feed on its account (verified
    weekly), when they are tighter, a plan without a limit of its own included."""
    p = get(name)
    if p is None:
        rpm = settings.for_source(name).tier.requests_per_minute
        return rpm, minute_window(rpm)
    n, seconds = p.rate_window(settings)
    return within(n, seconds, found_limits(name, settings))


SERVER_SHARE = 0.95  # of a server's own limit, when it is below the window a plugin keeps


def within(n: int | None, seconds: float, limits: list[tuple[float, float]]) -> tuple[int | None, float]:
    """A window (n requests in `seconds`; n None: no limit) kept within SERVER_SHARE of each server
    limit (requests, seconds): on average under the lowest rate of them, and n at once under each
    shorter window's. Unchanged when it already is. A few a second becomes one in a longer window,
    never rounded up to one a second (that was twice a server's 30 a minute)."""
    if not limits:
        return n, seconds
    rate = min(q / w for q, w in limits) * SERVER_SHARE  # requests a second
    if n is not None and n / seconds <= rate + 1e-9 and all(n <= q * SERVER_SHARE for q, w in limits if w <= seconds):
        return n, seconds
    seconds = min(seconds or 60.0, min(w for _, w in limits))
    n = int(rate * seconds + 1e-9)
    if n < 1:
        n, seconds = 1, float(math.ceil(1 / rate))
    return n, float(seconds)


def server_limits(seen: Mapping[str, str]) -> list[tuple[float, float]]:
    """Each limit a server's rate-limit headers give, as (requests, seconds): X-RateLimit-Limit, a
    minute's (Alpaca's, the one feed known to send it); the IETF draft's RateLimit-Policy, each of
    its policies ("10;w=1, 1000;w=3600", or '"default";q=100;w=60'), else its RateLimit-Limit, a
    minute's when no window is said."""
    def number(text) -> float | None:
        try:
            return float(str(text).strip().strip('"'))
        except (TypeError, ValueError):
            return None

    low = {k.lower(): v for k, v in seen.items()}
    out: list[tuple[float, float]] = []
    x = number(str(low.get("x-ratelimit-limit", "")).split(",")[0].split(";")[0])
    if x:
        out.append((x, 60.0))
    policies = []
    for part in str(low.get("ratelimit-policy") or "").split(","):
        bits = [b.strip() for b in part.split(";") if b.strip()]
        if not bits:
            continue
        q = next((number(b[2:]) for b in bits if b.startswith("q=")), None) or number(bits[0])
        w = next((number(b[2:]) for b in bits if b.startswith("w=")), None) or 60.0
        if q:
            policies.append((q, w))
    if policies:
        out += policies
    else:
        limit = number(str(low.get("ratelimit-limit", "")).split(",")[0].split(";")[0])
        if limit:
            out.append((limit, 60.0))
    return [(q, w) for q, w in out if q > 0 and w > 0]


def server_per_minute(seen: Mapping[str, str]) -> float | None:
    """The lowest rate a server's rate-limit headers give, a minute's (server_limits), or None."""
    found = server_limits(seen)
    if not found:
        return None
    pm = min(q * 60 / w for q, w in found)
    return int(pm) if pm == int(pm) else round(pm, 3)


def _per(q: float, w: float) -> str:
    """A limit in words: "200 a minute", "50 an hour", "10 a second", "3 per 7 s"."""
    words = {1.0: "a second", 60.0: "a minute", 3600.0: "an hour", 86400.0: "a day"}
    return f"{q:,.0f} {words[float(w)]}" if float(w) in words else f"{q:,.0f} per {w:,.0f} s"


def sharing(name: str) -> list[str]:
    """The feeds whose requests count against the same account as `name`'s, its own included
    (Provider.account: Blue Ocean's on Alpaca's key)."""
    ps = providers()
    account = (ps[name].account if name in ps else "") or name
    return [n for n, p in ps.items() if (p.account or n) == account] or [name]


def found_limits(name: str, settings: "Settings") -> list[tuple[float, float]]:
    """The limits their servers' headers gave on the last capability scans of every feed on its
    account (kept in the database it serves): Blue Ocean's scan tells Alpaca's limiter too."""
    path = getattr(settings, "db_path", None)
    return [lim for n in sharing(name) for lim in scan_limits(path, n)] if path else []


def scan_limits(db_path, name: str) -> list[tuple[float, float]]:
    """The limits kept with a feed's last capability scan in a database (from the headers kept)."""
    from ..sourcing import scans_at

    try:
        kept = scans_at(db_path).get(name, {}).get("limits") or {}
    except Exception:  # a database being moved: the plugin's own window
        return []
    found = server_limits(kept.get("seen") or {})
    return found or ([(float(kept["perMinute"]), 60.0)] if kept.get("perMinute") else [])


# -- the registry ----------------------------------------------------------------------------------

_lock = threading.RLock()
_providers: dict[str, Provider] | None = None
_publishers: dict[str, Publisher] = {}
_errors: list[tuple[str, str]] = []


def _plugins_in(module, origin: str) -> list[Provider | Publisher]:
    """A module's plugins: its feed (`PLUGIN`, `PLUGINS`: Providers) and its publishers
    (`PUBLISHER`, `PUBLISHERS`: sources.publisher.Publisher), at least one of them."""
    if isinstance(module, (Provider, Publisher)):
        return [replace(module, origin=origin)]
    found = list(getattr(module, "PLUGINS", ())) + list(getattr(module, "PUBLISHERS", ()))
    found += [p for p in (getattr(module, "PLUGIN", None), getattr(module, "PUBLISHER", None))
              if isinstance(p, (Provider, Publisher))]
    if not found or not all(isinstance(p, (Provider, Publisher)) for p in found):
        raise TypeError("defines no PLUGIN (a sources.registry.Provider) nor PUBLISHER (a sources.publisher.Publisher)")
    return [replace(p, origin=origin) for p in found]


def _entry_points() -> Iterable:
    from importlib.metadata import entry_points

    try:
        return entry_points(group=ENTRY_POINTS)
    except Exception:  # a broken installed distribution must not stop the app
        return ()


def _distribution(ep) -> tuple[str, str]:
    """The installed distribution an entry point comes from: (name, version); empty when unknown."""
    try:
        dist = getattr(ep, "dist", None)
        return (str(dist.name or ""), str(dist.version or "")) if dist is not None else ("", "")
    except Exception:  # broken metadata: the plugin still loads, unversioned
        return "", ""


def _load(where: str, load: Callable[[], object], origin: str, version: str,
          found: list, errors: list[tuple[str, str]]) -> None:
    """One module's or entry point's plugins into `found`, those this app's contract cannot take
    (api_refusal) and a module that fails to load into `errors`; `version` is the package's."""
    try:
        plugins_ = _plugins_in(load(), origin)
    except Exception as e:
        errors.append((where, f"{type(e).__name__}: {e}"))
        return
    for p in plugins_:
        why = api_refusal(p.api)
        if why:
            errors.append((where, f"{p.name}: {why}"))
        else:
            found.append(p if p.version or not version else replace(p, version=version))


def discover() -> tuple[dict[str, Provider], list[tuple[str, str]]]:
    """Every provider plugin: the built-in modules, then the installed packages' entry points.
    Returns (providers by name in their order, [(where, why)] for the ones left out)."""
    providers_, _, errors_ = discover_all()
    return providers_, errors_


def discover_all() -> tuple[dict[str, Provider], dict[str, Publisher], list[tuple[str, str]]]:
    """The providers and the publishers (both by name, in their order), and the plugins left out:
    one that fails to load, one written for a contract this app does not offer (api_refusal), a
    second of one name."""
    from . import plugins

    found: list[Provider | Publisher] = []
    errors: list[tuple[str, str]] = []
    for info in sorted(pkgutil.iter_modules(plugins.__path__), key=lambda m: m.name):
        where = f"{plugins.__name__}.{info.name}"
        _load(where, lambda: importlib.import_module(where), "built in", "", found, errors)
    for ep in _entry_points():
        dist, version = _distribution(ep)
        _load(f"{ep.name} ({ep.value})", ep.load, f"package: {dist} ({ep.value})" if dist else f"package: {ep.value}",
              version, found, errors)
    out: dict[str, Provider] = {}
    pubs: dict[str, Publisher] = {}
    keys: dict[str, str] = {}  # a feed's key -> its publisher (keys name the state kept)
    sets: dict[str, str] = {}  # a dataset's name -> its publisher (names its state and versions)
    lives: dict[str, str] = {}  # a live feed's name -> its provider (names its bars, status and kills)
    files: dict[str, str] = {}  # a dataset's file (Dataset.provider: datasets/<provider>.db) -> its publisher
    # the built-ins first, so an installed package never takes the name, a feed or a dataset of one
    # (whatever its order); all listed in their order after
    for p in sorted(found, key=lambda p: (p.origin != "built in", p.order, p.name)):
        mine = pubs if isinstance(p, Publisher) else out
        if p.name in out or p.name in pubs:
            first = (out.get(p.name) or pubs[p.name]).origin
            kind = "publisher" if isinstance(p, Publisher) else "provider"
            errors.append((p.origin, f"a second {kind} named {p.name!r}: the first kept ({first})"))
            continue
        why = _databases_refused(p.databases)
        if why:
            kind = "publisher" if isinstance(p, Publisher) else "provider"
            errors.append((p.origin, f"{kind} {p.name!r}: {why}"))
            continue
        if isinstance(p, Publisher):
            clash = next((f.key for f in p.feeds if f.key in keys), None)
            if clash:
                errors.append((p.origin, f"publisher {p.name!r}: feed {clash!r} is {keys[clash]!r}'s already"))
                continue
            clash = next((d.name for d in p.declared if d.name in sets), None)
            if clash:
                errors.append((p.origin, f"publisher {p.name!r}: dataset {clash!r} is {sets[clash]!r}'s already"))
                continue
            clash = next((d.provider for d in p.declared if files.get(d.provider, p.name) != p.name), None)
            if clash:
                errors.append((p.origin, f"publisher {p.name!r}: the datasets file {clash!r} is {files[clash]!r}'s"))
                continue
            keys.update({f.key: p.name for f in p.feeds})
            sets.update({d.name: p.name for d in p.declared})
            files.update({d.provider: p.name for d in p.declared})
        else:
            # a live feed's name is its own provider's, or a name no other provider has
            clash = next((x.name for x in p.live if lives.get(x.name, p.name) != p.name
                          or (x.name != p.name and x.name in {q.name for q in found if isinstance(q, Provider)})), None)
            if clash:
                errors.append((p.origin, f"provider {p.name!r}: live feed {clash!r} is {lives.get(clash, clash)!r}'s"))
                continue
            lives.update({x.name: p.name for x in p.live})
        mine[p.name] = p
    in_order = lambda d: dict(sorted(d.items(), key=lambda kv: (kv[1].order, kv[0])))  # noqa: E731
    return in_order(out), in_order(pubs), errors


def _databases_refused(dbs: Iterable[Database]) -> str | None:
    """Why a plugin's own databases are refused (API 1.8): an engine the app has not, a server off this
    machine, a path out of the data folder, or the app's own file (the main one, gov.db) not declared
    shared."""
    for d in dbs:
        why = layout.not_here(d)
        if why:
            return why
        if not d.shared and d.where in (layout.MAIN, layout.GOV):
            return f"{d.where}: the app's file, every plugin's (declare it shared)"
    return None


def providers() -> dict[str, Provider]:
    """The providers by name, in their order (found once a process; `reload()` looks again)."""
    global _providers
    with _lock:
        if _providers is None:
            _providers, found, found_errors = discover_all()
            _publishers.clear()
            _publishers.update(found)
            _errors[:] = found_errors
        return _providers


def publishers() -> dict[str, Publisher]:
    """The publisher plugins by name, in their order (found with the providers)."""
    with _lock:
        providers()
        return dict(_publishers)


def publisher_of(name: str) -> str:
    """The plugin a publisher is in: the one it is part of (Publisher.part_of), a publisher
    (Federal's agencies) or a feed plugin (API 1.3: Massive's headlines, in Massive); or its own
    (a part_of naming neither here leaves it one of its own)."""
    pubs = publishers()
    p = pubs.get(name)
    if p is None or not p.part_of or p.part_of == name:
        return name
    if p.part_of in pubs:
        return p.part_of
    return plugin_of(p.part_of) if p.part_of in providers() else name


def publisher_parts(name: str) -> list[str]:
    """A plugin's publishers, in their order: a publisher's parts (Federal: fed, nyfed, sec, bls,
    fedreg, cftc), or a feed plugin's (Massive: massive-news)."""
    return [n for n in publishers() if n != name and publisher_of(n) == name]


def plugin_keys(name: str) -> list[KeyVar]:
    """Every variable a plugin reads that the user sets: a feed plugin's keys with its feeds' and its
    publishers' (each once, by name), or a publisher's with its parts' (Federal: the SEC's GOV_CONTACT)."""
    pubs = publishers()
    if name in pubs:
        found = [k for n in (name, *publisher_parts(name)) for k in pubs[n].keys]
    else:
        ps = providers()
        found = [k for n in plugin_feeds(name) if n in ps for k in ps[n].keys]
        found += [k for n in publisher_parts(name) for k in pubs[n].keys]
    out: dict[str, KeyVar] = {}
    for k in found:
        out.setdefault(k.name, k)
    return list(out.values())


def reload() -> dict[str, Provider]:
    global _providers
    with _lock:
        _providers = None
        return providers()


def leave_out(name: str, why: str) -> None:
    """A plugin found unusable after loading (its settings builder raised: config.load_settings) is
    left out as one that fails to load is: it and the feeds part of it go from providers(), its
    publishers from publishers(), and the cause into errors()."""
    with _lock:
        p = providers().get(name)
        if p is None:
            return
        for feed in plugin_feeds(name):
            _providers.pop(feed, None)
        for pub in [n for n, q in _publishers.items() if name in (q.part_of, q.account)]:
            _publishers.pop(pub)
        _errors.append((p.origin, f"{name}: {why}"))


def get(name: str) -> Provider | None:
    return providers().get(name)


def plugin_of(name: str) -> str:
    """The plugin a feed is listed under: the one it is part of (Provider.part_of), or its own
    (a part_of naming no provider here leaves it a plugin of its own)."""
    p = providers().get(name)
    return p.part_of if p is not None and p.part_of in providers() and p.part_of != name else name


def plugin_feeds(name: str) -> list[str]:
    """A plugin's feeds: its own first, then the ones part of it (Alpaca: alpaca, boats)."""
    return [name, *(n for n in providers() if n != name and plugin_of(n) == name)]


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"


def plugin_fetchers() -> dict[str, list[tuple[str, Fetcher]]]:
    """The plugins that fetch on their own (API 1.9), by plugin: each (part, its Fetcher), its own and
    its feeds' and publishers' (a part's jobs are its plugin's, taken by its plugin's worker)."""
    out: dict[str, list[tuple[str, Fetcher]]] = {}
    for n, p in providers().items():
        if p.fetcher is not None:
            out.setdefault(plugin_of(n), []).append((n, p.fetcher))
    for n, p in publishers().items():
        if p.fetcher is not None:
            out.setdefault(publisher_of(n), []).append((n, p.fetcher))
    return out


def plugin_databases(name: str) -> list[Database]:
    """Every database a plugin's data is in (API 1.8; layout.py), each once (what it holds of the
    plugin's together), in this order: each feed's bars and trades (feeds/<feed>.db, -ticks.db; its
    live feeds' too: Alpaca's iex), its datasets' files and their downloads (datasets/<file>.db,
    sources/datasets/<file>/), what its publishers read (gov/gov.db), the databases its own code
    writes (`databases`), and its rows in the main file. A feed plugin's include its feeds' and
    publishers' (Massive: its headlines); a publisher's, its parts' (Federal: its agencies'); a part's,
    its own. The user, 2026-10-31: "the plugin will point to the databases it stores the data in"."""
    ps, pubs = providers(), publishers()
    found: list[Database] = []
    main: list[str] = []
    declared: list[Database] = []
    if name in ps:
        for n in plugin_feeds(name) if plugin_of(name) == name else [name]:
            f = ps[n]
            for feed, called in ((n, label(n)), *((x.name, x.label or x.name) for x in f.live if x.name != n)):
                found += [Database(layout.bars(feed), f"{called}: bars"),
                          Database(layout.history(feed), f"{called}: closed months' bars, a DuckDB file a month (when "
                                   "its bars store is DuckDB)", engine="duckdb"),
                          Database(layout.ticks(feed), f"{called}: trades")]
            declared += f.databases
        main.append("its rows by feed: requests a day, events, the reference it gave")
        parts = publisher_parts(name) if plugin_of(name) == name else []
    else:
        parts = [n for n in (name, *publisher_parts(name)) if n in pubs]
    sets: dict[str, list[str]] = {}
    for n in parts:
        for d in pubs[n].declared:
            sets.setdefault(d.provider, []).append(d.name)
    for provider, names in sets.items():
        found += [Database(layout.dataset_file(provider), f"series of {', '.join(names)}"),
                  Database(layout.downloads(provider), "its files, every version as downloaded")]
    if sets:
        main.append("its fetched files' ledger")
    readers = [pubs[n] for n in parts if pubs[n].feeds]
    if readers:
        own = [q.title for q in readers if q.read is None]
        found.append(Database(layout.GOV, (f"what {_and(own)} read" + ", and " if own else "")
                              + "the reads' record: requests, robots.txt, rechecks", shared=True))
        # its documents when its documents store is MongoDB (stores.py): the server here, the app's database
        server = f"mongodb://127.0.0.1:{_mongo_port()}/liminal."
        if own:
            found.append(Database(server + "gov_items", f"what {_and(own)} read, when its documents are in MongoDB",
                                  shared=True))
        if any(q.read is not None for q in readers):
            found.append(Database(server + "news", "its headlines, when its documents are in MongoDB", shared=True))
    declared += [d for n in parts for d in pubs[n].databases]
    found += [d for d in declared if d.where != layout.MAIN]
    if main:  # the main file last, what the app keeps of it first
        found.append(Database(layout.MAIN, "; ".join(main), shared=True))
    found += [d for d in declared if d.where == layout.MAIN]
    out: dict[str, Database] = {}
    for d in found:
        was = out.get(d.where)
        out[d.where] = d if was is None else replace(was, holds=f"{was.holds}; {d.holds}", shared=was.shared or d.shared)
    return list(out.values())


def label(name: str) -> str:
    """How a feed is named to the user: a plugin's own by its name, one part of another plugin as
    that plugin with what it is of it (the user, 2026-10-30: "the source isn't blue ocean its alpaca
    w/ blue ocean"): alpaca (blue ocean), tiingo (forex). A name no provider has stays as it is."""
    owner = plugin_of(name)
    return f"{owner} ({providers()[name].title.lower()})" if owner != name else name


def live_feeds() -> dict[str, tuple[Provider, Live]]:
    """Every live feed the plugins declare, by its name: (its provider, the feed), in their order."""
    return {x.name: (p, x) for p in providers().values() for x in p.live}


def partial_feeds() -> frozenset:
    """The feeds of one venue's share of the market (Provider.partial, Live.partial): a chart of all
    feeds uses them only where no feed of the whole market has the span, never for days."""
    return frozenset({n for n, p in providers().items() if p.partial}
                     | {n for n, (_, x) in live_feeds().items() if x.partial})


def errors() -> list[tuple[str, str]]:
    """The plugins left out at loading: (module or entry point, why)."""
    providers()
    return list(_errors)


def variables() -> tuple[str, ...]:
    """Every variable a plugin reads (a provider's keys, plans and options; a publisher's keys), for
    Settings.env and the saved user variables (config.saved_names)."""
    return tuple(dict.fromkeys([*(n for p in providers().values() for n in (*p.key_names, *p.plan_vars, *p.options)),
                                *(n for p in publishers().values() for k in p.keys for n in k.names)]))


def key_row(k: KeyVar, settings: "Settings | None" = None) -> dict:
    """A variable for the key script, `plugins` and the Plugins view: never its value, only whether it
    is set (with settings)."""
    row = {"name": k.name, "label": k.label, "aliases": list(k.aliases), "secret": k.secret, "hint": k.hint}
    if settings is not None:
        row["set"] = bool(k.value(settings.env))
    return row


def menu(settings: "Settings | None" = None) -> list[dict]:
    """The provider plugins for the key script and the `plugins` command, each once, with the feeds
    part of it (`feeds`: Alpaca's Blue Ocean, on its keys): no key's value, only whether it is set
    (when settings are given)."""
    out = []
    for p in providers().values():
        if plugin_of(p.name) != p.name:
            continue  # listed in its plugin's row
        row = {"name": p.name, "title": p.title, "about": p.about, "signup": p.signup, "origin": p.origin,
               "version": p.version, "api": p.api, "archive": p.archival, "assets": sorted(p.assets),
               "keyless": sorted(p.keyless),
               "keys": [key_row(k) for k in plugin_keys(p.name)],
               "publishers": [_publisher_row(publishers()[n]) for n in publisher_parts(p.name)],
               "planChoices": [{"name": v, "choices": list(c)} for v, c in p.plan_choices.items()],
               "planNote": p.plan_note, "scan": p.scan is not None, "feeds": [],
               "databases": [database_row(d) for d in plugin_databases(p.name)]}
        for n in plugin_feeds(p.name)[1:]:
            f = providers()[n]
            feed = {"name": f.name, "title": f.title, "label": label(n), "about": f.about, "assets": sorted(f.assets)}
            if settings is not None:
                feed["serves"] = sorted(f.served(settings) or ())
            row["feeds"].append(feed)
        if settings is not None:
            served = p.served(settings)
            row.update(keysSet=p.keys_set(settings), serves=sorted(served) if served else [],
                       plan=settings.for_source(p.name).tier.name, planVars=p.plan_state(settings))
        out.append(row)
    return out


def _mongo_port() -> int:
    from .. import mongo

    return mongo.port()


def database_row(d: Database, main=None) -> dict:
    """A database for the `plugins` command and the Plugins view; with the main file's path, where it
    is here and its size (None: not made yet, or a server's)."""
    row = {"where": d.where, "holds": d.holds, "shared": d.shared, "engine": d.kind}
    if main is not None:
        at = layout.locate(d.where, main)
        row.update(path=str(at) if at else d.where, bytes=layout.size(d.where, main))
    return row


def _publisher_row(p: Publisher) -> dict:
    return {"name": p.name, "title": p.title, "about": p.about, "kind": p.kind, "account": p.account,
            "origin": p.origin, "version": p.version, "api": p.api, "datasets": list(p.dataset_names),
            "keys": [key_row(k) for k in p.keys], "databases": [database_row(d) for d in plugin_databases(p.name)]}


def publisher_menu() -> list[dict]:
    """The publisher plugins for the `plugins` command and the key script, each once, with its parts
    (`parts`: Federal's agencies) and every variable it reads (`keys`, its parts' too): what each is,
    its version and origin. A publisher part of a feed plugin is listed in that one's row (menu)."""
    pubs = publishers()
    return [{**_publisher_row(p), "keys": [key_row(k) for k in plugin_keys(p.name)],
             "parts": [_publisher_row(pubs[n]) for n in publisher_parts(p.name)]}
            for p in pubs.values() if publisher_of(p.name) == p.name]
