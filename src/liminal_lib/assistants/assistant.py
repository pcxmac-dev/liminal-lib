"""An AI assistant's API through the contract's client (plugin API 2.0, assistants): what every AI
plugin's assistant is, and the vendor hooks a plugin fills.

`Assistant` sends an argument's prompt to one vendor model and reads the answer back as an Evaluation,
inside the plugin's caps (requests and tokens a day on this machine, every database together: the
machine ledgers) and its plan's rate window; `connect` is its capability scan (the key tried on the
models list; the server's limits read, the plan they show, the written-in tables checked against
them). A vendor's plugin subclasses it with its headers, its request body, its answer's parsing, its
error shapes and its rate-limit headers; `ResponsesAssistant` is the OpenAI-shaped Responses API
(OpenAI's own, xAI's). The vendor's prices, plans and models are the plugin's tables (plugin.py:
Plan, Model, Source), dated and verified against the vendor's public pages.

Nothing is sent from an offline copy (assistants.evaluate refuses first). Every error message is
scrubbed of the key and the account before it is logged or shown.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Mapping

from ..errors import BudgetExhausted, NotEntitled, RateLimited, SourceError, Unauthorized
from ..ratelimit import MachineLedger, RateLimiter, fingerprint
from ..sources.http import HttpClient
from ..sources.plugin import Scanned
from .arguments import Argument, Prompt
from .evaluation import Evaluation, Usage, new_evaluation_id, now_iso

if TYPE_CHECKING:
    from .plugin import AssistantPlugin


class Offline(RuntimeError):
    """Nothing is sent from an offline copy (LIMINAL_OFFLINE, LIMINAL_READONLY)."""


# -- what a plugin writes in: its vendor's plans, models and where they are published -------------

@dataclass(frozen=True)
class Plan:
    """A vendor's plan (a usage tier, a subscription): what it allows, as the vendor publishes it."""
    name: str  # the value of the plugin's <NAME>_PLAN variable
    title: str
    api_access: bool = True  # False: the plan reaches no API (a chat subscription, a free account without credits)
    rpm: int | None = None  # requests a minute the vendor publishes for it (the default model's class); None: not published
    tpm: int | None = None  # tokens a minute
    rpd: int | None = None  # requests a day, where a vendor caps those
    price: str = ""  # how it is paid, in a line (prepaid credits, $5 bought, a monthly fee)
    models: tuple[str, ...] = ()  # the model ids it unlocks; empty: every model in the table
    spend_cap: str = ""  # its monthly spend cap in words, where the vendor publishes one
    note: str = ""  # what to know about it (a free plan: how to get API access at all)

    def allows(self, model_id: str) -> bool:
        return not self.models or model_id in self.models


@dataclass(frozen=True)
class Model:
    """A vendor's model: its id on the API and its prices per million tokens, as published."""
    id: str
    title: str
    input_usd: float
    output_usd: float
    cached_usd: float | None = None  # a cached input token's price; None: the vendor publishes none
    context: int | None = None  # its context window, tokens
    plans: tuple[str, ...] = ()  # the plans it is on; empty: every plan with API access
    reasoning: bool = False  # it thinks before answering (its reasoning tokens are billed as output)
    note: str = ""

    def cost(self, usage: Usage) -> float:
        """What an answer cost by these prices, USD (cached input tokens at their price where there is one)."""
        cached = usage.cached_tokens if self.cached_usd is not None else 0
        uncached = max(usage.input_tokens - cached, 0)
        return round((uncached * self.input_usd + cached * (self.cached_usd or 0.0) + usage.output_tokens * self.output_usd) / 1e6, 6)


@dataclass(frozen=True)
class Source:
    """A vendor's public page the tables were written from: what it holds, a literal it must still
    show (its heading or a sentence), and literals from the tables (a price, a limit) it must still
    hold. The plugin's weekly CI test reads it (verify.check_sources): gone, the table is stale."""
    url: str
    holds: str
    sentinel: str
    markers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Caps:
    """What the plugin spends at most on this machine: requests and tokens a day (every database
    together: the machine ledgers), output tokens an answer."""
    requests_per_day: int = 50
    tokens_per_day: int = 200_000
    max_output_tokens: int = 1024


@dataclass(frozen=True)
class AssistantSettings:
    """A plugin's own settings (Settings.plugin(name)): its plan, its model, its caps, its effort;
    the key and the account (shown nowhere: not in a repr, not in a log)."""
    name: str
    plan: str
    model: str
    caps: Caps = field(default_factory=Caps)
    effort: str = ""  # the vendor's effort or reasoning level, where its API takes one; "" for the vendor's default
    key: str | None = field(default=None, repr=False, compare=False)
    account: str = field(default="", repr=False, compare=False)  # the email or account name the key belongs to

    @property
    def has_key(self) -> bool:
        return bool(self.key)

    @property
    def user_id(self) -> str:
        """What the vendor is told about whose requests these are: a digest of the account, never
        the email itself (the vendors' user-id fields take no identifying information)."""
        return fingerprint(self.account) if self.account else ""


# -- the client and the answer ---------------------------------------------------------------------

def header(headers: Mapping[str, Any], name: str) -> str | None:
    """A response header by name, however the server cased it."""
    low = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == low:
            return str(v)
    return None


def _json_or_none(resp):
    try:
        body = resp.json()
    except (ValueError, AttributeError):
        return None
    return body


class AssistantClient(HttpClient):
    """One vendor host (plugin API 2.0): the key in its headers, POST through the contract's client
    (sources/http.py: no transport retry on a POST, a timed-out answer may have been billed; 429 and
    5xx tried again), every error scrubbed of the key and the account, the vendor's rate-limit
    headers kept (limits_seen), its error mapping and what is final from its Assistant."""

    def __init__(self, base_url: str, headers: dict, secrets: tuple[str, ...], assistant: "Assistant",
                 rate_limiter: RateLimiter | None = None, request_budget: int | None = None, session=None,
                 sleep: Callable[[float], None] = time.sleep):
        super().__init__(base_url, headers, rate_limiter=rate_limiter, session=session, request_budget=request_budget,
                         sleep=sleep)
        self._secrets = tuple(s for s in secrets if s)
        self._assistant = assistant
        self.limit_prefixes = tuple(assistant.limit_prefixes)

    def scrub(self, text: str) -> str:
        out = str(text)
        for s in self._secrets:
            out = out.replace(s, "…")
        return out

    def _refusal(self, resp, message: str):
        err = self._assistant.error(resp.status_code, _json_or_none(resp), message)
        return err

    def _final(self, resp) -> bool:
        return self._assistant.final(resp.status_code, _json_or_none(resp))

    def _request(self, path_or_url: str, params, *, method: str = "GET", json=None):
        try:
            return super()._request(path_or_url, params, method=method, json=json)
        except RateLimited as e:
            raise RateLimited(e.status, self.scrub(e.message), e.retry_after) from None
        except SourceError as e:
            raise type(e)(e.status, self.scrub(e.message)) from None

    def post_json(self, path: str, body: dict) -> tuple[dict, dict]:
        """A POST's JSON answer and its headers (the vendor's request id and limits among them)."""
        resp = self._request(path, None, method="POST", json=body)
        try:
            data = resp.json()
        except ValueError as e:
            raise SourceError(resp.status_code, f"not JSON: {e}") from e
        return data, dict(getattr(resp, "headers", None) or {})


@dataclass
class Parsed:
    """What a vendor's answer holds, read by its Assistant."""
    text: str
    usage: Usage
    stop: str = "end_turn"  # end_turn | length | refusal
    request_id: str = ""
    model: str = ""
    cost_usd: float | None = None  # the vendor's own figure, where it sends one (xAI does)
    raw: Any = None


class Assistant:
    """A vendor's API through the contract's client: `models()` (the server's list), `evaluate(argument,
    model=None)` (one answer, an Evaluation) and `connect(light=False)` (the capability scan). A
    vendor's plugin gives the hooks: `headers`, `body`, `parse`, `parse_models`, `error`, `final`,
    `limits_from`; and the paths and header names below."""

    base_url: str = ""
    models_path: str = "/v1/models"
    answers_path: str = ""  # where an argument's prompt is posted
    request_id_header: str = "x-request-id"
    limit_prefixes: tuple[str, ...] = ()  # the vendor's rate-limit headers, by prefix (kept in limits_seen)
    probe_tokens: int = 16  # what a full connect's answer may be, tokens (the smallest the vendors take)

    def __init__(self, plugin: "AssistantPlugin", settings: AssistantSettings, session=None,
                 now: Callable[[], datetime] | None = None, sleep: Callable[[float], None] = time.sleep):
        self.plugin, self.settings = plugin, settings
        self.name = plugin.name
        self.now = now or (lambda: datetime.now(timezone.utc))
        key = settings.key or ""
        # the machine-wide ledgers: requests and tokens today under the plugin's name and its key's digest
        self.requests = MachineLedger(self.name, key or None)
        self.tokens = MachineLedger(f"{self.name}:tokens", key or None)
        limiter = RateLimiter(*self.window())
        limiter.share(self.requests)
        self.client = AssistantClient(self.base_url, self.headers(key) if key else {}, (key, settings.account), self,
                                      rate_limiter=limiter, session=session, sleep=sleep)
        self.client.take_day = lambda: self.requests.take_day(settings.caps.requests_per_day, self.now())

    # -- the hooks: a vendor's plugin fills them ---------------------------------------------------

    def headers(self, key: str) -> dict:
        """The request headers with the key in them."""
        raise NotImplementedError(f"{self.name}: no headers")

    def body(self, prompt: Prompt, model: str, user_id: str) -> dict:
        """The request body for a prompt (the system line, the user text, the output cap, JSON wanted
        or not), the model and the hashed user id ("" when no account is set: left out)."""
        raise NotImplementedError(f"{self.name}: no body")

    def parse(self, raw: Mapping[str, Any]) -> Parsed:
        """What the answer holds: its text, its usage (input tokens with the cached ones among them,
        output tokens with the reasoning ones), how it ended, its ids."""
        raise NotImplementedError(f"{self.name}: no parse")

    def parse_models(self, raw: Mapping[str, Any]) -> list[dict]:
        """The server's model list: [{id, title}] (the OpenAI-shaped `data[]` by default)."""
        return [{"id": str(m.get("id", "")), "title": str(m.get("display_name") or m.get("id", ""))}
                for m in (raw or {}).get("data", []) if isinstance(m, Mapping) and m.get("id")]

    def error(self, status: int, body, message: str) -> SourceError | None:
        """The error a refused answer is, by the vendor's shape; None for the client's default (401
        Unauthorized, 403 NotEntitled, 429 RateLimited, else SourceError)."""
        return None

    def final(self, status: int, body) -> bool:
        """Whether a 429 is final (a quota, a spend cap: trying again changes nothing) rather than a
        rate limit to wait out."""
        return False

    def limits_from(self, headers: Mapping[str, Any]) -> tuple[int | None, int | None]:
        """(requests a minute, tokens a minute) the server's rate-limit headers give; None where it sends none."""
        return None, None

    # -- what every assistant does ------------------------------------------------------------------

    def window(self) -> tuple[int | None, float]:
        """The rate window kept: the plan's requests a minute (None: no limit published), a minute."""
        plan = self.plugin.plan_of(self.settings.plan)
        return (plan.rpm if plan is not None and plan.rpm else None), 60.0

    def refusal(self) -> str | None:
        """Why nothing can be sent: no key, an unknown plan, a plan without API access, today's token cap."""
        s = self.settings
        if not s.has_key:
            return f"{self.name}: no key ({self.plugin.key_names[0]} is not set)"
        plan = self.plugin.plan_of(s.plan)
        if plan is None:
            return f"{self.name}: unknown plan {s.plan!r}"
        if not plan.api_access:
            return (f"{self.name}: the {plan.title} plan has no API access" + (f" ({plan.note})" if plan.note else "")
                    + ": choose the plan your key is on")
        used = self.tokens.today(self.now())
        if used >= s.caps.tokens_per_day:
            return f"{self.name}: today's token cap is reached on this machine ({used:,} of {s.caps.tokens_per_day:,})"
        return None

    def _refuse(self) -> None:
        why = self.refusal()
        if why is None:
            return
        if not self.settings.has_key:
            raise Unauthorized(0, why)
        if "token cap" in why:
            raise BudgetExhausted(why)
        raise NotEntitled(0, why)

    def models(self) -> list[dict]:
        """The server's model list (one request; the key must be set)."""
        if not self.settings.has_key:
            raise Unauthorized(0, f"{self.name}: no key ({self.plugin.key_names[0]} is not set)")
        return self.parse_models(self.client.get(self.models_path))

    def answer(self, prompt: Prompt, model: str) -> tuple[Parsed, dict, float]:
        """One answer to a prompt from a model: (what it holds, the response headers, seconds)."""
        started = time.monotonic()
        raw, headers = self.client.post_json(self.answers_path, self.body(prompt, model, self.settings.user_id))
        seconds = round(time.monotonic() - started, 3)
        parsed = self.parse(raw)
        self.tokens.charge_day(parsed.usage.total, self.now())
        if not parsed.request_id:
            parsed.request_id = header(headers, self.request_id_header) or ""
        return parsed, headers, seconds

    def evaluate(self, argument: Argument, model: str | None = None) -> Evaluation:
        """One answer to an argument from the plugin's model (or `model`): an Evaluation, with what it
        cost (the vendor's figure where it sends one, else the table's prices)."""
        self._refuse()
        model = model or self.settings.model
        plan = self.plugin.plan_of(self.settings.plan)
        if plan is not None and not self.plugin.allows(plan, model):
            raise NotEntitled(0, f"{self.name}: the {plan.title} plan does not reach {model}: choose one of "
                                 f"{', '.join(self.plugin.reach(plan))}")
        at = now_iso(self.now())
        parsed, headers, seconds = self.answer(argument.prompt(self.settings.caps.max_output_tokens), model)
        verdict, fields = argument.parse(parsed.text) if parsed.stop in ("end_turn", "length") else (None, {})
        cost = parsed.cost_usd if parsed.cost_usd is not None else self.plugin.cost(parsed.model or model, parsed.usage)
        return Evaluation(new_evaluation_id(), argument.id, argument.kind, self.name, self.plugin.vendor, parsed.model or model,
                          parsed.text, verdict, fields, argument.inputs(), argument.context_ids(), parsed.usage, cost, at,
                          seconds, parsed.request_id, parsed.stop)

    def connect(self, light: bool = False) -> Scanned:
        """The capability scan (Connect, `capabilities`): the key tried on the models list (the default
        model listed?), and unless `light`, one answer of a few tokens for the limits the server sends:
        the plan they show (Scanned.plan, applied where you set none) and the written-in tables checked
        against them and against the models listed (a disagreement names the stale table)."""
        s = self.settings
        rows: list[tuple[str, bool, str]] = []
        plan_found: dict[str, str] = {}
        if not s.has_key:
            raise Unauthorized(0, f"{self.name}: no key ({self.plugin.key_names[0]} is not set)")
        try:
            listed = self.models()
        except SourceError as e:
            rows.append(("the key", False, f"refused: {e.message}"))
            return Scanned(tuple(rows))
        ids = {m["id"] for m in listed}
        rows.append(("the key", True, f"accepted: {len(ids)} models listed by the server"))
        rows.append((f"the model {s.model}", s.model in ids, "listed" if s.model in ids
                     else "not listed by the server: choose another model (the ones listed are in the scan's row below)"))
        table = {m.id for m in self.plugin.models}
        gone = sorted(table - ids)
        if gone:
            rows.append(("the table's models", False, f"no longer listed by the server: {', '.join(gone)} (the table is "
                                                       "stale: update the plugin)"))
        new = sorted(ids - table)
        if new:
            rows.append(("models not in the table", True, ", ".join(new[:12]) + (" ..." if len(new) > 12 else "")))
        rpm, tpm = self.limits_from(self.client.last_headers)
        plan = self.plugin.plan_of(s.plan)
        if not light and plan is not None and plan.api_access:
            probe = Prompt("Reply with the single word OK.", "Are you there?", self.probe_tokens)
            try:
                parsed, headers, seconds = self.answer(probe, s.model)
            except SourceError as e:
                rows.append(("an answer", False, f"refused: {e.message}"))
            else:
                rows.append(("an answer", True, f"{parsed.model or s.model} answered in {seconds:g} s: {parsed.usage.total} tokens"
                             + (f", ${parsed.cost_usd:.6f} by the vendor's count" if parsed.cost_usd is not None else "")))
                r, t = self.limits_from(headers)
                rpm, tpm = r or rpm, t or tpm
                if parsed.cost_usd is not None:
                    mine = self.plugin.cost(parsed.model or s.model, parsed.usage)
                    if mine is not None and parsed.cost_usd > 0 and abs(mine - parsed.cost_usd) > 0.1 * parsed.cost_usd:
                        rows.append(("the table's prices", False, f"the vendor counted ${parsed.cost_usd:.6f}, the table "
                                                                   f"${mine:.6f}: the table is stale, update the plugin"))
        if rpm or tpm:
            found = self.plan_from(rpm, tpm)
            said = (f"{rpm:,} requests a minute" if rpm else "") + (" and " if rpm and tpm else "") + (f"{tpm:,} tokens a minute" if tpm else "")
            if found:
                plan_found = {self.plugin.plan_var: found}
                rows.append(("its plan", True, f"the server allows {said}: the {self.plugin.plan_of(found).title} plan"))
            else:
                rows.append(("its plan", True, f"the server allows {said}: no plan in the table has these limits"
                                               " (the table may be stale, or the limits are your organization's own)"))
        seen = {k: v for k, v in self.client.limits_seen.items()}
        return Scanned(tuple(rows), plan_found, {}, {"seen": seen, "perMinute": rpm, "kept": list(self.window())})

    def plan_from(self, rpm: int | None, tpm: int | None) -> str | None:
        """The plan in the table whose published limits the server's are (requests a minute first)."""
        for p in self.plugin.plans:
            if rpm and p.rpm == rpm:
                return p.name
        for p in self.plugin.plans:
            if tpm and p.tpm == tpm and not rpm:
                return p.name
        return None


# -- the OpenAI-shaped Responses API (OpenAI's own, xAI's) --------------------------------------------

QUOTA_CODES = ("insufficient_quota", "credit_balance_exhausted", "organization_spend_limit_exceeded",
               "project_spend_limit_exceeded", "organization_usage_limit_exceeded")


class ResponsesAssistant(Assistant):
    """The Responses API shape (POST /v1/responses: `instructions`, `input`, `max_output_tokens`, `store`;
    the answer's `output[]` message items with `output_text`; `usage.input_tokens`, `output_tokens`,
    the cached and reasoning details): OpenAI's API and xAI's. A vendor sets `user_field` (OpenAI's
    `safety_identifier`; "" where the vendor takes none) and its own error codes."""

    answers_path = "/v1/responses"
    request_id_header = "x-request-id"
    limit_prefixes = ("x-ratelimit-",)
    user_field = "safety_identifier"

    def headers(self, key: str) -> dict:
        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def body(self, prompt: Prompt, model: str, user_id: str) -> dict:
        body: dict = {"model": model, "instructions": prompt.system, "input": [{"role": "user", "content": prompt.user}],
                      "max_output_tokens": max(int(prompt.max_tokens), 16), "store": False}
        if prompt.json:
            body["text"] = {"format": {"type": "json_object"}}
        if user_id and self.user_field:
            body[self.user_field] = user_id
        if self.settings.effort:
            body["reasoning"] = {"effort": self.settings.effort}
        return body

    def parse(self, raw: Mapping[str, Any]) -> Parsed:
        items = [i for i in (raw.get("output") or ()) if isinstance(i, Mapping)]
        parts = [c for i in items if i.get("type") == "message" for c in (i.get("content") or ()) if isinstance(c, Mapping)]
        text = "".join(str(c.get("text", "")) for c in parts if c.get("type") == "output_text")
        refused = any(c.get("type") == "refusal" for c in parts)
        status = str(raw.get("status") or "completed")
        reason = str((raw.get("incomplete_details") or {}).get("reason") or "")
        if status == "failed":
            err = raw.get("error") or {}
            raise SourceError(0, f"the answer failed: {err.get('message') or err.get('code') or 'no reason given'}")
        stop = "refusal" if refused or reason == "content_filter" else "length" if reason == "max_output_tokens" else "end_turn"
        u = raw.get("usage") or {}
        usage = Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                      int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0),
                      int((u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0))
        cost = None
        if u.get("cost_in_nano_usd") is not None:
            cost = round(float(u["cost_in_nano_usd"]) / 1e9, 9)
        elif u.get("cost_in_usd_ticks") is not None:
            cost = round(float(u["cost_in_usd_ticks"]) / 1e10, 10)
        return Parsed(text, usage, stop, str(raw.get("id") or ""), str(raw.get("model") or ""), cost, raw)

    def error(self, status: int, body, message: str) -> SourceError | None:
        err = (body or {}).get("error") if isinstance(body, Mapping) else None
        err = err if isinstance(err, Mapping) else {}
        code, kind = str(err.get("code") or ""), str(err.get("type") or "")
        msg = str(err.get("message") or message)
        if status == 401:
            return Unauthorized(401, msg)
        if status == 404 or code == "model_not_found":
            return NotEntitled(status, f"no such model: {msg} (choose one from Connect's list)")
        if status == 403:
            return NotEntitled(403, msg)
        if status == 429 and (code in QUOTA_CODES or kind == "insufficient_quota"):
            return NotEntitled(429, f"no credit for it: {msg}")
        return None

    def final(self, status: int, body) -> bool:
        err = (body or {}).get("error") if isinstance(body, Mapping) else None
        err = err if isinstance(err, Mapping) else {}
        return status == 429 and (str(err.get("code") or "") in QUOTA_CODES or str(err.get("type") or "") == "insufficient_quota")

    def limits_from(self, headers: Mapping[str, Any]) -> tuple[int | None, int | None]:
        return _int(header(headers, "x-ratelimit-limit-requests")), _int(header(headers, "x-ratelimit-limit-tokens"))


def _int(value) -> int | None:
    try:
        return int(float(str(value).strip())) if value not in (None, "") else None
    except ValueError:
        return None
