"""Evidence shared through the repository, so a team learns from everyone's subagents, not only its own.

A repository opts in by holding the directory `.claude/model-router/` (`router project --share` creates it; once it
is committed, every teammate's installation takes part, unless MODEL_ROUTER_SHARE=0). Each installation adds there
one small file per closed ISO week, `<id>-<year>W<week>.json`, and never changes it afterwards: `c` counts the
subagents the router started per kind, risk and start cell and how many were relaunched higher; `a` counts the
runs of the agents committed in the repository and how many another subagent repeated (`agents.py`). Nothing else:
no prompt, signature, path, cost or time of day. The file format is in docs/claude-code.md.

The id is a keyed hash of the repository's path with this installation's secret salt, so it tells nothing and
links no two repositories. This installation's own files are never read back, since its subagents already count
from the local log. A week is written a day after it closes, so a late relaunch is in it. One installation weighs
at most FILE_CAP launches per bucket, at its own fail rate, and anyone can commit a file under a made-up id: that
is why the team's counts alone never raise a start, and why `router agents` only advises. Files are read and
written without following links, and a shared directory that is a link elsewhere is not used.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any  # Any: decoded JSON (subagent records, shared files)

from . import clock
from .agents import TeamRuns, committed_agents, latest, repeated_ids
from .catalog import UnknownModelError, parse_cell
from .clock import week_of, week_start
from .paths import home, locked, read_json, read_regular, replace_file
from .projects import (
    RELAUNCH_WINDOW,
    Bucket,
    Counts,
    install_key,
    relaunched_ids,
    served_cell,
    window_start,
)
from .task import KINDS, RISKS

SHARED_DIR = (".claude", "model-router")
SHARE_ENV = "MODEL_ROUTER_SHARE"
IDS_FILE = "shared-ids.json"  # every id this installation wrote under, per repository path, so none is read back
FORMAT = 1
FILE_CAP = 30  # launches one installation weighs per kind, risk and cell
MAX_FILE_BYTES = 16 * 1024
MAX_COUNT = 10_000
ID_BYTES = 6  # twelve hex characters: no clash in a team, nothing to recognise
IDS_KEY_CHARS = 16
SETTLE = timedelta(days=1)  # a week is written this long after it closes, so a late relaunch is in it
DELETE_MARGIN = timedelta(weeks=1)  # a machine whose clock runs ahead must not delete weeks the others still count
ENTRY_FIELDS = 2  # [launches, fails]
FILE_NAME = re.compile(r"^([0-9a-f]{12})-(\d{4})W(\d{2})\.json$")
BUCKET_KEY = re.compile(r"^([a-z]+)\|([a-z]+)\|([^|]+)$")  # kind|risk|cell
AGENT_KEY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.-]{0,63})\|([^|]+)$")  # agent|cell, an agent file's name


def shared_dir(project: str) -> Path:
    """Return the directory of shared counts in a repository."""
    return Path(project, *SHARED_DIR)


def sharing(project: str | None) -> bool:
    """Say whether a repository shares counts (it holds `.claude/model-router/`, a real directory inside it, not a
    link elsewhere) and this installation takes part.
    """
    if not project or os.environ.get(SHARE_ENV, "1") == "0":
        return False
    folder = shared_dir(project)
    try:
        return folder.is_dir() and folder.resolve() == Path(project).resolve() / Path(*SHARED_DIR)
    except OSError:
        return False


def own_id(project: str, key: bytes) -> str:
    """Return this installation's file id in a repository: a keyed hash of its path, meaningless elsewhere."""
    return hashlib.blake2b(project.encode(), digest_size=ID_BYTES, key=key).hexdigest()


def own_counts(project: str, records: list[dict[str, Any]], *, relaunched: set[str],
               now: datetime | None = None) -> dict[str, dict[str, list[int]]]:
    """Return this installation's counts per week ready to share (closed for SETTLE, inside the evidence window):
    per `kind|risk|cell`, [launches, relaunched], for the subagents whose model and effort the router chose.
    """
    moment = now or clock.now()
    first = window_start(moment)
    weeks: dict[str, dict[str, list[int]]] = {}
    for record in records:
        if record.get("project") != project or not record.get("routed") or not record.get("router_chose"):
            continue
        if record.get("kind") not in KINDS or record.get("risk") not in RISKS:
            continue
        week = _settled_week(record, first=first, now=moment)
        if week is None:
            continue
        tally = weeks.setdefault(week, {}).setdefault(f"{record['kind']}|{record['risk']}|{record['routed']}", [0, 0])
        tally[0] += 1
        tally[1] += str(record.get("agent_id")) in relaunched
    return weeks


def own_agent_counts(project: str, records: list[dict[str, Any]], *,
                     now: datetime | None = None) -> dict[str, dict[str, list[int]]] | None:
    """Return this installation's runs of the repository's agents fixed by hand, per settled week: per
    `agent|cell`, [runs, repeated]. An agent defined outside the repository (a plugin's, a profile's) never goes in.
    None when there are runs to count and git cannot say which agents the repository commits.
    """
    moment = now or clock.now()
    first = window_start(moment)
    recent = [r for r in records if r.get("project") == project and (at := clock.parse_moment(r.get("at")))
              and at >= first - RELAUNCH_WINDOW]  # a repeat starts within RELAUNCH_WINDOW of the run it repeats
    candidates = []
    for record in latest(recent):
        agent, cell = str(record.get("agent_type") or ""), served_cell(record)
        week = _settled_week(record, first=first, now=moment)
        if cell is not None and week is not None and not record.get("routed") and AGENT_KEY.match(f"{agent}|{cell.label}"):
            candidates.append((week, agent, cell.label, str(record.get("agent_id"))))
    committed = committed_agents(project) if candidates else {}
    if committed is None:
        return None
    if not committed:
        return {}
    repeated = repeated_ids(recent)
    weeks: dict[str, dict[str, list[int]]] = {}
    for week, agent, label, agent_id in candidates:
        if agent in committed:
            tally = weeks.setdefault(week, {}).setdefault(f"{agent}|{label}", [0, 0])
            tally[0] += 1
            tally[1] += agent_id in repeated
    return weeks


def _settled_week(record: dict[str, Any], *, first: datetime, now: datetime) -> str | None:
    """Return the ISO week of a record when it is inside the window and closed for SETTLE; else None."""
    at = clock.parse_moment(record.get("at"))
    if at is None or at < first:
        return None
    week = week_of(at)
    begins = week_start(week)
    if begins is None or begins + timedelta(weeks=1) + SETTLE > now:  # not closed, or closed too recently
        return None
    return week


def prune(project: str, now: datetime | None = None) -> list[Path]:
    """In a repository that shares counts, delete the week files a week past the evidence window (anyone's: they
    count for nobody, and a deletion merges cleanly) and this installation's files under ids it no longer uses;
    return what was deleted. An entry that cannot be deleted, or is not a file, is left alone.
    """
    if not sharing(project):
        return []
    key, kept = install_key()
    if not kept:
        return []
    mine = own_id(project, key)
    old_ids = remember(project, mine) - {mine}
    expired_before = window_start(now) - DELETE_MARGIN
    deleted = []
    for path in shared_dir(project).glob("*.json"):
        match = FILE_NAME.match(path.name)
        if not match or not _plain(path):
            continue
        begins = week_start(f"{match[2]}-W{match[3]}")
        if match[1] in old_ids or begins is None or begins < expired_before:
            with contextlib.suppress(OSError):
                path.unlink()
                deleted.append(path)
    return deleted


def write_own(project: str, records: list[dict[str, Any]], *, decision_log: Path,
              now: datetime | None = None) -> list[Path]:
    """In a repository that shares counts, add a file for each settled week this installation has counts for and
    has not written yet, and return the files added. A file once written is never changed, so when git cannot say
    which agents the repository commits nothing is written, and the next session tries again.
    """
    if not sharing(project):
        return []
    key, kept = install_key()
    if not kept:  # an id that would change on every run: better not to share at all
        return []
    mine = own_id(project, key)
    remember(project, mine)
    folder = shared_dir(project)
    added = []
    routed = own_counts(project, records, relaunched=relaunched_ids(decision_log), now=now)
    fixed = own_agent_counts(project, records, now=now)
    if fixed is None:
        return []
    for week in sorted(routed.keys() | fixed.keys()):
        path = folder / f"{mine}-{week.replace('-', '')}.json"
        if os.path.lexists(path):
            continue
        body: dict[str, Any] = {"v": FORMAT, "c": routed.get(week, {})}
        if week in fixed:
            body["a"] = fixed[week]
        text = json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n"
        if len(text.encode()) > MAX_FILE_BYTES:  # every reader would drop the whole file: keep the routing counts
            body.pop("a", None)
            text = json.dumps(body, sort_keys=True, separators=(",", ":")) + "\n"
        replace_file(path, text)
        added.append(path)
    return added


def pooled(project: str, *, since: str | None = None, now: datetime | None = None) -> tuple[Counts, int]:
    """Return the evidence in the other installations' files of a repository that shares counts, from the weeks
    that start inside the evidence window and after `since`, each installation capped at FILE_CAP launches per
    bucket; and how many installations gave some. Malformed files and entries are skipped.
    """
    start = max(moment for moment in (clock.parse_moment(since), window_start(now)) if moment is not None)
    per_install: dict[str, Counts] = {}
    for install, path in _others(project, start=start):
        theirs = per_install.setdefault(install, {})
        for bucket, launches, fails in _read(path):
            _add(theirs, bucket, n=launches, fails=fails)
    return _capped(per_install), sum(1 for theirs in per_install.values() if theirs)


def pooled_agents(project: str, *, keep: Callable[[str, str], bool], now: datetime | None = None) -> tuple[TeamRuns, int]:
    """Return the other installations' runs of the repository's agents fixed by hand, per (agent, cell label),
    for the pairs `keep` accepts, from the weeks inside the evidence window, each installation capped at FILE_CAP
    runs per agent and cell; and how many installations gave some. No reset applies: it is advice, not a start.
    """
    per_install: dict[str, TeamRuns] = {}
    for install, path in _others(project, start=window_start(now)):
        theirs = per_install.setdefault(install, {})
        for (agent, label), runs, repeated in _read_agents(path):
            if keep(agent, label):
                _add(theirs, (agent, label), n=runs, fails=repeated)
    return _capped(per_install), sum(1 for theirs in per_install.values() if theirs)


def _others(project: str, *, start: datetime) -> list[tuple[str, Path]]:
    """Return (installation id, file) for the other installations' week files that start at `start` or later."""
    if not sharing(project):
        return []
    key, kept = install_key()
    skip = known_ids(project) | ({own_id(project, key)} if kept else set())
    out = []
    for path in sorted(shared_dir(project).glob("*.json")):
        match = FILE_NAME.match(path.name)
        if not match or match[1] in skip:
            continue
        begins = week_start(f"{match[2]}-W{match[3]}")
        if begins is not None and begins >= start:
            out.append((match[1], path))
    return out


def _add[Key](tallies: dict[Key, dict[str, int]], bucket: Key, *, n: int, fails: int) -> None:
    # Key: (kind, risk, cell) for routing counts, (agent, cell label) for agents fixed by hand
    tally = tallies.setdefault(bucket, {"n": 0, "fails": 0})
    tally["n"] += n
    tally["fails"] += fails


def _capped[Key](per_install: dict[str, dict[Key, dict[str, int]]]) -> dict[Key, dict[str, int]]:
    """Sum the installations' tallies, each weighing at most FILE_CAP per bucket at its own fail rate."""
    out: dict[Key, dict[str, int]] = {}
    for theirs in per_install.values():
        for bucket, tally in theirs.items():
            n, fails = tally["n"], tally["fails"]
            if n > FILE_CAP:  # one installation weighs at most FILE_CAP, at its own fail rate
                n, fails = FILE_CAP, round(fails * FILE_CAP / n)
            _add(out, bucket, n=n, fails=fails)
    return out


def known_ids(project: str) -> set[str]:
    """Return the ids this installation wrote under for this repository path. Another clone or worktree of the
    same repository is another path, whose subagents this one's log does not hold, so its files are a teammate's.
    """
    return set(_all_ids().get(_ids_key(project), []))


def remember(project: str, install_id: str) -> set[str]:
    """Add `install_id` to the ids this installation has written under for this repository path, and return them
    all. The file is replaced whole, under a lock, so a reader never sees it cut short.
    """
    path = home() / IDS_FILE
    with locked(path):
        ids = _all_ids()
        row = ids.setdefault(_ids_key(project), [])
        if install_id not in row:
            row.append(install_id)
            replace_file(path, json.dumps(ids, sort_keys=True) + "\n")
        return set(row)


def _plain(path: Path) -> bool:
    """Say whether `path` is a regular file or a link (which unlink removes, never its target), not a directory."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _load(path: Path) -> dict[str, Any]:
    """Read one shared file: a regular file, not a link, at most MAX_FILE_BYTES, of this format; else empty."""
    raw = read_regular(path, MAX_FILE_BYTES)
    if raw is None:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) and data.get("v") == FORMAT else {}


def _read(path: Path) -> list[tuple[Bucket, int, int]]:
    """Return a shared file's routing counts, `c`, skipping any malformed entry."""
    counts = _load(path).get("c")
    if not isinstance(counts, dict):
        return []
    return [entry for entry in (_entry(k, v) for k, v in counts.items()) if entry is not None]


def _read_agents(path: Path) -> list[tuple[tuple[str, str], int, int]]:
    """Return a shared file's runs of agents fixed by hand, `a`, as ((agent, cell label), runs, repeated)."""
    runs = _load(path).get("a")
    out = []
    for key, value in (runs if isinstance(runs, dict) else {}).items():
        match = AGENT_KEY.match(key) if isinstance(key, str) else None
        pair = _pair(value)
        if match is None or pair is None or pair[0] < 1:  # an empty entry would only add a row with no runs
            continue
        try:
            cell = parse_cell(match[2]).label
        except (UnknownModelError, ValueError):
            continue
        out.append(((match[1], cell), *pair))
    return out


def _pair(value: object) -> tuple[int, int] | None:
    """Return [count, fails] as two ints when well formed (0 <= fails <= count <= MAX_COUNT); else None."""
    if not isinstance(value, list) or len(value) != ENTRY_FIELDS:
        return None
    count, fails = value
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (count, fails)):
        return None
    return (count, fails) if 0 <= fails <= count <= MAX_COUNT else None


def _entry(key: object, value: object) -> tuple[Bucket, int, int] | None:
    """Return one shared count as (bucket, launches, fails), or None when any part of it is not well formed."""
    match = BUCKET_KEY.match(key) if isinstance(key, str) else None
    if not match or match[1] not in KINDS or match[2] not in RISKS:
        return None
    try:
        cell = parse_cell(match[3]).label
    except (UnknownModelError, ValueError):
        return None
    pair = _pair(value)
    if pair is None:
        return None
    return (match[1], match[2], cell), *pair


def _ids_key(project: str) -> str:
    return hashlib.sha256(project.encode()).hexdigest()[:IDS_KEY_CHARS]


def _all_ids() -> dict[str, list[str]]:
    data = read_json(home() / IDS_FILE)
    return {k: [i for i in v if isinstance(i, str)] for k, v in data.items() if isinstance(v, list)}
