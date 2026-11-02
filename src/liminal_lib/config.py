"""Settings and plan tiers.

Settings come from environment variables, optionally loaded from a `.env` file
(variables already set in the environment win):

    LIMINAL_DB        path of the SQLite database (default: ~/LiminalData/liminal.db)
    (A feed's key, plans and caps are its plugin's own settings, read with these (Provider.settings,
    plugin API 1.11): Massive's MASSIVE_API_KEY, MASSIVE_TIER and the other classes' plans, MASSIVE_RPM,
    MASSIVE_BASE_URL and MASSIVE_MAX_REQUESTS_PER_RUN / _DAY are liminal-plugin-massive's, in its README.)
    LIMINAL_REFRESH_HOURS  extra minimum hours between refreshes of a backfilled symbol (default: 0;
                           symbols are already only refreshed when their plan should have new data)
    SOURCE_PRIORITY   feed order when sources overlap (default: massive,alpaca); a span is downloaded by
                      one feed only, ranked by resolution, then audited accuracy, then this order
    AUDITS_PER_RUN    spot audits of another feed's newly downloaded data per feed run (default: 2)
    AUDITS_RANDOM_PER_RUN   spot audits of a random stored session per feed run (default: 1); each
                      compares one random hour, and a run's audits use at most 2 % of its requests
    SECOND_HISTORY_DAYS how far back 1-second bars are backfilled where a feed has them (default:
                      365); minute bars cover all the history (fetched first), back to the plan's start
    BACKUPS_KEEP      `admin backup` keeps the newest this many of its backups (default: 7; 0 keeps all)
    TICK_HISTORY_DAYS (no longer used for history: no ticks are downloaded, only the live streams'
                      trades are kept, to build their bars)
    TICK_MIN_FREE_GB  tick backfill pauses when the disk has less free space (default: 250); below
                      a fifth of it no ticks are downloaded at all
    FUTURES_PER_RUN   futures requests a sync may make per run (default: 100, about 20 minutes at the
                      free class's 5 a minute): futures go first in a run and the stocks take the rest
    CHART_CACHE_MB    memory the chart app's caches may hold together (default: 1024): recent
                      windows, bricks and 30-minute bins; the databases stay on disk
    BARS_ONLY         on (default): the app works from bars alone and no command opens the feeds'
                      tick files (the chart app, the sync, audits and checks never read them), except
                      the live recorder, which records the streams' trades and builds their bars;
                      off: commands that read ticks (exports, volume profiles from trades) may

Public historical sources (closed feeds: they fill history nobody else holds, then are not asked
again): Bitstamp, Coinbase and Bitfinex (crypto bars) and Cboe (index daily history), each its own
package (liminal-plugin-<name>: pip install -e ".[plugins]"), need nothing; Tiingo (US stock daily
bars from 1962, its forex; liminal-plugin-tiingo) a free account key, TIINGO_API_KEY (environment
only; each plugin's keys: `python -m liminal plugins`);
    CRYPTO_VENUE_DAYS limit the crypto exchange feeds (Coinbase, Bitfinex) to this many days of history
                      (default: 0, everything they hold: Coinbase from 2015, Bitfinex from 2013)

Government and regulators (gov.py; the Gov view): the Fed, BLS, the Federal Register and the New York
Fed need nothing. GOV_CONTACT (your name and email) is sent to the agencies that ask automated
readers to identify themselves (the SEC): their feeds wait until it is set.

Alpaca (second feed; stocks and crypto) is the liminal-plugin-alpaca package: its keys
(ALPACA_API_KEY_ID, ALPACA_API_SECRET_KEY), plan, feed, crypto venue, paper or live host and caps are
its own settings (Provider.settings, plugin API 1.11: Settings.plugin("alpaca")), documented in its
README; keys are only ever read from the environment. The live streams:
    LIVE_STOCKS       on (default) | off: with keys, the live recorder streams IEX's trades for the
                      symbols open on a chart (at most 30; live.py)
    LIVE_STREAMS      selected (default) | always: selected, every live stream (coins and stocks)
                      runs only for the symbols open on a chart, and with none open nothing is
                      connected (the sync downloads the time later); always, the coins of LIVE_CRYPTO
                      stream all the time, and stocks open on a chart or on the rail's watchlists
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Mapping, MutableMapping

# Stock data lives on this machine, outside the repository, so it never ends up in git.
DEFAULT_DB_PATH = Path.home() / "LiminalData" / "liminal.db"
# The app's settings by the names they had before the project was renamed (massive -> liminal,
# 2026-11-01): still read where the new name is not set. The Massive provider's keep their names.
RENAMED = {f"LIMINAL_{n}": f"MASSIVE_{n}"
           for n in ("DB", "OFFLINE", "READONLY", "REFRESH_HOURS", "LIVE_LOCK", "MONGO_PORT", "DATA_REPOS")}


def renamed(mapping: Mapping[str, str], name: str) -> str | None:
    """A setting's value from `mapping` by its name, or by the one it had before the rename (RENAMED)."""
    return mapping.get(name) or mapping.get(RENAMED.get(name, ""))


def setting(name: str) -> str | None:
    """An app setting read from the process's environment alone, not through Settings (the live
    lock, the data repositories, MongoDB's port): by its name, or the one it had before the rename."""
    return renamed(os.environ, name)


@dataclass(frozen=True)
class Tier:
    name: str
    requests_per_minute: int | None  # None = unlimited
    history_years: int | None  # None = no plan limit
    history_start: date | None = None  # the source holds nothing older (Alpaca: 2016)
    history_days: int | None = None  # collect only this far back (a case-study venue feed)
    # Plans of other asset classes on the same source ((asset, Tier), ...): Massive sells one per class.
    by_asset: tuple = ()


# Backfill depth used when a plan has no history limit and its source no known start.
UNLIMITED_BACKFILL_YEARS = 20
# The settings' plan where no feed's view has set one (Settings.for_source): no plan at all. Each
# feed's plans are its plugin's (Massive's TIERS in liminal-plugin-massive).
NO_PLAN = Tier("none", requests_per_minute=None, history_years=None)


def asset_tier(tier: Tier, asset: str | None) -> Tier:
    """The plan for one asset class: the tier's own, unless it carries another for that class."""
    for a, t in tier.by_asset if asset else ():
        if a == asset:
            return t
    return tier


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    db_path: Path
    # This feed's plan and request caps, as its plugin's view sets them (for_source); the base has no
    # feed's plan. A feed's key is its plugin's own setting (Settings.plugin), never here.
    tier: Tier = NO_PLAN
    max_requests_per_run: int = 250
    max_requests_per_day: int = 2000
    refresh_hours: float = 0.0
    # Feed order when several sources cover the same ticker (SOURCE_PRIORITY, comma-separated).
    source_priority: tuple[str, ...] = ("massive", "alpaca")

    audits_per_run: int = 2
    audits_random_per_run: int = 1
    tick_history_days: int = 365
    second_history_days: int = 365
    backups_keep: int = 7
    tick_min_free_gb: int = 250
    gov_contact: str | None = field(default=None, repr=False)  # GOV_CONTACT: sent to agencies that ask (gov.py)
    crypto_venue_days: int = 0  # 0: all the history the exchange feeds hold
    # Coins recorded live from the exchanges' public streams (live.py); LIVE_CRYPTO=off for none.
    live_coins: tuple[str, ...] = ("BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "LINK")
    # Stocks streamed live from Alpaca's IEX feed (with its keys; live.py); LIVE_STOCKS=off for none.
    live_stocks: bool = True
    # LIVE_STREAMS=selected (default): live streams only for the symbols open on a chart (live.py).
    live_selected_only: bool = True
    # BARS_ONLY (on by default): no tick file is opened but by the live recorder (cli.main).
    bars_only: bool = True
    # CHART_CACHE_MB: every cache of the chart app together (gui/bars.py CACHE_SHARES).
    chart_cache_mb: int = 1024
    # FUTURES_PER_RUN: the futures' share of a run (collector.FUTURES_PER_RUN).
    futures_per_run: int = 100
    # LIMINAL_OFFLINE=1: nothing is fetched and no recorder starts (a test copy of the app on a
    # scratch database must never spend the real plan's requests: its own ledger would not know them).
    offline: bool = False
    # LIMINAL_READONLY=1: the data is read, never written (every database connection query-only),
    # and nothing fetched or started (offline too). The user, 2026-10-31: "lets not have testing
    # data, lets just safely query the plugins": a test copy of the app on the real data folder.
    read_only: bool = False
    # The variables the provider plugins declare (their keys, plans and options), as read: a
    # plugin's own settings (sources/registry.py). Keys among them: never in a repr.
    env: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)
    # The plugins' own settings (Provider.settings, plugin API 1.11), by plugin name, built when these loaded.
    plugins: Mapping[str, object] = field(default_factory=dict, compare=False, repr=False)
    # Every variable they were read from (None: made directly, not read), and the plan variables
    # the provider plugins' scans found and these settings apply (with_detected).
    variables: "Mapping[str, str] | None" = field(default=None, compare=False, repr=False)
    detected: Mapping[str, str] = field(default_factory=dict, compare=False, repr=False)

    # The settings a feed view was made from, so any view can switch to another feed.
    origin: "Settings | None" = field(default=None, compare=False, repr=False)

    def for_source(self, name: str) -> "Settings":
        """These settings as seen by one feed: its plan, rate and request caps (its plugin's view)."""
        from .sources.registry import get

        base = self.origin or self
        p = get(name)
        return p.settings_view(base) if p else base

    def plugin(self, name: str):
        """A plugin's own settings (Provider.settings, plugin API 1.11), built when these loaded; None
        when it has none, or these were made directly (no variables read)."""
        return (self.origin or self).plugins.get(name)


def read_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Blank lines and # comments are ignored, also one after
    a value (`MASSIVE_TIER=starter  # my plan`: a '#' after a space or tab); a quoted value is
    taken whole, '#' included."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ("'", '"') and value.find(value[0], 1) > 0:
            value = value[1:value.find(value[0], 1)]  # what follows the closing quote is a comment
        else:
            for mark in (" #", "\t#"):
                value = value.split(mark, 1)[0]
            value = value.strip()
        values[key.strip()] = value
    return values


# API keys set as user environment variables after a process started (the chart app, the runs it
# starts) are not in its environment; on Windows they are read from the user's saved variables:
# every variable a plugin declares (sources/registry.py: its keys, plans and options; a publisher's
# keys, the SEC's GOV_CONTACT among them).
def saved_names() -> tuple[str, ...]:
    """The variables read from the user's saved ones: every plugin's (registry.variables)."""
    from .sources.registry import variables

    return variables()


def saved_user_keys() -> dict[str, str]:
    """The API keys and plan names among the user's saved environment variables (the Windows
    registry's user Environment key): a process started before they were saved still sees them."""
    if os.name != "nt":
        return {}
    try:
        import winreg

        out = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name in saved_names():
                try:
                    value, _ = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    continue
                if value:
                    out[name] = str(value)
        return out
    except OSError:
        return {}


def save_user_variable(name: str, value: str | None, registry=None) -> None:
    """Save one of the user's environment variables (None: remove it) where tools/set-key.ps1 saves
    them, the Windows registry's user Environment key, and in this process's environment: processes
    started from now on read it, and this one at once (its own environment wins over the saved
    variables in load_settings). `registry`: winreg, or a stand-in in tests. Windows only."""
    real = registry is None
    if real:
        if os.name != "nt":
            raise ConfigError("saving a setting needs Windows' user environment variables: set it in .env")
        import winreg as registry
    with registry.OpenKey(registry.HKEY_CURRENT_USER, "Environment", 0, registry.KEY_SET_VALUE) as key:
        if value is None:
            try:
                registry.DeleteValue(key, name)
            except FileNotFoundError:
                pass
        else:
            registry.SetValueEx(key, name, 0, registry.REG_SZ, value)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    if real:
        _announce_environment()


def _announce_environment() -> None:
    """Tell Windows the user's environment changed (as [Environment]::SetEnvironmentVariable does),
    so programs started from Explorer see it; best effort."""
    try:
        import ctypes
        from ctypes import wintypes

        result = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, "Environment", 0x0002, 2000, ctypes.byref(result))
    except Exception:  # noqa: BLE001 - the variable is saved either way
        pass


def load_settings(
    env: Mapping[str, str] | None = None,
    env_file: Path | None = Path(".env"),
) -> Settings:
    merged: MutableMapping[str, str] = {}
    if env_file is not None:
        merged.update(read_env_file(env_file))
    if env is None:
        merged.update(saved_user_keys())  # the process's own environment still wins
    merged.update(os.environ if env is None else env)
    for name in RENAMED:  # the app's settings under their old names
        if name not in merged and (old := renamed(merged, name)) is not None:
            merged[name] = old
    from .sources.registry import leave_out, providers  # when called, not on import: the registry imports the plugins

    plugins = {}  # the plugins' own settings (API 1.11), by feed
    for name, p in list(providers().items()):
        if p.settings is None:
            continue
        try:
            plugins[name] = p.settings(merged)
        except ConfigError:
            raise  # a value of yours it refuses: stops the app, as any setting's does
        except Exception as e:  # noqa: BLE001 - the plugin's own failure: left out and reported, never fatal
            leave_out(name, f"its settings failed ({type(e).__name__}: {e})")

    refresh = _float_setting(merged, "LIMINAL_REFRESH_HOURS")  # hours, fractions too (0.5)
    from .sources.registry import variables

    return Settings(
        db_path=Path(merged["LIMINAL_DB"]).expanduser() if merged.get("LIMINAL_DB") else DEFAULT_DB_PATH,
        refresh_hours=float(refresh or 0),
        source_priority=tuple(
            s.strip().lower() for s in (merged.get("SOURCE_PRIORITY") or "massive,alpaca").split(",") if s.strip()
        ),
        plugins=plugins,
        audits_per_run=_default(_int_setting(merged, "AUDITS_PER_RUN"), 2),
        audits_random_per_run=_default(_int_setting(merged, "AUDITS_RANDOM_PER_RUN"), 1),
        tick_history_days=_default(_int_setting(merged, "TICK_HISTORY_DAYS"), 365),
        second_history_days=_default(_int_setting(merged, "SECOND_HISTORY_DAYS"), 365),
        backups_keep=_default(_int_setting(merged, "BACKUPS_KEEP"), 7),
        tick_min_free_gb=_default(_int_setting(merged, "TICK_MIN_FREE_GB"), 250),
        gov_contact=(merged.get("GOV_CONTACT") or "").strip() or None,
        crypto_venue_days=_default(_int_setting(merged, "CRYPTO_VENUE_DAYS"), 0),
        live_coins=_live_coins(merged.get("LIVE_CRYPTO")),
        live_stocks=str(merged.get("LIVE_STOCKS", "on")).strip().lower() not in ("off", "0", "false", "no", "none"),
        live_selected_only=_live_streams(merged.get("LIVE_STREAMS")),
        offline=any(str(merged.get(n, "")).strip().lower() in ("1", "true", "yes", "on")
                    for n in ("LIMINAL_OFFLINE", "LIMINAL_READONLY")),
        read_only=str(merged.get("LIMINAL_READONLY", "")).strip().lower() in ("1", "true", "yes", "on"),
        bars_only=str(merged.get("BARS_ONLY", "on")).strip().lower() not in ("off", "0", "false", "no", "none"),
        chart_cache_mb=max(64, _default(_int_setting(merged, "CHART_CACHE_MB"), 1024)),
        futures_per_run=_default(_int_setting(merged, "FUTURES_PER_RUN"), 100),
        env={n: merged[n] for n in variables() if merged.get(n)},
        variables=dict(merged),
    )


def with_detected(settings: Settings, plans: Mapping[str, str]) -> Settings:
    """The settings with the plan variables the provider plugins' scans found (registry.Scanned),
    each where the user set none: the user's own setting always wins. Settings made directly (no
    `variables`) are kept as they are. A plan value no plugin knows is not applied."""
    if settings.variables is None:
        return settings
    base = settings.variables
    new = {k: str(v) for k, v in plans.items() if v and not base.get(k)}
    if new == dict(settings.detected):
        return settings
    try:
        out = load_settings(env={**base, **new}, env_file=None)
    except ConfigError:
        return settings  # a value its settings refuse: the plan stays as the user's settings have it
    return replace(out, variables=base, detected=new)


def _live_coins(value: str | None) -> tuple[str, ...]:
    """LIVE_CRYPTO: coins every live exchange stream lists (LIVE_COINS); another would be refused
    by Coinbase (taking its whole stream down) and entered as covered by Bitstamp."""
    from .live import LIVE_COINS

    if value is None or not value.strip():
        return LIVE_COINS
    if value.strip().lower() in ("off", "none", "0", "false"):
        return ()
    coins = tuple(c.strip().upper() for c in value.split(",") if c.strip())
    unknown = [c for c in coins if c not in LIVE_COINS]
    if unknown:
        raise ConfigError(f"LIVE_CRYPTO: {', '.join(unknown)} not streamed live; choose from {', '.join(LIVE_COINS)}")
    return coins


def _live_streams(value: str | None) -> bool:
    """LIVE_STREAMS: whether the live streams run only for the symbols open on a chart."""
    v = (value or "selected").strip().lower()
    if v not in ("selected", "always"):
        raise ConfigError(f"LIVE_STREAMS must be selected or always, got {value!r}")
    return v == "selected"


def _default(value: int | None, default: int) -> int:
    return default if value is None else value


def _int_setting(values: Mapping[str, str], name: str) -> int | None:
    raw = values.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from e
    if value < 0:
        raise ConfigError(f"{name} must not be negative")
    return value


def _float_setting(values: Mapping[str, str], name: str) -> float | None:
    raw = values.get(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e
    if not value >= 0:  # negative, or not a number at all (nan)
        raise ConfigError(f"{name} must not be negative")
    return value
