"""Where the account's usage windows go: the status line, the context warning and `router usage`.

Claude Code hands the status line command, on every update, the share of the account's five-hour and seven-day
windows already used (`rate_limits`) and the size of the last turn's context (`context_window`). `router
statusline` prints them in one line and keeps a sample in `usage.jsonl` whenever they move: numbers only, never
text. The hook's UserPromptSubmit warns once per band when a session's context grows past the threshold, since
every turn reads the whole context again. `router usage` adds up what the transcripts of one or more profiles
spent (the windows belong to the account, which several profiles may share) and fits which weighting of those
tokens best follows how the seven-day window rose: the one that prices every point of the week the same.
"""

from __future__ import annotations

import bisect
import json
import math
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any  # Any: status line input, transcript entries and state files are decoded JSON

from . import clock, limits
from .catalog import MODELS, SHORT_TO_ID, Usage, configured_models, cost_usd
from .paths import append_jsonl, home, locked, read_config, read_json, read_jsonl, replace_file

USAGE_LOG = "usage.jsonl"  # the status line's samples of the windows and the context
STATE_FILE = "usage-state.json"  # per session: the last sample kept and the highest context band warned about
DEFAULT_WARN = 300_000  # context tokens from which a session is warned
BAND_STEP = 200_000  # then again at each step above it: 300k, 500k, 700k
BANDS_WARNED = 3
SAMPLE_SECONDS = 30.0  # at most one sample per session in this time, unless a window moved
CONTEXT_STEP = 10_000  # a context change smaller than this is not worth a sample on its own
STATE_DAYS = 7  # sessions older than this leave the state file
MAX_LOG_BYTES = 4_000_000  # past this the usage log keeps only its newer half (a few weeks of samples)
SAMPLES_TAIL_BYTES = 8_000_000  # the end of each usage log `router usage` reads
TAIL_BYTES = 262_144  # the end of a transcript read for the last turn's context
REPORT_BANDS = (0, 100_000, 200_000, 400_000, 700_000)  # context bands of the main conversation's calls
MAX_GAP_SECONDS = 2 * 3600  # two samples further apart than this are not one interval of the fit
MIN_INTERVALS = 20  # fewer intervals than this give no weighting
RESET_TOLERANCE = 120  # seconds: two samples name the same weekly reset when their reset times are this close
K = 1000
FABLE = "fable"
FABLE_WINDOW = "seven_day_overage_included"  # the weekly window of the models outside the all-models one: Fable
FABLE_REFRESH_SECONDS = 1800  # a background Fable probe at most this often per profile, while a status line runs
FABLE_STALE_SECONDS = 6 * 3600  # an older reading of Fable's window is not shown


# ---- the status line ------------------------------------------------------------------------------------------


@dataclass
class Status:
    """What one status line update says: the session, the model, the last turn's context and the windows."""

    session: str = ""
    model: str = ""
    context: int | None = None  # input + cache read + cache write of the last call, None before the first
    window_size: int | None = None
    five_hour: float | None = None  # percent used, 0-100
    five_hour_resets: int | None = None  # epoch seconds
    seven_day: float | None = None
    seven_day_resets: int | None = None
    fable: float | None = None  # Fable's own weekly window, from the last Fable probe (not in Claude Code's JSON)
    fable_resets: int | None = None

    def sample(self, at: float) -> dict[str, Any]:
        """Return the sample kept in the log: these numbers and the moment, never any text of the session."""
        return {"at": datetime.fromtimestamp(at, clock.now().tzinfo).isoformat(timespec="seconds"),
                "session": self.session, "model": self.model, "context": self.context,
                "window_size": self.window_size, "five_hour": self.five_hour,
                "five_hour_resets": self.five_hour_resets, "seven_day": self.seven_day,
                "seven_day_resets": self.seven_day_resets, "fable": self.fable, "fable_resets": self.fable_resets}


def read_status(data: dict[str, Any]) -> Status:
    """Return the Status in the JSON Claude Code gives the status line command; missing fields stay None."""
    window = _dict(data.get("context_window"))
    current = _dict(window.get("current_usage"))  # null before the first call and right after /compact
    context = None
    if current:
        context = sum(_int(current.get(k)) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                          "cache_creation_input_tokens"))
    limits = _dict(data.get("rate_limits"))
    five, week = _dict(limits.get("five_hour")), _dict(limits.get("seven_day"))
    return Status(session=str(data.get("session_id") or ""), model=str(_dict(data.get("model")).get("id") or ""),
                  context=context, window_size=_int(window.get("context_window_size")),
                  five_hour=_float(five.get("used_percentage")), five_hour_resets=_int(five.get("resets_at")),
                  seven_day=_float(week.get("used_percentage")), seven_day_resets=_int(week.get("resets_at")))


def statusline_text(status: Status, *, warn_at: int) -> str:
    """Return the status line: the context, then the five-hour and seven-day windows when Claude Code sends them,
    with a mark when the context is past `warn_at` (0: never).
    """
    parts = []
    if status.context is not None:
        size = f"/{tokens(status.window_size)}" if status.window_size else ""
        over = bool(warn_at) and status.context >= warn_at
        parts.append(f"{'⚠ ' if over else ''}ctx {tokens(status.context)}{size}{' · /compact?' if over else ''}")
    if status.five_hour is not None:
        parts.append(f"5h {status.five_hour:.0f}%")
    same = status.fable is None or _same_reset(status.seven_day_resets, status.fable_resets)
    if status.seven_day is not None:
        reset = f" (resets {_when(status.seven_day_resets)})" if status.seven_day_resets and not same else ""
        parts.append(f"week {status.seven_day:.0f}%{reset}")
    if status.fable is not None:
        parts.append(f"fable {status.fable:.0f}%")
    resets = status.fable_resets if status.fable is not None else status.seven_day_resets
    if resets and (status.fable is not None or status.seven_day is not None):
        parts[-1] += f" (resets {_when(resets)})"
    return " · ".join(parts)


def _same_reset(a: int | None, b: int | None) -> bool:
    return a is None or b is None or abs(a - b) <= RESET_TOLERANCE


def keep_sample(status: Status, *, now: float | None = None, state_dir: Path | None = None) -> bool:
    """Append `status` to the usage log when it says something new for its session (a window moved, a reset
    changed, the context moved by CONTEXT_STEP, or SAMPLE_SECONDS passed with any change) and return whether it did.
    A status with no window and no context is never kept.
    """
    if status.seven_day is None and status.five_hour is None and status.context is None:
        return False
    moment = time.time() if now is None else now
    folder = state_dir or home()
    state_path = folder / STATE_FILE
    with locked(state_path):
        state = read_json(state_path)
        sessions = _dict(state.get("sessions"))
        own = _dict(sessions.get(status.session))
        last = _dict(own.get("last"))
        if not _worth_keeping(status, last, since=moment - float(own.get("kept_at") or 0)):
            return False
        append_jsonl(folder / USAGE_LOG, status.sample(moment))
        sessions[status.session] = {**own, "last": status.sample(moment), "kept_at": moment, "seen": moment}
        _save_state(state_path, state, sessions, now=moment)
    _prune_log(folder / USAGE_LOG)
    return True


def _prune_log(path: Path) -> None:
    """Keep the usage log under MAX_LOG_BYTES: past it, only its newer half stays (whole lines)."""
    try:
        if path.stat().st_size <= MAX_LOG_BYTES:
            return
    except OSError:
        return
    with locked(path):
        with path.open("rb") as fh:
            fh.seek(-(MAX_LOG_BYTES // 2), 2)
            tail = fh.read().decode("utf-8", errors="replace").partition("\n")[2]
        replace_file(path, tail)


def _worth_keeping(status: Status, last: dict[str, Any], *, since: float) -> bool:
    if not last:
        return True
    windows = ("five_hour", "seven_day", "five_hour_resets", "seven_day_resets", "fable", "fable_resets")
    moved = any(getattr(status, k) != last.get(k) for k in windows)
    context_moved = abs((status.context or 0) - (_int(last.get("context")) or 0)) >= CONTEXT_STEP
    changed = moved or status.context != last.get("context")
    return moved or context_moved or (changed and since >= SAMPLE_SECONDS)


def _save_state(path: Path, state: dict[str, Any], sessions: dict[str, Any], *, now: float) -> None:
    cutoff = now - STATE_DAYS * 86400
    kept = {k: v for k, v in sessions.items() if isinstance(v, dict) and float(v.get("seen") or 0) >= cutoff}
    replace_file(path, json.dumps({**state, "sessions": kept}, ensure_ascii=False) + "\n")


# ---- Fable's own weekly window ---------------------------------------------------------------------------------


def fable_available() -> bool:
    """Say whether this installation may use Fable (`router config --models`): only then is its window shown."""
    return SHORT_TO_ID.get(FABLE) in configured_models()


def fable_window(now: datetime | None = None) -> tuple[float, int | None] | None:
    """Return Fable's own weekly window, which Claude Code does not give the status line: percent used and reset
    (epoch seconds), from the last Fable probe in `limits.json`; 100 % until its reset when Fable is limited. None
    when this installation has no Fable or no probe saw the window in the last FABLE_STALE_SECONDS.
    """
    if not fable_available():
        return None
    moment = now or clock.now()
    held = limits.active(moment).get(FABLE)
    if held is not None and held.until is not None:
        return 100.0, int(held.until.timestamp())
    window = _dict(limits.windows().get(FABLE_WINDOW))
    seen = clock.parse_moment(window.get("seen") or limits.checked())
    used = _float(window.get("utilization"))
    if used is None or seen is None or (moment - seen).total_seconds() > FABLE_STALE_SECONDS:
        return None
    reset = clock.parse_moment(window.get("resets_at"))
    return used * 100, int(reset.timestamp()) if reset else None


def refresh_fable(*, now: float | None = None, state_dir: Path | None = None,
                  start: Callable[[], None] | None = None) -> bool:
    """Start a background Fable probe (`router limits --check fable`: one-word answer, a sliver of Fable's window)
    when the profile has not started one in FABLE_REFRESH_SECONDS and Fable is available; return whether it did.
    The status line never waits for it: the next update shows what it found.
    """
    if not fable_available():
        return False
    moment = time.time() if now is None else now
    state_path = (state_dir or home()) / STATE_FILE
    with locked(state_path):
        state = read_json(state_path)
        if moment - (_float(state.get("fable_probe_at")) or 0.0) < FABLE_REFRESH_SECONDS:
            return False
        replace_file(state_path, json.dumps({**state, "fable_probe_at": moment}, ensure_ascii=False) + "\n")
    (start or _start_fable_probe)()
    return True


def _start_fable_probe() -> None:
    subprocess.Popen([sys.executable, "-m", "model_router.cli", "limits", "--check", FABLE],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


# ---- the context warning --------------------------------------------------------------------------------------


def warn_threshold() -> int:
    """Return the context size from which a session is warned (`router config --context-warn`; 0 = never)."""
    value = read_config().get("context_warn")
    return max(0, value) if isinstance(value, int) and not isinstance(value, bool) else DEFAULT_WARN


def bands(threshold: int) -> tuple[int, ...]:
    """Return the context sizes a session is warned at: the threshold and BAND_STEP above it, BANDS_WARNED times."""
    return tuple(threshold + i * BAND_STEP for i in range(BANDS_WARNED)) if threshold > 0 else ()


def last_context(transcript: Path) -> int | None:
    """Return the context of the main conversation's last call (input + cache read + cache write), read from the end
    of its transcript; None when no call with usage is there. Subagent entries are skipped.
    """
    for entry in reversed(read_jsonl(transcript, tail_bytes=TAIL_BYTES)):
        message = entry.get("message")
        if entry.get("isSidechain") or not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        if isinstance(usage, dict):
            return sum(_int(usage.get(k)) or 0 for k in ("input_tokens", "cache_read_input_tokens",
                                                         "cache_creation_input_tokens"))
    return None


def context_warning(event: dict[str, Any], *, now: float | None = None,
                    state_dir: Path | None = None) -> dict[str, Any] | None:
    """Return the hook's answer to a UserPromptSubmit: a message for the person the first time the session's
    context passes each band, else None. The state file remembers the highest band warned per session, and forgets
    it once the context is back under the first band (a /compact), so a session that grows again is warned again.
    """
    marks = bands(warn_threshold())
    transcript = event.get("transcript_path")
    if not marks or not transcript:
        return None
    context = last_context(Path(str(transcript)))
    if context is None:
        return None
    reached = [b for b in marks if context >= b]
    session = str(event.get("session_id") or "")
    moment = time.time() if now is None else now
    state_path = (state_dir or home()) / STATE_FILE
    with locked(state_path):
        state = read_json(state_path)
        sessions = _dict(state.get("sessions"))
        own = _dict(sessions.get(session))
        warned = _int(own.get("warned")) or 0
        if not reached:  # below every band: after a /compact the bands are warned about again
            if warned:
                sessions[session] = {**own, "warned": 0, "seen": moment}
                _save_state(state_path, state, sessions, now=moment)
            return None
        if reached[-1] <= warned:
            return None
        sessions[session] = {**own, "warned": reached[-1], "seen": moment}
        _save_state(state_path, state, sessions, now=moment)
    return {"systemMessage": f"router: this session's context is {tokens(context or 0)} tokens, and every turn reads "
                             "all of it again from the account's usage windows. /compact, or a new session, when the "
                             "task allows (`router config --context-warn 0` turns this notice off)."}


# ---- what the transcripts spent -------------------------------------------------------------------------------


@dataclass
class Tally:
    """Tokens of a set of calls, by kind, with the number of calls."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, usage: Usage) -> None:
        """Count one call's usage."""
        self.calls += 1
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens


@dataclass
class Call:
    """One API call found in a transcript: when, on which model, its usage, and whether a subagent made it."""

    at: float  # epoch seconds
    model_id: str
    usage: Usage
    subagent: bool


@dataclass
class Spent:
    """What the transcripts of some profiles spent in a period: per part (main, subagents) and model, per context
    band of the main conversation, and every call in time order for the fit.
    """

    profiles: list[Path]
    since: datetime
    parts: dict[str, dict[str, Tally]] = field(default_factory=dict)
    bands: dict[int, Tally] = field(default_factory=dict)
    calls: list[Call] = field(default_factory=list)
    unknown_models: int = 0


def model_id(name: str) -> str | None:
    """Return the catalogue id of a model name as transcripts write it ('claude-opus-5-5', with a suffix such as
    '[1m]' or a date), or None when it names no catalogued model.
    """
    if name in MODELS:
        return name
    return next((mid for mid, spec in MODELS.items() if spec.claude and spec.short in name.lower()), None)


def transcript_calls(path: Path, *, subagent: bool, seen: set[str] | None = None) -> Iterator[Call]:
    """Yield the calls of one transcript, once per message id (a transcript repeats a message's usage on every
    content block), skipping entries without usage; a model outside the catalogue is yielded with model_id ''.
    `seen` carries the ids already counted across transcripts: a resumed or forked session copies earlier messages,
    with their ids, into its new file.
    """
    seen = set() if seen is None else seen
    for entry in read_jsonl(path):
        message = entry.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        usage = message.get("usage")
        moment = clock.parse_moment(entry.get("timestamp"))
        if not isinstance(usage, dict) or moment is None:
            continue
        key = str(message.get("id") or entry.get("uuid") or f"{path}:{len(seen)}")
        if key in seen:
            continue
        seen.add(key)
        yield Call(moment.timestamp(), model_id(str(message.get("model") or "")) or "", _usage(usage),
                   subagent or bool(entry.get("isSidechain")))


def spent(profiles: list[Path], *, since: datetime) -> Spent:
    """Add up the calls in every transcript of `profiles` (their `projects/` folders) made since `since`."""
    out = Spent(profiles=profiles, since=since)
    cutoff = since.timestamp()
    seen: set[str] = set()
    for profile in profiles:
        for path in sorted((profile / "projects").rglob("*.jsonl")):
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            for call in transcript_calls(path, subagent="subagents" in path.parts, seen=seen):
                if call.at < cutoff:
                    continue
                if not call.model_id:
                    out.unknown_models += 1
                    continue
                out.calls.append(call)
                part = "subagents" if call.subagent else "main"
                out.parts.setdefault(part, {}).setdefault(MODELS[call.model_id].short, Tally()).add(call.usage)
                if not call.subagent:
                    out.bands.setdefault(_band(call.usage), Tally()).add(call.usage)
    out.calls.sort(key=lambda c: c.at)
    return out


def _band(usage: Usage) -> int:
    context = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    return REPORT_BANDS[bisect.bisect_right(REPORT_BANDS, context) - 1]


# ---- the fit: which weighting of tokens moves the seven-day window ---------------------------------------------


def samples(profiles: list[Path]) -> list[dict[str, Any]]:
    """Return the status line samples of every profile in `profiles`, in time order (the windows are the account's,
    so samples from two profiles on one account read the same windows).
    """
    out = [r for p in profiles for r in read_jsonl(p / "model-router" / USAGE_LOG, tail_bytes=SAMPLES_TAIL_BYTES)
           if clock.parse_moment(r.get("at")) is not None]
    return sorted(out, key=lambda r: clock.parse_moment(r["at"]) or clock.now())


@dataclass
class Interval:
    """Two samples in a row of one weekly window: from, to (epoch seconds) and how many points of the week it used."""

    start: float
    end: float
    used: float


def intervals(rows: list[dict[str, Any]], *, since: float = 0.0) -> list[Interval]:
    """Return the intervals between consecutive rises of the seven-day window, from samples taken at or after
    `since` (epoch seconds). Claude Code reports whole percents, so the tokens between two rises are what one point
    (or a few) cost; counting from any other sample would add the part of a point used before it.

    Each session reads the windows from its own last call, so an idle session can report a lower, older percent
    after a busy one moved on: a sample under the highest percent seen in the week is stale and skipped. A chain
    breaks, and starts again at its next rise, where nobody sampled for MAX_GAP_SECONDS or the reset changes.
    """
    points = sorted((m.timestamp(), float(r["seven_day"]), _int(r.get("seven_day_resets")) or 0)
                    for r in rows if isinstance(r.get("seven_day"), int | float)
                    and (m := clock.parse_moment(r.get("at"))) is not None and m.timestamp() >= since)
    out: list[Interval] = []
    anchor: float | None = None  # when the highest percent of the chain was first seen
    top, reset, last = -1.0, None, None
    for moment, percent, resets in points:
        if reset is None or abs(resets - reset) > RESET_TOLERANCE:
            anchor, top, reset = None, percent, resets  # a new week
        elif last is not None and moment - last > MAX_GAP_SECONDS:
            anchor, top = None, max(top, percent)  # nobody watched: the next rise starts a chain
        elif percent > top:
            if anchor is not None:
                out.append(Interval(anchor, moment, percent - top))
            anchor, top = moment, percent
        last = moment
    return out


Weight = Callable[[Call], float]


def _list_price(call: Call) -> float:
    return cost_usd(call.model_id, call.usage)


def _price_without_cache_reads(call: Call) -> float:
    u = call.usage
    return cost_usd(call.model_id, Usage(u.input_tokens, u.output_tokens, 0, u.cache_write_tokens))


def _raw_tokens(call: Call) -> float:
    return float(call.usage.total)


HYPOTHESES: dict[str, tuple[Weight, str]] = {
    "list price": (_list_price, "USD at list price"),
    "list price, cache reads free": (_price_without_cache_reads, "USD at list price without cache reads"),
    "raw tokens": (_raw_tokens, "tokens"),
}


@dataclass
class Fit:
    """How well one weighting of the calls follows the window: what a point of the week costs in it, and how much
    that cost spreads from one interval to the next (the weighting the window really counts gives the same cost
    for every point).
    """

    name: str
    unit: str
    intervals: int
    per_point: float  # units of the weighting per point of the seven-day window, over all intervals
    spread: float  # coefficient of variation of the cost per point across intervals: lower follows better


def fit(calls: list[Call], spans: list[Interval]) -> list[Fit]:
    """Price one point of the week in each weighting of HYPOTHESES, interval by interval, and return them with the
    steadiest first. Empty when fewer than MIN_INTERVALS intervals saw any call.
    """
    moments = [c.at for c in calls]
    cuts = [(bisect.bisect_right(moments, s.start), bisect.bisect_right(moments, s.end), s.used) for s in spans]
    cuts = [(lo, hi, used) for lo, hi, used in cuts if hi > lo and used > 0]  # one set of intervals for every weighting
    if len(cuts) < MIN_INTERVALS:
        return []
    out = []
    for name, (weight, unit) in HYPOTHESES.items():
        running = [0.0]
        for call in calls:
            running.append(running[-1] + weight(call))
        costs = [(running[hi] - running[lo]) / used for lo, hi, used in cuts]
        mean = sum(costs) / len(costs)
        if mean <= 0:
            continue
        deviation = (sum((c - mean) ** 2 for c in costs) / len(costs)) ** 0.5
        per_point = sum(running[hi] - running[lo] for lo, hi, _ in cuts) / sum(used for *_, used in cuts)
        out.append(Fit(name, unit, len(costs), per_point, deviation / mean))
    return sorted(out, key=lambda f: f.spread)


# ---- helpers --------------------------------------------------------------------------------------------------


def tokens(n: int | float) -> str:
    """Return a token count the way the status line shows it: 950, 412k, 1.2M."""
    if n >= K * K:
        return f"{n / (K * K):.1f}M".replace(".0M", "M")
    return f"{n / K:.0f}k" if n >= K else str(int(n))


def since_days(days: int) -> datetime:
    """Return the moment `days` days ago."""
    return clock.now() - timedelta(days=days)


def _when(epoch: int) -> str:
    seconds = epoch / 1000 if epoch > 10**11 else epoch  # milliseconds, should a version send them
    try:
        return datetime.fromtimestamp(seconds).astimezone().strftime("%a %H:%M")
    except (ValueError, OverflowError, OSError):
        return "?"


def _usage(raw: dict[str, Any]) -> Usage:
    return Usage(_int(raw.get("input_tokens")) or 0, _int(raw.get("output_tokens")) or 0,
                 _int(raw.get("cache_read_input_tokens")) or 0, _int(raw.get("cache_creation_input_tokens")) or 0)


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: object) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _float(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)
