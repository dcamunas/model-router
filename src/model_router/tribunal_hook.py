"""Claude Code hook of the `tribunal` plugin: final QA of the code a turn changed, by a Claude judge and a Codex
judge blind to each other, and an arbiter on a split (see `qa.py`).

Events, one entry point, dispatch on `hook_event_name`:

* UserPromptSubmit: forget the session's last turn; in a git repository, snapshot the working tree and keep the
  prompt as the brief and the settings as they stand.
* Stop: when the tree changed since that snapshot (and not only in documentation), judge the change against
  the brief. A pass is reported in one line. A fail goes back to Claude as the reason to keep working, with the
  judges' issues, until the turn has had `rounds` reviews (2 by default); the last fail is reported to the user
  and Claude stops. A judge that cannot be reached (a quota, a network) never blocks.
* SessionEnd: forget the session's turn.

Another session can edit the same repository during a turn; its edits are then in this turn's diff. When the
transcript of a session whose turn overlapped this one shows an editing tool used since this turn began, a fail
is reported and not sent back.

The judges' own sessions run with MODEL_ROUTER_TRIBUNAL=0, which this hook honours, so they are never judged.
MODEL_ROUTER_TRIBUNAL=0 in your environment turns the plugin off too. The hook never fails a session: on any
error it exits 0 with a note on stderr.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .catalog import load_external
from .hook import main_model
from .paths import home, read_config
from .qa import (
    CHILD_ENV,
    MAX_BRIEF_CHARS,
    changed_files,
    chat_cell,
    diff_text,
    files_now,
    only_docs,
    raw_settings,
    repo_root,
    resolve,
    run_qa,
    snapshot,
)

STATE_FILE = "tribunal-state.json"
MAX_SESSIONS = 50  # sessions kept in the state file, the most recent ones
MAX_REASON_CHARS = 4000
STALE_TURN = timedelta(hours=6)  # an open turn older than this is taken for one that never reached its Stop
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit", "Bash"})  # Bash may edit: counted as editing


@contextlib.contextmanager
def _state() -> Iterator[dict[str, Any]]:
    """Yield the per-session state, locked against other sessions, and save it on exit."""
    path = home() / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with (home() / (STATE_FILE + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        yield data
        if len(data) > MAX_SESSIONS:
            for key in sorted(data, key=lambda k: data[k].get("at", ""))[: len(data) - MAX_SESSIONS]:
                data.pop(key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)


def on_prompt(event: dict[str, Any]) -> None:
    """Start a turn: forget the last one, then, in a git repository, snapshot the working tree and keep the prompt
    as the brief and the settings as they stand now, before the work can change them.
    """
    session = str(event.get("session_id") or "")
    if not session:
        return
    with _state() as state:
        state.pop(session, None)  # a turn that cannot be set up must not be judged against the last one
    repo = repo_root(Path(str(event.get("cwd") or ".")))
    if repo is None:
        return
    load_external(read_config())  # a declared Codex model in the settings must resolve
    brief = str(event.get("prompt") or "")[:MAX_BRIEF_CHARS]
    raw = raw_settings(repo, brief)
    tree = snapshot(repo)
    now = _now()
    with _state() as state:
        overlaps = [k for k, t in state.items()
                    if t.get("repo") == str(repo) and t.get("open") and not _stale(t.get("at", ""))]
        for k in overlaps:
            state[k].setdefault("overlaps", []).append(session)
        state[session] = {"repo": str(repo), "base": tree, "brief": brief, "settings": raw, "rounds": 0,
                          "judged": None, "at": now, "open": True, "overlaps": overlaps,
                          "transcript": str(event.get("transcript_path") or "")}


def on_stop(event: dict[str, Any]) -> dict[str, Any] | None:
    """Judge what the turn changed; return the hook's answer (a block with the issues, a one-line note) or None.
    The turn is closed whatever happens, unless it is sent back to Claude.
    """
    session = str(event.get("session_id") or "")
    with _state() as state:
        turn = dict(state.get(session) or {})
        others = {k: dict(state[k]) for k in turn.get("overlaps", []) if k in state}
    if not turn:
        return None
    blocked = False
    try:
        out = _judge_turn(event, turn, others)
        blocked = bool(out and out.get("decision") == "block")
        return out
    finally:
        if not blocked:
            _close(session, turn["base"])


def _judge_turn(event: dict[str, Any], turn: dict[str, Any], others: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    session = str(event.get("session_id") or "")
    repo = Path(turn["repo"])
    load_external(read_config())
    chat = main_model(event.get("transcript_path"))
    s = resolve(turn.get("settings") or raw_settings(repo, turn.get("brief", "")), chat)
    if s.mode == "off":
        return None
    current = snapshot(repo)
    files = changed_files(repo, turn["base"], current)
    if not files or only_docs(files) or current == turn.get("judged") or turn["rounds"] >= s.rounds:
        return None  # nothing new to judge, or the last review's fail was already reported
    result = run_qa(repo, turn.get("brief", ""), diff_text(repo, turn["base"], current), s, author=chat_cell(chat),
                    current_files=files_now(repo, current, files))
    rounds = turn["rounds"] + 1
    with _state() as state:
        if state.get(session, {}).get("base") == turn["base"]:  # not a turn that started while this one was judged
            state[session].update(rounds=rounds, judged=current)
    attempt = result.attempts[0]
    who = s.describe()
    if result.status == "solved":
        return {"systemMessage": f"tribunal: pass ({who}), {len(files)} files"}
    if result.status == "error":
        heard = " | ".join(f"{v.cell}: {'pass' if v.passed else 'fail'}: {v.reason}" for v in attempt.verdicts)
        return {"systemMessage": f"tribunal: could not judge ({who}): {attempt.reason[:300]}"
                                 + (f". Heard from {heard[:600]}" if heard else "")}
    editors = [k for k, t in others.items() if _edited_since(t.get("transcript", ""), turn["at"])]
    if editors:  # another session edited the repository during this turn: its edits are in this diff too
        return {"systemMessage": f"tribunal: fail ({who}), not sent back because another session edited "
                                 f"{repo.name} during this turn and its edits are part of this change: "
                                 f"{attempt.reason[:600]}"}
    if rounds < s.rounds:
        text = (f"The tribunal ({who}) reviewed the change you made in this turn and failed it "
                f"(review {rounds} of {s.rounds}). Fix what is right in these findings, say briefly what you changed "
                f"and what you disagree with and why, then finish:\n\n{attempt.reason}")
        return {"decision": "block", "reason": text[:MAX_REASON_CHARS]}
    return {"systemMessage": f"tribunal: fail after {rounds} reviews ({who}): {attempt.reason[:600]}"}


def on_session_end(event: dict[str, Any]) -> None:
    """Forget a session that ended: its turn is no longer open."""
    session = str(event.get("session_id") or "")
    with _state() as state:
        state.pop(session, None)


def _close(session: str, base: str) -> None:
    """Mark the session's turn as finished (it no longer edits the repository), unless a newer turn replaced it."""
    with _state() as state:
        if state.get(session, {}).get("base") == base:
            state[session]["open"] = False


def _edited_since(transcript: str, since: str) -> bool:
    """Say whether the session transcript at `transcript` used an editing tool (EDIT_TOOLS) at or after `since`. An
    unreadable transcript counts as edited: when in doubt, a fail is reported rather than sent back.
    """
    try:
        lines = Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return True
    start = _when(since)
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        when = _when(str(entry.get("timestamp", ""))) if isinstance(entry, dict) else None
        if when is None or start is None or when < start or not isinstance(content, list):
            continue
        if any(isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") in EDIT_TOOLS for c in content):
            return True
    return False


def _when(text: str) -> datetime | None:
    """Parse an ISO timestamp (a transcript's `...Z` or this hook's `+00:00`) as an aware datetime, or None."""
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _stale(at: str) -> bool:
    moment = _when(at)
    return moment is None or datetime.now(UTC) - moment > STALE_TURN


def handle(event: dict[str, Any]) -> dict[str, Any] | None:
    """Dispatch one hook event; return the JSON to print, or None for nothing."""
    if os.environ.get(CHILD_ENV) == "0":
        return None
    name = event.get("hook_event_name")
    if name == "UserPromptSubmit":
        on_prompt(event)
    elif name == "Stop":
        return on_stop(event)
    elif name == "SessionEnd":
        on_session_end(event)
    return None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def main() -> None:
    """Entry point `router-tribunal-hook`: read the event on stdin, answer on stdout, never fail the session."""
    try:
        event = json.load(sys.stdin)
        out = handle(event) if isinstance(event, dict) else None
    except Exception as exc:  # noqa: BLE001 - a hook must never break the session
        print(f"router-tribunal-hook: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(0)
    if out is not None:
        print(json.dumps(out))
    sys.exit(0)
