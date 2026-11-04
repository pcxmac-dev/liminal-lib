"""Context items from what the app holds (plugin API 2.0, assistants): a headline, a Federal item, the
integrator's assessment of a symbol, a plugin's report, a scheme's signal, an earlier evaluation, or
plain text, each as an arguments.ContextItem an argument is evaluated from."""
from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from .arguments import ContextItem
from .evaluation import Evaluation

MAX_ITEM_CHARS = 12_000  # one item's text, at most (a long document is cut, its end kept)


def item_from(obj: Any, kind: str | None = None, title: str = "", id: str = "") -> ContextItem:
    """One context item from what is given: a ContextItem as it is; an Evaluation as its own; a
    headline or Federal item ({title, url, published, agency ...}: the news and gov views' rows); an
    assessment ({symbol, jobs, feeds ...}: the integrator's); a plugin's Report (its lines); a
    scheme's signal ({scheme, symbol, side ...}); a mapping as JSON; anything else as text."""
    if isinstance(obj, ContextItem):
        return obj
    if isinstance(obj, Evaluation):
        return obj.as_context()
    if hasattr(obj, "lines") and hasattr(obj, "as_dict") and hasattr(obj, "capabilities"):  # a provider's Report
        return ContextItem(kind or "report", _cut("\n".join(obj.lines())), id or getattr(obj, "source", ""),
                           title or f"{getattr(obj, 'source', '')} ({getattr(obj, 'title', '')})", obj.as_dict())
    if isinstance(obj, Mapping):
        d = dict(obj)
        if kind is None:
            kind = ("headline" if "url" in d and ("title" in d or "headline" in d)
                    else "assessment" if "symbol" in d and ("jobs" in d or "offers" in d)
                    else "signal" if "scheme" in d and "symbol" in d else "evaluation" if "argumentId" in d else "data")
        if kind == "headline":
            head = str(d.get("title") or d.get("headline") or "")
            bits = [b for b in (d.get("agency") or d.get("source"), d.get("published") or d.get("at"), d.get("url")) if b]
            text = head + ("\n" + "\n".join(str(b) for b in bits) if bits else "") + ("\n" + str(d["summary"]) if d.get("summary") else "")
            return ContextItem(kind, _cut(text), id or str(d.get("id") or ""), title or head, d)
        if kind == "assessment":
            feeds = ", ".join(d.get("feeds") or ()) or "no feed"
            jobs = "; ".join(f"{j.get('plugin')}: {j.get('resolution')} bars of {j.get('market')} from {j.get('start')}"
                             + (f" to {j['end']}" if j.get("end") else "") for j in d.get("jobs") or ())
            text = f"Symbol {d.get('symbol')} ({d.get('asset') or 'asset unknown'}): the plan asks {feeds}" + (f"; jobs: {jobs}" if jobs else "") \
                + (f"; live: {', '.join(s for s, _ in d.get('live') or ())}" if d.get("live") else "") + (f"; note: {d['note']}" if d.get("note") else "")
            return ContextItem(kind, _cut(text), id or str(d.get("symbol") or ""), title or str(d.get("symbol") or ""), d)
        if kind == "evaluation":
            return Evaluation.from_dict(d).as_context()
        return ContextItem(kind, _cut(json.dumps(d, default=str, sort_keys=True)), id, title, d)
    return ContextItem(kind or "text", _cut(str(obj)), id, title)


def items_from(objs: Iterable[Any], kind: str | None = None) -> tuple[ContextItem, ...]:
    return tuple(item_from(o, kind) for o in objs)


def _cut(text: str) -> str:
    text = str(text).strip()
    return text if len(text) <= MAX_ITEM_CHARS else "[cut] ..." + text[-MAX_ITEM_CHARS:]
