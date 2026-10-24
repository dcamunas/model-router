"""Claude Code hook: the router decides model and effort for subagents nobody configured, measures what they
spent, and learns per repository where each kind of task should start.

Events handled (one entry point, dispatch on `hook_event_name`):

* PreToolUse, matcher Agent: when Claude launches a general-purpose subagent without choosing a
  `model`, the hook takes kind and risk from a declaration on the first lines of the prompt
  (`risk: high`, `kind: review`), else infers them from the text, asks the Policy for the start
  cell (with the repository's tuning) and returns `hookSpecificOutput.updatedInput` with `model`
  (and `effort` when unset). The repository's floors (`.claude/model-router.json`, see `floors.py`) can
  raise the risk from what the brief says, never lower it; the start cell is never at `max` effort.
  A model over its usage limit (`limits.py`) is not available until its reset, and the notice says until
  when; a launch that names such a model is denied with that time. A launch that repeats a finished
  subagent's task higher is logged as its relaunch, the quality signal the learning uses.
* SubagentStop: reads the finished subagent's transcript and records what it spent, next to the
  decision that routed it (`subagents.py`).
* StopFailure: when a subagent or the turn dies on a usage limit, asks that model for its exact reset with
  one `claude -p` probe (a refused one costs nothing) and keeps it (`limits.py`).
* SessionStart: finishes the last session's measurements, raises the repository's start cells that
  keep failing (`projects.py`), writes and prunes this installation's shared counts when the
  repository shares them (`shared.py`), and gives Claude the context it needs to declare kind,
  risk and access, naming any model over its usage limit. A raise comes with a one-line notice. Outside the
  router's own sessions it also writes, once, the native settings it builds on into the profile's settings.json
  (`natives.py`), and says so in one line.

Cross-check rule: a review subagent whose risk calls for a full check (see VERIFY_BY_RISK)
never runs on the model that serves the main chat. The chat's model is read from the last
assistant entry of the session transcript (`transcript_path` in the event); when it is unknown
the rule does nothing. This is the only verification the orchestrator itself gets: it has no
hook that can change its own model.

Rules: explicit beats automatic. A `model` chosen by Claude is kept, unless its usage limit is spent. A custom agent
(`.claude/agents/<name>.md` in the repository, else `agents/<name>.md` in the profile) whose frontmatter sets no model,
or `inherit`, gets `model` (and `effort` unless its frontmatter sets one) and keeps its type; one with a fixed model is
skipped (its frontmatter is its routing), and so are built-in agents, plugin agents and forks (the Agent tool
ignores model and effort for them). Apart from that one denial the hook never blocks: on any error it
exits 0 with one line on stderr.

Environment: MODEL_ROUTER_HOOK=0 disables it; MODEL_ROUTER_HOOK_LOG=0 stops the decisions log
(<profile>/model-router/hook.jsonl), the measurements and with them all learning; MODEL_ROUTER_HOOK_QUIET=1
hides the one-line notices in the UI; MODEL_ROUTER_SHARE=0 keeps this installation out of shared counts.
MODEL_ROUTER_CHILD=1 marks the sessions the router starts itself, where the hook never probes a limit.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any  # Any: hook events and log records are decoded JSON

from . import clock, floors, limits, natives
from .agents import unpinned
from .catalog import MODELS, SHORT_TO_ID, Cell, lattice, parse_cell, worker_cell
from .classify import classify_rules, read_only
from .paths import DECISION_LOG, HOOK_ENV, append_jsonl, home, is_child, logging_on, quiet, read_config, read_jsonl
from .policy import VERIFY_BY_RISK, Policy
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
from .shared import pooled, prune, write_own
from .subagents import LOG_FILE as SUBAGENT_LOG
from .subagents import finalize as finalize_subagents
from .subagents import find_decision, load_recent, prompt_hash, read_transcript
from .subagents import load as load_subagents
from .subagents import record as subagent_record
from .task import Task

ROUTED_SUBAGENTS = frozenset({"", "general-purpose", "claude"})
PLUGIN_NAME = "model-router"
PLUGIN_AGENT = Path("agents") / "reader.md"  # inside the plugin's root
TRANSCRIPT_TAIL_BYTES = 262_144  # enough for the last few assistant entries of a long session

SESSION_CONTEXT = (
    "Model router active (hooks on the Agent tool): when you launch a general-purpose subagent without `model`, "
    "the hook sets `model` and `effort` from the task's kind and risk, and can send it to a read-only agent that "
    "carries far less context per turn. Put on the first line of the prompt `kind: review, risk: high, access: "
    "read`: the risk is the damage a wrong result does (low, medium, high or critical, which only you know); "
    "`access: read` when the subagent only reads, `access: write` when it must edit, save, run commands or use "
    "MCP tools. Without `access: read` on that first line the subagent stays on the general agent. If the read-only "
    "agent says it cannot do a task, relaunch it with `access: write`. Do not set `model` yourself unless the user asks or the "
    "task is high risk. A critical review goes to a model other than the one serving this chat. If a subagent's "
    "result fails, relaunch it on the next model up with the reason in the brief. Do not launch a subagent "
    "for what you would do in two or three steps, or for work that depends on this conversation and does not fit "
    "in a short brief: a subagent costs the brief and the reading of its answer. The `router` skill has the rest: "
    "checking and relaunching by risk, limits and floors."
)


def main_model(transcript_path: str | None) -> str | None:
    """Return the short name of the model serving the main chat, read from the last non-sidechain
    assistant entry of the session transcript; None when there is no transcript or it cannot be read.
    """
    if not transcript_path:
        return None
    for entry in reversed(read_jsonl(Path(transcript_path), tail_bytes=TRANSCRIPT_TAIL_BYTES)):
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        message = entry.get("message")
        model_id = str(message.get("model") or "") if isinstance(message, dict) else ""
        if not model_id:
            continue  # an assistant entry without a model (or of an odd shape) says nothing; look further back
        if model_id in MODELS:
            return MODELS[model_id].short
        return next((spec.short for spec in MODELS.values() if spec.short in model_id), None)
    return None


def decide(tool_input: dict[str, Any], chat_model: str | None = None, *,
           cwd: str | None = None) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Compute what the hook would change for one Agent call: (updated input or None, decision record).
    `chat_model` is the short name of the model serving the main chat, for the cross-check rule.
    """
    subagent = str(tool_input.get("subagent_type") or "")
    if tool_input.get("model") and (subagent in ROUTED_SUBAGENTS or ":" not in subagent):
        return None, {"skipped": "model chosen explicitly", "model": tool_input["model"]}
    project = project_of(cwd)
    # Claude Code reads .claude/agents/ in the working folder whether or not it is a git repository
    custom = None if subagent in ROUTED_SUBAGENTS else unpinned(subagent, project or cwd)
    if subagent not in ROUTED_SUBAGENTS and custom is None:
        return None, {"skipped": f"subagent_type={subagent}"}
    prompt = str(tool_input.get("prompt") or "")
    if not prompt.strip():
        return None, {"skipped": "empty prompt"}
    description = str(tool_input.get("description") or "")
    cls = classify_rules(prompt, description=description)
    claude = lattice()  # Claude models only: the Agent tool runs nothing else, and the router routes nothing else
    raised = floors.apply(cls.risk, floors.head(prompt, description), floors=floors.load(project))
    risk = raised.risk
    policy = Policy(cells=claude, tuning=load_tuning(project))
    plan = policy.plan(Task(prompt=prompt, kind=cls.kind, risk=risk))
    cell, cross = _cross_review(worker_cell(plan.start, [c.model for c in claude]), kind=cls.kind, risk=risk,
                                chat_model=chat_model, policy=policy)
    cc = cell.claude_code()
    updated = dict(tool_input)
    updated["model"] = cc["model"]
    fields = ["model"]
    if not tool_input.get("effort") and not (custom or {}).get("effort"):
        updated["effort"] = cc["effort"]
        fields.append("effort")
    record = {
        "kind": cls.kind, "risk": risk, "cell": cell.label, "set": fields, "why": cls.reason,
        "prompt_hash": prompt_hash(prompt), "project": project,
        # model and effort both the router's, on the table's cell: evidence about its tables (a cross-checked review
        # runs another cell than the start, so its outcome says nothing about the start)
        # a custom agent brings its own prompt and tools, so its outcome says nothing about the tables either
        "router_chose": "effort" in fields and not cross and custom is None,
    }
    if cross:
        record["cross"] = cross
    record.update(_notes(cls.risk, raised))
    if custom is not None:  # its own agent: never renamed, never sent to the read-only agent
        record["agent"] = subagent
        return updated, record
    lean = lean_agent()
    safe, why = read_only(prompt, description)
    if lean is not None and safe:
        updated["subagent_type"] = lean
        fields.append("subagent_type")
        record["agent"] = lean
    record["lean"] = why if lean is not None else "no lean agent installed or lean off"
    return updated, record


def _notes(declared: str, raised: floors.Raised) -> dict[str, Any]:
    """Return what a decision record adds about the repository's floors and the models over their usage limit."""
    notes: dict[str, Any] = {}
    if raised.rule is not None:
        notes["floor"] = f"{declared} -> {raised.risk}: {raised.rule.label}"
        notes["floor_match"] = raised.rule.match
    if raised.unsearched:
        notes["floor_unsearched"] = list(raised.unsearched)
    held = {name: limit for name, limit in limits.active().items() if name != limits.ALL_MODELS}
    if held:
        notes["limited"] = "; ".join(f"{name} limited until {limit.when()}" for name, limit in sorted(held.items()))
    return notes


def _cross_review(cell: Cell, *, kind: str, risk: str, chat_model: str | None, policy: Policy) -> tuple[Cell, str | None]:
    """Return the cell a review runs on, and why it moved: a review that gets a full judge (critical) does not run
    on the chat's own model when a Claude model at least as capable is there to take it.
    """
    if kind != "review" or VERIFY_BY_RISK[risk] != "full" or cell.claude_code()["model"] != chat_model:
        return cell, None
    other = policy.verifier_for(cell)
    if not other.spec.claude or other.claude_code()["model"] == chat_model or other.tier < cell.tier:
        return cell, None
    return other, f"{risk} review on another model than the chat ({chat_model}): {cell} -> {other}"


def lean_agent() -> str | None:
    """Return the name of the plugin's read-only agent (`model-router:reader`), or None when the hook does not run
    from the plugin or `router config --lean off` turned it off.
    """
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if read_config().get("lean") is False or not root or not (Path(root) / PLUGIN_AGENT).is_file():
        return None
    return f"{PLUGIN_NAME}:reader"


def _cwd(event: dict[str, Any]) -> str | None:
    """Return the directory the event's session works in, or None when the event does not say."""
    return str(event.get("cwd") or "") or None


def _log(event: dict[str, Any], record: dict[str, Any]) -> None:
    if not logging_on():
        return
    append_jsonl(home() / DECISION_LOG, {"at": clock.now().isoformat(timespec="seconds"),
                                         "session_id": event.get("session_id"), "cwd": event.get("cwd"), **record})


def _measure(event: dict[str, Any]) -> None:
    """Record what a finished subagent spent (see `subagents.py`) next to the hook's decision on its prompt; a
    transcript that cannot be read, or a log that cannot be written, is skipped quietly.
    """
    if not logging_on() or not event.get("agent_transcript_path"):
        return
    try:
        usage = read_transcript(Path(str(event["agent_transcript_path"])))
        decision = find_decision(home() / DECISION_LOG, usage.prompt, session=event.get("session_id"))
        append_jsonl(home() / SUBAGENT_LOG, subagent_record(event, usage, decision=decision))
    except OSError as exc:
        sys.stderr.write(f"router-hook: subagent not measured: {exc}\n")


def _relaunch_of(event: dict[str, Any], tool_input: dict[str, Any], *, updated: dict[str, Any] | None,
                 record: dict[str, Any]) -> str | None:
    """Return the agent id of the finished subagent this launch repeats higher (see `projects.find_relaunch`), or
    None. The new launch's cell is the model and effort it will really run on: the hook's, or Claude's own.
    """
    prompt = str(tool_input.get("prompt") or "")
    if not prompt.strip() or not logging_on():
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
        cls = classify_rules(prompt, description=str(tool_input.get("description") or ""))
        kind, risk = cls.kind, cls.risk
    found = find_relaunch(load_recent(home() / SUBAGENT_LOG), event.get("session_id"),
                          prompt_signature=signature(prompt), cell=cell, kind=kind, risk=risk)
    return str(found["agent_id"]) if found and found.get("agent_id") else None


def _tune(event: dict[str, Any]) -> list[str]:
    """Raise the start cells in force that keep failing in the session's project (`projects.raise_failing`), on
    the evidence since the project's last reset or applied change (this machine's, plus the other installations'
    counts when the repository shares them), and return one line per change; nothing is ever lowered here.
    """
    project = project_of(_cwd(event))
    if not project or not logging_on():
        return []
    try:
        tuning = load_tuning(project)
        since = tuning.get("since")
        mine = evidence(project, load_subagents(home() / SUBAGENT_LOG),
                        relaunched=relaunched_ids(home() / DECISION_LOG), since=since)
        changes = raise_failing(project, combine(mine, pooled(project, since=since)[0]),
                                current=start_in_force(tuning), local=mine)
    except Exception as exc:  # noqa: BLE001 - tuning is a side task: it must never cost the session its context
        sys.stderr.write(f"router-hook: tuning skipped: {type(exc).__name__}: {exc}\n")
        return []
    return [f"{c['kind']} ({c['risk']}) in {Path(project).name} now starts on {c['to']}, not {c['from']}: "
            f"{c['relaunched']} of {c['n']} relaunched higher" for c in changes]


def _share(event: dict[str, Any]) -> None:
    """Prune the repository's shared counts and add this installation's settled weeks, when it shares them."""
    project = project_of(_cwd(event))
    if not project or not logging_on():
        return
    try:
        prune(project)
        write_own(project, load_subagents(home() / SUBAGENT_LOG), decision_log=home() / DECISION_LOG)
    except Exception as exc:  # noqa: BLE001 - sharing is a side task: it must not cost the session nor hide a raise
        sys.stderr.write(f"router-hook: shared counts not written: {type(exc).__name__}: {exc}\n")


def start_in_force(tuning: dict[str, Any]) -> Callable[[str, str], Cell]:
    """Return a function giving the start cell the hook would use for a kind and risk with `tuning` applied."""
    policy = Policy(cells=lattice(), tuning=tuning)
    return lambda kind, risk: policy.plan(Task(prompt="x", kind=kind, risk=risk)).start


def handle(event: dict[str, Any]) -> dict[str, Any] | None:
    """Return the JSON the hook prints for this event, or None for silence."""
    name = event.get("hook_event_name")
    if name == "SessionStart":
        return _session_start(event)
    if name == "SubagentStop":
        _measure(event)
        return None
    if name == "StopFailure":
        return _stop_failure(event)
    if name != "PreToolUse" or event.get("tool_name") != "Agent":
        return None
    return _pre_agent(event)


def _session_start(event: dict[str, Any]) -> dict[str, Any]:
    """Finish the last measurements, raise the project's failing starts, update the shared counts, and give Claude
    the session context.
    """
    with contextlib.suppress(OSError):
        finalize_subagents(home() / SUBAGENT_LOG)  # the last session's subagents, now that their transcripts settled
    context = SESSION_CONTEXT + _limits_context()
    out: dict[str, Any] = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}
    raised = _tune(event)
    _share(event)
    notices = ["router: " + "; ".join(raised) + " (`router project` shows why; `--reset` undoes it)"] if raised else []
    notices += _natives()
    if notices and not quiet():
        out["systemMessage"] = "\n".join(notices)
    return out


def _natives() -> list[str]:
    """Write the native settings the router builds on (`natives.apply`), outside its own sessions, and return the
    notice that says what was set, if anything was.
    """
    if is_child():
        return []
    try:
        notice = natives.notice(natives.apply())
    except Exception as exc:  # noqa: BLE001 - settings are a side task: they must never cost the session its context
        sys.stderr.write(f"router-hook: native settings skipped: {type(exc).__name__}: {exc}\n")
        return []
    return [notice] if notice else []


def _limits_context() -> str:
    """Return a sentence for the session context on the models the account cannot use now, or nothing."""
    held = limits.active()
    if not held:
        return ""
    if limits.ALL_MODELS in held:
        return f" The account's usage limit is spent until {held[limits.ALL_MODELS].when()}: every model is refused."
    names = "; ".join(f"{name} until {limit.when()}" for name, limit in sorted(held.items()))
    return (f" Usage limits: {names}. The router routes around them; do not set `model` to one of them, the launch "
            "would be refused.")


def _stop_failure(event: dict[str, Any]) -> dict[str, Any] | None:
    """Read a usage-limit failure of a turn or a subagent: find the model, ask it for its exact reset and keep it.
    The next launches go elsewhere until then.
    """
    if "rate_limit" not in str(event.get("error") or "") or is_child():
        return None  # in a session the router started (its own probe, say), asking again would never end
    text = str(event.get("last_assistant_message") or event.get("error_details") or "")
    limit = limits.learn(text, models={s.short: s.id for s in MODELS.values() if s.claude})
    if limit is None or quiet():
        return None
    who = "every model" if limit.model == limits.ALL_MODELS else limit.model
    return {"systemMessage": f"router: {who} limited until {limit.when()} ({limit.window}); subagents go to the next "
                             "model until then"}


def _limited_explicit(tool_input: dict[str, Any]) -> dict[str, Any] | None:
    """Refuse a launch that names a model whose usage limit is spent: it would fail. Say until when, and what to do."""
    wanted = str(tool_input.get("model") or "").lower().split("[")[0]  # `fable[1m]` is Fable too
    if wanted in MODELS:
        wanted = MODELS[wanted].short  # a full id, `claude-fable-5-1`, names the same model as `fable`
    limit = limits.active().get(wanted) if wanted else None
    if limit is None:
        return None
    reason = (f"{wanted} is over its usage limit until {limit.when()}. Launch again without `model` (the router "
              "picks an available one) or with another model.")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": reason}}


def _pre_agent(event: dict[str, Any]) -> dict[str, Any] | None:
    """Route one Agent launch, note whether it relaunches an earlier subagent higher, and log the decision."""
    tool_input = event.get("tool_input") or {}
    refused = _limited_explicit(tool_input)
    if refused is not None:
        return refused
    updated, record = decide(tool_input, main_model(event.get("transcript_path")), cwd=_cwd(event))
    try:
        relaunch = _relaunch_of(event, tool_input, updated=updated, record=record)
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
    if not quiet():
        out["systemMessage"] = f"router: {record['cell']} ({record['kind']}/{record['risk']})" + (
            " · cross-checked against the chat" if record.get("cross") else "") + (
            f" · {record['limited']}" if record.get("limited") else "")
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
