"""The AI assistants' evaluations (plugin API 2.0, assistants): every answer an assistant gave to an
argument, with the argument, what it cost and what it built on, in the main file's `evaluations`
table; read by the Plugins view (an assistant's usage and its last answers), the `ai` commands and a
judge (its inputs by id)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

_COLS = ("id", "at", "assistant", "provider", "model", "kind", "argument_id", "argument", "inputs", "text", "verdict", "fields",
         "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens", "cost_usd", "seconds", "request_id", "stop",
         "error")


def _row(r) -> dict:
    d = dict(zip(_COLS, r))
    d["argument"] = json.loads(d["argument"]) if d["argument"] else None
    d["inputs"] = json.loads(d["inputs"]) if d["inputs"] else []
    d["fields"] = json.loads(d["fields"]) if d["fields"] else {}
    return d


def _as_evaluation(d: dict):
    from ..assistants.evaluation import Evaluation, Usage

    return Evaluation(d["id"], d["argument_id"], d["kind"], d["assistant"], d["provider"], d["model"], d["text"] or "",
                      d["verdict"], d["fields"], tuple(d["inputs"]), tuple((d["argument"] or {}).get("context_ids") or ()),
                      Usage(d["input_tokens"] or 0, d["output_tokens"] or 0, d["cached_tokens"] or 0, d["reasoning_tokens"] or 0),
                      d["cost_usd"], d["at"], d["seconds"] or 0.0, d["request_id"] or "", d["stop"] or "end_turn", d["error"] or "")


class EvaluationsStore:
    """Store methods: the assistants' evaluations (see the module's docstring)."""

    def save_evaluation(self, argument: dict, evaluation) -> None:
        """An evaluation kept with its argument (the argument's dict, its context items' ids with it)."""
        arg = {**argument, "context_ids": list(evaluation.context)}
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO evaluations (id, at, assistant, provider, model, kind, argument_id, argument, inputs, text, "
                "verdict, fields, input_tokens, output_tokens, cached_tokens, reasoning_tokens, cost_usd, seconds, request_id, "
                "stop, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (evaluation.id, evaluation.at, evaluation.assistant, evaluation.provider, evaluation.model, evaluation.kind,
                 evaluation.argument_id, json.dumps(arg, default=str), json.dumps(list(evaluation.inputs)), evaluation.text,
                 evaluation.verdict, json.dumps(dict(evaluation.fields), default=str), evaluation.usage.input_tokens,
                 evaluation.usage.output_tokens, evaluation.usage.cached_tokens, evaluation.usage.reasoning_tokens,
                 evaluation.cost_usd, evaluation.seconds, evaluation.request_id, evaluation.stop, evaluation.error))

    def evaluations(self, assistant: str | None = None, kind: str | None = None, since: str | None = None, limit: int = 50,
                    argument_id: str | None = None) -> list[dict]:
        """The evaluations kept, newest first: by assistant, kind, since an ISO moment, of one argument."""
        where, args = [], []
        for col, val in (("assistant", assistant), ("kind", kind), ("argument_id", argument_id)):
            if val:
                where.append(f"{col} = ?")
                args.append(val)
        if since:
            where.append("at >= ?")
            args.append(since)
        sql = f"SELECT {', '.join(_COLS)} FROM evaluations" + (" WHERE " + " AND ".join(where) if where else "") \
            + " ORDER BY at DESC, rowid DESC LIMIT ?"
        return [_row(r) for r in self.conn.execute(sql, (*args, int(limit))).fetchall()]

    def evaluation(self, eid: str):
        """One evaluation by id, as an Evaluation (a judge's input); None when unknown."""
        r = self.conn.execute(f"SELECT {', '.join(_COLS)} FROM evaluations WHERE id = ?", (eid,)).fetchone()
        return _as_evaluation(_row(r)) if r else None

    def evaluation_row(self, eid: str) -> dict | None:
        r = self.conn.execute(f"SELECT {', '.join(_COLS)} FROM evaluations WHERE id = ?", (eid,)).fetchone()
        return _row(r) if r else None

    def evaluation_chain(self, eid: str, depth: int = 5) -> list[dict]:
        """An evaluation and what it built on, breadth first (a judge's inputs, theirs ...), `depth` deep."""
        out, seen, frontier = [], set(), [eid]
        for _ in range(depth + 1):
            nxt = []
            for e in frontier:
                if e in seen:
                    continue
                seen.add(e)
                row = self.evaluation_row(e)
                if row is None:
                    continue
                out.append(row)
                nxt += [i for i in row["inputs"] if i not in seen]
            if not nxt:
                break
            frontier = nxt
        return out

    def evaluations_today(self, assistant: str | None = None, now: datetime | None = None) -> dict:
        """What this database's evaluations spent today (UTC): requests, tokens, cost (the machine
        ledgers count every database; the Plugins view shows both)."""
        day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
        sql = ("SELECT COUNT(*), COALESCE(SUM(input_tokens + output_tokens), 0), COALESCE(SUM(cost_usd), 0.0) FROM evaluations "
               "WHERE substr(at, 1, 10) = ?" + (" AND assistant = ?" if assistant else ""))
        n, tokens, cost = self.conn.execute(sql, (day, assistant) if assistant else (day,)).fetchone()
        return {"requests": int(n or 0), "tokens": int(tokens or 0), "costUsd": round(float(cost or 0.0), 6)}
