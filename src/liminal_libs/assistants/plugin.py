"""The AI assistant kind of plugin (plugin API 2.0): a vendor's API behind one uniform interface, its
plans, models and prices written in and dated, its key and account from the user's environment
variables, and conservative caps on what it may spend a day on this machine.

A plugin is a subclass of `AssistantPlugin` (plugin-platform-claude: `ClaudePlugin`; -grok:
`GrokPlugin`; -codex: `CodexPlugin`) declaring its `vendor`, its `assistant` (an Assistant subclass:
the vendor's headers, body, parsing and error shapes), its tables (`plans`, free first; `models`, the
default first; `sources`: the pages they were written from; `verified`: the date they were checked),
its `caps` and the effort levels its API takes. Everything else is derived: its keys (`<NAME>_API_KEY`,
`<NAME>_ACCOUNT`), its plan variables (`<NAME>_PLAN`, `_MODEL`, `_REQUESTS_PER_DAY`, `_TOKENS_PER_DAY`), its
options (`<NAME>_MAX_OUTPUT_TOKENS`, `<NAME>_EFFORT`), their choices, its settings (AssistantSettings),
its capability scan (Assistant.connect). The user's rule (2026-11-03): "the ai plugins should support
free, and if that means they have no API access, then so be it"; "every time any of the three vendors
changes their plans/rates there will have to be a way to query through the plugin to update itself,
or worst case, just version up every time w/ CI catching the disparity between code and latest rates
through unit testing": Connect compares the tables with the server, the plugin's weekly CI test with
the vendor's pages (verify.py).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Mapping

from ..config import ConfigError
from ..sources.keys import KeyVar
from ..sources.plugin import PlatformPlugin, Scanned
from .assistant import Assistant, AssistantSettings, Caps, Model, Plan, Source
from .evaluation import Usage

if TYPE_CHECKING:
    from ..config import Settings

REQUEST_CHOICES = ("50", "10", "20", "100", "200", "500")
TOKEN_CHOICES = ("200000", "50000", "100000", "500000", "1000000", "5000000")
OUTPUT_CHOICES = ("1024", "256", "512", "2048", "4096", "8192")


class AssistantPlugin(PlatformPlugin):
    """An AI assistant's plugin: see the module's docstring. Its hooks: `settings` (AssistantSettings
    from the variables read), `build` (its Assistant), `scan` (Connect); what the app asks it goes
    through `keys_set`, `served`, `plan_state`, `rate_window`, `cost`, `free_note`."""

    # not a CONTRACT class: its scan (Connect), settings and build are the real hooks every assistant gives
    KIND = "assistant"
    HOOKS = ("build", "scan", "settings")

    vendor: str = ""  # Anthropic, xAI, OpenAI
    assistant: "type[Assistant] | None" = None  # its Assistant subclass
    plans: tuple[Plan, ...] = ()  # the free plan first: the default
    models: tuple[Model, ...] = ()  # the default model first
    default_model: str = ""  # else the first of `models`
    verified: str = ""  # the ISO date the tables were checked against the sources
    sources: tuple[Source, ...] = ()
    caps: Caps = Caps()
    efforts: tuple[str, ...] = ()  # the effort levels its API takes ("" first: the vendor's default); empty: none
    key_aliases: tuple[str, ...] = ()  # other names the key is read under (the vendor's own: ANTHROPIC_API_KEY)
    account_label: str = "account (email or name)"
    account_hint: str = "the email or name the key belongs to: shown with its evaluations, sent to the vendor only as a digest"

    # -- what the record form declared as fields: derived here ------------------------------------

    @property
    def var(self) -> str:
        """The stem of its variables: CLAUDE for claude, GROK, CODEX."""
        return self.name.upper().replace("-", "_")

    @property
    def key_var(self) -> str:
        return f"{self.var}_API_KEY"

    @property
    def account_var(self) -> str:
        return f"{self.var}_ACCOUNT"

    @property
    def plan_var(self) -> str:
        return f"{self.var}_PLAN"

    @property
    def model_var(self) -> str:
        return f"{self.var}_MODEL"

    @property
    def keys(self) -> tuple[KeyVar, ...]:
        return (KeyVar(self.key_var, label=f"{self.vendor} API key", aliases=tuple(self.key_aliases),
                       hint="an API key from the vendor's console"),
                KeyVar(self.account_var, label=self.account_label, secret=False, hint=self.account_hint))

    @property
    def plan_vars(self) -> tuple[str, ...]:
        return (self.plan_var, self.model_var, f"{self.var}_REQUESTS_PER_DAY", f"{self.var}_TOKENS_PER_DAY")

    @property
    def options(self) -> tuple[str, ...]:
        return (f"{self.var}_MAX_OUTPUT_TOKENS", *([f"{self.var}_EFFORT"] if self.efforts else []))

    @property
    def plan_choices(self) -> Mapping[str, tuple[str, ...]]:
        out = {self.plan_var: tuple(p.name for p in self.plans), self.model_var: tuple(m.id for m in self.model_order()),
               f"{self.var}_REQUESTS_PER_DAY": _first(str(self.caps.requests_per_day), REQUEST_CHOICES),
               f"{self.var}_TOKENS_PER_DAY": _first(str(self.caps.tokens_per_day), TOKEN_CHOICES),
               f"{self.var}_MAX_OUTPUT_TOKENS": _first(str(self.caps.max_output_tokens), OUTPUT_CHOICES)}
        if self.efforts:
            out[f"{self.var}_EFFORT"] = tuple(self.efforts)
        return out

    @property
    def plan_note(self) -> str:
        return ("its plan is what you choose; Connect reads the limits its server sends and names the plan they show, "
                f"applied where you set none (the tables verified {self.verified or 'at release'})")

    # -- the tables ----------------------------------------------------------------------------------

    def plan_of(self, name: str) -> Plan | None:
        return next((p for p in self.plans if p.name == name), None)

    def model_of(self, model_id: str) -> Model | None:
        return next((m for m in self.models if m.id == model_id), None)

    def allows(self, plan: Plan, model_id: str) -> bool:
        """Whether a plan reaches a model: the plan's own list, and the model's plans (its table row)."""
        m = self.model_of(model_id)
        return plan.allows(model_id) and (m is None or not m.plans or plan.name in m.plans)

    def reach(self, plan: Plan) -> tuple[str, ...]:
        """The model ids a plan reaches."""
        return tuple(m.id for m in self.models if self.allows(plan, m.id))

    def model_order(self) -> tuple[Model, ...]:
        """The models with the default first."""
        default = self.default_model or (self.models[0].id if self.models else "")
        return tuple(sorted(self.models, key=lambda m: (m.id != default, self.models.index(m))))

    @property
    def free_plan(self) -> Plan | None:
        return self.plans[0] if self.plans else None

    def cost(self, model_id: str, usage: Usage) -> float | None:
        """What an answer cost by the table's prices; None for a model the table has not."""
        m = self.model_of(model_id)
        return m.cost(usage) if m is not None else None

    # -- the hooks -----------------------------------------------------------------------------------

    def settings(self, variables: Mapping[str, str]) -> AssistantSettings:
        """Its own settings from the variables read (Settings.plugin(name)): a ConfigError for a plan
        or a model the tables have not, a cap that is not a number, or a cap above the plan's own."""
        v = {k: str(x).strip() for k, x in variables.items() if k.startswith(self.var + "_") and x is not None}
        plan_name = (v.get(self.plan_var) or (self.plans[0].name if self.plans else "")).lower()
        plan = self.plan_of(plan_name)
        if plan is None:
            raise ConfigError(f"{self.plan_var}: {plan_name!r} is not a plan of {self.title}: one of "
                              f"{', '.join(p.name for p in self.plans)}")
        model = v.get(self.model_var) or self.default_model or (self.models[0].id if self.models else "")
        if self.model_of(model) is None:
            raise ConfigError(f"{self.model_var}: {model!r} is not a model of {self.title}: one of "
                              f"{', '.join(m.id for m in self.models)}")
        if not self.allows(plan, model):
            raise ConfigError(f"{self.model_var}: {model} is not on the {plan.title} plan: one of {', '.join(self.reach(plan))}")
        caps = Caps(_int_cap(v, f"{self.var}_REQUESTS_PER_DAY", self.caps.requests_per_day),
                    _int_cap(v, f"{self.var}_TOKENS_PER_DAY", self.caps.tokens_per_day),
                    _int_cap(v, f"{self.var}_MAX_OUTPUT_TOKENS", self.caps.max_output_tokens))
        if plan.rpd and caps.requests_per_day > plan.rpd:
            raise ConfigError(f"{self.var}_REQUESTS_PER_DAY: {caps.requests_per_day} is above the {plan.title} plan's "
                              f"{plan.rpd} a day")
        effort = v.get(f"{self.var}_EFFORT", "")
        if effort and effort not in self.efforts:
            raise ConfigError(f"{self.var}_EFFORT: {effort!r} is not one of {', '.join(e or 'default' for e in self.efforts)}")
        key, account = self.keys
        return AssistantSettings(self.name, plan.name, model, caps, effort, key.value(variables) or None,
                                 account.value(variables) or "")

    def build(self, settings: "Settings", session=None, now=None) -> Assistant:
        """Its Assistant on these settings (the plugin's own among them: Settings.plugin), with a
        session for the tests."""
        own = settings.plugin(self.name) if hasattr(settings, "plugin") else None
        if own is None:  # settings made directly, or this plugin's failed: from the variables at hand
            own = self.settings(dict(getattr(settings, "variables", None) or getattr(settings, "env", None) or {}))
        if self.assistant is None:
            raise NotImplementedError(f"{self.name}: no assistant class")
        return self.assistant(self, own, session=session, now=now)

    def scan(self, source) -> Scanned:
        """Its capability scan: Connect (Assistant.connect), the source being its Assistant."""
        return source.connect(light=False)

    # -- what the app asks ---------------------------------------------------------------------------

    def run_scan(self, source, settings=None) -> Scanned:
        return self.scan(source)

    def own(self, settings: "Settings") -> AssistantSettings | None:
        own = settings.plugin(self.name) if hasattr(settings, "plugin") else None
        if own is None:
            try:
                own = self.settings(dict(getattr(settings, "variables", None) or getattr(settings, "env", None) or {}))
            except ConfigError:
                return None
        return own

    def keys_set(self, settings: "Settings") -> bool:
        """Whether its key is set (the account is optional)."""
        own = self.own(settings)
        return bool(own and own.has_key)

    def served(self, settings: "Settings") -> bool:
        """Whether it answers here: its key set and its plan with API access."""
        own = self.own(settings)
        plan = self.plan_of(own.plan) if own else None
        return bool(own and own.has_key and plan is not None and plan.api_access)

    def free_note(self, settings: "Settings") -> str:
        """What the free plan means for this vendor, when it is the plan chosen; "" otherwise."""
        own = self.own(settings)
        plan = self.plan_of(own.plan) if own else self.free_plan
        if plan is None or plan.api_access:
            return ""
        return f"the {plan.title} plan has no API access" + (f": {plan.note}" if plan.note else "")

    def plan_state(self, settings: "Settings") -> list[dict]:
        """Each of its plan variables: its value here and where it comes from, its choices (as a feed's)."""
        mine = getattr(settings, "variables", None) or {}
        found = getattr(settings, "detected", None) or {}
        out = []
        for v in self.plan_vars:
            choices = list(self.plan_choices.get(v, ()))
            out.append({"name": v, "value": mine.get(v) or found.get(v) or (choices[0] if choices else None),
                        "origin": "you" if mine.get(v) else "scan" if found.get(v) else "default", "choices": choices})
        return out

    def rate_window(self, settings: "Settings") -> tuple[int | None, float]:
        """The window its limiter keeps: its plan's requests a minute (None: none published), a minute."""
        own = self.own(settings)
        plan = self.plan_of(own.plan) if own else self.free_plan
        return (plan.rpm if plan is not None and plan.rpm else None), 60.0

    def table_rows(self) -> dict:
        """The tables for the Plugins view and `plugins --json`: plans, models, sources, the date verified."""
        return {"vendor": self.vendor, "verified": self.verified,
                "plans": [{"name": p.name, "title": p.title, "apiAccess": p.api_access, "rpm": p.rpm, "tpm": p.tpm, "rpd": p.rpd,
                           "price": p.price, "models": list(p.models), "spendCap": p.spend_cap, "note": p.note} for p in self.plans],
                "models": [{"id": m.id, "title": m.title, "inputUsd": m.input_usd, "outputUsd": m.output_usd, "cachedUsd": m.cached_usd,
                            "context": m.context, "plans": list(m.plans), "reasoning": m.reasoning, "note": m.note}
                           for m in self.model_order()],
                "sources": [{"url": s.url, "holds": s.holds} for s in self.sources],
                "caps": {"requestsPerDay": self.caps.requests_per_day, "tokensPerDay": self.caps.tokens_per_day,
                         "maxOutputTokens": self.caps.max_output_tokens}, "efforts": list(self.efforts)}


def _first(value: str, choices: tuple[str, ...]) -> tuple[str, ...]:
    """The choices with `value` first (added when it is not among them)."""
    return (value, *(c for c in choices if c != value))


def _int_cap(v: Mapping[str, str], name: str, default: int) -> int:
    raw = v.get(name)
    if raw in (None, ""):
        return default
    try:
        out = int(str(raw).replace(",", "").replace("_", ""))
    except ValueError:
        raise ConfigError(f"{name}: {raw!r} is not a whole number") from None
    if out < 1:
        raise ConfigError(f"{name}: {out} is below 1")
    return out
