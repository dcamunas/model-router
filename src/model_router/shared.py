"""Evidence shared through the repository, so a team learns from everyone's subagents, not only its own.

A repository opts in by holding the directory `.claude/model-router/` (`router project --share` creates it).
Then each installation keeps one small file there, `<id>.json`, and commits it with the rest of the repo:

    {"v": 1, "weeks": {"2026-W42": {"review|high|opus/medium": [12, 1]}}}

per closed ISO week, per kind, risk and start cell, how many subagents the router started there and how many of
them were relaunched higher. Nothing else: no prompt, signature, path, name, cost or time of day. The id comes
from this installation's salt and the repository's path, so it tells nothing and links no two repositories.

The counts are the same evidence `projects.evidence` counts on this machine, and they are summed the same way:
a fail rate is fails over launches whatever install they came from. This installation's own file is never read
back (its subagents already count from the local log), only closed weeks are written (so the file changes at
most once a week), and a week counts only when it starts after the project's last reset or applied change and
within the evidence window. One file per installation means no two people ever edit the same file.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .catalog import UnknownModelError, parse_cell
from .projects import WINDOW, _salt, _when, relaunched_ids
from .task import KINDS, RISKS

SHARED_DIR = (".claude", "model-router")
FORMAT = 1
MAX_FILE_BYTES = 256 * 1024  # anything bigger is not one of these files
MAX_COUNT = 100_000
WEEK = re.compile(r"^(\d{4})-W(\d{2})$")
Evidence = dict[tuple[str, str, str], dict[str, int]]


def shared_dir(project: str) -> Path:
    """Return the directory of shared counts in a repository."""
    return Path(project, *SHARED_DIR)


def sharing(project: str | None) -> bool:
    """Say whether a repository opted in to shared counts (it holds `.claude/model-router/`)."""
    return bool(project) and shared_dir(str(project)).is_dir()


def own_id(project: str) -> str:
    """Return this installation's file id in a repository: a keyed hash of its path, meaningless elsewhere."""
    return hashlib.blake2b(project.encode(), digest_size=6, key=_salt()).hexdigest()


def week_of(moment: datetime) -> str:
    """Return the ISO week of a moment, as `2026-W42`."""
    year, week, _ = moment.astimezone(UTC).isocalendar()
    return f"{year}-W{week:02d}"


def week_start(label: str) -> datetime | None:
    """Return the Monday 00:00 UTC a week label starts on, or None for a bad label."""
    m = WEEK.match(label)
    if not m:
        return None
    try:
        day = date.fromisocalendar(int(m[1]), int(m[2]), 1)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def own_counts(project: str, records: list[dict[str, Any]], relaunched: set[str],
               now: datetime | None = None) -> dict[str, dict[str, list[int]]]:
    """Return this installation's counts for the shared file: per closed week in the evidence window, per
    `kind|risk|cell`, [launches, relaunched], for the subagents whose model and effort the router chose.
    """
    moment = now or datetime.now(UTC)
    this_week = week_start(week_of(moment))
    weeks: dict[str, dict[str, list[int]]] = {}
    for r in records:
        if r.get("project") != project or not r.get("routed") or not r.get("router_chose"):
            continue
        if r.get("kind") not in KINDS or r.get("risk") not in RISKS:
            continue
        at = _when(r.get("at"))
        if at is None or at < moment - WINDOW or this_week is None or at >= this_week:  # only closed weeks
            continue
        bucket = weeks.setdefault(week_of(at), {}).setdefault(f"{r['kind']}|{r['risk']}|{r['routed']}", [0, 0])
        bucket[0] += 1
        bucket[1] += str(r.get("agent_id")) in relaunched
    return weeks


def write_own(project: str, records: list[dict[str, Any]], decision_log: Path,
              now: datetime | None = None) -> Path | None:
    """Write this installation's file in an opted-in repository when its counts changed; return it, or None when
    nothing was written.
    """
    if not sharing(project):
        return None
    weeks = own_counts(project, records, relaunched_ids(decision_log), now)
    path = shared_dir(project) / f"{own_id(project)}.json"
    text = json.dumps({"v": FORMAT, "weeks": weeks}, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        if path.read_text(encoding="utf-8") == text:
            return None
    except OSError:
        pass
    if not weeks and not path.exists():
        return None
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}", suffix=".tmp",
                                     delete=False) as tmp:
        tmp.write(text)
    Path(tmp.name).replace(path)
    return path


def pooled(project: str, since: str | None = None, now: datetime | None = None) -> tuple[Evidence, int]:
    """Return the evidence in the other installations' files of an opted-in repository, counting weeks that start
    after `since` and within the evidence window, and how many files gave some. Malformed files and entries are
    skipped.
    """
    if not sharing(project):
        return {}, 0
    moment = now or datetime.now(UTC)
    start = max(m for m in (_when(since), moment - WINDOW) if m is not None)
    mine = f"{own_id(project)}.json"
    out: Evidence = {}
    files = 0
    for path in sorted(shared_dir(project).glob("*.json")):
        if path.name == mine:
            continue
        weeks = _read(path)
        used = False
        for label, buckets in weeks.items():
            begins = week_start(label)
            if begins is None or begins < start or not isinstance(buckets, dict):
                continue
            for key, value in buckets.items():
                parsed = _bucket(key, value)
                if parsed is None:
                    continue
                e = out.setdefault(parsed[0], {"n": 0, "fails": 0})
                e["n"] += parsed[1]
                e["fails"] += parsed[2]
                used = True
        files += used
    return out, files


def _read(path: Path) -> dict[str, Any]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or data.get("v") != FORMAT or not isinstance(data.get("weeks"), dict):
        return {}
    return data["weeks"]


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
