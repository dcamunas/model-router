"""What each Claude Code subagent really spent. When a subagent finishes, Claude Code hands the hook its
transcript (`SubagentStop`, `agent_transcript_path`); this module reads it and records, per subagent, the model
that served it, its turns, the tokens of every turn (input, output, cache reads and writes), the list-price cost,
the largest context it carried, its tool calls and its duration, next to the routing decision the hook took for
it. `router stats` reads the records back.

The bill of a subagent is mostly its context read again on every turn (cache reads): a review on Opus with 36
turns and 59 tool calls read 3.6M cached tokens and wrote 0.14M, 1.43 USD at list price for its context alone.
So the numbers that matter are turns, peak context and cache reads per kind of task.

Output tokens are a lower bound: every transcript entry keeps the usage snapshot taken when the message began
to stream, so the output it reports is a few tokens per message. Input, cache reads and cache writes are exact.
The recorded cost is therefore a floor (`cost_usd`), and `context_cost_usd` is the exact part, the context.

A transcript repeats a message's usage on every entry that carries one of its content blocks: usage is counted
once per message id. And when `SubagentStop` fires, the subagent's last message is not in the transcript yet: the
hook writes a first reading and the transcript's path, and `finalize` reads it again once it has stopped
changing (`router stats` and the next session's start call it).
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import tempfile
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ._fmt import money
from .catalog import MODELS, Usage, cost_usd
from .classify import classify_rules
from .projects import project_of, signature

LOG_FILE = "subagents.jsonl"
PROMPT_HASH_CHARS = 16
SETTLED_SECONDS = 30.0  # a transcript untouched for this long is complete


def prompt_hash(prompt: str) -> str:
    """Return the short hash that ties a subagent's transcript to the hook's decision on its prompt."""
    return hashlib.sha256(prompt.strip().encode("utf-8")).hexdigest()[:PROMPT_HASH_CHARS]


@dataclass
class SubagentUsage:
    """What one subagent spent, read from its transcript."""

    model: str = ""
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    peak_context: int = 0  # the largest prompt of a single turn: input + cache read + cache write
    tool_calls: int = 0
    seconds: float = 0.0
    prompt: str = ""
    by_model: dict[str, int] = field(default_factory=dict)  # output tokens per model, to name the one that served

    @property
    def usage(self) -> Usage:
        return Usage(self.input_tokens, self.output_tokens, self.cache_read_tokens, self.cache_write_tokens)

    @property
    def tokens(self) -> int:
        """Every token the subagent's turns processed or wrote."""
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def cost(self) -> float | None:
        """Return the list-price cost in USD, or None when the model is not in the catalogue."""
        return cost_usd(self.model, self.usage) if self.model in MODELS else None


def read_transcript(path: Path) -> SubagentUsage:
    """Sum a subagent transcript's usage, one count per message id, with its first prompt, its tool calls and the
    time between its first and last entries. An unreadable line is skipped.
    """
    out = SubagentUsage()
    messages: dict[str, tuple[str, dict[str, Any]]] = {}
    moments: list[datetime] = []
    for entry in _entries(path):
        moment = _when(entry.get("timestamp"))
        if moment is not None:
            moments.append(moment)
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user" and not out.prompt:
            out.prompt = _text(message.get("content"))
        if message.get("role") == "assistant":
            out.tool_calls += _tool_calls(message.get("content"))
            if isinstance(message.get("usage"), dict):
                messages[str(message.get("id") or len(messages))] = (str(message.get("model") or ""), message["usage"])
    for model, usage in messages.values():
        _add_turn(out, model, usage)
    if out.by_model:
        out.model = max(out.by_model, key=lambda m: out.by_model[m])
    if moments:
        out.seconds = max(0.0, (moments[-1] - moments[0]).total_seconds())
    return out


def _entries(path: Path) -> list[dict[str, Any]]:
    entries = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    return entries


def _tool_calls(content: Any) -> int:
    return sum(1 for c in content if isinstance(c, dict) and c.get("type") == "tool_use") if isinstance(content, list) else 0


def _add_turn(out: SubagentUsage, model: str, usage: dict[str, Any]) -> None:
    u = {k: int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                             "cache_creation_input_tokens")}
    out.turns += 1
    out.input_tokens += u["input_tokens"]
    out.output_tokens += u["output_tokens"]
    out.cache_read_tokens += u["cache_read_input_tokens"]
    out.cache_write_tokens += u["cache_creation_input_tokens"]
    out.peak_context = max(out.peak_context, u["input_tokens"] + u["cache_read_input_tokens"]
                           + u["cache_creation_input_tokens"])
    out.by_model[model] = out.by_model.get(model, 0) + u["output_tokens"] + 1


def record(event: dict[str, Any], usage: SubagentUsage, decision: dict[str, Any] | None) -> dict[str, Any]:
    """Build the record of one finished subagent: who it was, what it spent, and how it was routed (the hook's
    decision on its prompt, or the kind and risk read from the prompt when the hook left it alone).
    """
    cls = classify_rules(usage.prompt) if usage.prompt else None
    cost = usage.cost()
    spec = MODELS.get(usage.model)
    effort = event.get("effort")
    return {
        "at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "session": event.get("session_id"),
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
        "model": spec.short if spec else usage.model,
        "effort": effort.get("level") if isinstance(effort, dict) else effort,
        "routed": decision.get("cell") if decision else None,
        "kind": (decision or {}).get("kind") or (cls.kind if cls else None),
        "risk": (decision or {}).get("risk") or (cls.risk if cls else None),
        "prompt_hash": prompt_hash(usage.prompt) if usage.prompt else None,
        "sig": signature(usage.prompt) if usage.prompt else None,
        "project": project_of(str(event.get("cwd") or "") or None),
        **{k: v for k, v in asdict(usage).items() if k not in ("prompt", "by_model", "model")},
        "tokens": usage.tokens,
        "cost_usd": round(cost, 6) if cost is not None else None,
        "context_cost_usd": _context_cost(usage),
        "transcript": str(event.get("agent_transcript_path") or ""),
        "final": False,
    }


def _context_cost(usage: SubagentUsage) -> float | None:
    """Return the exact part of the cost: input, cache reads and cache writes, without the output (a lower bound)."""
    if usage.model not in MODELS:
        return None
    return round(cost_usd(usage.model, Usage(usage.input_tokens, 0, usage.cache_read_tokens, usage.cache_write_tokens)), 6)


@contextlib.contextmanager
def locked(log: Path) -> Iterator[None]:
    """Hold an exclusive lock on `log`'s lock file: the hook's appends and `finalize`'s rewrite never interleave."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def append(log: Path, line: dict[str, Any]) -> None:
    """Append one record to `log` under the lock."""
    with locked(log), log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def finalize(log: Path, *, settled: float = SETTLED_SECONDS, now: float | None = None) -> int:
    """Read again the transcript of every record not yet final whose transcript has stopped changing (untouched
    for `settled` seconds), update its numbers and mark it final; a transcript that is gone leaves the first
    reading as final. Rewrite the log only when something changed; return how many records were finalised.
    """
    if not log.exists():
        return 0
    with locked(log):
        records = load(log)
        moment = time.time() if now is None else now
        changed = sum(_settle(r, moment, settled) for r in records)
        if changed:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=log.parent, prefix=log.name, suffix=".tmp",
                                             delete=False) as tmp:
                tmp.write("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))
            Path(tmp.name).replace(log)
    return changed


def _settle(r: dict[str, Any], moment: float, settled: float) -> bool:
    """Read one record's transcript again when it has settled; True when the record changed. A transcript that is
    gone, is not a file or cannot be read leaves the first reading as final.
    """
    if r.get("final", True):
        return False
    path = Path(str(r.get("transcript") or ""))
    try:
        if not path.is_file():
            raise FileNotFoundError(path)
        if moment - path.stat().st_mtime < settled:
            return False
        usage = read_transcript(path)
    except OSError:
        r["final"] = True
        return True
    cost = usage.cost()
    r.update({k: v for k, v in asdict(usage).items() if k not in ("prompt", "by_model", "model")})
    r.update(tokens=usage.tokens, cost_usd=round(cost, 6) if cost is not None else None,
             context_cost_usd=_context_cost(usage), final=True)
    return True


def find_decision(log: Path, prompt: str, session: str | None) -> dict[str, Any] | None:
    """Return the hook's latest decision on `prompt` in `session` (the decisions log, newest last), or None."""
    if not prompt:
        return None
    wanted = prompt_hash(prompt)
    found = None
    try:
        lines = log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("prompt_hash") == wanted and entry.get("session_id") in (session, None):
            found = entry
    return found


def load(path: Path) -> list[dict[str, Any]]:
    """Read the subagent records; a line that is not a JSON object is skipped."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def load_recent(path: Path, max_bytes: int = 256_000) -> list[dict[str, Any]]:
    """Read only the end of the subagent records (the last `max_bytes`, a few hundred records): what a relaunch can
    point at, without reading months of history on every launch. A cut first line is skipped.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            chunk = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = chunk.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(c.get("text") or "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


def _when(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def summary_rows(records: list[dict[str, Any]]) -> list[list[object]]:
    """Group subagent records by agent type and the model and effort that served them; one row each, most spent
    first: how many, mean turns, mean peak context, mean cache reads, and the mean and total cost at list price
    (formatted; a floor, since output tokens are a lower bound).
    """
    latest = {(r.get("session"), r.get("agent_id")) if r.get("agent_id") else (None, i): r for i, r in enumerate(records)}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in latest.values():  # a resumed subagent, or one measured by two installs, counts once: its last record
        key = (str(r.get("agent_type") or "?"), f"{r.get('model') or '?'}/{r.get('effort') or '?'}")
        groups.setdefault(key, []).append(r)
    rows = []
    for (agent, cell), rs in groups.items():
        n = len(rs)
        costs = [float(r["cost_usd"]) for r in rs if r.get("cost_usd") is not None]
        total = sum(costs)
        rows.append([agent, cell, n, round(sum(int(r.get("turns") or 0) for r in rs) / n, 1),
                     _k(sum(int(r.get("peak_context") or 0) for r in rs) / n),
                     _k(sum(int(r.get("cache_read_tokens") or 0) for r in rs) / n),
                     money(total / len(costs)) if costs else "?", money(total), total])
    rows.sort(key=lambda row: -float(str(row[-1])))
    return [row[:-1] for row in rows]


def _k(n: float) -> str:
    return f"{n / 1000:.1f}K" if n >= 1000 else str(round(n))
