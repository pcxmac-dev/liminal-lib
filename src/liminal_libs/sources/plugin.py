"""The plugin contract's classes (plugin API 2.0): what every plugin of Liminal is, whatever its kind.

A plugin is an instance of a subclass of `Plugin`: a feed's (`FeedPlugin`: bars, trades, reference
data, its own fetching), a publisher's (`PublisherPlugin`, sources/publisher.py: news, releases,
rates, datasets) or an assistant's (`AssistantPlugin`, assistants/plugin.py: an AI vendor's API,
evaluating arguments). Each plugs into one of Liminal's systems, and its abstract base names it
(the user, 2026-11-03: "each of those plugins has an abstract base class, based on the system they
plug in to"): `IntegratorPlugin`, the stock-data plugins' (feeds and publishers: "all plugins that
work with integrator are ones that connect to get stock data"), and `PlatformPlugin`, the AI
plugins' ("all the plugins that work with platform are ai"; the platform reports the machine and is
the nexus of the systems' IPC). What a plugin declares is a class attribute (`name`, `title`, `assets`,
`keys` ...); what it does is a method overriding the contract's default (`build`, `view`, `scan`,
`settings` ...): the hooks. The registry (sources/registry.py) finds an instance (`PLUGIN`,
`PLUGINS`, `PUBLISHER`, `PUBLISHERS`, `ASSISTANT`, `ASSISTANTS` in a module, or the entry point
itself) or a class, which it instantiates.

The record form (plugin API 1.x: `Provider`, a frozen dataclass with the hooks as callback fields;
`Publisher` likewise) is a subclass of the same classes (`Provider(FeedPlugin)`,
`Publisher(PublisherPlugin)`, `is_record`), so everything the app asks of a plugin it asks the same
way of both: a hook is answered by a record's callback field when set, by a class's method
otherwise, and by the contract's default where neither is given (`gives` says which).

Plugin API 2.1 adds the data specs (sources/data.py): what each kind of data a plugin delivers is,
how complete and fresh a frame of it is, whether its rows are sound. A feed declares its data classes
(`FeedPlugin.data`: an asset class -> a TimeSeriesData subclass, a vendor's where its data differs
from the asset class's: Massive's SIP tape) and the contract gives the asset class's otherwise
(`data_for`); a publisher declares its document class (`PublisherPlugin.data`, a DocumentData
subclass) or takes the one for its kind (`document_data`). The fetching stays in each plugin's
DataSource; the specs describe and judge what it fetches.

A plugin instance is shared by every thread of a process: it holds what it declares, never a run's
state. This module imports no registry and no collector: the contract knows nothing of who loads it.
"""
from __future__ import annotations

import copy
import inspect
import math
from dataclasses import dataclass, field, replace
from datetime import timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING, Iterable, Mapping

from .. import layout
from ..layout import Database
from ..models import is_foreign
from .fetching import Fetcher
from .keys import KeyVar
from .stream import Live
from ..apps.authorization import Login  # the authorization interface a plugin holds once attached

if TYPE_CHECKING:
    from ..config import Settings, Tier
    from ..freshness import Delay
    from .base import DataSource

DEFAULT_PER_REQUEST = 1000  # bars a request returns, for a provider that does not say
NOTHING: Mapping = MappingProxyType({})  # the empty mapping a plugin declares nothing with (never changed)


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


# -- the contract ----------------------------------------------------------------------------------

class Plugin:
    """What every plugin is, whatever its kind: its identity, what the user sets for it, its place
    among the plugins, and the hooks every kind has (a capability scan, settings of its own)."""

    CONTRACT = True  # on the contract's own classes only (gives): their hook methods are the defaults
    KIND = ""  # feed | publisher | assistant
    SYSTEM = ""  # the system it plugs into: integrator (the stock-data plugins) | platform (the AI plugins)
    is_record = False  # the record form (Provider, Publisher): a dataclass, the hooks as callback fields

    name: str = ""  # its name everywhere: its database file, the watchlist, the ledgers, its settings
    title: str = ""
    about: str = ""  # one line for menus and lists
    order: int = 100  # its place in lists (the first feeds: 0-60)
    version: str = ""  # the plugin's own version (a package's, left empty: its distribution's)
    api: str = "2.0"  # the contract it was written for (registry.PLUGIN_API): another major is left out
    origin: str = "built in"  # set when loaded: built in, the plugin directory's checkout, or the package
    keys: tuple[KeyVar, ...] = ()  # what the user sets for it: user environment variables, never written to a file
    signup: str = ""  # where a key is made
    plan_vars: tuple[str, ...] = ()  # its plan's variables: read from the saved user variables like keys
    plan_choices: Mapping[str, tuple[str, ...]] = NOTHING  # plan variable -> its values (the default first)
    plan_note: str = ""  # what its scan cannot tell about its plan (the user's choice is then the plan)
    options: tuple[str, ...] = ()  # its other variables (Settings.env)
    # the plugin whose account (key and limits) its requests count against, when another's: a 429 or
    # a refused key kills both (killswitch.sharing)
    account: str = ""
    # the plugin it is a part of, when another's (API 1.1, 1.2; Blue Ocean: Alpaca's; the Fed:
    # Federal's): listed, keyed and rescanned inside that one, its own everywhere else
    part_of: str = ""
    limits: str = ""  # what its server publishes, beside the window kept
    # databases its own code writes besides the standard ones (API 1.8; layout.Database): a SQLite,
    # DuckDB or Parquet file in the data folder, or a MongoDB collection on this machine
    databases: tuple[Database, ...] = ()
    # its own fetching (API 1.9; sources/fetching.py): its jobs on the shared fetch queue, taken by a
    # worker of its own; None: fetched by the app (the bars sync, the news reader, `datasets`)
    fetcher: Fetcher | None = None
    # The IPC surface (the user, 2026-11-06: "the plugins use an ABC plugin to communicate IPC between
    # themselves and the APP for functionality"; "the plugin ABC will enumerate attached plugins and their
    # API"; "the app plugins need to use the Abstract Base Class login as the only way to present an
    # interface for IPC, between plugins and login and the app as far as AUTHORIZATION"): the hooks of
    # its kind (what `interface` enumerates), the app it is attached to and that app's login (`attach`).
    HOOKS: tuple[str, ...] = ("scan", "settings")
    login: "Login | None" = None  # the one way to a key, an account or a password: self.login.key(self.name)
    app: object = None  # the app it is attached to (an apps.App, the integrator's or the charts app's)

    def __init__(self, **attributes) -> None:
        """A plugin with these attributes set on it (a test's FeedPlugin(name=..., build=...)); a real
        plugin's are its class's attributes and methods. An attribute the class has not is an error."""
        for k, v in attributes.items():
            if not hasattr(type(self), k):
                raise TypeError(f"{type(self).__name__} has no attribute {k!r}")
            object.__setattr__(self, k, v)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name!r}: {self.origin}>"

    @property
    def key_names(self) -> tuple[str, ...]:
        return tuple(n for k in self.keys for n in k.names)

    @property
    def key_hint(self) -> str:
        return " and ".join(k.name for k in self.keys)

    # -- the hooks every kind has: a plugin that answers them overrides them (has_scan, has_settings) --

    def scan(self, source):
        """Its capability scan (`capabilities`; on each chart app start, weekly in the sync, Rescan in
        the Plugins view): a few requests asking what its plan really gives, each answer (what,
        available, detail), or a `Scanned` with the plan they show too; kept, and shown in its
        report. `source` is what its build made. The contract has none: run_scan asks only a plugin
        that has one."""
        raise NotImplementedError(f"{self.name}: no capability scan")

    def settings(self, variables: Mapping[str, str]):
        """Its own settings (plugin API 1.11): built from every variable the app read (the keys, plans
        and options it declares among them) when the settings load, kept as Settings.plugins[name]
        and reached with Settings.plugin(name) by it and by the plugins that are parts of it; a
        ConfigError for a value it refuses stops the app as any setting's does. The contract has none."""
        raise NotImplementedError(f"{self.name}: no settings of its own")

    @property
    def has_scan(self) -> bool:
        return self.gives("scan")

    @property
    def has_settings(self) -> bool:
        return self.gives("settings")

    def settings_of(self, variables: Mapping[str, str]):
        """Its own settings from the variables read (config.load_settings); None when it has none."""
        return self.settings(variables) if self.has_settings else None

    def gives(self, name: str) -> bool:
        """Whether this plugin answers a hook itself: a record's callback field set, an attribute set
        on the instance (with_, __init__), or a method of its class overriding the contract's (a
        class attribute `scan = None` in a subclass drops an inherited one)."""
        if name in self.__dict__:
            return self.__dict__[name] is not None
        for cls in type(self).__mro__:
            if name in cls.__dict__:
                return "CONTRACT" not in cls.__dict__ and cls.__dict__[name] is not None
        return False

    def _answer(self, name: str, default, *args):
        """A hook's answer: the record's callback (or an attribute set on the instance), else the
        class's method; `default` (the contract's own method) where a record leaves it None."""
        if name in self.__dict__:
            given = self.__dict__[name]
            return given(*args) if given is not None else default(self, *args)
        return getattr(self, name)(*args)

    def with_(self, **changes) -> "Plugin":
        """A copy with these attributes changed (the registry's origin and version; a test's
        with_(scan=None)): a record's replace, a class instance's copy with them set on it."""
        if self.is_record:
            return replace(self, **changes)
        out = copy.copy(self)
        for k, v in changes.items():
            if not hasattr(type(self), k):
                raise TypeError(f"{type(self).__name__} has no attribute {k!r}")
            out.__dict__[k] = v
        return out

    # -- the IPC surface --------------------------------------------------------------------------
    def interface(self) -> dict:
        """What this plugin offers its app, as the app enumerates it (App.plugins): its name, kind,
        system, contract version and version, and the hooks it answers itself with their signatures
        (the contract's defaults left out: `gives`). The same shape an app gives of itself."""
        hooks: dict[str, str] = {}
        for name in self.HOOKS:
            if not self.gives(name):
                continue
            given = self.__dict__.get(name) if name in self.__dict__ else getattr(self, name, None)
            try:
                hooks[name] = str(inspect.signature(given)) if callable(given) else ""
            except (TypeError, ValueError):
                hooks[name] = "(...)"
        return {"name": self.name, "kind": self.KIND, "system": self.SYSTEM, "api": self.api, "version": self.version,
                "attached": self.app is not None, "hooks": hooks}

    def attach(self, app, login: "Login | None") -> dict:
        """Attached by `app`: the app and its login (the authorization interface of its namespace) are
        this plugin's from here on; returns the interface the app records. A record is frozen: set
        through object.__setattr__, as __init__ does."""
        object.__setattr__(self, "app", app)
        object.__setattr__(self, "login", login)
        return self.interface()

    def detach(self) -> None:
        object.__setattr__(self, "app", None)
        object.__setattr__(self, "login", None)

    def ask(self, hook: str, *args, **kwargs):
        """One of this plugin's hooks called by name (the app's `ask`; across the pipe once the
        plugin is resident): only a hook `interface` lists; LookupError for any other name."""
        if hook not in self.interface()["hooks"]:
            raise LookupError(f"{self.name or type(self).__name__} offers no {hook!r}")
        given = self.__dict__.get(hook) if hook in self.__dict__ else getattr(self, hook)
        return given(*args, **kwargs)

    def refusal(self) -> str | None:
        """Why the registry cannot take it: no name, or a database it may not write (databases_refused)."""
        if not self.name:
            return "it has no name"
        return databases_refused(self.databases)


class IntegratorPlugin(Plugin):
    """The abstract base of the plugins that plug into the integrator: the ones that connect to get
    stock data (a feed's bars, trades and reference data; a publisher's news, releases, rates and
    datasets), fetched by the integrator's runs and read back through its requests."""

    CONTRACT = True
    SYSTEM = "integrator"


class PlatformPlugin(Plugin):
    """The abstract base of the plugins that plug into the platform: the AI assistants, through which
    the platform (the machine's information, the systems' IPC nexus) and the apps on it evaluate
    arguments (assistants/plugin.py: AssistantPlugin)."""

    CONTRACT = True
    SYSTEM = "platform"


class FeedPlugin(IntegratorPlugin):
    """A feed's plugin: everything the app needs to know about a data provider (sources/registry.py
    says what for). Its hooks, each with the contract's default here: `build` (its DataSource),
    `view` (its plan and caps), `rate` (the window its limiter keeps), `has_keys`, `serves`,
    `markets`, `carries`, `plan_resolutions`, `reporter`; `scan` and `settings` from Plugin. What
    the rest of the app asks it goes through the templates below (`keys_set`, `served`,
    `settings_view`, `rate_window`, `market_of`, `can_carry`, `resolutions_for`, `run_scan`,
    `plan_state`, `request_bars`, `report`), the same for a record and a class."""

    CONTRACT = True
    KIND = "feed"
    HOOKS = ("build", "view", "rate", "has_keys", "serves", "markets", "carries", "plan_resolutions", "reporter", "scan",
             "settings")

    assets: frozenset = frozenset()  # asset classes it can collect at all (with keys / on some plan)
    keyless: frozenset = frozenset()  # asset classes served without its keys (Alpaca: crypto)
    archival: bool = False  # a closed feed: each symbol filled once, then never asked again
    tier: "Tier | None" = None  # its plan, when it has one fixed plan
    per_run: int | None = None  # its request caps with that plan
    per_day: int | None = None
    kind: str = "vendor"  # its data for the composite model: exchange, consolidated, official, vendor
    kind_by_asset: Mapping[str, str] = NOTHING
    reference_for: frozenset = frozenset()  # asset classes whose gold standard exchange it is
    venue_notes: Mapping[str, str] = NOTHING  # audits: what its data of an asset is
    symbols_per_month: int | None = None  # different symbols its plan allows a month
    # the initializer's bulk symbols start at this resolution, or one per asset class ({"stock": ...})
    bulk_resolution: "str | Mapping[str, str]" = "auto"
    missing_keys_note: str = ""  # the Fetcher's hover text when its keys are not set
    missing_keys_chip: str = "no key"
    # Capabilities, for the delivery scheme (sourcing.py): the resolutions it provides per asset class
    # (day, minute, second; default: day and minute for each of `assets`), the bars one request
    # returns per resolution (None: a symbol's whole history in one request), its live streams.
    provides: Mapping[str, tuple[str, ...]] = NOTHING
    per_request: Mapping[str, int | None] = NOTHING
    streams: Mapping[str, str] = NOTHING  # asset class -> what its stream is
    # its live feeds for the recorder (API 1.5; sources/stream.py): a stream's class or a poller each,
    # with its own feed name (Coinbase's "coinbase", Alpaca's "iex"); a property where one needs self
    live: tuple[Live, ...] = ()
    # its bars are one venue's share of the market (API 1.5; Blue Ocean's nights): a chart of all
    # feeds uses them only where no feed of the whole market has the span, never for days
    partial: bool = False
    # the exchanges whose stock listings it carries (1.12; MICs of models.VENUES, "US" for every US
    # listing); empty: US listings only
    exchanges: frozenset = frozenset()
    # 2.0: when its data is published, what freshness.py held for every feed before: each of its
    # plans' publication delays (tier name -> asset class -> Delay: Alpaca's Basic 16 minutes behind,
    # J-Quants' free plan twelve weeks) and a market with hours of its own (asset class -> the
    # calendar it keeps: Blue Ocean's "overnight"); declared into freshness.DELAYS when it loads
    delays: "Mapping[str, Mapping[str, Delay]]" = NOTHING
    hours: Mapping[str, str] = NOTHING
    # 2.1: its data classes (sources/data.py), an asset class -> a TimeSeriesData subclass, where its
    # data differs from the asset class's (Massive's SIP tape and its ticks, Tiingo's split-adjusted
    # days, Alpaca's IEX stream): the spec of a row, the assessment of a frame, its integrity; the
    # contract's default for the asset class otherwise (data_for). A plugin declaring one says api "2.1".
    data: "Mapping[str, type]" = NOTHING

    # -- the hooks, each the contract's default here -----------------------------------------------

    def build(self, settings: "Settings", request_budget: int | None) -> "DataSource":
        """Its DataSource (sources/base.py), with its limiter and caps (limiter, machine_caps and
        public_client in sources/registry.py). Every feed plugin gives one."""
        raise NotImplementedError(f"{self.name}: no build (a feed plugin makes its source here)")

    def view(self, base: "Settings") -> "Settings":
        """The settings as this feed sees them (Settings.for_source): its plan and caps. Default:
        its fixed `tier` and caps when it has them, otherwise the settings as they are."""
        if self.tier is None:
            return base
        return replace(base, tier=self.tier, max_requests_per_run=self.per_run or base.max_requests_per_run,
                       max_requests_per_day=self.per_day or base.max_requests_per_day, origin=base)

    def rate(self, settings: "Settings") -> tuple[int | None, float]:
        """The window its limiter keeps: (requests, seconds). Default: its plan's rate a minute."""
        rpm = settings.for_source(self.name).tier.requests_per_minute
        return rpm, minute_window(rpm)

    def has_keys(self, settings: "Settings") -> bool:
        """Whether its keys are set (asked only of a plugin with keys). Default: every key's variable
        has a value."""
        return all(k.value(settings.env) for k in self.keys)

    def serves(self, settings: "Settings") -> frozenset | None:
        """The asset classes it collects on this machine; None when it is not set up here at all.
        Default: all of `assets` with its keys (or needing none), only `keyless` without."""
        return self.assets if self.keys_set(settings) in (None, True) else frozenset(self.keyless)

    def markets(self, settings: "Settings") -> dict[str, str]:
        """Which market each of its asset classes is (feeds of one market are copies of each other:
        one download per span between them). Default: each class its own market, named after the feed."""
        return {a: self.name for a in self.assets}

    def carries(self, spec) -> bool:
        """Whether it holds a symbol of an asset class it serves (can_carry asks that first): its own
        list, or the default, for a stock the exchanges it declares (1.12): none, the US alone (a
        stock feed from before); some, exactly those, with "US" standing for every US listing (Tiingo:
        "US", XSHG, XSHE; TWSE: XTAI)."""
        if spec.asset != "stock":
            return True
        if not self.exchanges:
            return not is_foreign(spec.exchange)
        return spec.exchange in self.exchanges if is_foreign(spec.exchange) else "US" in self.exchanges

    def plan_resolutions(self, tier: "Tier", asset: str) -> tuple[str, ...]:
        """The resolutions its plan (`tier`) provides for an asset class it serves. Default:
        `provides`, or day and minute."""
        return tuple(self.provides.get(asset, ("day", "minute")))

    def reporter(self, report: Report, settings: "Settings | None") -> Report:
        """Its standard report amended with what its plan says beyond its declarations (a trial's
        end in a note). Default: the report as it is."""
        return report

    # -- the templates: what the app asks, the same of a record and a class ------------------------

    def keys_set(self, settings: "Settings") -> bool | None:
        """Whether its keys are set (None: it needs none)."""
        if not self.keys:
            return None
        return bool(self._answer("has_keys", FeedPlugin.has_keys, settings))

    def served(self, settings: "Settings") -> frozenset | None:
        """The asset classes it collects on this machine; None when it is not set up here at all."""
        return self._answer("serves", FeedPlugin.serves, settings)

    def settings_view(self, base: "Settings") -> "Settings":
        """The settings as this feed sees them (Settings.for_source): its plan and caps."""
        return self._answer("view", FeedPlugin.view, base)

    def rate_window(self, settings: "Settings") -> tuple[int | None, float]:
        """The window its limiter keeps: (requests, seconds)."""
        return self._answer("rate", FeedPlugin.rate, settings)

    def market_of(self, settings: "Settings") -> dict[str, str]:
        """Which market each of its asset classes is."""
        return self._answer("markets", FeedPlugin.markets, settings)

    def can_carry(self, spec) -> bool:
        """Whether it can hold a symbol at all: its asset class, then `carries`."""
        if spec.asset not in self.assets:
            return False
        return bool(self._answer("carries", FeedPlugin.carries, spec))

    def kind_for(self, asset: str) -> str:
        return self.kind_by_asset.get(asset) or self.kind

    def data_for(self, asset: str):
        """Its data class for an asset class, as an instance for this feed (2.1; sources/data.py): the
        one it declares (`data`), else the contract's for the asset class (StockSymbolData, IndexData,
        FxData, CryptoSymbolData, FutureData). ValueError for an asset class the library has not."""
        from .data import default_series  # when asked, not on import: the specs read the calendars

        cls = self.data.get(asset) or default_series(asset)
        if cls is None:
            raise ValueError(f"{self.name}: no data class for {asset!r}")
        out = cls(feed=self.name)
        own = self.hours.get(asset) if isinstance(self.hours, Mapping) else None
        if own and out.calendar in ("", asset):  # a market with hours of its own (Blue Ocean's nights): its calendar
            out.calendar = own
        return out

    def resolutions_for(self, tier: "Tier", asset: str) -> tuple[str, ...]:
        """The resolutions its plan (`tier`) provides for an asset class (none it does not serve)."""
        if asset not in self.assets:
            return ()
        return tuple(self._answer("plan_resolutions", FeedPlugin.plan_resolutions, tier, asset))

    def run_scan(self, source: "DataSource", settings: "Settings | None" = None) -> Scanned:
        """Its capability scan, its plan held to its own plan variables (a plugin sets no other's),
        and its limits verified: the rate-limit headers its server sent on the scan's requests,
        against the window its limiter keeps (with `settings`)."""
        out = self.scan(source) if self.has_scan else ()
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
        from .registry import rate_window as kept_window  # the registry's: its server's lower window too

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
                        rate=kept_window(self.name, settings) if settings is not None else None,
                        symbols_a_month=self.symbols_per_month, published=self.limits)
        out = Report(self.name, self.title, profile.tier.name, self.keys_set(settings) if settings is not None else None,
                     limits, tuple(caps), tuple((a, w) for a, w in self.streams.items() if a in profile.assets),
                     self.kind, ("a closed archive: each symbol filled once, then not asked again",) if self.archival else (),
                     tuple((str(w), bool(ok), str(d)) for w, ok, d in (scanned or {}).get("results", [])),
                     (scanned or {}).get("at"))
        return self._answer("reporter", FeedPlugin.reporter, out, settings)


# -- what a plugin's declarations are checked against -------------------------------------------

def databases_refused(dbs: Iterable[Database]) -> str | None:
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


# -- rate windows: what a plugin keeps, what its server publishes ----------------------------------

def minute_window(rpm: int | None) -> float:
    """The seconds a limiter of `rpm` requests a minute keeps them in. A few a minute (Massive's
    free classes: 5): a couple of seconds' margin, so jitter between sending and arriving never
    puts six in the server's minute."""
    return 62.0 if rpm and rpm <= 10 else 60.0


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
