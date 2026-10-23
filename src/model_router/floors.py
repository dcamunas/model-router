r"""Risk floors a repository sets for its own work: what counts as high or critical there, said once in a file.

The risk of a subagent is what Claude declares (or what the rules read from the text), and Claude can forget
that in this repository "migration" or "production" is never low. A repository writes it down in
`.claude/model-router.json`, committed with the code:

    {"v": 1, "floors": [
      {"match": "producci[oó]n|production", "risk": "critical", "why": "reaches production"},
      {"match": "\\.sql\\b|migration", "risk": "high"}
    ]}

Each `match` is a regular expression, case-insensitive, searched in the subagent's description and the first
HEAD_LINES non-empty lines of its prompt, where the task is said, never in the material pasted below. A floor only
raises: the risk is the highest of the declared one and every rule that matches, so a rule can never make work
cheaper than Claude asked for. A rule that cannot be used (a bad pattern, an unknown risk) is skipped, and
`router doctor` lists it; a file that cannot be read gives no floors. The file is read as a regular file, never
through a link, and only its first MAX_FILE_BYTES.

A pattern comes from a file anyone with commit rights wrote, and a regular expression can take exponential time
(`(a+)+$`). So each search runs under a timer of RULE_SECONDS (SIGALRM, which interrupts Python's `re`): a rule that
runs out of time counts as not matching, and the decision says which one. Where no timer can be set (a thread other
than the main one, or a timer already running) the rules are not searched at all.

The file sits next to `.claude/model-router/`, not inside it: that directory is the opt-in for shared counts
(`shared.py`), and setting floors must not turn sharing on.
"""

from __future__ import annotations

import contextlib
import json
import re
import signal
import threading
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path

from .paths import read_regular
from .task import RISKS

FLOORS_FILE = Path(".claude") / "model-router.json"
FORMAT = 1
MAX_FILE_BYTES = 32 * 1024
MAX_RULES = 50
MAX_PATTERN_CHARS = 200  # a pattern runs on every subagent launch: long ones are where slow ones hide
HEAD_LINES = 4  # the declaration line and the sentences that say the task
MAX_HEAD_CHARS = 2000
RULE_SECONDS = 0.05  # a sane pattern on MAX_HEAD_CHARS takes microseconds; MAX_RULES of these stay far under a hook's
# timeout


@dataclass(frozen=True)
class FloorRule:
    """One floor: the pattern as written, its compiled form, the risk it sets at least, and the reason shown."""

    match: str
    pattern: re.Pattern[str]
    risk: str
    why: str

    @property
    def label(self) -> str:
        """Return what a decision record and `router project` call this rule: its reason, else its pattern."""
        return self.why or self.match


@dataclass(frozen=True)
class Floors:
    """A repository's floors: the usable rules, and what was wrong with the others (empty when all are fine)."""

    path: Path | None
    rules: tuple[FloorRule, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Raised:
    """Hold the risk after the floors, the rule that set it (None when none raised it) and the rules not searched:
    out of time, or no timer to search them under.
    """

    risk: str
    rule: FloorRule | None = None
    unsearched: tuple[str, ...] = ()


def floors_path(project: str) -> Path:
    """Return where a repository keeps its floors."""
    return Path(project) / FLOORS_FILE


def load(project: str | None) -> Floors:
    """Return the floors of the repository at `project` (no floors outside a repository or without the file)."""
    if not project:
        return Floors(None)
    path = floors_path(project)
    raw = read_regular(path, MAX_FILE_BYTES)
    if raw is None:
        try:
            there = path.is_symlink() or path.exists()
        except OSError:  # a directory on the way that cannot be read
            there = False
        if not there:
            return Floors(None)
        return Floors(path, problems=(f"{path} is not a regular file of at most {MAX_FILE_BYTES // 1024} KB",))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:  # RecursionError: nesting deep enough to exhaust the parser
        return Floors(path, problems=(f"{path} is not valid JSON: {exc}",))
    if (not isinstance(data, dict) or type(data.get("v")) is not int or data["v"] != FORMAT
            or not isinstance(data.get("floors", []), list)):
        return Floors(path, problems=(f'{path}: expected {{"v": {FORMAT}, "floors": [...]}}',))
    return parse(path, data.get("floors", []))


def parse(path: Path | None, entries: list[object]) -> Floors:
    """Turn the `floors` list of a file into rules, keeping a note for each entry that cannot be used."""
    rules: list[FloorRule] = []
    problems: list[str] = []
    for n, entry in enumerate(entries[:MAX_RULES], start=1):
        rule, problem = _rule(entry)
        if rule is not None:
            rules.append(rule)
        else:
            problems.append(f"floor {n}: {problem}")
    if len(entries) > MAX_RULES:
        problems.append(f"only the first {MAX_RULES} floors are read; {len(entries) - MAX_RULES} left out")
    return Floors(path, tuple(rules), tuple(problems))


def _rule(entry: object) -> tuple[FloorRule | None, str]:
    if not isinstance(entry, dict):
        return None, "expected an object with `match` and `risk`"
    match, risk, why = entry.get("match"), entry.get("risk"), entry.get("why", "")
    if not isinstance(match, str) or not match.strip():
        return None, "`match` must be a non-empty pattern"
    if len(match) > MAX_PATTERN_CHARS:
        return None, f"`match` is longer than {MAX_PATTERN_CHARS} characters"
    if risk not in RISKS:
        return None, f"`risk` must be one of {', '.join(RISKS)}, not {risk!r}"
    if not isinstance(why, str):
        return None, "`why` must be text"
    try:
        pattern = re.compile(match, re.IGNORECASE)
    except (re.error, OverflowError, RecursionError, ValueError) as exc:  # `a{99999999999}` overflows, not re.error
        return None, f"`match` is not a valid pattern ({exc})"
    return FloorRule(match, pattern, str(risk), why.strip()), ""


def head(prompt: str, description: str = "") -> str:
    """Return the text a floor is searched in: the description and the first HEAD_LINES non-empty prompt lines."""
    lines = [line for line in prompt.splitlines() if line.strip()][:HEAD_LINES]
    return "\n".join([description, *lines])[:MAX_HEAD_CHARS]


def apply(risk: str, text: str, *, floors: Floors) -> Raised:
    """Return the risk after the floors that match `text`: the highest of `risk` and every matching rule's."""
    best: FloorRule | None = None
    unsearched: list[str] = []
    for rule in floors.rules:
        if RISKS.index(rule.risk) <= RISKS.index(best.risk if best else risk):
            continue  # it could not raise anything: no need to run it
        found = _search(rule, text)
        if found is None:
            unsearched.append(rule.label)
        elif found:
            best = rule
    return Raised(best.risk if best else risk, best, tuple(unsearched))


class _OutOfTimeError(Exception):
    """Raised by the timer inside a search that took longer than RULE_SECONDS."""


def _search(rule: FloorRule, text: str) -> bool | None:
    """Say whether `rule` matches `text`, or None when it ran out of time or no timer could be set."""
    with _timer(RULE_SECONDS) as armed:
        if not armed:
            return None
        try:
            return rule.pattern.search(text) is not None
        except _OutOfTimeError:
            return None


@contextlib.contextmanager
def _timer(seconds: float) -> Generator[bool]:
    """Raise _OutOfTimeError in the main thread after `seconds`; yield False, setting nothing, where that is not
    possible (another thread, or a timer someone else already runs). The previous handler is put back.
    """
    if threading.current_thread() is not threading.main_thread() or signal.getitimer(signal.ITIMER_REAL)[0] > 0:
        yield False
        return

    def out_of_time(_signum: int, _frame: object) -> None:
        raise _OutOfTimeError

    previous = signal.signal(signal.SIGALRM, out_of_time)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield True
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
