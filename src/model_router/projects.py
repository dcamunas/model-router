"""Per-project tuning of where subagents start, from what happened to them in that project.

Each measured subagent carries its project (the git repository the session works in; a directory that is not
in one is no project and teaches nothing) and a signature of its prompt: sixteen numbers from its word
shingles, hashed with a salt kept in `~/.model-router`, so they tell two prompts alike or apart on this machine
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
LOWER_SAVING cheaper, and applied by hand (`router project --apply`). A tuned cell remembers the cell it
replaced and is ignored when the table or the calibration no longer gives that cell. The risk floors always
hold. Tunings live in `~/.model-router/projects/`, never in the repository.
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
from typing import Any

from .catalog import Cell, UnknownModelError, lattice, parse_cell, step_up
from .classify import DECLARATION_LINE
from .paths import home, locked

SIG_SIZE = 16
SHINGLE_WORDS = 4
ALIKE = 0.6  # share of matching signature slots from which two prompts are the same task
RELAUNCH_WINDOW = timedelta(minutes=30)  # a relaunch comes soon after the first one finished
MIN_N, MIN_FAILS, RAISE_RATE = 6, 2, 0.30
DOWN_N = 10
WINDOW = timedelta(weeks=13)  # evidence older than this says little about the models of today
LOWER_SAVING = 0.20  # a proposed cheaper start must cost at least this much less, unless it is the cell raised from
PROJECTS_DIR = "projects"
SALT_FILE = "signature.salt"


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
    one: the file is created exclusively and the loser reads the winner's. A salt that cannot be kept is this run's
    only, and then nothing that must stay stable (the shared counts' file names) uses it.
    """
    path = home() / SALT_FILE
    salt = secrets.token_bytes(32)
    for _ in range(3):
        try:
            kept = path.read_bytes()
            if len(kept) >= 16:
                return kept[:32], True
            path.rename(path.with_name(SALT_FILE + ".bad"))  # a damaged salt: set it aside, then make one exclusively
            continue
        except FileNotFoundError:
            pass
        except OSError:
            return salt, False
        tmp = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{SALT_FILE}", suffix=".tmp")
            with os.fdopen(fd, "wb") as f:
                f.write(salt)
            os.link(tmp, path)  # whole and exclusive: nobody ever reads a salt half written
        except FileExistsError:
            continue  # another process made it first: read theirs
        except OSError:
            return salt, False
        finally:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
        return salt, True
    return salt, False


def _salt() -> bytes:
    return install_key()[0]


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
    key = _salt()
    return [min(int.from_bytes(hashlib.blake2b(f"{seed}:{s}".encode(), digest_size=8, key=key).digest(), "big")
                for s in shingles) for seed in range(SIG_SIZE)]


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


def find_relaunch(records: list[dict[str, Any]], session: str | None, sig: list[int], cell: Cell | None,
                  kind: str | None, risk: str | None, now: datetime | None = None) -> dict[str, Any] | None:
    """Return the finished subagent of `session` that a new launch relaunches higher, or None: the same kind and
    risk, an alike prompt, finished within RELAUNCH_WINDOW, and the new cell above the one it ran on.
    """
    if cell is None or not kind or not risk:
        return None
    moment = now or datetime.now(UTC)
    for old in reversed(records):
        if old.get("session") != session or old.get("kind") != kind or old.get("risk") != risk:
            continue
        if alike(old.get("sig"), sig) < ALIKE:
            continue
        before = served_cell(old)
        finished = _when(old.get("at"))
        if before is None or finished is None or moment - finished > RELAUNCH_WINDOW:
            continue
        if higher(before, cell):
            return old
    return None


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


# ---- weeks -------------------------------------------------------------------------------------------------------


def week_of(moment: datetime) -> str:
    """Return the ISO week of a moment, as `2026-W42`."""
    year, week, _ = moment.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def week_start(label: str) -> datetime | None:
    """Return the Monday 00:00 UTC a week label (`2026-W42`) starts on, or None for a bad label."""
    m = re.fullmatch(r"(\d{4})-?W(\d{2})", label)
    if not m:
        return None
    try:
        day = date.fromisocalendar(int(m[1]), int(m[2]), 1)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def window_start(now: datetime | None = None) -> datetime:
    """Return where evidence starts counting: the first whole ISO week inside WINDOW, the same here and in the
    shared counts, so both sources weigh the same weeks.
    """
    begins = week_start(week_of((now or datetime.now(UTC)) - WINDOW))
    assert begins is not None  # week_of always gives a valid label
    return begins + timedelta(weeks=1)


# ---- evidence ----------------------------------------------------------------------------------------------------


def evidence(project: str, records: list[dict[str, Any]], relaunched: set[str],
             since: str | None = None) -> dict[tuple[str, str, str], dict[str, int]]:
    """Count, per (kind, risk, start cell) in `project`, the subagents whose model and effort the router chose
    (`router_chose`) in the whole weeks of the last WINDOW and since `since` when given, and how many of them
    were relaunched higher.
    """
    start = max(m for m in (_when(since), window_start()) if m is not None)
    out: dict[tuple[str, str, str], dict[str, int]] = {}
    for r in records:
        if r.get("project") != project or not r.get("routed") or not r.get("router_chose"):
            continue
        if not r.get("kind") or not r.get("risk"):
            continue
        moment = _when(r.get("at"))
        if moment is None or moment <= start:  # weighed already, or too old to say much about today's models
            continue
        e = out.setdefault((str(r["kind"]), str(r["risk"]), str(r["routed"])), {"n": 0, "fails": 0})
        e["n"] += 1
        e["fails"] += str(r.get("agent_id")) in relaunched
    return out


def combine(*sources: dict[tuple[str, str, str], dict[str, int]]) -> dict[tuple[str, str, str], dict[str, int]]:
    """Add up evidence from several sources (this machine's, the repository's shared counts)."""
    out: dict[tuple[str, str, str], dict[str, int]] = {}
    for source in sources:
        for key, e in source.items():
            total = out.setdefault(key, {"n": 0, "fails": 0})
            total["n"] += e["n"]
            total["fails"] += e["fails"]
    return out


def failing(e: dict[str, int]) -> bool:
    """Say whether a bucket's evidence calls for a raise."""
    return e["n"] >= MIN_N and e["fails"] >= MIN_FAILS and e["fails"] / e["n"] >= RAISE_RATE


# ---- tuning files ------------------------------------------------------------------------------------------------


def tuning_path(project: str) -> Path:
    """Return where a project's tuning lives: `~/.model-router/projects/<name>-<hash>.json`, never in the repo."""
    digest = hashlib.sha256(project.encode()).hexdigest()[:12]
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(project).name) or "root"
    return home() / PROJECTS_DIR / f"{name}-{digest}.json"


def load_tuning(project: str | None) -> dict[str, Any]:
    """Return a project's tuning, keeping only well-formed entries: `start` maps kind -> risk -> {"cell", "base"},
    with cells this router knows; `changes` is the history; `since` the moment evidence counts from.
    """
    if not project:
        return {}
    try:
        data = json.loads(tuning_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
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
    if not isinstance(data.get("since"), str) or _when(data["since"]) is None:
        out.pop("since", None)
    return out


def update_tuning(project: str, change: Callable[[dict[str, Any]], Any]) -> Path:
    """Load a project's tuning, let `change` modify it, and save it, all under the projects lock."""
    path = tuning_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path.parent / "tuning"):
        tuning = load_tuning(project) or {"start": {}, "changes": []}
        change(tuning)
        tuning.update(name=Path(project).name, project=project)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=path.name, suffix=".tmp",
                                         delete=False) as tmp:
            tmp.write(json.dumps(tuning, indent=1, ensure_ascii=False) + "\n")
        Path(tmp.name).replace(path)
    return path


def reset(project: str) -> Path:
    """Drop a project's tuned starts and count evidence only from now on, so nothing comes back on its own."""
    def change(t: dict[str, Any]) -> None:
        t["start"] = {}
        t["since"] = _now()
        t["changes"].append({"at": t["since"], "what": "reset"})
    return update_tuning(project, change)


# ---- moves -------------------------------------------------------------------------------------------------------


def raises(ev: dict[tuple[str, str, str], dict[str, int]],
           current: Callable[[str, str], Cell]) -> list[dict[str, Any]]:
    """Return a step up for every (kind, risk) whose cell in force (`current(kind, risk)`) is failing in `ev`. A
    cell that is not the one in force is history: it moves nothing.
    """
    cells = lattice()
    changes: list[dict[str, Any]] = []
    for (kind, risk, label), e in sorted(ev.items()):
        if not failing(e) or not _valid(label) or current(kind, risk).label != label:
            continue
        up = step_up(parse_cell(label), cells)
        if up is not None:
            changes.append(_change("raised", kind, risk, label, up.label, e))
    return changes


def raise_failing(project: str, ev: dict[tuple[str, str, str], dict[str, int]],
                  current: Callable[[str, str], Cell],
                  local: dict[tuple[str, str, str], dict[str, int]] | None = None) -> list[dict[str, Any]]:
    """Apply and return the `raises` of `ev`. With `local` (this machine's part of `ev`), only those with a
    relaunch here too: counts from elsewhere alone never move a start on their own (`router project` proposes
    them).
    """
    changes = [c for c in raises(ev, current) if local is None or _local_fails(local, c)]
    if changes:
        update_tuning(project, lambda t: _write(t, changes))
    return changes


def team_raises(ev: dict[tuple[str, str, str], dict[str, int]], current: Callable[[str, str], Cell],
                local: dict[tuple[str, str, str], dict[str, int]]) -> list[dict[str, Any]]:
    """Return the raises the pooled evidence supports with no relaunch on this machine: proposed, not applied."""
    return [c for c in raises(ev, current) if not _local_fails(local, c)]


def _local_fails(local: dict[tuple[str, str, str], dict[str, int]], change: dict[str, Any]) -> bool:
    return local.get((change["kind"], change["risk"], change["from"]), {}).get("fails", 0) > 0


def propose_lower(project: str, ev: dict[tuple[str, str, str], dict[str, int]], floors: dict[str, str],
                  current: Callable[[str, str], Cell]) -> list[dict[str, Any]]:
    """Return the cheaper starts the evidence supports, without applying them: for the cell in force of a (kind,
    risk) with DOWN_N launches or more and none relaunched, the cell it was raised from (when the last change
    there was a raise), else the next one down the ladder if it costs at least LOWER_SAVING less; never below the
    risk's floor, never to a cell whose own evidence is failing.
    """
    tuning = load_tuning(project)
    cells = lattice()
    out = []
    for (kind, risk, label), e in sorted(ev.items()):
        if e["n"] < DOWN_N or e["fails"] or not _valid(label) or current(kind, risk).label != label:
            continue
        raised_from = _raised_from(tuning, kind, risk, label)
        down = parse_cell(raised_from) if raised_from else _cheaper_below(parse_cell(label), cells)
        if down is None:
            continue
        floor = floors.get(risk)
        if floor and down.capability < parse_cell(floor).capability:
            continue
        if failing(ev.get((kind, risk, down.label), {"n": 0, "fails": 0})):
            continue
        out.append(_change("lowered", kind, risk, label, down.label, e))
    return out


def apply(project: str, changes: list[dict[str, Any]]) -> Path:
    """Write proposed `changes` into the project's tuning, and count evidence from now on."""
    def change(t: dict[str, Any]) -> None:
        _write(t, changes)
        t["since"] = _now()
    return update_tuning(project, change)


def _write(tuning: dict[str, Any], changes: list[dict[str, Any]]) -> None:
    for c in changes:
        row = tuning["start"].setdefault(c["kind"], {})
        base = row.get(c["risk"], {}).get("base") or c["from"]  # the table's cell the tuning stands in for
        row[c["risk"]] = {"cell": c["to"], "base": base}
    tuning["changes"].extend(changes)


def _raised_from(tuning: dict[str, Any], kind: str, risk: str, label: str) -> str | None:
    last = next((c for c in reversed(tuning.get("changes") or [])
                 if c.get("kind") == kind and c.get("risk") == risk), None)
    if last and last.get("what") == "raised" and last.get("to") == label and _valid(last.get("from")):
        return str(last["from"])
    return None


def _cheaper_below(cell: Cell, cells: list[Cell]) -> Cell | None:
    below = [c for c in cells if c != cell and step_up(c, cells) == cell
             and c.expected_cost() <= cell.expected_cost() * (1 - LOWER_SAVING)]
    return max(below, key=lambda c: (c.capability, -c.expected_cost())) if below else None


def _change(what: str, kind: str, risk: str, old: str, new: str, e: dict[str, int]) -> dict[str, Any]:
    return {"at": _now(), "what": what, "kind": kind, "risk": risk, "from": old, "to": new, "n": e["n"],
            "relaunched": e["fails"]}


def _valid(label: Any) -> bool:
    if not isinstance(label, str):
        return False
    try:
        parse_cell(label)
    except (UnknownModelError, ValueError):
        return False
    return True


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _when(text: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
