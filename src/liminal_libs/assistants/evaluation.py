"""What an assistant answered (plugin API 2.0, assistants): an Evaluation of one argument by one
assistant's model, with what it cost, kept in the main file (storage/evaluations.py) and reused as
the input of a higher-order argument (arguments.Judge) or as context for another question."""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping

STOPS = ("end_turn", "length", "refusal", "error")  # how an answer ended: whole, cut at the token cap, declined, failed


def new_evaluation_id() -> str:
    return f"ev_{secrets.token_hex(6)}"


@dataclass(frozen=True)
class Usage:
    """The tokens an answer took (the vendor's count): what the caps and the cost are reckoned on."""
    input_tokens: int = 0
    output_tokens: int = 0  # reasoning tokens included where the vendor bills them as output
    cached_tokens: int = 0  # of the input, read from the vendor's cache (billed less)
    reasoning_tokens: int = 0  # of the output, spent thinking (shown; billed as output)
    requests: int = 1

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict:
        return {"inputTokens": self.input_tokens, "outputTokens": self.output_tokens, "cachedTokens": self.cached_tokens,
                "reasoningTokens": self.reasoning_tokens, "requests": self.requests}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any] | None) -> "Usage":
        d = d or {}
        return cls(int(d.get("inputTokens", d.get("input_tokens", 0)) or 0), int(d.get("outputTokens", d.get("output_tokens", 0)) or 0),
                   int(d.get("cachedTokens", d.get("cached_tokens", 0)) or 0),
                   int(d.get("reasoningTokens", d.get("reasoning_tokens", 0)) or 0), int(d.get("requests", 1) or 1))


@dataclass(frozen=True)
class Evaluation:
    """One assistant's answer to one argument: the text, the verdict and fields parsed from it (the
    argument's answer form), what it built on (`inputs`: earlier evaluations, a judge's), the
    context it was given (the items' ids), what it cost, and how it ended (`stop`)."""
    id: str
    argument_id: str
    kind: str  # the argument's kind: question, judge ...
    assistant: str  # the plugin's name: claude, grok, codex
    provider: str  # the vendor: Anthropic, xAI, OpenAI
    model: str
    text: str = ""
    verdict: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)
    inputs: tuple[str, ...] = ()  # evaluation ids it built on
    context: tuple[str, ...] = ()  # context item ids it was given
    usage: Usage = field(default_factory=Usage)
    cost_usd: float | None = None  # the vendor's own figure where it sends one, else the plugin's price table's
    at: str = ""  # ISO, UTC
    seconds: float = 0.0
    request_id: str = ""  # the vendor's request id, for its support
    stop: str = "end_turn"
    error: str = ""  # why it failed, scrubbed of the key and the account

    @property
    def ok(self) -> bool:
        return self.stop in ("end_turn", "length") and not self.error

    def as_dict(self) -> dict:
        return {"id": self.id, "argumentId": self.argument_id, "kind": self.kind, "assistant": self.assistant,
                "provider": self.provider, "model": self.model, "text": self.text, "verdict": self.verdict,
                "fields": dict(self.fields), "inputs": list(self.inputs), "context": list(self.context),
                "usage": self.usage.as_dict(), "costUsd": self.cost_usd, "at": self.at, "seconds": self.seconds,
                "requestId": self.request_id, "stop": self.stop, "error": self.error}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Evaluation":
        return cls(str(d["id"]), str(d.get("argumentId", d.get("argument_id", ""))), str(d.get("kind", "")),
                   str(d.get("assistant", "")), str(d.get("provider", "")), str(d.get("model", "")), str(d.get("text", "") or ""),
                   d.get("verdict"), dict(d.get("fields") or {}), tuple(d.get("inputs") or ()), tuple(d.get("context") or ()),
                   Usage.from_dict(d.get("usage")), d.get("costUsd", d.get("cost_usd")), str(d.get("at", "") or ""),
                   float(d.get("seconds", 0.0) or 0.0), str(d.get("requestId", d.get("request_id", "")) or ""),
                   str(d.get("stop", "end_turn") or "end_turn"), str(d.get("error", "") or ""))

    def as_context(self):
        """This evaluation as a context item of another argument (arguments.ContextItem)."""
        from .arguments import ContextItem

        head = f"{self.assistant} ({self.model})" + (f", verdict {self.verdict}" if self.verdict else "")
        return ContextItem("evaluation", self.text, id=self.id, title=head, data=self.as_dict())

    def with_(self, **changes) -> "Evaluation":
        return replace(self, **changes)


def now_iso(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
