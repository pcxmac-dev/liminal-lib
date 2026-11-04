"""Standardized arguments (plugin API 2.0, assistants): what a plugin or the app asks an AI assistant
to evaluate, in one form every assistant takes and every caller can build.

An argument is an instance of an `Argument` subclass: a `Question` (a question with context items and
the answer form wanted: text, a verdict on a scale, or JSON fields) or a `Judge` (higher order: earlier
evaluations as its inputs, compared, scored or reconciled). Each kind is registered by name
(`register_argument`), so a plugin adds a kind of its own by subclassing, and an argument crosses
processes and the store as a dict (`as_dict`, `argument_from_dict`). An argument renders itself as a
prompt (`prompt`: the system line, the user text, the token cap, whether JSON is wanted) and reads the
answer back (`parse`: the verdict and the fields).
"""
from __future__ import annotations

import json
import re
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Mapping

from .evaluation import Evaluation

ANSWER_KINDS = ("text", "verdict", "fields")
JUDGE_TASKS = ("compare", "score", "reconcile", "free")
ROLE = ("You are an analyst's assistant inside a market-data application. You evaluate one standardized "
        "argument at a time from the material given, state what the material supports and what it does "
        "not, and never invent figures, sources or events.")
JUDGE_ROLE = ("You are a reviewing analyst inside a market-data application. You judge earlier answers "
              "to one argument against each other and against the material given, naming which is better "
              "supported and why; you never invent figures, sources or events.")
MAX_CONTEXT_CHARS = 60_000  # an argument's context, rendered: what one request may carry

ARGUMENTS: dict[str, type] = {}  # kind -> the Argument subclass (register_argument)


def register_argument(cls):
    """A kind of argument by its name (a class attribute `kind`): a plugin's own kind too."""
    if not getattr(cls, "kind", ""):
        raise TypeError(f"{cls.__name__} has no kind")
    ARGUMENTS[cls.kind] = cls
    return cls


def new_argument_id() -> str:
    return f"arg_{secrets.token_hex(6)}"


@dataclass(frozen=True)
class ContextItem:
    """One piece of material an argument is evaluated from: a headline, a document's text, the
    integrator's assessment of a symbol, a plugin's report, a scheme's signal, an earlier evaluation,
    or plain text (`kind`); `data` is the structured original when there is one (context.item_from)."""
    kind: str
    text: str
    id: str = ""
    title: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)

    def render(self) -> str:
        head = f"[{self.kind}" + (f": {self.title}" if self.title else "") + "]"
        return f"{head} {self.text}".strip()

    def as_dict(self) -> dict:
        return {"kind": self.kind, "text": self.text, "id": self.id, "title": self.title, "data": dict(self.data)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ContextItem":
        return cls(str(d.get("kind", "text")), str(d.get("text", "")), str(d.get("id", "") or ""),
                   str(d.get("title", "") or ""), dict(d.get("data") or {}))


@dataclass(frozen=True)
class Answer:
    """The answer form wanted: free text, a verdict on a scale (the last line "Verdict: <one of the
    scale>"), or a JSON object with the named fields."""
    kind: str = "text"
    scale: tuple[str, ...] = ()  # verdict: the allowed verdicts, in order
    fields: tuple[str, ...] = ()  # fields: the JSON fields wanted
    max_words: int | None = None

    def __post_init__(self):
        if self.kind not in ANSWER_KINDS:
            raise ValueError(f"answer kind {self.kind!r} is not one of {', '.join(ANSWER_KINDS)}")
        if self.kind == "verdict" and not self.scale:
            raise ValueError("a verdict answer needs its scale")
        if self.kind == "fields" and not self.fields:
            raise ValueError("a fields answer needs its fields")

    @property
    def json(self) -> bool:
        return self.kind == "fields"

    def instructions(self) -> str:
        words = f" Keep the answer under {self.max_words} words." if self.max_words else ""
        if self.kind == "verdict":
            return (f"Reason briefly from the material, then end with one line of the form 'Verdict: <verdict>' where "
                    f"<verdict> is exactly one of: {', '.join(self.scale)}.{words}")
        if self.kind == "fields":
            return (f"Answer with one JSON object and nothing else, with exactly these fields: {', '.join(self.fields)}. "
                    f"A field you cannot fill from the material is null.{words}")
        return f"Answer in plain text.{words}"

    def as_dict(self) -> dict:
        return {"kind": self.kind, "scale": list(self.scale), "fields": list(self.fields), "maxWords": self.max_words}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "Answer":
        d = d or {}
        return cls(str(d.get("kind", "text")), tuple(d.get("scale") or ()), tuple(d.get("fields") or ()),
                   d.get("maxWords", d.get("max_words")))


@dataclass(frozen=True)
class Prompt:
    """What is sent: the system line, the user text, the output cap, whether a JSON object is wanted."""
    system: str
    user: str
    max_tokens: int
    json: bool = False


class Argument:
    """What every argument is: its kind (registered), its id, its context items and the answer form;
    it renders its prompt and parses its answer. A subclass is a dataclass with `kind` set."""

    kind: ClassVar[str] = ""
    id: str = ""
    context: tuple[ContextItem, ...] = ()
    answer: Answer = Answer()

    def prompt(self, max_tokens: int) -> Prompt:
        raise NotImplementedError(f"{type(self).__name__}: no prompt")

    def inputs(self) -> tuple[str, ...]:
        """The evaluation ids it builds on (a judge's)."""
        return ()

    def context_ids(self) -> tuple[str, ...]:
        return tuple(c.id for c in self.context if c.id)

    def rendered_context(self) -> str:
        """The context items, each on its own, cut to what a request may carry (the last ones kept
        whole: the newest items are usually given last)."""
        parts = [c.render() for c in self.context]
        text = "\n\n".join(parts)
        if len(text) > MAX_CONTEXT_CHARS:
            text = "[earlier material cut]\n\n" + text[-MAX_CONTEXT_CHARS:]
        return text

    def parse(self, text: str) -> tuple[str | None, dict]:
        """The verdict and the fields an answer holds, by the answer form."""
        if self.answer.kind == "verdict":
            return parse_verdict(text, self.answer.scale), {}
        if self.answer.kind == "fields":
            fields = parse_fields(text)
            verdict = fields.get("verdict")
            return (str(verdict) if verdict not in (None, "") else None), fields
        return None, {}

    def as_dict(self) -> dict:
        return {"kind": self.kind, "id": self.id, "context": [c.as_dict() for c in self.context], "answer": self.answer.as_dict()}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], lookup: Callable[[str], Evaluation | None] | None = None):
        raise NotImplementedError


@register_argument
@dataclass(frozen=True)
class Question(Argument):
    """A question about some material: the context items it is asked over (headlines, a document, an
    assessment ...), the answer form, and the assistant's role line (ROLE unless given)."""
    kind: ClassVar[str] = "question"
    question: str = ""
    context: tuple[ContextItem, ...] = ()
    answer: Answer = field(default_factory=Answer)
    role: str = ""
    id: str = field(default_factory=new_argument_id)

    def prompt(self, max_tokens: int) -> Prompt:
        material = self.rendered_context()
        user = (f"Material:\n{material}\n\nQuestion: {self.question}" if material else f"Question: {self.question}")
        return Prompt(f"{self.role or ROLE} {self.answer.instructions()}", user, max_tokens, self.answer.json)

    def as_dict(self) -> dict:
        return {**super().as_dict(), "question": self.question, "role": self.role}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], lookup=None) -> "Question":
        return cls(question=str(d.get("question", "")), context=tuple(ContextItem.from_dict(c) for c in d.get("context") or ()),
                   answer=Answer.from_dict(d.get("answer")), role=str(d.get("role", "") or ""),
                   id=str(d.get("id") or new_argument_id()))


@register_argument
@dataclass(frozen=True)
class Judge(Argument):
    """A higher-order argument: earlier evaluations (of one question, or of several) judged together.
    `task`: compare (which is better supported), score (each 0-10 with reasons), reconcile (one
    answer from them), free (the `question` says what to do with them)."""
    kind: ClassVar[str] = "judge"
    evaluations: tuple[Evaluation, ...] = ()
    task: str = "compare"
    question: str = ""
    context: tuple[ContextItem, ...] = ()
    answer: Answer = field(default_factory=lambda: Answer("fields", fields=("verdict", "ranking", "reasons")))
    role: str = ""
    id: str = field(default_factory=new_argument_id)

    def __post_init__(self):
        if self.task not in JUDGE_TASKS:
            raise ValueError(f"judge task {self.task!r} is not one of {', '.join(JUDGE_TASKS)}")
        if not self.evaluations:
            raise ValueError("a judge needs the evaluations it judges")

    def inputs(self) -> tuple[str, ...]:
        return tuple(e.id for e in self.evaluations)

    def task_text(self) -> str:
        if self.task == "compare":
            return ("Compare the answers: which is best supported by the material and by sound reasoning? 'verdict' "
                    "names the best answer's label, 'ranking' lists the labels best first, 'reasons' says why in a few sentences.")
        if self.task == "score":
            return ("Score each answer from 0 to 10 for support and soundness: 'verdict' names the best answer's label, "
                    "'ranking' lists the labels best first, 'reasons' gives each label's score and why.")
        if self.task == "reconcile":
            return ("Reconcile the answers into one: 'verdict' is the reconciled conclusion in a few words, 'ranking' lists "
                    "the labels by how much each contributed, 'reasons' states the reconciled answer and what was set aside.")
        return self.question or "Judge the answers as the question asks."

    def prompt(self, max_tokens: int) -> Prompt:
        answers = "\n\n".join(f"Answer {chr(65 + i)} ({e.assistant}, {e.model}" + (f", verdict {e.verdict}" if e.verdict else "")
                              + f"):\n{e.text}" for i, e in enumerate(self.evaluations))
        material = self.rendered_context()
        parts = [f"Material:\n{material}" if material else "", f"Answers:\n{answers}", f"Task: {self.task_text()}"]
        if self.task != "free" and self.question:
            parts.append(f"The question the answers were given: {self.question}")
        return Prompt(f"{self.role or JUDGE_ROLE} {self.answer.instructions()}", "\n\n".join(p for p in parts if p), max_tokens,
                      self.answer.json)

    def as_dict(self) -> dict:
        return {**super().as_dict(), "task": self.task, "question": self.question, "role": self.role,
                "evaluations": [e.id for e in self.evaluations]}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any], lookup=None) -> "Judge":
        found = []
        for e in d.get("evaluations") or ():
            if isinstance(e, Mapping):
                found.append(Evaluation.from_dict(e))
                continue
            ev = lookup(str(e)) if lookup else None
            if ev is None:
                raise LookupError(f"evaluation {e!r} is not known here")
            found.append(ev)
        return cls(evaluations=tuple(found), task=str(d.get("task", "compare")), question=str(d.get("question", "") or ""),
                   context=tuple(ContextItem.from_dict(c) for c in d.get("context") or ()),
                   answer=Answer.from_dict(d.get("answer")) if d.get("answer") else Answer("fields", fields=("verdict", "ranking", "reasons")),
                   role=str(d.get("role", "") or ""), id=str(d.get("id") or new_argument_id()))


def argument_from_dict(d: Mapping[str, Any], lookup: Callable[[str], Evaluation | None] | None = None) -> Argument:
    """An argument from its dict (a request's body, a stored row): by its kind, the plugins' kinds
    found first when it is not known yet (loading the plugins registers theirs)."""
    kind = str(d.get("kind") or "question")
    cls = ARGUMENTS.get(kind)
    if cls is None:
        from ..sources.registry import providers  # the plugins register their kinds when loaded

        providers()
        cls = ARGUMENTS.get(kind)
    if cls is None:
        raise ValueError(f"unknown argument kind {kind!r}; known: {', '.join(sorted(ARGUMENTS))}")
    return cls.from_dict(d, lookup)


# -- reading answers -------------------------------------------------------------------------------

def parse_verdict(text: str, scale: tuple[str, ...]) -> str | None:
    """The verdict an answer ends with ("Verdict: bullish"), matched to the scale however it is cased or
    punctuated; the last verdict line counts. None when the answer gives none of the scale."""
    found = re.findall(r"verdict\s*[:\-]\s*\**\s*([A-Za-z][A-Za-z0-9 _/+-]*)", text or "", flags=re.IGNORECASE)
    for candidate in reversed(found):
        c = candidate.strip().strip("*.").strip().lower()
        for s in scale:
            if c == s.lower() or c.startswith(s.lower()):
                return s
    return None


def parse_fields(text: str) -> dict:
    """The JSON object an answer holds (a code fence or prose around it allowed); {} when none."""
    raw = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, flags=re.DOTALL)
    candidates = [fence.group(1)] if fence else []
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        candidates.append(raw[start:end + 1])
    for c in candidates:
        try:
            out = json.loads(c)
        except ValueError:
            continue
        if isinstance(out, dict):
            return out
    return {}
