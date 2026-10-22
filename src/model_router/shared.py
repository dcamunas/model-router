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
deletes the files a week after they left the window. Files are read and written without following links, and a shared directory
that is a link elsewhere is not used.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any  # Any: decoded JSON (subagent records, shared files)

from . import clock
from .catalog import UnknownModelError, parse_cell
from .paths import home, locked
from .projects import Bucket, Counts, install_key, relaunched_ids, week_of, week_start, window_start
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
        at = clock.parse_moment(record.get("at"))
        if at is None or at < first:
            continue
        week = week_of(at)
        begins = week_start(week)
        if begins is None or begins + timedelta(weeks=1) + SETTLE > moment:  # not closed, or closed too recently
            continue
        tally = weeks.setdefault(week, {}).setdefault(f"{record['kind']}|{record['risk']}|{record['routed']}", [0, 0])
        tally[0] += 1
        tally[1] += str(record.get("agent_id")) in relaunched
    return weeks


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
    has not written yet, and return the files added. A file once written is never changed.
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
    for week, counts in sorted(own_counts(project, records, relaunched=relaunched_ids(decision_log), now=now).items()):
        path = folder / f"{mine}-{week.replace('-', '')}.json"
        if os.path.lexists(path):
            continue
        text = json.dumps({"v": FORMAT, "c": counts}, sort_keys=True, separators=(",", ":")) + "\n"
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".", suffix=".tmp")  # a fresh file: no link of anyone's followed
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            Path(tmp).replace(path)
        finally:
            Path(tmp).unlink(missing_ok=True)  # gone after the replace; left only by a failed write
        added.append(path)
    return added


def pooled(project: str, *, since: str | None = None, now: datetime | None = None) -> tuple[Counts, int]:
    """Return the evidence in the other installations' files of a repository that shares counts, from the weeks
    that start inside the evidence window and after `since`, each installation capped at FILE_CAP launches per
    bucket; and how many installations gave some. Malformed files and entries are skipped.
    """
    if not sharing(project):
        return {}, 0
    start = max(moment for moment in (clock.parse_moment(since), window_start(now)) if moment is not None)
    key, kept = install_key()
    skip = known_ids(project) | ({own_id(project, key)} if kept else set())
    per_install: dict[str, Counts] = {}
    for path in sorted(shared_dir(project).glob("*.json")):
        match = FILE_NAME.match(path.name)
        if not match or match[1] in skip:
            continue
        begins = week_start(f"{match[2]}-W{match[3]}")
        if begins is None or begins < start:
            continue
        theirs = per_install.setdefault(match[1], {})
        for bucket, launches, fails in _read(path):
            tally = theirs.setdefault(bucket, {"n": 0, "fails": 0})
            tally["n"] += launches
            tally["fails"] += fails
    out: Counts = {}
    for theirs in per_install.values():
        for bucket, tally in theirs.items():
            launches, fails = tally["n"], tally["fails"]
            if launches > FILE_CAP:  # one installation weighs at most FILE_CAP launches, at its own fail rate
                launches, fails = FILE_CAP, round(fails * FILE_CAP / launches)
            total = out.setdefault(bucket, {"n": 0, "fails": 0})
            total["n"] += launches
            total["fails"] += fails
    return out, sum(1 for theirs in per_install.values() if theirs)


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
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".shared-ids", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(ids, sort_keys=True) + "\n")
            Path(tmp).replace(path)
        return set(row)


def _plain(path: Path) -> bool:
    """Say whether `path` is a regular file or a link (which unlink removes, never its target), not a directory."""
    try:
        mode = path.lstat().st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) or stat.S_ISLNK(mode)


def _read(path: Path) -> list[tuple[Bucket, int, int]]:
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
    return [entry for entry in (_entry(k, v) for k, v in data["c"].items()) if entry is not None]


def _entry(key: object, value: object) -> tuple[Bucket, int, int] | None:
    """Return one shared count as (bucket, launches, fails), or None when any part of it is not well formed."""
    match = BUCKET_KEY.match(key) if isinstance(key, str) else None
    if not match or match[1] not in KINDS or match[2] not in RISKS:
        return None
    try:
        cell = parse_cell(match[3]).label
    except (UnknownModelError, ValueError):
        return None
    if not isinstance(value, list) or len(value) != ENTRY_FIELDS:
        return None
    launches, fails = value
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (launches, fails)):
        return None
    if not 0 <= fails <= launches <= MAX_COUNT:
        return None
    return (match[1], match[2], cell), launches, fails


def _ids_key(project: str) -> str:
    return hashlib.sha256(project.encode()).hexdigest()[:IDS_KEY_CHARS]


def _all_ids() -> dict[str, list[str]]:
    try:
        data = json.loads((home() / IDS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: [i for i in v if isinstance(i, str)] for k, v in data.items() if isinstance(v, list)}
