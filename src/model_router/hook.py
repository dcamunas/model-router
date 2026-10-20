"""Claude Code hook: the router decides model and effort for subagents nobody configured.

Events handled (one entry point, dispatch on `hook_event_name`):

* PreToolUse, matcher Agent: when Claude launches a general-purpose subagent without choosing a
  `model`, the hook infers kind and risk from the subagent's prompt, asks the Policy for the start
  cell and returns `hookSpecificOutput.updatedInput` with `model` (and `effort` when unset).
* SessionStart: one line of context so Claude knows the hook exists and does not fight it.

Cross-check rule: a review subagent whose risk calls for full verification (see VERIFY_BY_RISK)
never runs on the model that serves the main chat. The chat's model is read from the last
assistant entry of the session transcript (`transcript_path` in the event); when it is unknown
the rule does nothing. This is the only verification the orchestrator itself gets: it has no
hook that can change its own model.

Rules: explicit beats automatic. A `model` chosen by Claude is kept. Custom agents from
`.claude/agents/*.md` are skipped (their frontmatter is their routing), and so are forks (the Agent
tool ignores model and effort for them). The hook never blocks: on any error it exits 0 silently.

Environment: MODEL_ROUTER_HOOK=0 disables it; MODEL_ROUTER_HOOK_LOG=0 stops the decisions log
(~/.model-router/hook.jsonl); MODEL_ROUTER_HOOK_QUIET=1 hides the one-line notice in the UI.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog import MODELS, lattice, load_external, worker_cell
from .classify import classify_rules
from .feedback import FeedbackStore
from .paths import HOOK_ENV, HOOK_LOG_ENV, HOOK_QUIET_ENV, home, read_config
from .policy import VERIFY_BY_RISK, Policy
from .task import Task

ROUTED_SUBAGENTS = frozenset({"", "general-purpose", "claude"})
PROMPT_HEAD_CHARS = 60
TRANSCRIPT_TAIL_BYTES = 262_144  # enough for the last few assistant entries of a long session

SESSION_CONTEXT = (
    "Model router active (PreToolUse hook on the Agent tool): when you launch a general-purpose subagent "
    "without `model`, the hook sets `model` and `effort` from the kind and the risk it infers from the "
    "subagent's prompt. Do not set `model` yourself unless the user asks for it or the task is high risk; "
    "if you set it, the hook keeps it. A critical-risk review goes to a model other than the one serving "
    "this chat (a blind cross-check): if you designed something critical here, launch a review subagent "
    "with the brief and the design and let the hook choose. Text-only subtasks (classify, extract, "
    "summarise, draft, review a pasted text) go through `router run \"...\"` instead of a subagent: a "
    "hundred times less context, verified and recorded. `router judge \"<task>\" --answer FILE` verifies a "
    "subagent's result with the plan's judges and records it. To verify and escalate by risk, use the `router` "
    "skill."
)


def main_model(transcript_path: str | None) -> str | None:
    """Return the short name of the model serving the main chat, read from the last non-sidechain
    assistant entry of the session transcript; None when there is no transcript or it cannot be read.
    """
    if not transcript_path:
        return None
    try:
        with Path(transcript_path).open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - TRANSCRIPT_TAIL_BYTES))
            tail = fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        if '"assistant"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:  # the first line of the tail is usually cut
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        model_id = str(message.get("model") or "") if isinstance(message, dict) else ""
        if not model_id:
            continue  # an assistant entry without a model (or of an odd shape) says nothing; look further back
        if model_id in MODELS:
            return MODELS[model_id].short
        return next((spec.short for spec in MODELS.values() if spec.short in model_id), None)
    return None


def decide(tool_input: dict[str, Any], chat_model: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Compute what the hook would change for one Agent call: (updated input or None, decision record).
    `chat_model` is the short name of the model serving the main chat, for the cross-check rule.
    """
    subagent = str(tool_input.get("subagent_type") or "")
    if subagent not in ROUTED_SUBAGENTS:
        return None, {"skipped": f"subagent_type={subagent}"}
    if tool_input.get("model"):
        return None, {"skipped": "model chosen explicitly", "model": tool_input["model"]}
    prompt = str(tool_input.get("prompt") or "")
    if not prompt.strip():
        return None, {"skipped": "empty prompt"}
    description = str(tool_input.get("description") or "")
    cls = classify_rules(f"{description}\n{prompt}")
    claude = [c for c in lattice() if c.spec.provider == "anthropic"]  # the Agent tool runs Claude models only
    if not claude:  # `--models` lists declared models only: the hook still has every Claude model to choose from
        claude = lattice([m for m, spec in MODELS.items() if spec.provider == "anthropic"])
    policy = Policy(cells=claude, feedback=FeedbackStore(home() / "feedback.json"))
    plan = policy.plan(Task(prompt=prompt, kind=cls.kind, risk=cls.risk))
    cell, cross = worker_cell(plan.start, [c.model for c in claude]), None
    if cls.kind == "review" and VERIFY_BY_RISK[cls.risk] == "full" and cell.claude_code()["model"] == chat_model:
        other = policy.verifier_for(cell, "full")
        if (other is not None and other.spec.provider == "anthropic" and other.claude_code()["model"] != chat_model
                and other.tier >= cell.tier):
            cross = f"{cls.risk} review on another model than the chat ({chat_model}): {cell} -> {other}"
            cell = other
    cc = cell.claude_code()
    updated = dict(tool_input)
    updated["model"] = cc["model"]
    fields = ["model"]
    if not tool_input.get("effort"):
        updated["effort"] = cc["effort"]
        fields.append("effort")
    record = {
        "kind": cls.kind, "risk": cls.risk, "cell": cell.label, "set": fields, "why": cls.reason,
        "subagent": subagent or "general-purpose", "prompt_head": prompt.strip()[:PROMPT_HEAD_CHARS],
    }
    if cross:
        record["cross"] = cross
    return updated, record


def _log(event: dict[str, Any], record: dict[str, Any]) -> None:
    if os.environ.get(HOOK_LOG_ENV, "1") == "0":
        return
    path = home() / "hook.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "session_id": event.get("session_id"),
            "cwd": event.get("cwd"), **record}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def handle(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the JSON the hook prints for this event, or None for silence."""
    name = event.get("hook_event_name")
    if name == "SessionStart":
        return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": SESSION_CONTEXT}}
    if name != "PreToolUse" or event.get("tool_name") != "Agent":
        return None
    updated, record = decide(event.get("tool_input") or {}, main_model(event.get("transcript_path")))
    if updated is None:
        return None
    try:
        _log(event, record)
    except OSError as exc:  # a log that cannot be written must not cost the routing
        sys.stderr.write(f"router-hook: decision not logged: {exc}\n")
    out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": updated}}
    if os.environ.get(HOOK_QUIET_ENV) != "1":
        out["systemMessage"] = f"router: {record['cell']} ({record['kind']}/{record['risk']})" + (
            " · cross-checked against the chat" if record.get("cross") else "")
    return out


def main() -> int:
    """Run the hook on the event JSON in stdin and print the answer; exit 0 whatever happens."""
    if os.environ.get(HOOK_ENV, "1") == "0":
        return 0
    try:
        load_external(read_config())  # a configured external judge must resolve, or Policy() would not build
        event = json.load(sys.stdin)
        out = handle(event)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - a hook must never block Claude
        sys.stderr.write(f"router-hook: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
