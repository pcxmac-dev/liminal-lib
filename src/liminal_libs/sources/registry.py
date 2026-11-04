"""Data providers are plugins: a feed plugin each (sources/plugin.py: FeedPlugin; its record form, the
`Provider` here), holding everything the app needs to know about it.

A plugin is a module that defines `PLUGIN` (a FeedPlugin subclass, or an instance of one: a Provider
record; `PLUGINS` for several), a publisher's `PUBLISHER` (sources/publisher.py) or an assistant's
`ASSISTANT` (assistants/plugin.py). Every one is a package of its own (plugin-integrator-<name>,
2026-11-01), registering under the entry point group `liminal.providers` (pointing at the module, or
at the plugin itself); the plugin directory's checkouts are loaded from there (plugin_checkouts),
and a module dropped in `sources/plugins/` is still found on the next start, as the built-ins were.

What a plugin gives:
- `build(settings, request_budget)`: its DataSource (sources/base.py), with its limiter and caps.
- its keys (`KeyVar`: user environment variables; never written to a file), and how to get one
  (`signup`): tools/set-key.ps1 offers each plugin's keys from `liminal-charts plugins --json`.
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

Hooks left out take the contract's defaults (FeedPlugin's methods), so a plain keyed REST feed needs
little more than its build. A plugin that fails to load is left out and reported (`errors()`,
`plugins`), never fatal.

The contract is versioned (`PLUGIN_API`, major.minor; docs/ARCHITECTURE.md says what it holds): a
class is written for 2.x, the record form (Provider, Publisher: the hooks as callback fields) for
1.x (`RECORD_API`, every minor up to it still loaded). Each plugin gives its own `version` (a
package's may leave it empty: its distribution's is used) and the contract it was written for
(`api`). One written for another major, or for a newer minor than this app offers, is left out and
reported like a broken one.

Which plugins a process loads is a selection (`Selection`, LIMINAL_PLUGINS_LOADED; `select`,
`selection`, `plugins_loaded`): every plugin found by default, the ones named (each with its parts),
or none at all (the user, 2026-11-03: "the default case is to load all plugins, however the
integrator can be loaded with select plugins or none": the integrator's `--plugins`). The plugins
found but not named are left out quietly (`Selection.left_out`, not `errors()`), and a process
started by this one inherits the selection.
"""
from __future__ import annotations

import importlib
import os
import pkgutil
import sys
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Callable, Iterable, Mapping

from .. import layout
from ..layout import MAIN, Database  # noqa: F401 - plugins import them from here
from ..ratelimit import MachineLedger, RateLimiter  # plugins import MachineLedger from here (1.10)
from .fetching import Fetched, FetchContext, Fetcher, Job  # noqa: F401 - plugins import them from here
from .keys import KeyVar  # noqa: F401 - plugins import it from here
from .plugin import (DEFAULT_PER_REQUEST, SERVER_SHARE, Capability, FeedPlugin, IntegratorPlugin,  # noqa: F401 - from here too
                     Limits, PlatformPlugin, Plugin, Report, Scanned, _per, databases_refused, minute_window,
                     server_limits, server_per_minute, within)
from .publisher import Publisher, PublisherPlugin
from .stream import Live

if TYPE_CHECKING:
    from ..config import Settings, Tier
    from .base import DataSource

ENTRY_POINTS = "liminal.providers"
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
# 1.12 (2026-11-02): Provider.exchanges, the exchanges outside the US whose stock listings a feed
#   carries (models.VENUES: 7203.T on XTKS); without it, and without `carries`, a stock feed holds
#   US listings only. The directory asks a feed's listings() for those exchanges too.
# 2.0 (2026-11-03): the class contract (sources/plugin.py): a plugin is an instance of a FeedPlugin
#   or PublisherPlugin subclass (an AssistantPlugin's too: assistants/plugin.py), what it declares
#   class attributes, what it does methods overriding the contract's defaults; FeedPlugin.delays and
#   hours (freshness.Delays). The record form (Provider, Publisher: the hooks as callback fields) is
#   plugin API 1.x still, every minor up to RECORD_API, loaded as before.
PLUGIN_API = "2.0"
RECORD_API = "1.12"  # the record form's contract: what a Provider or a Publisher may declare


def api_version(api) -> tuple[int, int]:
    """A contract version's (major, minor): "1.2", "1" (1.0). ValueError when it is not one."""
    parts = str(api).strip().split(".")
    if not 1 <= len(parts) <= 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"api {api!r} is not a contract version (major.minor)")
    return int(parts[0]), int(parts[1]) if len(parts) == 2 else 0


def api_refusal(api, offered: str | None = None, *, record: bool = False) -> str | None:
    """Why a plugin written for contract `api` cannot load in an app offering `offered` (None: it
    can): its major must be the app's, its minor no newer than the app's. A class is held to
    PLUGIN_API; the record form (`record`: a Provider or a Publisher) to RECORD_API, the 1.x contract
    it is, so every plugin from before the classes loads as it did."""
    try:
        major, minor = api_version(api)
    except ValueError as e:
        return str(e)
    if record:
        offered = offered or RECORD_API
        have, have_minor = api_version(offered)
        if major > have:
            return (f"written for plugin API {api} as a record (a Provider or a Publisher); the record form is "
                    f"plugin API {have}.x (this app's {offered}): a plugin written for {major}.x is a class "
                    "(FeedPlugin, PublisherPlugin)")
        if major < have:
            return f"written for plugin API {api}; this app offers {offered} for records: it needs a release for {have}.x"
        if minor > have_minor:
            return f"written for plugin API {api}, newer than this app's {offered} for records: it needs a newer app"
        return None
    offered = offered or PLUGIN_API
    have, have_minor = api_version(offered)
    if major < have:
        return f"written for plugin API {api}; this app offers {offered}: it needs a release for {have}.x"
    if major > have:
        return f"written for plugin API {api}; this app offers {offered}: it needs a newer app"
    if minor > have_minor:
        return f"written for plugin API {api}, newer than this app's {offered}: it needs a newer app"
    return None


@dataclass(frozen=True, eq=False)
class Provider(FeedPlugin):
    """The record form of a feed plugin (plugin API 1.x, RECORD_API): FeedPlugin's attributes as
    fields and its hooks as callback fields (None: the contract's default, described at each
    FeedPlugin method), loaded and asked as any FeedPlugin is (sources/plugin.py). The four fields
    without a default are required ones (`field()`: the class's attributes are not their defaults)."""

    is_record = True

    name: str = field()  # the feed's name everywhere: its database file, the watchlist, the ledgers
    title: str = field()
    build: Callable[["Settings", int | None], "DataSource"] = field()
    assets: frozenset = field()  # asset classes it can collect at all (with keys / on some plan)
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
    # 1.12: the exchanges whose stock listings it carries (MICs of models.VENUES, "US" for every US
    # listing); empty: US listings only
    exchanges: frozenset = frozenset()
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


# -- helpers for building a provider's client ----------------------------------------------------

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
_providers: dict[str, FeedPlugin] | None = None
_publishers: dict[str, PublisherPlugin] = {}
_assistants: dict[str, "AssistantPlugin"] = {}  # the AI assistants (2.0; assistants/plugin.py)
_errors: list[tuple[str, str]] = []
SELECTION_VAR = "LIMINAL_PLUGINS_LOADED"  # the plugins a process loads: names, `none`, or unset for all


@dataclass(frozen=True)
class Selection:
    """Which plugins a process loads (LIMINAL_PLUGINS_LOADED): the plugins' names, comma separated
    (a part's name keeps its whole plugin: boats keeps Alpaca, sec keeps Federal), `none` for no
    plugin at all, unset or `all` for every plugin found. Once applied (discovery) it lists the
    plugins found but not named and the names no plugin answers to."""
    names: tuple[str, ...] | None = None  # None: every plugin found; (): none at all
    left_out: tuple[str, ...] = ()  # the plugins found but not named (by plugin, its parts with it)
    unknown: tuple[str, ...] = ()  # the names no plugin found answers to

    @classmethod
    def parse(cls, raw: "str | None") -> "Selection":
        """As typed: names comma or space separated, any case; `none`; empty or `all`."""
        text = (raw or "").strip().lower()
        if not text or text == "all":
            return cls(None)
        if text == "none":
            return cls(())
        return cls(tuple(dict.fromkeys(text.replace(",", " ").split())))

    @property
    def everything(self) -> bool:
        return self.names is None

    @property
    def text(self) -> str:
        """What LIMINAL_PLUGINS_LOADED holds for it: `all`, `none`, or the names comma separated."""
        return "all" if self.names is None else "none" if not self.names else ",".join(self.names)


_selection = Selection()  # the one applied when the plugins were found

NAMES = ("PLUGIN", "PUBLISHER", "ASSISTANT")  # what a module calls its plugin (the plural: several)


def _is_plugin_class(x) -> bool:
    return isinstance(x, type) and issubclass(x, Plugin)


def _plugins_in(module, origin: str) -> list[Plugin]:
    """A module's plugins: its feed (`PLUGIN`, `PLUGINS`), its publishers (`PUBLISHER`, `PUBLISHERS`)
    and its assistants (`ASSISTANT`, `ASSISTANTS`), at least one of them; each an instance of a
    Plugin subclass (a Provider or a Publisher record among them) or the subclass itself,
    instantiated here. An entry point may point at the instance or the class instead of a module."""
    if isinstance(module, Plugin) or _is_plugin_class(module):
        return _plugins_in(SimpleNamespace(PLUGIN=module), origin)
    found = [p for name in NAMES for p in getattr(module, name + "S", ())]
    found += [p for p in (getattr(module, name, None) for name in NAMES) if isinstance(p, Plugin) or _is_plugin_class(p)]
    if not found or not all(isinstance(p, Plugin) or _is_plugin_class(p) for p in found):
        raise TypeError("defines no PLUGIN (a FeedPlugin: a class, an instance, or a sources.registry.Provider record) "
                        "nor PUBLISHER (a PublisherPlugin, or a sources.publisher.Publisher record) nor ASSISTANT "
                        "(an AssistantPlugin)")
    return [(p() if _is_plugin_class(p) else p).with_(origin=origin) for p in found]


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
        why = api_refusal(p.api, record=p.is_record)
        if why:
            errors.append((where, f"{p.name}: {why}"))
        else:
            found.append(p if p.version or not version else p.with_(version=version))


def plugin_checkouts(folder: Path) -> list[tuple[str, Path, str, str]]:
    """The plugin checkouts in `folder` (the plugin directory, layout.plugins_dir): each subfolder
    whose pyproject.toml declares entry points of this contract's group, as (the checkout's name,
    the folder its package imports from (src/, else itself), 'module:attr', its version)."""
    try:
        import tomllib
    except ImportError:  # Python 3.10: the installed packages' entry points alone
        return []
    out: list[tuple[str, Path, str, str]] = []
    if not folder.is_dir():
        return out
    for sub in sorted(p for p in folder.iterdir() if p.is_dir()):
        pyproject = sub / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project") or {}
        except (OSError, ValueError):
            continue
        src = sub / "src"
        for value in ((project.get("entry-points") or {}).get(ENTRY_POINTS) or {}).values():
            out.append((sub.name, src if src.is_dir() else sub, str(value), str(project.get("version") or "")))
    return out


def _directory_plugins(found: list, errors: list[tuple[str, str]]) -> set[str]:
    """The checkouts of the plugin directory loaded (the user, 2026-11-01: "all plugins must be in a
    plugin directory assigned by the fetcher"; 2026-11-02: "make sure plugins get loaded in to
    plugins"), each one's package imported from its own folder. Returns the entry point values
    loaded, so the same plugin installed as a package besides is not loaded twice."""
    folder = layout.plugins_dir()
    loaded: set[str] = set()
    if folder is None:
        return loaded
    for name, src, value, version in plugin_checkouts(folder):
        if str(src) not in sys.path:
            sys.path.append(str(src))
        module_name, _, attr = value.partition(":")

        def load(module_name=module_name, attr=attr):
            module = importlib.import_module(module_name)
            return getattr(module, attr) if attr else module
        _load(f"{name} ({value})", load, f"plugins/{name} ({value})", version, found, errors)
        loaded.add(value)
    return loaded


def discover() -> tuple[dict[str, FeedPlugin], list[tuple[str, str]]]:
    """Every provider plugin: the built-in modules, the plugin directory's checkouts, then the
    installed packages' entry points. Returns (providers by name in their order, [(where, why)]
    for the ones left out)."""
    providers_, _, errors_ = discover_all()
    return providers_, errors_


def discover_all() -> tuple[dict[str, FeedPlugin], dict[str, PublisherPlugin], list[tuple[str, str]]]:
    """The providers and the publishers (both by name, in their order), and the plugins left out
    (discover_everything, without the assistants)."""
    found, pubs, _, errors_ = discover_everything()
    return found, pubs, errors_


def _kind(p: Plugin) -> str:
    from ..assistants.plugin import AssistantPlugin  # when asked, not on import: the assistants import the client

    return "publisher" if isinstance(p, PublisherPlugin) else "assistant" if isinstance(p, AssistantPlugin) else "provider"


def discover_everything() -> tuple[dict[str, FeedPlugin], dict[str, PublisherPlugin], dict[str, "AssistantPlugin"],
                                   list[tuple[str, str]]]:
    """The providers, the publishers and the assistants (each by name, in their order), and the
    plugins left out: one that fails to load, one written for a contract this app does not offer
    (api_refusal), a second of one name."""
    from . import plugins
    from ..assistants.plugin import AssistantPlugin

    found: list[Plugin] = []
    errors: list[tuple[str, str]] = []
    for info in sorted(pkgutil.iter_modules(plugins.__path__), key=lambda m: m.name):
        where = f"{plugins.__name__}.{info.name}"
        _load(where, lambda: importlib.import_module(where), "built in", "", found, errors)
    loaded = _directory_plugins(found, errors)
    for ep in _entry_points():
        if ep.value in loaded:
            continue  # the plugin directory's checkout of it is loaded already
        dist, version = _distribution(ep)
        _load(f"{ep.name} ({ep.value})", ep.load, f"package: {dist} ({ep.value})" if dist else f"package: {ep.value}",
              version, found, errors)
    out: dict[str, FeedPlugin] = {}
    pubs: dict[str, PublisherPlugin] = {}
    assts: dict[str, AssistantPlugin] = {}
    keys: dict[str, str] = {}  # a feed's key -> its publisher (keys name the state kept)
    sets: dict[str, str] = {}  # a dataset's name -> its publisher (names its state and versions)
    lives: dict[str, str] = {}  # a live feed's name -> its provider (names its bars, status and kills)
    files: dict[str, str] = {}  # a dataset's file (Dataset.provider: datasets/<provider>.db) -> its publisher
    # the built-ins first, so an installed package never takes the name, a feed or a dataset of one
    # (whatever its order); all listed in their order after
    for p in sorted(found, key=lambda p: (p.origin != "built in", p.order, p.name)):
        mine = pubs if isinstance(p, PublisherPlugin) else assts if isinstance(p, AssistantPlugin) else out
        if p.name in out or p.name in pubs or p.name in assts:
            first = (out.get(p.name) or pubs.get(p.name) or assts[p.name]).origin
            errors.append((p.origin, f"a second {_kind(p)} named {p.name!r}: the first kept ({first})"))
            continue
        why = p.refusal()
        if why:
            errors.append((p.origin, f"{_kind(p)} {p.name!r}: {why}"))
            continue
        if isinstance(p, PublisherPlugin):
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
        elif isinstance(p, AssistantPlugin):
            pass  # an assistant has no feeds, datasets or live feeds to clash
        else:
            # a live feed's name is its own provider's, or a name no other provider has
            clash = next((x.name for x in p.live if lives.get(x.name, p.name) != p.name
                          or (x.name != p.name and x.name in {q.name for q in found if isinstance(q, FeedPlugin)})), None)
            if clash:
                errors.append((p.origin, f"provider {p.name!r}: live feed {clash!r} is {lives.get(clash, clash)!r}'s"))
                continue
            lives.update({x.name: p.name for x in p.live})
        mine[p.name] = p
    global _selection
    _selection = _apply_selection(Selection.parse(os.environ.get(SELECTION_VAR)), out, pubs, assts)
    from ..freshness import DELAYS  # when loaded, not on import: the freshness rules import the calendars

    for p in out.values():
        DELAYS.declare(p)  # its own publication delays and market hours (2.0)
    in_order = lambda d: dict(sorted(d.items(), key=lambda kv: (kv[1].order, kv[0])))  # noqa: E731
    return in_order(out), in_order(pubs), in_order(assts), errors


_databases_refused = databases_refused  # its name before 2.0 (sources/plugin.py: Plugin.refusal)


def _owner(p: Plugin, feeds: Mapping[str, FeedPlugin], pubs: Mapping[str, PublisherPlugin]) -> str:
    """The plugin a loaded plugin is listed under (plugin_of, publisher_of: before the registry
    holds them): the one it is part of, else itself."""
    if isinstance(p, PublisherPlugin):
        if p.part_of and p.part_of != p.name:
            if p.part_of in pubs:
                return p.part_of
            if p.part_of in feeds:
                return _owner(feeds[p.part_of], feeds, pubs)
        return p.name
    if isinstance(p, FeedPlugin) and p.part_of and p.part_of != p.name and p.part_of in feeds:
        return p.part_of
    return p.name


def _apply_selection(chosen: Selection, feeds: dict, pubs: dict, assts: dict) -> Selection:
    """The selection applied to what was found: the plugins not named taken out of the three (each
    with its parts), the names answering to no plugin noted. What applied."""
    if chosen.everything:
        return chosen
    found = {**feeds, **pubs, **assts}
    owners = {n: _owner(p, feeds, pubs) for n, p in found.items()}
    wanted = {owners[n] for n in chosen.names if n in owners}
    left_out: list[str] = []
    for d in (feeds, pubs, assts):
        for n in list(d):
            if owners[n] not in wanted:
                d.pop(n)
                if owners[n] not in left_out:
                    left_out.append(owners[n])
    return replace(chosen, left_out=tuple(left_out), unknown=tuple(n for n in chosen.names if n not in owners))


def selection() -> Selection:
    """The selection applied when the plugins were found (LIMINAL_PLUGINS_LOADED as it then read)."""
    with _lock:
        providers()
        return _selection


def select(names: "Iterable[str] | str | None") -> Selection:
    """Load these plugins alone from now on, in this process and the ones it starts: names (a list,
    or their text comma separated), `none` or [] for no plugin at all, None or `all` for every plugin
    found. LIMINAL_PLUGINS_LOADED is set so and the plugins found again; what applied is returned."""
    chosen = Selection.parse(names if names is None or isinstance(names, str) else ",".join(names))
    if chosen.everything:
        os.environ.pop(SELECTION_VAR, None)
    else:
        os.environ[SELECTION_VAR] = chosen.text
    reload()
    return selection()


def plugins_loaded() -> list[str]:
    """The plugins loaded, by name, each once with its parts under it (Alpaca for alpaca and boats,
    Federal for its agencies, Massive for its headlines), in their order."""
    out: list[str] = []
    for n in providers():
        if (p := plugin_of(n)) not in out:
            out.append(p)
    for n in publishers():
        if (p := publisher_of(n)) not in out:
            out.append(p)
    for n in assistants():
        if n not in out:
            out.append(n)
    return out


def providers() -> dict[str, FeedPlugin]:
    """The providers by name, in their order (found once a process; `reload()` looks again)."""
    global _providers
    with _lock:
        if _providers is None:
            _providers, found, found_assistants, found_errors = discover_everything()
            _publishers.clear()
            _publishers.update(found)
            _assistants.clear()
            _assistants.update(found_assistants)
            _errors[:] = found_errors
        return _providers


def publishers() -> dict[str, PublisherPlugin]:
    """The publisher plugins by name, in their order (found with the providers)."""
    with _lock:
        providers()
        return dict(_publishers)


def assistants() -> dict[str, "AssistantPlugin"]:
    """The AI assistant plugins by name, in their order (found with the providers; 2.0)."""
    with _lock:
        providers()
        return dict(_assistants)


def assistant(name: str) -> "AssistantPlugin | None":
    return assistants().get(name)


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
    publishers' (each once, by name), a publisher's with its parts' (Federal: the SEC's GOV_CONTACT),
    an assistant's (its key and its account)."""
    if name in assistants():
        return list(assistants()[name].keys)
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


def reload() -> dict[str, FeedPlugin]:
    global _providers
    with _lock:
        _providers = None
        return providers()


def leave_out(name: str, why: str) -> None:
    """A plugin found unusable after loading (its settings builder raised: config.load_settings) is
    left out as one that fails to load is: it and the feeds part of it go from providers(), its
    publishers (itself, when it is one) from publishers(), and the cause into errors()."""
    with _lock:
        p = providers().get(name) or _publishers.get(name) or _assistants.get(name)
        if p is None:
            return
        for feed in plugin_feeds(name):
            _providers.pop(feed, None)
        for pub in [n for n, q in _publishers.items() if name in (n, q.part_of, q.account)]:
            _publishers.pop(pub)
        _assistants.pop(name, None)
        _errors.append((p.origin, f"{name}: {why}"))


def get(name: str) -> FeedPlugin | None:
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
    plugin's together), in this order: each feed's bars (feeds/<feed>.db) and, where it can have
    trades (1-second bars among what it provides, or a stream of its own), its trades (-ticks.db; a
    feed of daily bars alone, TWSE's, has no trades file), its live feeds' too (Alpaca's iex: a
    stream's trades), its datasets' files and their downloads (datasets/<file>.db,
    sources/datasets/<file>/), what its publishers read (gov/gov.db), the databases its own code
    writes (`databases`), and its rows in the main file. A feed plugin's include its feeds' and
    publishers' (Massive: its headlines); a publisher's, its parts' (Federal: its agencies'); a part's,
    its own. The user, 2026-10-31: "the plugin will point to the databases it stores the data in"."""
    if name in assistants():  # an assistant's answers: the main file's evaluations (2.0)
        return [Database(layout.MAIN, "its evaluations: the evaluations table (every answer, its argument, what it cost)",
                         shared=True)]
    ps, pubs = providers(), publishers()
    found: list[Database] = []
    main: list[str] = []
    declared: list[Database] = []
    if name in ps:
        for n in plugin_feeds(name) if plugin_of(name) == name else [name]:
            f = ps[n]
            ticks = not f.provides or any("second" in r for r in f.provides.values()) \
                or any(x.stream is not None for x in f.live)
            for feed, called, trades in ((n, label(n), ticks),
                                         *((x.name, x.label or x.name, x.stream is not None) for x in f.live if x.name != n)):
                found += [Database(layout.bars(feed), f"{called}: bars"),
                          Database(layout.history(feed), f"{called}: closed months' bars, a DuckDB file a month (when "
                                   "its bars store is DuckDB)", engine="duckdb")]
                if trades:
                    found.append(Database(layout.ticks(feed), f"{called}: trades"))
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
        own = [q.title for q in readers if not q.reads_itself]
        found.append(Database(layout.GOV, (f"what {_and(own)} read" + ", and " if own else "")
                              + "the reads' record: requests, robots.txt, rechecks", shared=True))
        # its documents when its documents store is MongoDB (stores.py): the server here, the app's database
        server = f"mongodb://127.0.0.1:{_mongo_port()}/liminal."
        if own:
            found.append(Database(server + "gov_items", f"what {_and(own)} read, when its documents are in MongoDB",
                                  shared=True))
        if any(q.reads_itself for q in readers):
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


def live_feeds() -> dict[str, tuple[FeedPlugin, Live]]:
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
    """Every variable a plugin reads (a provider's keys, plans and options; a publisher's keys; an
    assistant's key, account, plan and caps), for Settings.env and the saved user variables
    (config.saved_names)."""
    return tuple(dict.fromkeys([*(n for p in providers().values() for n in (*p.key_names, *p.plan_vars, *p.options)),
                                *(n for p in publishers().values() for k in p.keys for n in k.names),
                                *(n for p in assistants().values() for n in (*p.key_names, *p.plan_vars, *p.options))]))


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
               "planNote": p.plan_note, "scan": p.has_scan, "feeds": [],
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


def _publisher_row(p: PublisherPlugin) -> dict:
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


def assistant_menu(settings: "Settings | None" = None) -> list[dict]:
    """The AI assistant plugins (2.0) for the key script, `plugins --json` and the Plugins view: each
    with its keys (never a value, only whether set), its plan variables and their choices, its
    written-in tables (plans, models, sources, the date verified) and, with settings, whether its
    key is set, what it answers with and what its free plan means."""
    out = []
    for p in assistants().values():
        row = {"name": p.name, "title": p.title, "about": p.about, "vendor": p.vendor, "signup": p.signup, "origin": p.origin,
               "version": p.version, "api": p.api, "keys": [key_row(k, settings) for k in p.keys],
               "planChoices": [{"name": v, "choices": list(c)} for v, c in p.plan_choices.items()], "planNote": p.plan_note,
               "scan": p.has_scan, "tables": p.table_rows(), "databases": [database_row(d) for d in plugin_databases(p.name)]}
        if settings is not None:
            own = p.own(settings)
            row.update(keysSet=p.keys_set(settings), serves=p.served(settings), plan=own.plan if own else None,
                       model=own.model if own else None, planVars=p.plan_state(settings), freeNote=p.free_note(settings),
                       caps={"requestsPerDay": own.caps.requests_per_day, "tokensPerDay": own.caps.tokens_per_day,
                             "maxOutputTokens": own.caps.max_output_tokens} if own else None)
        out.append(row)
    return out
