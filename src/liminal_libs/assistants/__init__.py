"""AI assistants (plugin API 2.0): the assistant plugins (plugin-platform-claude, -grok, -codex) and
what the app and the other plugins ask of them through the library: evaluate a standardized argument
(arguments.py: a Question over context items, a Judge over earlier evaluations), by the assistant you
chose (LIMINAL_ASSISTANT) or each one set up, and keep the evaluations (storage/evaluations.py).

A plugin asking for an evaluation calls `evaluate` here, never an AI package: the assistants are
plugins too, found by the registry, and a plugin imports no other plugin. Nothing is sent from an
offline copy, and every answer counts on the assistant's caps for this machine (its machine ledgers).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Iterable

from ..errors import SourceError
from .arguments import (ARGUMENTS, Answer, Argument, ContextItem, Judge, Prompt, Question, argument_from_dict, parse_fields,
                        parse_verdict, register_argument)
from .assistant import (Assistant, AssistantClient, AssistantSettings, Caps, Model, Offline, Parsed, Plan, ResponsesAssistant,
                        Source)
from .evaluation import Evaluation, Usage, new_evaluation_id, now_iso
from .plugin import AssistantPlugin

if TYPE_CHECKING:
    from ..config import Settings

__all__ = ["ARGUMENTS", "Answer", "Argument", "ContextItem", "Judge", "Prompt", "Question", "argument_from_dict", "parse_fields",
           "parse_verdict", "register_argument", "Assistant", "AssistantClient", "AssistantSettings", "Caps", "Model", "Offline",
           "Parsed", "Plan", "ResponsesAssistant", "Source", "Evaluation", "Usage", "new_evaluation_id", "now_iso",
           "AssistantPlugin", "assistants", "default_assistant", "build", "evaluate", "evaluate_all", "judge", "record"]


def assistants() -> dict[str, AssistantPlugin]:
    """The assistant plugins by name, in their order (the registry's)."""
    from ..sources.registry import assistants as found

    return found()


def default_assistant(settings: "Settings") -> str | None:
    """The assistant an evaluation goes to when none is named: LIMINAL_ASSISTANT where it names one
    here, else the first set up (its key, a plan with API access); None when none is."""
    found = assistants()
    chosen = (getattr(settings, "assistant", None) or "").strip().lower()
    if chosen and chosen in found:
        return chosen
    return next((n for n, p in found.items() if p.served(settings)), None)


def build(name: str, settings: "Settings", session=None, now=None) -> Assistant:
    """An assistant plugin's Assistant on these settings."""
    p = assistants().get(name)
    if p is None:
        raise LookupError(f"no AI assistant named {name!r}; the plugins here: {', '.join(assistants()) or 'none'}")
    return p.build(settings, session=session, now=now)


def record(store, argument: Argument, evaluation: Evaluation) -> None:
    """An evaluation kept in the store (its argument with it), for the view and for later judges."""
    if store is not None:
        store.save_evaluation(argument.as_dict(), evaluation)


def evaluate(argument: Argument, settings: "Settings", *, assistant: str | None = None, model: str | None = None,
             store=None, session=None, now=None) -> Evaluation:
    """One assistant's evaluation of an argument, recorded (with `store`). Offline: nothing is sent
    (Offline). A refusal or a failure (SourceError: no key, no credit, a rate limit, a vendor error) is
    recorded as a failed evaluation and raised."""
    if getattr(settings, "offline", False) or getattr(settings, "read_only", False):
        raise Offline(f"offline (LIMINAL_OFFLINE): nothing sent to {assistant or default_assistant(settings) or 'an assistant'}")
    name = assistant or default_assistant(settings)
    if name is None:
        raise LookupError("no AI assistant is set up here: set one's key and plan on the Plugins view "
                          f"({', '.join(assistants()) or 'no assistant plugin is installed'})")
    a = build(name, settings, session=session, now=now)
    try:
        ev = a.evaluate(argument, model)
    except SourceError as e:
        failed = Evaluation(new_evaluation_id(), argument.id, argument.kind, name, a.plugin.vendor, model or a.settings.model,
                            inputs=argument.inputs(), context=argument.context_ids(), at=now_iso(a.now()), stop="error",
                            error=a.client.scrub(f"{type(e).__name__}: {e.message}"))
        record(store, argument, failed)
        raise
    record(store, argument, ev)
    return ev


def evaluate_all(argument: Argument, settings: "Settings", names: Iterable[str] | None = None, *, model: str | None = None,
                 store=None, session=None, now=None) -> list[Evaluation]:
    """The argument evaluated by each assistant set up here (or `names`), one after the other; one
    that fails is recorded as failed and listed with its error, the others still answer."""
    if getattr(settings, "offline", False) or getattr(settings, "read_only", False):
        raise Offline("offline (LIMINAL_OFFLINE): nothing sent to the assistants")
    chosen = list(names) if names is not None else [n for n, p in assistants().items() if p.served(settings)]
    out = []
    for name in chosen:
        try:
            out.append(evaluate(argument, settings, assistant=name, model=model, store=store, session=session, now=now))
        except SourceError as e:
            a = assistants()[name]
            out.append(Evaluation(new_evaluation_id(), argument.id, argument.kind, name, a.vendor, model or "", stop="error",
                                  error=f"{type(e).__name__}: {e.message}", at=now_iso()))
    return out


def judge(evaluations: Iterable[Evaluation], settings: "Settings", task: str = "compare", question: str = "", *,
          assistant: str | None = None, model: str | None = None, context: Iterable[ContextItem] = (), store=None,
          session=None, now=None) -> Evaluation:
    """A higher-order evaluation: earlier evaluations judged together (compare, score, reconcile, or
    as `question` says), by one assistant (the default, or `assistant`)."""
    arg = Judge(evaluations=tuple(evaluations), task=task, question=question, context=tuple(context))
    return evaluate(arg, settings, assistant=assistant, model=model, store=store, session=session, now=now)


def lookup_in(store) -> Callable[[str], Evaluation | None]:
    """The evaluation lookup a store gives (argument_from_dict's `lookup`: a judge's inputs by id)."""
    return (lambda eid: store.evaluation(eid)) if store is not None else (lambda eid: None)
