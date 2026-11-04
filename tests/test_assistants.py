"""The AI assistants (plugin API 2.0, liminal_lib.assistants): standardized arguments rendered and
read back, an assistant plugin's variables and settings derived from its tables, the registry's
assistant kind, an evaluation end to end on a fake session (the account sent as a digest, never the
email; the caps; the record kept), Connect reading the plan from the server's limits, errors mapped
and scrubbed, a judge built on earlier evaluations, the client's POST, the ledgers, the page check."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests

from liminal_lib import layout, sourcing
from liminal_lib.assistants import (Answer, ContextItem, Judge, Offline, Question, ResponsesAssistant, argument_from_dict,
                                    evaluate, evaluate_all, judge, parse_fields, parse_verdict)
from liminal_lib.assistants.assistant import Caps, Model, Plan, Source
from liminal_lib.assistants.context import item_from
from liminal_lib.assistants.evaluation import Evaluation, Usage
from liminal_lib.assistants.plugin import AssistantPlugin
from liminal_lib.assistants.verify import check_sources, text_of
from liminal_lib.config import ConfigError, load_settings
from liminal_lib.errors import BudgetExhausted, NotEntitled, RateLimited, SourceError, Unauthorized
from liminal_lib.ratelimit import MachineLedger, fingerprint
from liminal_lib.sources import registry
from liminal_lib.sources.http import HttpClient
from liminal_lib.sources.registry import plugin_databases, plugin_keys, variables
from liminal_lib.storage import Store

NOW = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
KEY, EMAIL = "sk-acme-secret-key-123", "me@example.com"


# -- fakes ------------------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self._body, self.headers = status, body if body is not None else {}, headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class FakeSession:
    """Queued answers; every request recorded as (method, url, params, json, headers)."""

    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append(("GET", url, params, None, headers))
        return self.responses.pop(0)

    def post(self, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, params, json, headers))
        return self.responses.pop(0)


def answer(text, model="acme-nano", in_=120, out=30, cached=20, reasoning=5, status="completed", reason=None, headers=None,
           nano_usd=None):
    body = {"id": "resp_1", "model": model, "status": status,
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": in_, "output_tokens": out, "input_tokens_details": {"cached_tokens": cached},
                      "output_tokens_details": {"reasoning_tokens": reasoning}, "total_tokens": in_ + out}}
    if reason:
        body["incomplete_details"] = {"reason": reason}
    if nano_usd is not None:
        body["usage"]["cost_in_nano_usd"] = nano_usd
    return FakeResponse(200, body, headers or {"x-request-id": "req_1"})


def models(*ids, headers=None):
    return FakeResponse(200, {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}, headers or {})


class AcmeAssistant(ResponsesAssistant):
    base_url = "https://api.acme-ai.example"


class AcmeAI(AssistantPlugin):
    name = "acme-ai"
    title = "Acme AI"
    about = "a test vendor"
    vendor = "Acme"
    assistant = AcmeAssistant
    verified = "2026-11-03"
    plans = (Plan("free", "Free", api_access=False, note="no credits: buy some at the console"),
             Plan("build", "Build", rpm=5000, tpm=1_000_000, price="$5 bought", spend_cap="$500 a month"),
             Plan("grow", "Grow", rpm=15000, tpm=40_000_000, price="$500 bought"))
    models = (Model("acme-nano", "Acme Nano", 0.05, 0.40, 0.005, 400_000),
              Model("acme-pro", "Acme Pro", 2.0, 10.0, 0.5, 1_000_000, plans=("grow",), reasoning=True))
    sources = (Source("https://acme-ai.example/pricing", "the models' prices", "Prices per 1M tokens", ("acme-nano", "$0.05", "$0.40")),)
    efforts = ("", "low", "medium", "high")


@pytest.fixture
def installed(monkeypatch, tmp_path):
    """The fake assistant loaded as an installed package's entry point; the machine ledgers in a scratch file."""
    monkeypatch.setattr(layout, "plugins_dir", lambda: None)
    monkeypatch.setenv("LIMINAL_LIVE_LOCK", str(tmp_path / "machine" / "live.lock"))

    def install(*plugins):
        eps = [SimpleNamespace(name=p.name, value=f"fake_{p.name}:PLUGIN", load=(lambda p=p: p)) for p in plugins]
        monkeypatch.setattr(registry, "_entry_points", lambda: eps)
        registry.reload()
        return registry.assistants()

    yield install
    registry._providers = None


@pytest.fixture
def settings_for(tmp_path):
    def make(**env):
        return load_settings(env={"LIMINAL_DB": str(tmp_path / "m.db"), **env}, env_file=None)
    return make


# -- arguments --------------------------------------------------------------------------------------

def test_arguments_render_prompts_and_read_answers_back():
    q = Question("Is the market open?", context=(ContextItem("headline", "NYSE closed for Thanksgiving", id="h1", title="Reuters"),),
                 answer=Answer("verdict", scale=("yes", "no", "unclear")))
    p = q.prompt(300)
    assert p.max_tokens == 300 and not p.json and "Verdict: <verdict>" in p.system and "yes, no, unclear" in p.system
    assert "[headline: Reuters] NYSE closed for Thanksgiving" in p.user and p.user.endswith("Question: Is the market open?")
    assert q.parse("The exchange is shut.\n\nVerdict: **No**.") == ("no", {})
    assert q.parse("I cannot tell.") == (None, {}) and q.context_ids() == ("h1",) and q.id.startswith("arg_")
    f = Question("Score it", answer=Answer("fields", fields=("verdict", "score")))
    assert f.prompt(100).json and "exactly these fields: verdict, score" in f.prompt(100).system
    assert f.parse('Here you go:\n```json\n{"verdict": "bullish", "score": 7}\n```') == ("bullish", {"verdict": "bullish", "score": 7})
    assert parse_fields("no json here") == {} and parse_verdict("verdict - Sell", ("buy", "sell")) == "sell"
    back = argument_from_dict(q.as_dict())
    assert isinstance(back, Question) and back.id == q.id and back.answer == q.answer and back.context == q.context
    with pytest.raises(ValueError, match="unknown argument kind"):
        argument_from_dict({"kind": "riddle"})
    with pytest.raises(ValueError):
        Answer("verdict")  # a verdict needs its scale


def test_a_judge_renders_the_earlier_answers_as_its_inputs():
    a = Evaluation("ev_a", "arg_1", "question", "acme-ai", "Acme", "acme-nano", "The market is shut.", "no")
    b = Evaluation("ev_b", "arg_1", "question", "other", "Other", "other-1", "It is open.", "yes")
    j = Judge(evaluations=(a, b), task="compare", question="Is the market open?")
    p = j.prompt(400)
    assert j.inputs() == ("ev_a", "ev_b") and p.json and "Answer A (acme-ai, acme-nano, verdict no):\nThe market is shut." in p.user
    assert "Answer B (other, other-1, verdict yes)" in p.user and "Compare the answers" in p.user and "Is the market open?" in p.user
    assert j.as_dict()["evaluations"] == ["ev_a", "ev_b"]
    back = argument_from_dict(j.as_dict(), lookup={"ev_a": a, "ev_b": b}.get)
    assert isinstance(back, Judge) and back.evaluations == (a, b) and back.task == "compare"
    with pytest.raises(LookupError):
        argument_from_dict(j.as_dict(), lookup=lambda eid: None)
    with pytest.raises(ValueError):
        Judge(evaluations=(a,), task="rank")
    assert a.as_context().kind == "evaluation" and "acme-ai (acme-nano), verdict no" == a.as_context().title


# -- the plugin's variables, settings and tables -----------------------------------------------------

def test_a_plugin_derives_its_variables_and_settings_from_its_tables():
    p = AcmeAI()
    assert p.KIND == "assistant" and p.var == "ACME_AI" and [k.name for k in p.keys] == ["ACME_AI_API_KEY", "ACME_AI_ACCOUNT"]
    assert p.keys[0].secret and not p.keys[1].secret and p.key_names == ("ACME_AI_API_KEY", "ACME_AI_ACCOUNT")
    assert p.plan_vars == ("ACME_AI_PLAN", "ACME_AI_MODEL", "ACME_AI_REQUESTS_PER_DAY", "ACME_AI_TOKENS_PER_DAY")
    assert p.options == ("ACME_AI_MAX_OUTPUT_TOKENS", "ACME_AI_EFFORT")
    assert p.plan_choices["ACME_AI_PLAN"] == ("free", "build", "grow") and p.plan_choices["ACME_AI_MODEL"] == ("acme-nano", "acme-pro")
    assert p.plan_choices["ACME_AI_REQUESTS_PER_DAY"][0] == "50" and p.plan_choices["ACME_AI_EFFORT"] == ("", "low", "medium", "high")
    assert p.has_scan and p.has_settings and p.free_plan.name == "free"
    s = p.settings({})
    assert (s.plan, s.model, s.caps, s.effort, s.key, s.account) == ("free", "acme-nano", Caps(), "", None, "")
    assert not s.has_key and s.user_id == "" and "sk-" not in repr(s)
    s = p.settings({"ACME_AI_API_KEY": KEY, "ACME_AI_ACCOUNT": EMAIL, "ACME_AI_PLAN": "Build", "ACME_AI_MODEL": "acme-nano",
                    "ACME_AI_REQUESTS_PER_DAY": "20", "ACME_AI_TOKENS_PER_DAY": "50,000", "ACME_AI_MAX_OUTPUT_TOKENS": "256",
                    "ACME_AI_EFFORT": "low"})
    assert s.plan == "build" and s.caps == Caps(20, 50_000, 256) and s.effort == "low" and s.has_key
    assert s.user_id == fingerprint(EMAIL) and EMAIL not in repr(s) and KEY not in repr(s)
    for bad in ({"ACME_AI_PLAN": "gold"}, {"ACME_AI_MODEL": "acme-max"}, {"ACME_AI_REQUESTS_PER_DAY": "many"},
                {"ACME_AI_TOKENS_PER_DAY": "0"}, {"ACME_AI_EFFORT": "xhigh"}, {"ACME_AI_PLAN": "build", "ACME_AI_MODEL": "acme-pro"}):
        with pytest.raises(ConfigError):
            p.settings(bad)
    assert p.cost("acme-nano", Usage(120, 30, 20, 5)) == pytest.approx(17.1e-6, abs=1e-7) and p.cost("acme-max", Usage()) is None
    rows = p.table_rows()
    assert rows["plans"][0] == {"name": "free", "title": "Free", "apiAccess": False, "rpm": None, "tpm": None, "rpd": None,
                                "price": "", "models": [], "spendCap": "", "note": "no credits: buy some at the console"}
    assert [m["id"] for m in rows["models"]] == ["acme-nano", "acme-pro"] and rows["verified"] == "2026-11-03"


def test_the_registry_takes_an_assistant_and_the_settings_build_its_own(installed, settings_for):
    found = installed(AcmeAI())
    assert list(found) == ["acme-ai"] and registry.errors() == [] and "acme-ai" not in registry.providers()
    assert [k.name for k in plugin_keys("acme-ai")] == ["ACME_AI_API_KEY", "ACME_AI_ACCOUNT"]
    assert set(variables()) >= {"ACME_AI_API_KEY", "ACME_AI_ACCOUNT", "ACME_AI_PLAN", "ACME_AI_MODEL", "ACME_AI_MAX_OUTPUT_TOKENS"}
    (db,) = plugin_databases("acme-ai")
    assert db.where == layout.MAIN and db.shared and "evaluations" in db.holds
    settings = settings_for(ACME_AI_API_KEY=KEY, ACME_AI_ACCOUNT=EMAIL, ACME_AI_PLAN="build", LIMINAL_ASSISTANT="Acme-AI")
    own = settings.plugin("acme-ai")
    assert own.plan == "build" and own.has_key and settings.assistant == "acme-ai"
    p = found["acme-ai"]
    assert p.keys_set(settings) and p.served(settings) and p.free_note(settings) == "" and p.rate_window(settings) == (5000, 60.0)
    assert p.plan_state(settings)[0] == {"name": "ACME_AI_PLAN", "value": "build", "origin": "you", "choices": ["free", "build", "grow"]}
    free = settings_for(ACME_AI_API_KEY=KEY)
    assert free.plugin("acme-ai").plan == "free" and p.keys_set(free) and not p.served(free)
    assert p.free_note(free) == "the Free plan has no API access: no credits: buy some at the console"
    assert sourcing.detected_plans({"acme-ai": {"plan": {"ACME_AI_PLAN": "grow", "OTHER": "x"}}}) == {"ACME_AI_PLAN": "grow"}
    row = next(r for r in registry.assistant_menu(settings) if r["name"] == "acme-ai")
    assert row["keysSet"] and row["serves"] and row["plan"] == "build" and row["model"] == "acme-nano" and row["tables"]["vendor"] == "Acme"
    assert row["keys"][0] == {"name": "ACME_AI_API_KEY", "label": "Acme API key", "aliases": [], "secret": True,
                              "hint": "an API key from the vendor's console", "set": True}
    with pytest.raises(ConfigError):
        settings_for(ACME_AI_PLAN="gold")  # a plan the table has not stops the app, as any setting's refusal does


# -- an evaluation end to end ------------------------------------------------------------------------

def test_an_evaluation_end_to_end_sends_the_digest_never_the_email_and_keeps_the_answer(installed, settings_for, tmp_path):
    installed(AcmeAI())
    settings = settings_for(ACME_AI_API_KEY=KEY, ACME_AI_ACCOUNT=EMAIL, ACME_AI_PLAN="build", ACME_AI_EFFORT="low")
    session = FakeSession(answer("Closed for the holiday.\nVerdict: no", nano_usd=None))
    store = Store(tmp_path / "m.db", check_same_thread=False)
    try:
        q = Question("Is the market open?", context=(ContextItem("headline", "NYSE closed", id="h1"),),
                     answer=Answer("verdict", scale=("yes", "no")))
        ev = evaluate(q, settings, store=store, session=session, now=lambda: NOW)
        (call,) = session.calls
        method, url, _, body, headers = call
        assert method == "POST" and url == "https://api.acme-ai.example/v1/responses" and headers["Authorization"] == f"Bearer {KEY}"
        assert body["model"] == "acme-nano" and body["max_output_tokens"] == 1024 and body["store"] is False
        assert body["safety_identifier"] == fingerprint(EMAIL) and body["reasoning"] == {"effort": "low"}
        assert EMAIL not in json.dumps(body) and body["input"] == [{"role": "user", "content": q.prompt(1024).user}]
        assert ev.assistant == "acme-ai" and ev.provider == "Acme" and ev.model == "acme-nano" and ev.verdict == "no" and ev.ok
        assert ev.usage == Usage(120, 30, 20, 5) and ev.cost_usd == pytest.approx(17.1e-6, abs=1e-7) and ev.request_id == "resp_1"
        assert ev.at == "2026-11-03T15:00:00+00:00" and ev.context == ("h1",) and ev.inputs == () and ev.stop == "end_turn"
        (row,) = store.evaluations(assistant="acme-ai")
        assert row["id"] == ev.id and row["verdict"] == "no" and row["argument"]["question"] == "Is the market open?"
        assert row["argument"]["context_ids"] == ["h1"] and row["cost_usd"] == ev.cost_usd and row["input_tokens"] == 120
        assert store.evaluation(ev.id) == ev and store.evaluations_today("acme-ai", NOW) == {"requests": 1, "tokens": 150,
                                                                                            "costUsd": ev.cost_usd}
        assert MachineLedger("acme-ai", KEY).today(NOW) == 1 and MachineLedger("acme-ai:tokens", KEY).today(NOW) == 150
        # the vendor's own cost figure wins over the table's; an answer cut at the cap still parses
        session.responses.append(answer("Verdict: yes", reason="max_output_tokens", status="incomplete", nano_usd=123_000))
        cut = evaluate(q, settings, store=store, session=session, now=lambda: NOW)
        assert cut.stop == "length" and cut.verdict == "yes" and cut.cost_usd == pytest.approx(0.000123)
    finally:
        store.close()


def test_nothing_is_sent_offline_without_a_key_on_the_free_plan_or_past_the_caps(installed, settings_for, tmp_path):
    installed(AcmeAI())
    session = FakeSession()
    with pytest.raises(Offline):
        evaluate(Question("x"), settings_for(ACME_AI_API_KEY=KEY, ACME_AI_PLAN="build", LIMINAL_OFFLINE="1"), session=session)
    with pytest.raises(LookupError, match="no AI assistant is set up"):
        evaluate(Question("x"), settings_for(), session=session)  # no key: not set up
    with pytest.raises(Unauthorized, match="no key"):
        evaluate(Question("x"), settings_for(), assistant="acme-ai", session=session)
    with pytest.raises(NotEntitled, match="Free plan has no API access"):
        evaluate(Question("x"), settings_for(ACME_AI_API_KEY=KEY), assistant="acme-ai", session=session)
    with pytest.raises(NotEntitled, match="not on the Grow plan|does not reach"):
        evaluate(Question("x"), settings_for(ACME_AI_API_KEY=KEY, ACME_AI_PLAN="build"), assistant="acme-ai", session=session,
                 model="acme-pro")
    assert session.calls == []
    settings = settings_for(ACME_AI_API_KEY=KEY, ACME_AI_PLAN="build", ACME_AI_REQUESTS_PER_DAY="1", ACME_AI_TOKENS_PER_DAY="100")
    store = Store(tmp_path / "m.db", check_same_thread=False)
    try:
        session = FakeSession(answer("ok", in_=80, out=30))
        evaluate(Question("x"), settings, store=store, session=session, now=lambda: NOW)
        with pytest.raises(BudgetExhausted, match="token cap"):  # 110 tokens charged: past 100 before anything else is sent
            evaluate(Question("y"), settings, store=store, session=session, now=lambda: NOW)
        settings = settings_for(ACME_AI_API_KEY=KEY, ACME_AI_PLAN="build", ACME_AI_REQUESTS_PER_DAY="1")
        with pytest.raises(BudgetExhausted, match="daily request cap"):  # the request ledger: one today already
            evaluate(Question("z"), settings, store=store, session=session, now=lambda: NOW)
        assert len(session.calls) == 1
        failed = [e for e in store.evaluations() if e["stop"] == "error"]
        assert failed == []  # a cap refusal is no evaluation: nothing was asked
    finally:
        store.close()


def test_a_vendor_refusal_is_recorded_as_a_failed_evaluation_scrubbed_of_the_key(installed, settings_for, tmp_path):
    installed(AcmeAI())
    settings = settings_for(ACME_AI_API_KEY=KEY, ACME_AI_ACCOUNT=EMAIL, ACME_AI_PLAN="build")
    store = Store(tmp_path / "m.db", check_same_thread=False)
    try:
        session = FakeSession(FakeResponse(401, {"error": {"message": f"Incorrect API key provided: {KEY} for {EMAIL}", "code": "invalid_api_key"}}))
        with pytest.raises(Unauthorized) as e:
            evaluate(Question("x"), settings, store=store, session=session)
        assert KEY not in str(e.value) and EMAIL not in str(e.value) and "Incorrect API key provided: … for …" in str(e.value)
        (row,) = store.evaluations()
        assert row["stop"] == "error" and row["error"].startswith("Unauthorized:") and KEY not in row["error"]
        session = FakeSession(FakeResponse(429, {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}}))
        with pytest.raises(NotEntitled, match="no credit"):
            evaluate(Question("x"), settings, session=session)
        assert len(session.calls) == 1  # a quota 429 is final: not tried again
        limited = FakeResponse(429, {"error": {"message": "Rate limit reached", "code": "rate_limit_exceeded"}}, {"Retry-After": "1"})
        session = FakeSession(limited, limited, limited, limited, limited)
        a = registry.assistant("acme-ai").build(settings, session=session)
        a.client._sleep = lambda s: None
        with pytest.raises(RateLimited):
            a.evaluate(Question("x"))
        assert len(session.calls) == 5  # tried again as a rate limit, then given up
        gone = FakeResponse(404, {"error": {"message": "The model `acme-nano` does not exist", "code": "model_not_found"}})
        with pytest.raises(NotEntitled, match="choose one from Connect's list"):
            registry.assistant("acme-ai").build(settings, session=FakeSession(gone)).evaluate(Question("x"))
    finally:
        store.close()


def test_connect_reads_the_plan_from_the_servers_limits_and_checks_the_tables(installed, settings_for):
    installed(AcmeAI())
    settings = settings_for(ACME_AI_API_KEY=KEY, ACME_AI_PLAN="build")
    limits = {"x-ratelimit-limit-requests": "15000", "x-ratelimit-limit-tokens": "40000000", "x-request-id": "req_9"}
    session = FakeSession(models("acme-nano", "acme-new"), answer("OK", in_=10, out=2, cached=0, reasoning=0, headers=limits))
    scanned = registry.assistant("acme-ai").run_scan(registry.assistant("acme-ai").build(settings, session=session))
    rows = {w: (ok, d) for w, ok, d in scanned.results}
    assert rows["the key"] == (True, "accepted: 2 models listed by the server") and rows["the model acme-nano"][0]
    assert not rows["the table's models"][0] and "acme-pro" in rows["the table's models"][1]  # listed no more: stale
    assert rows["models not in the table"] == (True, "acme-new")
    assert rows["an answer"][0] and "12 tokens" in rows["an answer"][1]
    assert rows["its plan"] == (True, "the server allows 15,000 requests a minute and 40,000,000 tokens a minute: the Grow plan")
    assert scanned.plan == {"ACME_AI_PLAN": "grow"} and scanned.limits["perMinute"] == 15000
    assert session.calls[0][0] == "GET" and session.calls[0][1] == "https://api.acme-ai.example/v1/models"
    assert session.calls[1][3]["max_output_tokens"] == 16 and session.calls[1][3]["instructions"] == "Reply with the single word OK."
    # the plan found is applied where the user set none (config.with_detected), as a feed's is
    from liminal_lib.config import with_detected

    applied = with_detected(settings_for(ACME_AI_API_KEY=KEY), scanned.plan)
    assert applied.plugin("acme-ai").plan == "grow" and applied.detected == {"ACME_AI_PLAN": "grow"}
    # light: the models list alone; a refused key is one row
    light = registry.assistant("acme-ai").build(settings, session=FakeSession(models("acme-nano", "acme-pro"))).connect(light=True)
    assert [w for w, _, _ in light.results] == ["the key", "the model acme-nano"] and light.plan == {}
    refused = registry.assistant("acme-ai").build(settings, session=FakeSession(FakeResponse(401, {"error": {"message": "bad key"}})))
    assert refused.connect(light=True).results == (("the key", False, "refused: bad key"),)


def test_a_judge_builds_on_the_earlier_evaluations_kept(installed, settings_for, tmp_path):
    installed(AcmeAI())
    settings = settings_for(ACME_AI_API_KEY=KEY, ACME_AI_PLAN="build")
    store = Store(tmp_path / "m.db", check_same_thread=False)
    try:
        session = FakeSession(answer("Shut.\nVerdict: no"), answer("Open.\nVerdict: yes"),
                              answer('{"verdict": "A", "ranking": ["A", "B"], "reasons": "A cites the closure."}'))
        q = Question("Is the market open?", answer=Answer("verdict", scale=("yes", "no")))
        first = evaluate(q, settings, store=store, session=session, now=lambda: NOW)
        second = evaluate(q, settings, store=store, session=session, now=lambda: NOW, model="acme-nano")
        verdict = judge([first, second], settings, store=store, session=session, now=lambda: NOW)
        body = session.calls[2][3]
        assert body["text"] == {"format": {"type": "json_object"}} and "Answer A (acme-ai, acme-nano, verdict no):\nShut." in body["input"][0]["content"]
        assert verdict.kind == "judge" and verdict.inputs == (first.id, second.id) and verdict.verdict == "A"
        assert verdict.fields["ranking"] == ["A", "B"]
        chain = store.evaluation_chain(verdict.id)
        assert [c["id"] for c in chain] == [verdict.id, first.id, second.id] and chain[0]["argument"]["task"] == "compare"
        back = argument_from_dict(chain[0]["argument"], lookup=store.evaluation)
        assert isinstance(back, Judge) and [e.id for e in back.evaluations] == [first.id, second.id]
        assert [e["verdict"] for e in store.evaluations(kind="question")] == ["yes", "no"]  # newest first
        all_ = evaluate_all(q, settings, store=store, session=FakeSession(answer("Verdict: no")), now=lambda: NOW)
        assert [e.assistant for e in all_] == ["acme-ai"] and all_[0].verdict == "no"
    finally:
        store.close()


# -- the pieces ---------------------------------------------------------------------------------------

def test_the_client_posts_json_and_never_sends_a_post_twice_after_a_transport_failure():
    class Flaky:
        def __init__(self):
            self.calls = 0

        def post(self, url, params=None, json=None, headers=None, timeout=None):
            self.calls += 1
            raise requests.ConnectionError("reset")

        def get(self, url, params=None, headers=None, timeout=None):
            self.calls += 1
            raise requests.ConnectionError("reset")
    flaky = Flaky()
    client = HttpClient("https://acme.example", session=flaky, sleep=lambda s: None, max_retries=3)
    with pytest.raises(SourceError, match="no answer"):
        client.post("/v1/x", {"a": 1})
    assert flaky.calls == 1
    with pytest.raises(SourceError):
        client.get("/v1/x")
    assert flaky.calls == 5  # a GET is tried again
    session = FakeSession(FakeResponse(200, {"ok": True}, {"X-RateLimit-Limit": "9", "x-acme-limit": "3", "Date": "now"}))
    client = HttpClient("https://acme.example", session=session, sleep=lambda s: None)
    client.limit_prefixes = ("x-acme-",)
    assert client.post("/v1/x", {"a": 1}) == {"ok": True} and session.calls[0][3] == {"a": 1}
    assert client.limits_seen == {"X-RateLimit-Limit": "9", "x-acme-limit": "3"} and client.last_headers["Date"] == "now"


def test_the_machine_ledger_takes_and_charges_a_days_count(tmp_path):
    ledger = MachineLedger("acme-ai:tokens", "k", path=tmp_path / "ledger.db")
    assert ledger.take_day(10, NOW, n=4) and ledger.today(NOW) == 4
    assert ledger.charge_day(5, NOW) == 9 and ledger.today(NOW) == 9
    assert ledger.take_day(10, NOW) and not ledger.take_day(10, NOW) and ledger.today(NOW) == 10  # the cap reached
    assert ledger.charge_day(100, NOW) == 110  # charged whatever the cap: an answer that came is counted


def test_context_items_from_what_the_app_holds():
    h = item_from({"title": "Fed holds rates", "url": "https://x", "agency": "Fed", "published": "2026-11-03"})
    assert h.kind == "headline" and h.title == "Fed holds rates" and "https://x" in h.text
    a = item_from({"symbol": "AAPL", "asset": "stock", "feeds": ["massive"], "jobs": [{"plugin": "massive", "resolution": "minute",
                                                                                     "market": "us", "start": "2026-01-01"}]})
    assert a.kind == "assessment" and a.text.startswith("Symbol AAPL (stock): the plan asks massive; jobs: massive: minute bars")
    e = item_from(Evaluation("ev_1", "arg", "question", "acme-ai", "Acme", "m", "text", "yes"))
    assert e.kind == "evaluation" and e.id == "ev_1"
    assert item_from("plain words").kind == "text" and item_from({"a": 1}).kind == "data"


def test_the_page_check_names_a_stale_table():
    page = "<html><body><h1>Prices per 1M tokens</h1><table><tr><td>acme-nano</td><td>$0.05</td><td>$0.40</td></tr></table></body></html>"
    assert check_sources(AcmeAI(), fetch=lambda url: text_of(page)) == []
    assert check_sources(AcmeAI(), fetch=lambda url: "Prices per 1M tokens acme-nano $0.08 $0.40") == [
        "https://acme-ai.example/pricing (the models' prices): the table is stale: '$0.05' no longer on the page"]
    assert check_sources(AcmeAI(), fetch=lambda url: "Our new pricing page")[0].endswith("its sentinel 'Prices per 1M tokens' is gone")

    def down(url):
        raise RuntimeError("HTTP 503")
    assert check_sources(AcmeAI(), fetch=down) == ["https://acme-ai.example/pricing (the models' prices): could not be read: RuntimeError: HTTP 503"]
