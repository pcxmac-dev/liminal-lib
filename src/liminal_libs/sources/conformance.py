"""The class contract's conformance check (plugin API 2.0): what a plugin must satisfy, by kind, read
from the plugin object alone (no settings, no network, nothing sent). The user, 2026-11-03: "evaluate
each functional piece of software or plugin and verify correctness and valid functionality through
unit tests created to address new class scheme". `problems(plugin)` lists what is wrong;
`assert_conforms(plugin)` and `assert_module_conforms(module)` raise an AssertionError naming it:
every plugin package's tests/test_contract.py calls the latter on its module, and the charts app's
loading test calls the former on every plugin it finds."""
from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from types import ModuleType

from ..freshness import Delay
from ..layout import Database
from .fetching import Fetcher
from .keys import APP, KeyVar
from .plugin import FeedPlugin, Plugin
from .publisher import Feed, Policy, PublisherPlugin
from .stream import Live

NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")  # a plugin's name: its files, its variables' stem
VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]*$")  # a user environment variable
ASSETS = frozenset({"stock", "index", "fx", "crypto", "future"})  # sources.base.RESOLUTIONS_BY_ASSET's
RESOLUTIONS = frozenset({"second", "minute", "day"})
FEED_KINDS = frozenset({"exchange", "consolidated", "official", "vendor", "mirror", "research"})
SYSTEMS = frozenset({"integrator", "platform"})
KINDS = {"feed": FeedPlugin, "publisher": PublisherPlugin}  # assistant: looked up when asked (its package imports the client)
FEED_HOOKS = ("build", "view", "rate", "has_keys", "serves", "markets", "carries", "plan_resolutions", "reporter", "scan",
              "settings")
PUBLISHER_HOOKS = ("moments", "calendar", "read", "scan", "settings")
ASSISTANT_HOOKS = ("build", "scan", "settings")


def problems(p: object) -> list[str]:
    """Every way `p` falls short of the contract, in words; empty when it conforms."""
    out: list[str] = []
    if not isinstance(p, Plugin):
        return [f"not a plugin: {type(p).__name__} is no Plugin subclass's instance"]
    if "CONTRACT" in type(p).__dict__:
        out.append(f"an instance of the contract's own {type(p).__name__}, not of a plugin's class")
    name = p.name
    if not isinstance(name, str) or not NAME.match(name):
        out.append(f"name {name!r}: lowercase letters, digits and dashes, starting with a letter or digit")
    tag = name if isinstance(name, str) and name else type(p).__name__
    if not isinstance(p.title, str) or not p.title.strip():
        out.append(f"{tag}: no title")
    from .registry import api_refusal  # when asked, not on import

    why = api_refusal(p.api, record=p.is_record)
    if why:
        out.append(f"{tag}: api {p.api!r}: {why}")
    if p.SYSTEM not in SYSTEMS:
        out.append(f"{tag}: SYSTEM {p.SYSTEM!r} is none of {sorted(SYSTEMS)}")
    expected = _kind_of(p)
    if expected is None:
        out.append(f"{tag}: neither a FeedPlugin, a PublisherPlugin nor an AssistantPlugin")
    elif p.KIND != expected:
        out.append(f"{tag}: KIND {p.KIND!r} for a {expected} plugin")
    refused = p.refusal()
    if refused:
        out.append(f"{tag}: refused: {refused}")
    out += _variables(tag, p)
    if not isinstance(p.order, int):
        out.append(f"{tag}: order {p.order!r} is not an int")
    if not isinstance(p.version, str):
        out.append(f"{tag}: version {p.version!r} is not a str")
    for attr in ("about", "signup", "plan_note", "account", "part_of", "limits", "origin"):
        if not isinstance(getattr(p, attr), str):
            out.append(f"{tag}: {attr} is not a str")
    if not isinstance(p.databases, tuple) or not all(isinstance(d, Database) for d in p.databases):
        out.append(f"{tag}: databases must be a tuple of layout.Database")
    if p.fetcher is not None and not isinstance(p.fetcher, Fetcher):
        out.append(f"{tag}: fetcher is not a sources.fetching.Fetcher")
    hooks = FEED_HOOKS if expected == "feed" else PUBLISHER_HOOKS if expected == "publisher" else ASSISTANT_HOOKS
    for hook in hooks:
        if p.gives(hook) and not callable(getattr(p, hook, None)):
            out.append(f"{tag}: gives {hook} but it is not callable")
    if p.has_scan != p.gives("scan") or p.has_settings != p.gives("settings"):
        out.append(f"{tag}: has_scan/has_settings disagree with gives()")
    try:
        copy = p.with_(order=p.order)
        if type(copy) is not type(p) or copy.name != p.name or copy.is_record != p.is_record:
            out.append(f"{tag}: with_() does not give a copy of the same kind and name")
    except Exception as e:  # noqa: BLE001 - what with_ raised is the problem
        out.append(f"{tag}: with_() failed: {type(e).__name__}: {e}")
    if expected == "feed":
        out += _feed(tag, p)
    elif expected == "publisher":
        out += _publisher(tag, p)
    elif expected == "assistant":
        out += _assistant(tag, p)
    return out


def _kind_of(p: Plugin) -> str | None:
    if isinstance(p, FeedPlugin):
        return "feed"
    if isinstance(p, PublisherPlugin):
        return "publisher"
    from ..assistants.plugin import AssistantPlugin  # when asked, not on import

    return "assistant" if isinstance(p, AssistantPlugin) else None


def _variables(tag: str, p: Plugin) -> list[str]:
    """Its keys, plan variables and options: user environment variables, each declared once, none
    the app's own."""
    out: list[str] = []
    seen: set[str] = set()
    if not isinstance(p.keys, tuple):
        return [f"{tag}: keys must be a tuple of KeyVar"]
    for k in p.keys:
        if not isinstance(k, KeyVar):
            out.append(f"{tag}: key {k!r} is not a KeyVar")
            continue
        for n in k.names:
            if not VARIABLE.match(n):
                out.append(f"{tag}: key variable {n!r} is not a variable name")
            if n in APP:
                out.append(f"{tag}: key variable {n!r} is the app's own")
            if n in seen:
                out.append(f"{tag}: key variable {n!r} declared twice")
            seen.add(n)
    for attr in ("plan_vars", "options"):
        values = getattr(p, attr)
        if not isinstance(values, tuple):
            out.append(f"{tag}: {attr} must be a tuple")
            continue
        for n in values:
            if not isinstance(n, str) or not VARIABLE.match(n):
                out.append(f"{tag}: {attr} {n!r} is not a variable name")
            elif n in APP:
                out.append(f"{tag}: {attr} {n!r} is the app's own")
            elif n in seen:
                out.append(f"{tag}: {attr} {n!r} declared twice")
            seen.add(n)
    choices = p.plan_choices
    if not isinstance(choices, Mapping):
        out.append(f"{tag}: plan_choices must be a mapping")
    else:
        for var, values in choices.items():
            if var not in p.plan_vars and var not in p.options:
                out.append(f"{tag}: plan_choices for {var!r}, none of its plan_vars or options")
            if not isinstance(values, tuple) or not values or not all(isinstance(v, str) for v in values):
                out.append(f"{tag}: plan_choices[{var!r}] must be a non-empty tuple of str")
    return out


def _feed(tag: str, p: FeedPlugin) -> list[str]:
    out: list[str] = []
    assets = p.assets
    if not isinstance(assets, frozenset) or not assets:
        out.append(f"{tag}: assets must be a non-empty frozenset")
        assets = frozenset()
    elif not assets <= ASSETS:
        out.append(f"{tag}: assets {sorted(assets - ASSETS)} are none of {sorted(ASSETS)}")
    if not isinstance(p.keyless, frozenset) or not p.keyless <= assets:
        out.append(f"{tag}: keyless must be a frozenset within assets")
    if not isinstance(p.reference_for, frozenset) or not p.reference_for <= ASSETS:
        out.append(f"{tag}: reference_for must be a frozenset of asset classes")
    if p.kind not in FEED_KINDS:
        out.append(f"{tag}: kind {p.kind!r} is none of {sorted(FEED_KINDS)}")
    for asset, kind in dict(p.kind_by_asset).items():
        if asset not in ASSETS or kind not in FEED_KINDS:
            out.append(f"{tag}: kind_by_asset {asset!r}: {kind!r}")
    for asset, resolutions in dict(p.provides).items():
        if asset not in assets:
            out.append(f"{tag}: provides {asset!r}, not one of its assets")
        if not isinstance(resolutions, tuple) or not resolutions or not set(resolutions) <= RESOLUTIONS:
            out.append(f"{tag}: provides[{asset!r}] must be a tuple of {sorted(RESOLUTIONS)}")
    for resolution, n in dict(p.per_request).items():
        if resolution not in RESOLUTIONS or not (n is None or (isinstance(n, int) and n > 0)):
            out.append(f"{tag}: per_request {resolution!r}: {n!r}")
    for asset in dict(p.streams):
        if asset not in assets:
            out.append(f"{tag}: streams {asset!r}, not one of its assets")
    if not isinstance(p.live, tuple) or not all(isinstance(x, Live) for x in p.live):
        out.append(f"{tag}: live must be a tuple of sources.stream.Live")
    if not isinstance(p.exchanges, frozenset) or not all(isinstance(x, str) and x.isupper() and 2 <= len(x) <= 4
                                                          for x in p.exchanges):
        out.append(f"{tag}: exchanges must be a frozenset of MICs (or US, the consolidated market)")
    if not isinstance(p.bulk_resolution, (str, Mapping)):
        out.append(f"{tag}: bulk_resolution must be a str or a mapping")
    for attr in ("per_run", "per_day", "symbols_per_month"):
        n = getattr(p, attr)
        if not (n is None or (isinstance(n, int) and n >= 0)):
            out.append(f"{tag}: {attr} {n!r}")
    for attr in ("archival", "partial"):
        if not isinstance(getattr(p, attr), bool):
            out.append(f"{tag}: {attr} is not a bool")
    delays = p.delays
    if not isinstance(delays, Mapping):
        out.append(f"{tag}: delays must map a tier to its delays by asset")
    else:
        for tier, by_asset in delays.items():
            if not isinstance(tier, str) or not isinstance(by_asset, Mapping):
                out.append(f"{tag}: delays[{tier!r}] must map assets to Delay")
                continue
            for asset, delay in by_asset.items():  # an asset class, or a calendar its hours name (overnight)
                if not isinstance(asset, str) or not isinstance(delay, Delay):
                    out.append(f"{tag}: delays[{tier!r}][{asset!r}] is not a Delay by asset or calendar")
    if not isinstance(p.hours, Mapping) or not all(a in ASSETS and isinstance(h, str) for a, h in dict(p.hours).items()):
        out.append(f"{tag}: hours must map assets to a calendar kind")
    if not p.gives("build"):
        out.append(f"{tag}: a feed must give build()")
    return out


def _publisher(tag: str, p: PublisherPlugin) -> list[str]:
    out: list[str] = []
    if not isinstance(p.feeds, tuple) or not all(isinstance(f, Feed) for f in p.feeds):
        out.append(f"{tag}: feeds must be a tuple of publisher.Feed")
    else:
        keys = [f.key for f in p.feeds]
        if len(set(keys)) != len(keys):
            out.append(f"{tag}: a feed key twice: {keys}")
    if not isinstance(p.policy, Policy):
        out.append(f"{tag}: policy must be a publisher.Policy")
    if not isinstance(p.kind, str) or not p.kind:
        out.append(f"{tag}: kind must be a non-empty str")
    if not isinstance(p.datasets, tuple):
        out.append(f"{tag}: datasets must be a tuple")
    else:
        from .dataset import Dataset

        if not all(isinstance(d, (Dataset, str)) for d in p.datasets):
            out.append(f"{tag}: datasets must hold Dataset definitions or names")
        names = [d.name if isinstance(d, Dataset) else d for d in p.datasets]
        if len(set(names)) != len(names):
            out.append(f"{tag}: a dataset twice: {names}")
    for prop in ("has_moments", "has_calendar", "reads_itself"):
        if not isinstance(getattr(p, prop), bool):
            out.append(f"{tag}: {prop} is not a bool")
    return out


def _assistant(tag: str, p) -> list[str]:
    from ..assistants.assistant import Assistant, Caps, Model, Plan, Source

    out: list[str] = []
    if not isinstance(p.vendor, str) or not p.vendor:
        out.append(f"{tag}: no vendor")
    if not (isinstance(p.assistant, type) and issubclass(p.assistant, Assistant)):
        out.append(f"{tag}: assistant must be an Assistant subclass")
    plans, models = p.plans, p.models
    if not isinstance(plans, tuple) or not plans or not all(isinstance(x, Plan) for x in plans):
        out.append(f"{tag}: plans must be a non-empty tuple of Plan")
        plans = ()
    if not isinstance(models, tuple) or not models or not all(isinstance(x, Model) for x in models):
        out.append(f"{tag}: models must be a non-empty tuple of Model")
        models = ()
    plan_names = [x.name for x in plans]
    model_ids = [x.id for x in models]
    if len(set(plan_names)) != len(plan_names):
        out.append(f"{tag}: a plan twice: {plan_names}")
    if len(set(model_ids)) != len(model_ids):
        out.append(f"{tag}: a model twice: {model_ids}")
    for x in plans:
        if not NAME.match(x.name):
            out.append(f"{tag}: plan name {x.name!r} is not a value for its plan variable")
        if not x.title:
            out.append(f"{tag}: plan {x.name!r} has no title")
        if not set(x.models) <= set(model_ids):
            out.append(f"{tag}: plan {x.name!r} unlocks unknown models {sorted(set(x.models) - set(model_ids))}")
        for attr in ("rpm", "tpm", "rpd"):
            n = getattr(x, attr)
            if not (n is None or (isinstance(n, int) and n > 0)):
                out.append(f"{tag}: plan {x.name!r} {attr} {n!r}")
    for x in models:
        if not x.title:
            out.append(f"{tag}: model {x.id!r} has no title")
        if not (x.input_usd >= 0 and x.output_usd >= 0 and (x.cached_usd is None or x.cached_usd >= 0)):
            out.append(f"{tag}: model {x.id!r} has a negative price")
        if not set(x.plans) <= set(plan_names):
            out.append(f"{tag}: model {x.id!r} on unknown plans {sorted(set(x.plans) - set(plan_names))}")
    if p.default_model and p.default_model not in model_ids:
        out.append(f"{tag}: default_model {p.default_model!r} is not in models")
    if not isinstance(p.sources, tuple) or not p.sources or not all(isinstance(x, Source) for x in p.sources):
        out.append(f"{tag}: sources must be a non-empty tuple of Source")
    elif not all(x.url.startswith("https://") and x.holds and x.sentinel for x in p.sources):
        out.append(f"{tag}: every source needs an https url, what it holds and a sentinel")
    try:
        date.fromisoformat(p.verified)
    except (TypeError, ValueError):
        out.append(f"{tag}: verified {p.verified!r} is not an ISO date")
    if not isinstance(p.caps, Caps) or min(p.caps.requests_per_day, p.caps.tokens_per_day, p.caps.max_output_tokens) <= 0:
        out.append(f"{tag}: caps must be a Caps with positive limits")
    if not isinstance(p.efforts, tuple) or not all(isinstance(e, str) for e in p.efforts):
        out.append(f"{tag}: efforts must be a tuple of str")
    if not isinstance(p.key_aliases, tuple) or not all(VARIABLE.match(a) for a in p.key_aliases):
        out.append(f"{tag}: key_aliases must be variable names")
    if not VARIABLE.match(p.key_var) or not VARIABLE.match(p.account_var):
        out.append(f"{tag}: its key and account variables are not variable names")
    return out


def plugins_of(module: ModuleType) -> list[Plugin]:
    """A module's plugins as the registry reads them (PLUGIN(S), PUBLISHER(S), ASSISTANT(S))."""
    from .registry import _plugins_in

    return _plugins_in(module, module.__name__)


def assert_conforms(p: object) -> None:
    found = problems(p)
    assert not found, "\n".join(found)


def assert_module_conforms(module: ModuleType) -> list[Plugin]:
    """Every plugin of a module conforms; returns them (at least one)."""
    found = plugins_of(module)
    assert found, f"{module.__name__} defines no plugin"
    wrong = [w for p in found for w in problems(p)]
    assert not wrong, "\n".join(wrong)
    return found
