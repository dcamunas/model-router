"""Advice on the agents whose model and effort someone fixed by hand (`.claude/agents/*.md`, a plugin's agents).

The hook leaves those agents alone: their frontmatter decides. It still measures every one when it ends
(`subagents.py`), and that is enough to say, per agent and cell in a repository, whether the cell looks too low,
too high, or right. The signal is the same one per-project learning uses: a relaunch higher. A finished agent is
repeated when, in the same session and within RELAUNCH_WINDOW of its end, another subagent (of any type) starts on
an alike prompt on a more capable cell (`projects.higher`): whoever launched it was not satisfied. The same agent
again on the same cell is not a repeat: a loop over files with one template brief looks exactly like that.

    measuring  fewer than MIN_N runs: nothing to say yet
    raise      at least MIN_FAILS repeats and RAISE_RATE of the runs: one step up the ladder (`step_up`)
    lower      DOWN_N runs or more and none repeated: the most capable cell that costs MIN_SAVING less
               while giving up little capability (`step_down`)
    keep       anything else

In a repository that shares counts (`shared.py`), the teammates' runs of the same agent on the same cell count
too: their runs and repeats add to this machine's before the thresholds apply. Only agents committed in the
repository (`committed_agents`) are shared or read back. Turns, spend and a saving come from this machine's runs
only, since the shared files carry no tokens or cost. A cell the agent's file no longer names gets no advice:
it would point at a change already made.

It never edits an agent: the advice is for a person to apply. When the advice changes the model, the saving or
extra cost is the measured tokens repriced at the new model's list prices; a change of effort also changes how
many tokens are spent, and that is not estimated. Both sides of a saving are priced from the same tokens, so the
difference is the model's price alone.
"""

from __future__ import annotations

import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any  # Any: measured subagent records, decoded JSON

from . import clock
from .catalog import MODELS, Cell, UnknownModelError, Usage, cost_usd, parse_cell, step_down, step_up
from .paths import claude_profile, read_regular
from .projects import (
    ALIKE,
    DOWN_N,
    MIN_FAILS,
    MIN_N,
    RAISE_RATE,
    RELAUNCH_WINDOW,
    alike,
    higher,
    served_cell,
)

# Not fixed by hand: the router's own choices, Claude Code's built-in agents, the read-only agent, workflow agents.
NOT_FIXED_BY_HAND = frozenset({"", "general-purpose", "claude", "workflow-subagent", "Explore", "Plan",
                               "claude-code-guide", "statusline-setup"})
TeamRuns = dict[tuple[str, str], dict[str, int]]  # (agent, cell label) -> {"n": runs, "fails": repeated}, from teammates
AGENTS_DIR = (".claude", "agents")
REGULAR_MODES = frozenset({"100644", "100755"})  # git's modes for a plain file; 120000 is a link
GIT_SECONDS = 10.0
FRONTMATTER_FIELD = re.compile(r"^(model|effort)\s*:(.*)$", re.M)
FRONTMATTER_NAME = re.compile(r"^name\s*:(.*)$", re.M)
MAX_AGENT_BYTES = 65_536  # an agent file larger than this is not read by the hook
MAX_AGENT_FILES = 200  # at most this many files per agents folder are looked at on one launch
Fixed = tuple[str, str | None]  # the model's short name and the effort an agent file names (None: not set)


@dataclass(frozen=True)
class Advice:
    """Hold what the measurements say about one agent on one cell in one repository, and the cell they advise."""

    agent: str
    cell: Cell
    runs: int
    repeated: int
    mean_turns: float
    total_cost: float
    action: str  # measuring, raise, lower or keep
    to: Cell | None = None
    saving: float | None = None  # USD over this machine's runs; negative when the advice costs more
    team_runs: int = 0  # of `runs`, how many teammates measured (shared counts)

    def to_dict(self) -> dict[str, Any]:
        local = self.runs > self.team_runs
        return {"agent": self.agent, "cell": self.cell.label, "runs": self.runs, "team_runs": self.team_runs,
                "repeated": self.repeated,
                "mean_turns": round(self.mean_turns, 1) if local else None, "total_cost_usd": round(self.total_cost, 4),
                "action": self.action, "to": self.to.label if self.to else None,
                "saving_usd": round(self.saving, 4) if self.saving is not None else None}


def fixed_by_hand(record: dict[str, Any], project: str) -> bool:
    """Say whether a measured subagent ran on a cell someone fixed: a named agent the router did not route, and,
    when its definition is in the repository, one whose frontmatter names a model (not `inherit`).
    """
    agent = str(record.get("agent_type") or "")
    if record.get("routed") or agent in NOT_FIXED_BY_HAND or agent.endswith("reader"):
        return False
    path = agent_file(project, agent)
    if path is None:
        return True  # a plugin's or the profile's agent: its definition is elsewhere
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return True
    return frontmatter(text).get("model", "") not in ("", "inherit")


def frontmatter(text: str) -> dict[str, str]:
    """Return the `model` and `effort` an agent file's frontmatter sets, as written but without quotes or a trailing
    comment (`model: "opus"  # why` is `opus`); empty when the file has no frontmatter.
    """
    block = _frontmatter_block(text)
    if block is None:
        return {}
    return {key: _plain(value) for key, value in FRONTMATTER_FIELD.findall(block)}


def _frontmatter_block(text: str) -> str | None:
    """Return what sits between the opening `---` line and the next line that is only `---`, or None when the file
    does not open with one. A `---` inside a value (`description: strict --- no style`) does not close it.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
    return "\n".join(lines[1:end]) if end is not None else None


def _plain(value: str) -> str:
    """Return a frontmatter value as written but without quotes or a trailing comment."""
    return value.split(" #")[0].strip().strip("'\"")


def committed_agents(project: str) -> dict[str, Fixed] | None:
    """Return the agents committed in the repository that fix a model, by name, with the model and effort their
    file names now: `.claude/agents/<name>.md` in git's index as a plain file and a plain file on disk too. A link,
    an untracked or ignored file, a file in a subfolder or one that says `inherit` is left out, so an agent from a
    profile or a plugin never reaches the shared counts. None when git cannot tell (missing, refused, too slow).
    """
    folder = "/".join(AGENTS_DIR)
    try:
        listed = subprocess.run(["git", "-C", project, "ls-files", "-s", "-z", "--", folder], capture_output=True,
                                text=True, timeout=GIT_SECONDS, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    out = {}
    for entry in filter(None, listed.split("\0")):
        meta, _, path = entry.partition("\t")
        name = path.removeprefix(folder + "/").removesuffix(".md")
        if meta.split(" ")[0] not in REGULAR_MODES or "/" in name or not path.endswith(".md"):
            continue
        fixed = _fixed(Path(project) / path)
        if fixed is not None:
            out[name] = fixed
    return out


def _fixed(path: Path) -> Fixed | None:
    """Return the model and effort an agent file fixes, or None (a link, unreadable, `inherit`, an unknown model)."""
    if path.is_symlink() or not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fields = frontmatter(text)
    model = next((spec.short for spec in MODELS.values() if fields.get("model") in (spec.short, spec.id)), None)
    return (model, fields.get("effort") or None) if model else None


def names_cell(fixed: Fixed, cell: Cell) -> bool:
    """Say whether `cell` is the one an agent file now fixes (its model, and its effort when the file sets one)."""
    model, effort = fixed
    return cell.spec.short == model and effort in (None, cell.effort)


def repeated_ids(records: list[dict[str, Any]]) -> set[str]:
    """Return the agent ids of the finished subagents that another subagent repeated (see the module docstring)."""
    starts: dict[str, list[tuple[Any, Cell, dict[str, Any]]]] = defaultdict(list)  # by session: no pair across two
    for r in records:
        end, cell = clock.parse_moment(r.get("at")), served_cell(r)
        if end is not None and cell is not None and r.get("sig") and r.get("session"):
            starts[str(r["session"])].append((end - timedelta(seconds=float(r.get("seconds") or 0)), cell, r))
    out: set[str] = set()
    for r in records:
        end, cell = clock.parse_moment(r.get("at")), served_cell(r)
        if end is None or cell is None or not r.get("sig") or not r.get("agent_id") or not r.get("session"):
            continue
        for start, later_cell, later in starts.get(str(r["session"]), []):
            if (later.get("agent_id") != r["agent_id"]
                    and timedelta(0) <= start - end <= RELAUNCH_WINDOW and higher(cell, later_cell)
                    and alike(r["sig"], later["sig"]) >= ALIKE):
                out.add(str(r["agent_id"]))
                break
    return out


def advise(records: list[dict[str, Any]], project: str, *, cells: list[Cell], team: TeamRuns | None = None) -> list[Advice]:
    """Return the advice for every agent fixed by hand that ran in `project`, here or on a teammate's machine
    (`team`, from the shared counts), the most runs first.
    """
    repeated = repeated_ids([r for r in records if r.get("project") == project])
    groups: dict[tuple[str, Cell], list[dict[str, Any]]] = {}
    for r in latest(records):
        cell = served_cell(r)
        if r.get("project") == project and cell is not None and fixed_by_hand(r, project):
            groups.setdefault((str(r["agent_type"]), cell), []).append(r)
    committed = (committed_agents(project) or {}) if team or groups else {}
    theirs: dict[tuple[str, Cell], dict[str, int]] = {}
    for (agent, label), tally in (team or {}).items():
        try:
            cell = parse_cell(label)
        except (UnknownModelError, ValueError):
            continue
        if agent in committed and tally.get("n", 0) > 0:
            theirs[(agent, cell)] = tally
            groups.setdefault((agent, cell), [])
    out = [_advice(agent, cell, records=rs, cells=cells, repeated=repeated, team=theirs.get((agent, cell)))
           for (agent, cell), rs in groups.items() if agent not in committed or names_cell(committed[agent], cell)]
    return sorted(out, key=lambda a: (-a.runs, -a.total_cost))


def latest(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one record per subagent: a resumed one, or one measured twice, counts once (its last record)."""
    last: dict[object, dict[str, Any]] = {}
    for i, r in enumerate(records):
        last[(r.get("session"), r.get("agent_id")) if r.get("agent_id") else i] = r
    return list(last.values())


def _advice(agent: str, cell: Cell, *, records: list[dict[str, Any]], cells: list[Cell], repeated: set[str],
            team: dict[str, int] | None) -> Advice:
    team_runs, team_fails = (team or {}).get("n", 0), (team or {}).get("fails", 0)
    runs = len(records) + team_runs
    fails = sum(1 for r in records if str(r.get("agent_id")) in repeated) + team_fails
    total = sum(float(r.get("cost_usd") or 0.0) for r in records)
    turns = sum(int(r.get("turns") or 0) for r in records) / len(records) if records else 0.0
    base = Advice(agent, cell, runs, fails, turns, total, "keep", team_runs=team_runs)
    if runs < MIN_N:
        return _with(base, "measuring")
    if fails >= MIN_FAILS and fails / runs >= RAISE_RATE:
        up = step_up(cell, cells)
        return _with(base, "raise", to=up, records=records) if up else base
    if runs >= DOWN_N and fails == 0:
        down = step_down(cell, cells)
        return _with(base, "lower", to=down, records=records) if down else base
    return base


def _with(base: Advice, action: str, *, to: Cell | None = None, records: list[dict[str, Any]] | None = None) -> Advice:
    """Return `base` with its action, the cell it advises and, when the model changes, the measured tokens repriced."""
    saving = None
    if to is not None and records and to.model != base.cell.model:
        usages = [_usage(r) for r in records]
        saving = sum(cost_usd(base.cell.model, u) - cost_usd(to.model, u) for u in usages)
    return Advice(base.agent, base.cell, base.runs, base.repeated, base.mean_turns, base.total_cost, action, to, saving,
                  team_runs=base.team_runs)


def _usage(record: dict[str, Any]) -> Usage:
    return Usage(int(record.get("input_tokens") or 0), int(record.get("output_tokens") or 0),
                 int(record.get("cache_read_tokens") or 0), int(record.get("cache_write_tokens") or 0))


def agent_file(project: str, agent: str) -> Path | None:
    """Return the repository's definition of `agent` (`.claude/agents/<name>.md`), or None when it is not there."""
    path = Path(project, *AGENTS_DIR) / f"{agent}.md"
    return path if path.is_file() else None


def unpinned(name: str, project: str | None) -> dict[str, str] | None:
    """Return the frontmatter of the custom agent `name` when its file sets no model (or `inherit`), so the hook may
    route it; None for any other agent. Claude Code names an agent by the `name` in its frontmatter (the file name
    when it has none) and reads `.claude/agents/` in the repository before `agents/` in the profile. Built-in agents
    and plugin agents (`plugin:name`) are never routed.
    """
    if not name or name in NOT_FIXED_BY_HAND or any(c in name for c in ":/\\") or name.startswith("."):
        return None
    folders = ([Path(project, *AGENTS_DIR)] if project else []) + [claude_profile() / "agents"]
    for folder in folders:
        try:
            files = sorted(folder.glob("*.md"), key=lambda f: f.stem != name)[:MAX_AGENT_FILES]  # the likely one first
        except OSError:
            continue
        for path in files:
            raw = read_regular(path, MAX_AGENT_BYTES)  # never a link, a FIFO or a huge file a repository planted
            if raw is None:
                continue
            text = raw.decode("utf-8", errors="replace")
            declared = FRONTMATTER_NAME.search(_frontmatter_block(text) or "")
            if (_plain(declared.group(1)) if declared else path.stem) != name:
                continue
            fields = frontmatter(text)
            return fields if fields.get("model", "") in ("", "inherit") else None
    return None
