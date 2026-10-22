"""Claude Code hook: the router decides model and effort for subagents nobody configured.

Events handled (one entry point, dispatch on `hook_event_name`):

* PreToolUse, matcher Agent: when Claude launches a general-purpose subagent without choosing a
  `model`, the hook takes kind and risk from a declaration in the description or the prompt
  (`risk: high`, `kind: review`), else infers them from the text, asks the Policy for the start
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

import contextlib
import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .catalog import MODELS, SHORT_TO_ID, Cell, lattice, parse_cell, worker_cell
from .classify import classify_rules, read_only
from .feedback import FeedbackStore
from .paths import HOOK_ENV, HOOK_LOG_ENV, HOOK_QUIET_ENV, LEAN_ENV, home, read_config
from .policy import DEFAULT_JUDGE, VERIFY_BY_RISK, Policy
from .projects import (
    combine,
    evidence,
    find_relaunch,
    load_tuning,
    project_of,
    raise_failing,
    relaunched_ids,
    signature,
)
from .shared import pooled, write_own
from .subagents import LOG_FILE as SUBAGENT_LOG
from .subagents import append as append_subagent
from .subagents import finalize as finalize_subagents
from .subagents import find_decision, load_recent, prompt_hash, read_transcript
from .subagents import load as load_subagents
from .subagents import record as subagent_record
from .task import Task

ROUTED_SUBAGENTS = frozenset({"", "general-purpose", "claude"})
PLUGIN_NAME = "model-router"
PLUGIN_AGENT = Path("src") / "model_router" / "claude_code" / "agents" / "reader.md"  # inside the plugin's root
PROFILE_AGENT = "router-reader"  # the name `router install` gives the copy in the profile's agents directory
PROMPT_HEAD_CHARS = 60
TRANSCRIPT_TAIL_BYTES = 262_144  # enough for the last few assistant entries of a long session

SESSION_CONTEXT = (
    "Model router active (hooks on the Agent tool): when you launch a general-purpose subagent without `model`, "
    "the hook sets `model` and `effort` from the task's kind and risk, and can send it to a read-only agent that "
    "carries far less context per turn. Put on the first line of the prompt `kind: review, risk: high, access: "
    "read`: the risk is the damage a wrong result does (low, medium, high or critical, which only you know); "
    "`access: read` when the subagent only reads, `access: write` when it must edit, save, run commands or use "
    "MCP tools. Without `access: read` on that first line the subagent stays on the general agent. If the read-only "
    "agent says it cannot do a task, relaunch it with `access: write`. Do not set `model` yourself unless the user asks or the "
    "task is high risk. A critical review goes to a model other than the one serving this chat. Text-only "
    "subtasks with no tools (classify, extract, summarise, draft) are cheaper through `router run \"...\"` than "
    "as a subagent. The `router` skill has the rest: verifying and escalating by risk."
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


def decide(tool_input: dict[str, Any], chat_model: str | None = None,
           cwd: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
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
    cls = classify_rules(f"{prompt}\n{description}")
    claude = lattice()  # Claude models only: the Agent tool runs nothing else, and the router routes nothing else
    feedback = FeedbackStore(home() / "feedback.json")
    project = project_of(cwd)
    tuning = load_tuning(project)
    try:
        policy = Policy(cells=claude, feedback=feedback, tuning=tuning)
    except ValueError:  # a judge setting this router cannot resolve: route anyway, `router doctor` reports it
        policy = Policy(cells=claude, feedback=feedback, judge=parse_cell(DEFAULT_JUDGE), tuning=tuning)
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
        "prompt_hash": prompt_hash(prompt), "project": project,
        # model and effort both the router's, on the table's cell: evidence about its tables (a cross-checked review
        # runs another cell than the start, so its outcome says nothing about the start)
        "router_chose": "effort" in fields and not cross,
    }
    if cross:
        record["cross"] = cross
    lean = lean_agent(cwd)
    safe, why = read_only(prompt, description)
    if lean is not None and safe:
        updated["subagent_type"] = lean
        fields.append("subagent_type")
        record["agent"] = lean
    record["lean"] = why if lean is not None else "no lean agent installed or lean off"
    return updated, record


def lean_agent(cwd: str | None = None) -> str | None:
    """Return the name of the read-only agent this installation has, or None (switched off, or not installed): the
    plugin's own (`model-router:reader`, when the hook runs from the plugin), else the one `router install` copied
    into the project (`<cwd>/.claude/agents`) or the profile (`router-reader`). MODEL_ROUTER_LEAN=0 or `router
    config --lean off` turn it off.
    """
    if os.environ.get(LEAN_ENV) == "0" or read_config().get("lean") is False:
        return None
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if root and (Path(root) / PLUGIN_AGENT).is_file():
        return f"{PLUGIN_NAME}:reader"
    profile = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser()
    places = [Path(cwd) / ".claude" / "agents"] if cwd else []
    if any((place / f"{PROFILE_AGENT}.md").is_file() for place in [*places, profile / "agents"]):
        return PROFILE_AGENT
    return None


def _log(event: dict[str, Any], record: dict[str, Any]) -> None:
    if os.environ.get(HOOK_LOG_ENV, "1") == "0":
        return
    path = home() / "hook.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "session_id": event.get("session_id"),
            "cwd": event.get("cwd"), **record}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def _measure(event: dict[str, Any]) -> None:
    """Record what a finished subagent spent (see `subagents.py`) next to the hook's decision on its prompt; a
    transcript that cannot be read, or a log that cannot be written, is skipped quietly.
    """
    if os.environ.get(HOOK_LOG_ENV, "1") == "0" or not event.get("agent_transcript_path"):
        return
    try:
        usage = read_transcript(Path(str(event["agent_transcript_path"])))
        decision = find_decision(home() / "hook.jsonl", usage.prompt, event.get("session_id"))
        append_subagent(home() / SUBAGENT_LOG, subagent_record(event, usage, decision))
    except OSError as exc:
        sys.stderr.write(f"router-hook: subagent not measured: {exc}\n")


def _relaunch_of(event: dict[str, Any], tool_input: dict[str, Any], updated: dict[str, Any] | None,
                 record: dict[str, Any]) -> str | None:
    """Return the agent id of the finished subagent this launch repeats higher (see `projects.find_relaunch`), or
    None. The new launch's cell is the model and effort it will really run on: the hook's, or Claude's own.
    """
    prompt = str(tool_input.get("prompt") or "")
    if not prompt.strip() or os.environ.get(HOOK_LOG_ENV, "1") == "0":
        return None
    final = updated or tool_input
    spec = MODELS.get(SHORT_TO_ID.get(str(final.get("model") or ""), str(final.get("model") or "")))
    if spec is None:  # no model given, or an alias the catalogue does not know: no cell to compare
        return None
    try:
        cell = parse_cell(f"{spec.short}/{final.get('effort') or spec.default_effort}")
    except ValueError:
        return None
    kind, risk = record.get("kind"), record.get("risk")
    if not kind or not risk:  # a launch the hook left alone: its kind and risk as the hook would read them
        cls = classify_rules(f"{prompt}\n{tool_input.get('description') or ''}")
        kind, risk = cls.kind, cls.risk
    found = find_relaunch(load_recent(home() / SUBAGENT_LOG), event.get("session_id"), signature(prompt), cell,
                          kind, risk)
    return str(found["agent_id"]) if found and found.get("agent_id") else None


def _tune(event: dict[str, Any]) -> list[str]:
    """Raise the start cells in force that keep failing in the session's project (`projects.raise_failing`), on
    the evidence since the project's last reset or applied change (this machine's, plus the other installations'
    counts when the repository shares them), write this installation's shared counts, and return one line per
    change; nothing is ever lowered here.
    """
    project = project_of(str(event.get("cwd") or "") or None)
    if not project or os.environ.get(HOOK_LOG_ENV, "1") == "0":
        return []
    try:
        tuning = load_tuning(project)
        records = load_subagents(home() / SUBAGENT_LOG)
        mine = evidence(project, records, relaunched_ids(home() / "hook.jsonl"), tuning.get("since"))
        changes = raise_failing(project, combine(mine, pooled(project, tuning.get("since"))[0]),
                                start_in_force(tuning), local=mine)
        write_own(project, records, home() / "hook.jsonl")  # only in a repository that shares, closed weeks only
    except Exception as exc:  # noqa: BLE001 - tuning is a side task: it must never cost the session its context
        sys.stderr.write(f"router-hook: tuning skipped: {type(exc).__name__}: {exc}\n")
        return []
    return [f"{c['kind']} ({c['risk']}) in {Path(project).name} now starts on {c['to']}, not {c['from']}: "
            f"{c['relaunched']} of {c['n']} relaunched higher" for c in changes]


def start_in_force(tuning: dict[str, Any]) -> Callable[[str, str], Cell]:
    """Return a function giving the start cell the hook would use for a kind and risk with `tuning` applied."""
    policy = Policy(cells=lattice(), feedback=FeedbackStore(home() / "feedback.json"), tuning=tuning)
    return lambda kind, risk: policy.plan(Task(prompt="x", kind=kind, risk=risk)).start


def handle(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the JSON the hook prints for this event, or None for silence."""
    name = event.get("hook_event_name")
    if name == "SessionStart":
        return _session_start(event)
    if name == "SubagentStop":
        _measure(event)
        return None
    if name != "PreToolUse" or event.get("tool_name") != "Agent":
        return None
    return _pre_agent(event)


def _session_start(event: dict[str, Any]) -> dict[str, Any]:
    """Finish the last measurements, raise the project's failing starts, and give Claude the session context."""
    with contextlib.suppress(OSError):
        finalize_subagents(home() / SUBAGENT_LOG)  # the last session's subagents, now that their transcripts settled
    out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": SESSION_CONTEXT}}
    raised = _tune(event)
    if raised and os.environ.get(HOOK_QUIET_ENV) != "1":
        out["systemMessage"] = "router: " + "; ".join(raised) + " (`router project` shows why; `--reset` undoes it)"
    return out


def _pre_agent(event: dict[str, Any]) -> dict[str, Any] | None:
    """Route one Agent launch, note whether it relaunches an earlier subagent higher, and log the decision."""
    tool_input = event.get("tool_input") or {}
    updated, record = decide(tool_input, main_model(event.get("transcript_path")), str(event.get("cwd") or "") or None)
    try:
        relaunch = _relaunch_of(event, tool_input, updated, record)
    except OSError:  # learning is a side task: it never costs the routing
        relaunch = None
    if relaunch is not None:
        record["relaunch_of"] = relaunch
        if updated is None:  # an explicit model, say: not routed, but the relaunch is the first one's verdict
            with contextlib.suppress(OSError):
                _log(event, {**record, "prompt_hash": prompt_hash(str(tool_input.get("prompt") or ""))})
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
        event = json.load(sys.stdin)
        out = handle(event)
        if out is not None:
            sys.stdout.write(json.dumps(out, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - a hook must never block Claude
        sys.stderr.write(f"router-hook: {type(exc).__name__}: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
