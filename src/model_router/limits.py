"""Usage limits of the Claude account: which models it cannot use right now, and exactly until when.

A subscription caps usage in windows (five hours, seven days, and a separate weekly allowance for some models,
Fable among them). When a window is spent, a call is refused with HTTP 429 until the window resets. The router
learns it from the failure itself: Claude Code fires `StopFailure` with `error: rate_limit` when a subagent dies on
a limit, and `router run` gets the same 429 back from `claude -p`.

Neither says when the window resets. `claude -p --output-format stream-json --verbose` does: before its answer it
prints a `rate_limit_event` with the window that decided (`rateLimitType`), its `status` (`allowed`,
`allowed_warning`, `rejected`) and `resetsAt`, a Unix time. So `probe` asks the limited model for one word with no
tools and no context: refused, it costs nothing and gives the exact reset; allowed, it costs one tiny call and
says the limit is gone.

What is known goes to `~/.model-router/limits.json`, one entry per model short name (`fable`), or `*` when a window
that covers every model is spent:

    {"models": {"fable": {"until": "2026-10-24T13:00:00+00:00", "window": "seven_day_overage_included",
                          "seen": "2026-10-22T21:14:05+00:00"}},
     "windows": {"five_hour": {"utilization": 0.2, "resets_at": "..."}, ...}, "checked": "..."}

A model is limited while `until` is in the future, and available again at that exact moment with no other step.
An entry without `until` (the probe could not tell) holds for RECHECK only. After that the model is available
again, and the next failure probes it anew.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any  # Any: decoded JSON (the limits file, the CLI's events)

from . import clock
from .paths import CHILD_ENV, home, locked

LIMITS_FILE = "limits.json"
ALL_MODELS = "*"  # a window that covers every model (five hours, seven days) is spent
RECHECK = timedelta(minutes=10)  # how long a limit with no known reset holds before it is probed again
PROBE_SECONDS = 30.0
PROBE_SYSTEM = "Answer with the single word ok."
NO_MCP = '{"mcpServers":{}}'
# "You've reached your Fable limit" names the model; "usage limit reached" with no model is the whole account.
MODEL_LIMIT = re.compile(r"reached your (\w+) limit", re.I)
ACCOUNT_LIMIT = re.compile(r"usage limit|rate limit", re.I)


@dataclass(frozen=True)
class Limit:
    """Describe a model the account cannot use: until when (None when unknown), the window that refused it, and
    when that was seen.
    """

    model: str
    until: datetime | None
    window: str
    seen: datetime

    def active(self, now: datetime) -> bool:
        """Say whether the limit still holds at `now`."""
        if self.until is not None:
            return now < self.until
        return now - self.seen < RECHECK

    def when(self) -> str:
        """Return the reset in the machine's local time, as a person reads it ('Sat 24/10 15:00 CEST')."""
        return clock.local(self.until) if self.until is not None else "unknown reset"


@dataclass(frozen=True)
class Probe:
    """Hold what one probe said: `allowed`, `rejected` or `unknown`. A rejection carries its reset and window.
    Every window the event listed comes along with its use (0 to 1) and reset.
    """

    status: str
    until: datetime | None = None
    window: str = ""
    windows: dict[str, dict[str, Any]] | None = None


def limits_path() -> Path:
    """Return the file the limits are kept in."""
    return home() / LIMITS_FILE


def _read() -> dict[str, Any]:
    try:
        data = json.loads(limits_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def active(now: datetime | None = None) -> dict[str, Limit]:
    """Return the limits that hold now, by model short name (`*` for the whole account)."""
    moment = now or clock.now()
    out = {}
    models = _read().get("models")
    for name, entry in (models if isinstance(models, dict) else {}).items():
        limit = _limit(str(name), entry)
        if limit is not None and limit.active(moment):
            out[limit.model] = limit
    return out


def _limit(name: str, entry: object) -> Limit | None:
    if not isinstance(entry, dict):
        return None
    seen = clock.parse_moment(entry.get("seen"))
    until = clock.parse_moment(entry["until"]) if entry.get("until") else None
    if seen is None:
        return None
    return Limit(name, until, str(entry.get("window") or ""), seen)


def windows() -> dict[str, dict[str, Any]]:
    """Return the account's windows as the last probe saw them: use from 0 to 1 and the reset, by window name."""
    data = _read().get("windows")
    return data if isinstance(data, dict) else {}


def checked() -> str | None:
    """Return when the windows were last seen (an ISO moment), or None before any probe."""
    value = _read().get("checked")
    return str(value) if value else None


def record(model: str, probe: Probe) -> Limit | None:
    """Keep what a probe of `model` said: a rejection becomes a limit until its reset (on `*` when the window covers
    every model), an allowed answer clears the model's limit, and an unknown one holds it for RECHECK. Return the
    limit kept, or None when there is none.
    """
    now = clock.now()
    key = ALL_MODELS if probe.status == "rejected" and probe.window in ("five_hour", "seven_day") else model
    path = limits_path()
    with locked(path):
        data = _read()
        stored = data.get("models")
        models: dict[str, Any] = stored if isinstance(stored, dict) else {}
        kept: Limit | None = None
        if probe.status == "allowed":
            models.pop(model, None)
        else:
            kept = Limit(key, probe.until, probe.window or "unknown", now)
            models[key] = {"until": probe.until.isoformat() if probe.until else None, "window": kept.window,
                           "seen": now.isoformat(timespec="seconds")}
        data["models"] = {k: v for k, v in models.items() if _worth_keeping(_limit(k, v), now)}
        if probe.windows:
            data["windows"] = probe.windows
            data["checked"] = now.isoformat(timespec="seconds")
        _write(path, data)
    return kept


def _worth_keeping(limit: Limit | None, now: datetime) -> bool:
    """Keep a limit that still holds, and one with no known reset until it is probed again; drop what has passed."""
    return limit is not None and (limit.until is None or limit.active(now))


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


def model_in_failure(text: str) -> str | None:
    """Return the model short name a limit message names (`fable`), `*` when it names the whole account, or None
    when the text is not about a usage limit.
    """
    match = MODEL_LIMIT.search(text)
    if match:
        return match.group(1).lower()
    return ALL_MODELS if ACCOUNT_LIMIT.search(text) else None


def probe(model_id: str, *, config_dir: Path | None = None, binary: str = "claude",
          timeout: float = PROBE_SECONDS) -> Probe:
    """Ask `model_id` for one word through `claude -p` and return what its rate-limit event says. A refused probe
    costs nothing; an allowed one is one tiny call. Any failure to run or read it gives `unknown`.
    """
    args = [binary, "-p", "--model", model_id, "--effort", "low", "--output-format", "stream-json", "--verbose",
            "--tools", "", "--no-session-persistence", "--strict-mcp-config", "--mcp-config", NO_MCP,
            "--system-prompt", PROBE_SYSTEM]
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env[CHILD_ENV] = "1"  # the router's own session: its hooks stay out of it
    env["MODEL_TRIBUNAL"] = "0"
    if config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    try:
        proc = subprocess.run(args, input="ok", capture_output=True, text=True, env=env, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return Probe("unknown")
    return parse_events(proc.stdout)


def parse_events(stream: str) -> Probe:
    """Read the rate-limit event of a `stream-json` output: rejected with its reset, allowed, or unknown."""
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        info = event.get("rate_limit_info") if isinstance(event, dict) and event.get("type") == "rate_limit_event" else None
        if not isinstance(info, dict):
            continue
        status = str(info.get("status") or "")
        seen = _windows(info.get("unifiedWindows"))
        if status == "rejected":
            return Probe("rejected", _moment(info.get("resetsAt")), str(info.get("rateLimitType") or ""), seen)
        if status.startswith("allowed"):
            return Probe("allowed", windows=seen)
    return Probe("unknown")


def _windows(raw: object) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, window in (raw if isinstance(raw, dict) else {}).items():
        if isinstance(window, dict):
            reset = _moment(window.get("resetsAt"))
            out[str(name)] = {"utilization": window.get("utilization"),
                              "resets_at": reset.isoformat() if reset else None}
    return out


def _moment(epoch: object) -> datetime | None:
    if not isinstance(epoch, int | float | str):
        return None
    try:
        return datetime.fromtimestamp(float(epoch), UTC)
    except (ValueError, OverflowError, OSError):
        return None
