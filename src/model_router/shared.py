"""Evidence shared through the repository, so a team learns from everyone's subagents, not only its own.

A repository opts in by holding the directory `.claude/model-router/` (`router project --share` creates it; once it
is committed, every teammate's installation takes part, unless MODEL_ROUTER_SHARE=0). Each installation adds there
one small file per closed ISO week, `<id>-<year>W<week>.json`, and never changes it afterwards:

    {"v":1,"c":{"review|high|opus/medium":[12,1]}}

per kind, risk and start cell, how many subagents the router started there that week and how many were relaunched
higher. Nothing else: no prompt, signature, path, name, cost or time of day. The id is a keyed hash of the
repository's path with this installation's secret salt, so it tells nothing and links no two repositories.

The counts are the same evidence `projects.evidence` counts on this machine, summed the same way. This
installation's files (under any id it ever used for this repository path) are never read back, since its subagents
already count from the local log. A week is written a day after it closes, so a late relaunch is in it, and only
weeks inside the evidence window and after the project's last reset or applied change count. One installation weighs
at most FILE_CAP launches per kind, risk and cell, scaled so its fail rate is kept, so an honest heavy user does not
drown the rest (anyone can commit files under made-up ids; that is why the team's counts alone never raise a start
on their own). Files never change once written, so branches and merges never fight over them; any installation
deletes the files that left the window. Files are read and written without following links, and a shared directory
that is a link elsewhere is not used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .catalog import UnknownModelError, parse_cell
from .paths import home, locked
from .projects import _when, install_key, relaunched_ids, week_of, week_start, window_start
from .task import KINDS, RISKS

SHARED_DIR = (".claude", "model-router")
SHARE_ENV = "MODEL_ROUTER_SHARE"
IDS_FILE = "shared-ids.json"  # every id this installation wrote under, so none of them is read back
FORMAT = 1
FILE_CAP = 30  # launches one installation weighs per kind, risk and cell
MAX_FILE_BYTES = 16 * 1024
MAX_COUNT = 10_000
SETTLE = timedelta(days=1)  # a week is written this long after it closes
FILE_NAME = re.compile(r"^([0-9a-f]{12})-(\d{4})W(\d{2})\.json$")
Evidence = dict[tuple[str, str, str], dict[str, int]]


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
    return hashlib.blake2b(project.encode(), digest_size=6, key=key).hexdigest()


def own_counts(project: str, records: list[dict[str, Any]], relaunched: set[str],
               now: datetime | None = None) -> dict[str, dict[str, list[int]]]:
    """Return this installation's counts per week ready to share (closed for a day, inside the evidence window):
    per `kind|risk|cell`, [launches, relaunched], for the subagents whose model and effort the router chose.
    """
    moment = now or datetime.now(UTC)
    first = window_start(moment)
    weeks: dict[str, dict[str, list[int]]] = {}
    for r in records:
        if r.get("project") != project or not r.get("routed") or not r.get("router_chose"):
            continue
        if r.get("kind") not in KINDS or r.get("risk") not in RISKS:
            continue
        at = _when(r.get("at"))
        if at is None or at < first:
            continue
        week = week_of(at)
        begins = week_start(week)
        if begins is None or begins + timedelta(weeks=1) + SETTLE > moment:  # not closed, or closed too recently
            continue
        bucket = weeks.setdefault(week, {}).setdefault(f"{r['kind']}|{r['risk']}|{r['routed']}", [0, 0])
        bucket[0] += 1
        bucket[1] += str(r.get("agent_id")) in relaunched
    return weeks


def write_own(project: str, records: list[dict[str, Any]], decision_log: Path,
              now: datetime | None = None) -> list[Path]:
    """In a repository that shares counts, add a file for each settled week this installation has counts for and
    has not written yet, delete its files that left the window and those under an id it no longer uses, and
    return the files added.
    """
    if not sharing(project):
        return []
    key, kept = install_key()
    if not kept:  # an id that would change on every run: better not to share at all
        return []
    mine = own_id(project, key)
    old_ids = _remember(project, mine) - {mine}
    folder = shared_dir(project)
    first = window_start(now)
    for path in folder.glob("*.json"):
        m = FILE_NAME.match(path.name)
        begins = week_start(f"{m[2]}-W{m[3]}") if m else None
        if m and (m[1] in old_ids or begins is None or begins < first):  # an old id of mine, or out of the window
            path.unlink(missing_ok=True)  # anyone's: an old week counts for nobody, and a deletion merges cleanly
    added = []
    for week, counts in sorted(own_counts(project, records, relaunched_ids(decision_log), now).items()):
        path = folder / f"{mine}-{week.replace('-', '')}.json"
        if os.path.lexists(path):
            continue  # written once, never changed
        text = json.dumps({"v": FORMAT, "c": counts}, sort_keys=True, separators=(",", ":")) + "\n"
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")  # a fresh file: no link of anyone's followed
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        Path(tmp).replace(path)
        added.append(path)
    return added


def pooled(project: str, since: str | None = None, now: datetime | None = None) -> tuple[Evidence, int]:
    """Return the evidence in the other installations' files of a repository that shares counts, from the weeks
    that start inside the evidence window and after `since`, each installation capped at FILE_CAP launches per
    bucket; and how many installations gave some. Malformed files and entries are skipped.
    """
    if not sharing(project):
        return {}, 0
    start = max(m for m in (_when(since), window_start(now)) if m is not None)
    key, kept = install_key()
    skip = _known_ids(project) | ({own_id(project, key)} if kept else set())
    per_install: dict[str, Evidence] = {}
    for path in sorted(shared_dir(project).glob("*.json")):
        m = FILE_NAME.match(path.name)
        if not m or m[1] in skip:
            continue
        begins = week_start(f"{m[2]}-W{m[3]}")
        if begins is None or begins < start:
            continue
        theirs = per_install.setdefault(m[1], {})
        for key3, n, fails in _read(path):
            e = theirs.setdefault(key3, {"n": 0, "fails": 0})
            e["n"] += n
            e["fails"] += fails
    out: Evidence = {}
    for theirs in per_install.values():
        for key3, e in theirs.items():
            n, fails = e["n"], e["fails"]
            if n > FILE_CAP:  # one installation weighs at most FILE_CAP launches, at its own fail rate
                n, fails = FILE_CAP, round(fails * FILE_CAP / n)
            total = out.setdefault(key3, {"n": 0, "fails": 0})
            total["n"] += n
            total["fails"] += fails
    return out, sum(1 for theirs in per_install.values() if theirs)


def _read(path: Path) -> list[tuple[tuple[str, str, str], int, int]]:
    """Read one shared file: a regular file, not a link, at most MAX_FILE_BYTES; anything else gives nothing."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError:
        return []
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return []
    if not isinstance(data, dict) or data.get("v") != FORMAT or not isinstance(data.get("c"), dict):
        return []
    return [b for b in (_bucket(k, v) for k, v in data["c"].items()) if b is not None]


def _bucket(key: Any, value: Any) -> tuple[tuple[str, str, str], int, int] | None:
    parts = key.split("|") if isinstance(key, str) else []
    if len(parts) != 3 or parts[0] not in KINDS or parts[1] not in RISKS:
        return None
    try:
        cell = parse_cell(parts[2]).label
    except (UnknownModelError, ValueError):
        return None
    if (not isinstance(value, list) or len(value) != 2
            or not all(isinstance(v, int) and not isinstance(v, bool) for v in value)):
        return None
    n, fails = value
    if not 0 <= fails <= n <= MAX_COUNT:
        return None
    return (parts[0], parts[1], cell), n, fails


def _ids_key(project: str) -> str:
    return hashlib.sha256(project.encode()).hexdigest()[:16]


def _all_ids() -> dict[str, list[str]]:
    try:
        data = json.loads((home() / IDS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: [i for i in v if isinstance(i, str)] for k, v in data.items() if isinstance(v, list)}


def _known_ids(project: str) -> set[str]:
    """Return the ids this installation wrote under for this repository path: another clone or worktree of the
    same repository is another path, whose subagents this one's log does not hold, so its files are a teammate's.
    """
    return set(_all_ids().get(_ids_key(project), []))


def _remember(project: str, mine: str) -> set[str]:
    """Add `mine` to the ids this installation has written under for this repository path, and return them all."""
    path = home() / IDS_FILE
    with locked(path):
        ids = _all_ids()
        row = ids.setdefault(_ids_key(project), [])
        if mine not in row:
            row.append(mine)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".shared-ids", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(ids, sort_keys=True) + "\n")
            Path(tmp).replace(path)
        return set(row)
