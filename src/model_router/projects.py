"""Per-project tuning of where subagents start, from what happened to them in that project.

Each measured subagent carries its project (the git root of the session's directory) and a signature of its
prompt: sixteen numbers from its word shingles, enough to tell two prompts apart or alike and nothing else, so
no prompt text is stored. When Claude launches a subagent whose prompt is like one that finished a little before
in the same session, and launches it higher (a more capable model, more effort, or the general agent after the
read-only one), the first one is taken to have failed: that is the quality signal a subagent otherwise lacks.

From those failures the tuning moves the start cell of a kind of task, per project, and only in one direction on
its own: up, when a cell keeps failing (MIN_N launches, MIN_FAILS of them relaunched, at least RAISE_RATE). Moving
down to save is only proposed, after DOWN_N launches with no relaunch, and applied by hand (`router project
--apply`). The risk floors always hold. Every change is written down in the project's file under
`~/.model-router/projects/`, never in the repository, and `router project --reset` removes it.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .catalog import Cell, lattice, parse_cell, step_up
from .classify import DECLARATION_LINE
from .paths import home

SIG_SIZE = 16
SHINGLE_WORDS = 4
ALIKE = 0.6  # share of matching signature slots from which two prompts are the same task
RELAUNCH_WINDOW = timedelta(minutes=30)  # a relaunch comes soon after the first one finished
MIN_N, MIN_FAILS, RAISE_RATE = 6, 2, 0.30
DOWN_N = 10
PROJECTS_DIR = "projects"
READER_AGENTS = ("model-router:reader", "router-reader")


def project_of(cwd: str | None) -> str | None:
    """Return the project a session works in: the nearest directory up from `cwd` with a `.git`, else `cwd`."""
    if not cwd:
        return None
    path = Path(cwd).expanduser()
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return str(candidate)
    return str(path)


def signature(prompt: str) -> list[int]:
    """Return the prompt's signature: for each of SIG_SIZE seeds, the smallest hash of its word shingles (a
    MinHash). The declaration line at the top is left out, so the same task relaunched with `access: write` or
    another risk keeps its signature.
    """
    lines = [line for line in prompt.splitlines() if line.strip()]
    if lines and DECLARATION_LINE.match(lines[0]):
        lines = lines[1:]
    words = re.findall(r"\w+", " ".join(lines).lower())
    shingles = {" ".join(words[i:i + SHINGLE_WORDS]) for i in range(max(1, len(words) - SHINGLE_WORDS + 1))}
    return [min(int.from_bytes(hashlib.blake2b(f"{seed}:{s}".encode(), digest_size=8).digest(), "big")
                for s in shingles) for seed in range(SIG_SIZE)]


def alike(a: list[int] | None, b: list[int] | None) -> float:
    """Return the share of matching slots of two signatures, an estimate of how alike the two prompts are."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


def higher(old: dict[str, Any], cell: Cell | None, agent: str) -> bool:
    """Say whether a launch on `cell` with `agent` is a step up from the finished subagent `old`: a more capable
    model, the same model with more effort, or the general agent after the read-only one.
    """
    if old.get("agent_type") in READER_AGENTS and agent not in READER_AGENTS:
        return True
    if cell is None or not old.get("model") or not old.get("effort"):
        return False
    try:
        before = parse_cell(f"{old['model']}/{old['effort']}")
    except ValueError:
        return False
    return cell.tier > before.tier or (cell.model == before.model and cell.effort_rank > before.effort_rank)


def find_relaunch(records: list[dict[str, Any]], session: str | None, sig: list[int], cell: Cell | None,
                  agent: str, now: datetime | None = None) -> dict[str, Any] | None:
    """Return the finished subagent of `session` that a new launch (`sig`, `cell`, `agent`) relaunches higher, or
    None: alike prompt, finished within RELAUNCH_WINDOW, and the new launch a step up. The latest one wins.
    """
    moment = now or datetime.now(UTC)
    for old in reversed(records):
        if old.get("session") != session or alike(old.get("sig"), sig) < ALIKE:
            continue
        try:
            finished = datetime.fromisoformat(str(old.get("at")))
        except ValueError:
            continue
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        if moment - finished <= RELAUNCH_WINDOW and higher(old, cell, agent):
            return old
    return None


# ---- evidence and tuning ---------------------------------------------------------------------------------------


def evidence(project: str, records: list[dict[str, Any]], relaunched: set[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Count, per (kind, start cell) in `project`, the subagents the hook routed and how many were relaunched
    higher (`relaunched` holds their agent ids), with the risks they ran at.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for r in records:
        if r.get("project") != project or not r.get("routed") or not r.get("kind"):
            continue
        e = out.setdefault((str(r["kind"]), str(r["routed"])), {"n": 0, "fails": 0, "risks": set()})
        e["n"] += 1
        e["fails"] += str(r.get("agent_id")) in relaunched
        if r.get("risk"):
            e["risks"].add(str(r["risk"]))
    return out


def tuning_path(project: str) -> Path:
    """Return where a project's tuning lives: `~/.model-router/projects/<name>-<hash>.json`, never in the repo."""
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(project).name) or "root"
    return home() / PROJECTS_DIR / f"{name}-{digest}.json"


def load_tuning(project: str | None) -> dict[str, Any]:
    """Return a project's tuning (`start`: kind -> risk -> cell, `changes`: the history), or an empty one."""
    if not project:
        return {}
    try:
        data = json.loads(tuning_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_tuning(project: str, tuning: dict[str, Any]) -> Path:
    """Write a project's tuning and return the file."""
    path = tuning_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    tuning.update(name=Path(project).name, project=project)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tuning, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def raise_failing(project: str, ev: dict[tuple[str, str], dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply the upward moves `ev` calls for to the project's tuning and return them: a start cell relaunched
    higher MIN_FAILS times or more, at least RAISE_RATE of MIN_N launches or more, moves one step up the ladder
    for the risks it ran at. A cell already moved away from is not moved twice.
    """
    tuning = load_tuning(project)
    start = tuning.setdefault("start", {})
    cells = lattice()
    changes = []
    for (kind, label), e in sorted(ev.items()):
        if e["n"] < MIN_N or e["fails"] < MIN_FAILS or e["fails"] / e["n"] < RAISE_RATE:
            continue
        up = step_up(parse_cell(label), cells)
        if up is None:
            continue
        for risk in sorted(e["risks"]):
            if start.get(kind, {}).get(risk) not in (None, label):
                continue  # already tuned to another cell: its own evidence decides from now on
            start.setdefault(kind, {})[risk] = up.label
            changes.append(_change("raised", kind, risk, label, up.label, e))
    if changes:
        tuning.setdefault("changes", []).extend(changes)
        save_tuning(project, tuning)
    return changes


def propose_lower(ev: dict[tuple[str, str], dict[str, Any]], floors: dict[str, str]) -> list[dict[str, Any]]:
    """Return the downward moves `ev` supports, without applying them: a start cell with DOWN_N launches or more and
    none relaunched, one step down to a cheaper cell that gives up little, never below the risk's floor.
    """
    cells = lattice()
    out = []
    for (kind, label), e in sorted(ev.items()):
        if e["n"] < DOWN_N or e["fails"]:
            continue
        down = step_back(parse_cell(label), cells)
        if down is None:
            continue
        for risk in sorted(e["risks"]):
            floor = floors.get(risk)
            if floor and down.capability < parse_cell(floor).capability:
                continue
            out.append(_change("lowered", kind, risk, label, down.label, e))
    return out


def relaunched_ids(decision_log: Path) -> set[str]:
    """Return the agent ids the hook's decisions log marks as relaunched higher."""
    try:
        lines = decision_log.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    out = set()
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("relaunch_of"):
            out.add(str(entry["relaunch_of"]))
    return out


def step_back(cell: Cell, cells: list[Cell]) -> Cell | None:
    """Return the cell one step down the ladder from `cell`: the most capable of those whose step up is `cell`, so
    lowering and raising walk the same ladder. None when nothing below climbs to it.
    """
    below = [c for c in cells if c != cell and step_up(c, cells) == cell]
    return max(below, key=lambda c: (c.capability, -c.expected_cost())) if below else None


def apply(project: str, changes: list[dict[str, Any]]) -> Path:
    """Write proposed `changes` into the project's tuning and return the file."""
    tuning = load_tuning(project)
    start = tuning.setdefault("start", {})
    for c in changes:
        start.setdefault(c["kind"], {})[c["risk"]] = c["to"]
    tuning.setdefault("changes", []).extend(changes)
    return save_tuning(project, tuning)


def _change(what: str, kind: str, risk: str, old: str, new: str, e: dict[str, Any]) -> dict[str, Any]:
    return {"at": datetime.now(UTC).isoformat(timespec="seconds"), "what": what, "kind": kind, "risk": risk,
            "from": old, "to": new, "n": e["n"], "relaunched": e["fails"]}
