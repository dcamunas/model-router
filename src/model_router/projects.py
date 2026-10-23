"""Per-project tuning of where subagents start, from what happened to them in that project.

Each measured subagent carries its project (the git repository the session works in; a directory that is not
in one is no project and teaches nothing) and a signature of its prompt: sixteen numbers from its word
shingles, hashed with a salt kept in `<profile>/model-router`, so they tell two prompts alike or apart on this machine
and cannot be checked against a guessed text anywhere else.

The quality signal is a relaunch: Claude launches again, soon after the first one finished, in the same
session, a task of the same kind and risk with an alike prompt, on a higher cell (a more capable model, or the
same model with more effort). That says the router's first choice was not enough. Moving from the read-only
agent to the general one is not a relaunch of this kind: the model was not the problem.

Only launches where the router chose both the model and the effort count, and only since the project's tuning
was last reset or applied by hand. From them the start cell of a kind and risk moves in one direction on its
own: up one step, when it keeps failing (MIN_N launches, MIN_FAILS relaunched, at least RAISE_RATE) with at least
one relaunch on this machine (a raise on other installations' counts alone is only proposed). Moving down
is only proposed, after DOWN_N launches with none relaunched, to the cell it was raised from or to one at least
MIN_SAVING cheaper, and applied by hand (`router project --apply`). A tuned cell remembers the cell it
replaced and is ignored when the table no longer gives that cell. The risk floors always
hold. Tunings live in `<profile>/model-router/projects/`, never in the repository.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, TypedDict  # Any: decoded JSON (subagent records, tunings, the decision log)

from . import clock
from .catalog import MIN_SAVING, Cell, UnknownModelError, lattice, parse_cell, step_up
from .classify import DECLARATION_LINE
from .paths import home, locked, read_json, read_jsonl, replace_file

SIG_SIZE = 16
SHINGLE_WORDS = 4
SIGNATURE_HASH_BYTES = 8
ALIKE = 0.6  # share of matching signature slots from which two prompts are the same task
RELAUNCH_WINDOW = timedelta(minutes=30)  # a relaunch comes soon after the first one finished
# A start cell is failing, and goes one step up, after MIN_N launches with MIN_FAILS relaunched, RAISE_RATE of them
# at least; it is proposed one step down after DOWN_N launches with none relaunched.
MIN_N = 6
MIN_FAILS = 2
RAISE_RATE = 0.30
DOWN_N = 10
WINDOW = timedelta(weeks=13)  # evidence older than this says little about the models of today
PROJECTS_DIR = "projects"
TUNING_HASH_CHARS = 12  # enough to tell apart two repositories with the same folder name
SALT_FILE = "signature.salt"
SALT_BYTES = 32
MIN_SALT_BYTES = 16  # a shorter salt was cut short on its way to disk
SALT_ATTEMPTS = 3  # enough for one lost race with another hook and one damaged file

Bucket = tuple[str, str, str]  # (kind, risk, start cell)
Counts = dict[Bucket, dict[str, int]]  # per bucket, {"n": launches, "fails": relaunched higher}
# One move of a start cell, as `router project` shows it and the tuning file keeps it. The functional form because
# `from` is a Python keyword.
Change = TypedDict("Change", {"at": str, "what": str, "kind": str, "risk": str, "from": str, "to": str, "n": int,
                              "relaunched": int})


# ---- projects and signatures -----------------------------------------------------------------------------------


def project_of(cwd: str | None) -> str | None:
    """Return the git repository a session works in (the nearest directory up from `cwd`, resolved, that holds a
    `.git`), or None outside one: a folder of many repositories must not pool their evidence.
    """
    if not cwd:
        return None
    try:
        path = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    for candidate in [path, *path.parents]:
        if (candidate / ".git").exists():
            return str(candidate)
    return None


def install_key() -> tuple[bytes, bool]:
    """Return this installation's secret salt and whether it is kept on disk. Two hooks starting at once agree on
    one: the file appears whole and exclusively, and the loser reads the winner's. A salt that cannot be kept is
    this run's only, and then nothing that must stay stable (the shared counts' file names) uses it.
    """
    path = home() / SALT_FILE
    salt = secrets.token_bytes(SALT_BYTES)
    for _ in range(SALT_ATTEMPTS):
        try:
            kept = path.read_bytes()
            if len(kept) >= MIN_SALT_BYTES:
                return kept[:SALT_BYTES], True
            # kept aside rather than overwritten: every signature and shared file name depends on the salt, so a
            # change of salt should leave a trace someone can look at
            path.rename(path.with_name(SALT_FILE + ".bad"))
            continue
        except FileNotFoundError:
            pass
        except OSError:
            return salt, False
        try:
            _create_whole(path, salt)
        except FileExistsError:
            continue  # another process made it first: read theirs
        except OSError:
            return salt, False
        return salt, True
    return salt, False


def _create_whole(path: Path, content: bytes) -> None:
    """Create `path` holding `content`, whole and only if it does not exist yet.

    Raises:
        FileExistsError: another process created it first.
        OSError: the directory cannot be written.
    """
    temporary_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}", suffix=".tmp")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.link(temporary_path, path)  # whole and exclusive: nobody ever reads it half written
    finally:
        if temporary_path:
            Path(temporary_path).unlink(missing_ok=True)


def signature(prompt: str) -> list[int]:
    """Return the prompt's signature: for each of SIG_SIZE seeds, the smallest salted hash of its word shingles (a
    MinHash). The declaration line at the top is left out, so the same task relaunched with another risk or
    access keeps its signature.
    """
    lines = [line for line in prompt.splitlines() if line.strip()]
    if lines and DECLARATION_LINE.match(lines[0]):
        lines = lines[1:]
    words = re.findall(r"\w+", " ".join(lines).lower())
    shingles = {" ".join(words[i:i + SHINGLE_WORDS]) for i in range(max(1, len(words) - SHINGLE_WORDS + 1))}
    key = install_key()[0]
    return [min(int.from_bytes(hashlib.blake2b(f"{seed}:{shingle}".encode(), digest_size=SIGNATURE_HASH_BYTES,
                                               key=key).digest(), "big")
                for shingle in shingles) for seed in range(SIG_SIZE)]


def alike(a: list[int] | None, b: list[int] | None) -> float:
    """Return the share of matching slots of two signatures, an estimate of how alike the two prompts are."""
    if not a or not b or len(a) != len(b):
        return 0.0
    return sum(x == y for x, y in zip(a, b, strict=True)) / len(a)


def served_cell(record: dict[str, Any]) -> Cell | None:
    """Return the cell a measured subagent ran on (its served model and effort; the model's default effort when the
    event gave none), or None when the model is unknown.
    """
    if not record.get("model"):
        return None
    try:
        model = parse_cell(f"{record['model']}/low").spec  # the model, whatever its effort
        return parse_cell(f"{model.short}/{record.get('effort') or model.default_effort}")
    except (UnknownModelError, ValueError):
        return None


def higher(before: Cell, after: Cell) -> bool:
    """Say whether `after` is a step up from `before`: a more capable model, or the same model with more effort."""
    return after.tier > before.tier or (after.model == before.model and after.effort_rank > before.effort_rank)


def find_relaunch(records: list[dict[str, Any]], session: str | None, *, prompt_signature: list[int],
                  cell: Cell | None, kind: str | None, risk: str | None,
                  now: datetime | None = None) -> dict[str, Any] | None:
    """Return the finished subagent of `session` that a new launch relaunches higher, or None: the same kind and
    risk, an alike prompt, finished within RELAUNCH_WINDOW, and the new cell above the one it ran on.
    """
    if cell is None or not kind or not risk:
        return None
    moment = now or clock.now()
    for old in reversed(records):
        if old.get("session") != session or old.get("kind") != kind or old.get("risk") != risk:
            continue
        if alike(old.get("sig"), prompt_signature) < ALIKE:
            continue
        before = served_cell(old)
        finished = clock.parse_moment(old.get("at"))
        if before is None or finished is None or moment - finished > RELAUNCH_WINDOW:
            continue
        if higher(before, cell):
            return old
    return None


def relaunched_ids(decision_log: Path) -> set[str]:
    """Return the agent ids the hook's decisions log marks as relaunched higher."""
    return {str(entry["relaunch_of"]) for entry in read_jsonl(decision_log) if entry.get("relaunch_of")}


# ---- weeks -------------------------------------------------------------------------------------------------------


def window_start(now: datetime | None = None) -> datetime:
    """Return where evidence starts counting: the first whole ISO week inside WINDOW, the same here and in the
    shared counts, so both sources weigh the same weeks.
    """
    year, week, _ = ((now or clock.now()) - WINDOW).astimezone(UTC).isocalendar()
    monday = date.fromisocalendar(year, week, 1)
    return datetime(monday.year, monday.month, monday.day, tzinfo=UTC) + timedelta(weeks=1)


# ---- evidence ----------------------------------------------------------------------------------------------------


def evidence(project: str, records: list[dict[str, Any]], *, relaunched: set[str], since: str | None = None,
             now: datetime | None = None) -> Counts:
    """Count, per (kind, risk, start cell) in `project`, the subagents whose model and effort the router chose
    (`router_chose`) in the whole weeks of the last WINDOW and since `since` when given, and how many of them
    were relaunched higher.
    """
    start = max(moment for moment in (clock.parse_moment(since), window_start(now)) if moment is not None)
    out: Counts = {}
    for record in records:
        if record.get("project") != project or not record.get("routed") or not record.get("router_chose"):
            continue
        if not record.get("kind") or not record.get("risk"):
            continue
        moment = clock.parse_moment(record.get("at"))
        if moment is None or moment <= start:  # weighed already, or too old to say much about today's models
            continue
        tally = out.setdefault((str(record["kind"]), str(record["risk"]), str(record["routed"])), {"n": 0, "fails": 0})
        tally["n"] += 1
        tally["fails"] += str(record.get("agent_id")) in relaunched
    return out


def combine(*sources: Counts) -> Counts:
    """Add up evidence from several sources (this machine's, the repository's shared counts)."""
    out: Counts = {}
    for source in sources:
        for bucket, tally in source.items():
            total = out.setdefault(bucket, {"n": 0, "fails": 0})
            total["n"] += tally["n"]
            total["fails"] += tally["fails"]
    return out


def failing(tally: dict[str, int]) -> bool:
    """Say whether a bucket's launches and relaunches call for a raise."""
    return tally["n"] >= MIN_N and tally["fails"] >= MIN_FAILS and tally["fails"] / tally["n"] >= RAISE_RATE


# ---- tuning files ------------------------------------------------------------------------------------------------


def tuning_path(project: str) -> Path:
    """Return where a project's tuning lives: `<profile>/model-router/projects/<name>-<hash>.json`, never in the repo."""
    digest = hashlib.sha256(project.encode()).hexdigest()[:TUNING_HASH_CHARS]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(project).name) or "root"
    return home() / PROJECTS_DIR / f"{name}-{digest}.json"


def load_tuning(project: str | None) -> dict[str, Any]:
    """Return a project's tuning, keeping only well-formed entries: `start` maps each kind, then each risk, to
    {"cell", "base"} with cells this router knows; `changes` is the history; `since` the moment evidence counts
    from.
    """
    if not project:
        return {}
    data = read_json(tuning_path(project))
    if not data:
        return {}
    start: dict[str, dict[str, dict[str, str]]] = {}
    raw = data.get("start")
    for kind, row in (raw.items() if isinstance(raw, dict) else []):
        for risk, entry in (row.items() if isinstance(row, dict) else []):
            cell = entry.get("cell") if isinstance(entry, dict) else None
            base = entry.get("base") if isinstance(entry, dict) else None
            if _valid(cell) and (not base or _valid(base)):
                start.setdefault(str(kind), {})[str(risk)] = {"cell": str(cell), "base": str(base or "")}
    changes = data.get("changes") if isinstance(data.get("changes"), list) else []
    out = {**data, "start": start, "changes": changes}
    if not isinstance(data.get("since"), str) or clock.parse_moment(data["since"]) is None:
        out.pop("since", None)
    return out


def update_tuning(project: str, change: Callable[[dict[str, Any]], None]) -> Path:
    """Load a project's tuning, let `change` modify it, and save it, all under the projects lock."""
    path = tuning_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path.parent / "tuning"):
        tuning = load_tuning(project) or {"start": {}, "changes": []}
        change(tuning)
        tuning.update(name=Path(project).name, project=project)
        replace_file(path, json.dumps(tuning, indent=1, ensure_ascii=False) + "\n")
    return path


def reset(project: str) -> Path:
    """Drop a project's tuned starts and count evidence only from now on, so nothing comes back on its own."""
    def change(tuning: dict[str, Any]) -> None:
        tuning["start"] = {}
        tuning["since"] = clock.stamp()
        tuning["changes"].append({"at": tuning["since"], "what": "reset"})
    return update_tuning(project, change)


# ---- moves -------------------------------------------------------------------------------------------------------


def raises(counts: Counts, current: Callable[[str, str], Cell]) -> list[Change]:
    """Return a step up for every (kind, risk) whose cell in force (`current(kind, risk)`) is failing in `counts`.
    A cell that is not the one in force is history: it moves nothing.
    """
    cells = lattice()
    changes: list[Change] = []
    for (kind, risk, label), tally in sorted(counts.items()):
        if not failing(tally) or not _valid(label) or current(kind, risk).label != label:
            continue
        up = step_up(parse_cell(label), cells)
        if up is not None:
            changes.append(_change("raised", (kind, risk, label), to=up.label, tally=tally))
    return changes


def raise_failing(project: str, counts: Counts, *, current: Callable[[str, str], Cell], local: Counts) -> list[Change]:
    """Apply and return the `raises` of `counts` that have a relaunch on this machine too (in `local`, this
    machine's part of `counts`): counts from elsewhere alone never move a start on their own; `team_raises`
    lists those, for a hand to apply.
    """
    changes = [change for change in raises(counts, current) if _local_fails(local, change)]
    if changes:
        update_tuning(project, lambda tuning: _write(tuning, changes))
    return changes


def team_raises(counts: Counts, current: Callable[[str, str], Cell], *, local: Counts) -> list[Change]:
    """Return the raises `counts` supports with no relaunch on this machine: proposed, not applied."""
    return [change for change in raises(counts, current) if not _local_fails(local, change)]


def _local_fails(local: Counts, change: Change) -> bool:
    return local.get((change["kind"], change["risk"], change["from"]), {}).get("fails", 0) > 0


def propose_lower(project: str, counts: Counts, *, floors: dict[str, str],
                  current: Callable[[str, str], Cell]) -> list[Change]:
    """Return the cheaper starts the evidence supports, without applying them: for the cell in force of a (kind,
    risk) with DOWN_N launches or more and none relaunched, the cell it was raised from (when the last change
    there was a raise), else the next one down the ladder if it costs at least MIN_SAVING less; never below the
    risk's floor, never to a cell whose own evidence is failing.
    """
    tuning = load_tuning(project)
    cells = lattice()
    out = []
    for (kind, risk, label), tally in sorted(counts.items()):
        if tally["n"] < DOWN_N or tally["fails"] or not _valid(label) or current(kind, risk).label != label:
            continue
        raised_from = _raised_from(tuning, (kind, risk, label))
        down = parse_cell(raised_from) if raised_from else cheaper_below(parse_cell(label), cells)
        if down is None:
            continue
        floor = floors.get(risk)
        if floor and down.capability < parse_cell(floor).capability:
            continue
        if failing(counts.get((kind, risk, down.label), {"n": 0, "fails": 0})):
            continue
        out.append(_change("lowered", (kind, risk, label), to=down.label, tally=tally))
    return out


def apply(project: str, changes: list[Change]) -> Path:
    """Write proposed `changes` into the project's tuning, and count evidence from now on."""
    def change(tuning: dict[str, Any]) -> None:
        _write(tuning, changes)
        tuning["since"] = clock.stamp()
    return update_tuning(project, change)


def cheaper_below(cell: Cell, cells: list[Cell]) -> Cell | None:
    """Return the most capable cell whose step up is `cell` and that costs at least MIN_SAVING less, or None."""
    below = [candidate for candidate in cells if candidate != cell and step_up(candidate, cells) == cell
             and candidate.expected_cost() <= cell.expected_cost() * (1 - MIN_SAVING)]
    return max(below, key=lambda candidate: (candidate.capability, -candidate.expected_cost())) if below else None


def _write(tuning: dict[str, Any], changes: list[Change]) -> None:
    for change in changes:
        row = tuning["start"].setdefault(change["kind"], {})
        base = row.get(change["risk"], {}).get("base") or change["from"]  # the table's cell the tuning stands in for
        row[change["risk"]] = {"cell": change["to"], "base": base}
    tuning["changes"].extend(changes)


def _raised_from(tuning: dict[str, Any], bucket: Bucket) -> str | None:
    kind, risk, label = bucket
    last = next((change for change in reversed(tuning.get("changes") or [])
                 if change.get("kind") == kind and change.get("risk") == risk), None)
    if last and last.get("what") == "raised" and last.get("to") == label and _valid(last.get("from")):
        return str(last["from"])
    return None


def _change(what: str, bucket: Bucket, *, to: str, tally: dict[str, int]) -> Change:
    kind, risk, label = bucket
    return {"at": clock.stamp(), "what": what, "kind": kind, "risk": risk, "from": label, "to": to, "n": tally["n"],
            "relaunched": tally["fails"]}


def _valid(label: object) -> bool:
    if not isinstance(label, str):
        return False
    try:
        parse_cell(label)
    except (UnknownModelError, ValueError):
        return False
    return True
