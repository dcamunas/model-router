"""What the router learns: pass rate per (kind, cell), separated into first attempts and escalated
attempts because escalated attempts only see the tasks a cheaper cell already failed. Enough
first-attempt evidence moves a kind's start cell up (too many failures) or, when exploration is on,
down (nearly everything passes, try the next cheaper cell on a fraction of tasks).

Verdicts come from checkers and judges, so this store is only as good as the verification. Human
overrides through the ledger correct it.

Several processes may share one file (two benches, a bench and `router feedback`): each store keeps
what it added since it read the file and merges that into the file under a lock when it saves, so
nobody's counts are lost; the write is atomic, so a crash never leaves half a file. A file that is
not a JSON object is treated as empty (`unreadable` says so) and replaced on the next save.
"""

from __future__ import annotations

import fcntl
import json
import os
import random
from pathlib import Path

from .catalog import Cell, parse_cell, step_down, step_up

Counts = dict[str, dict[str, dict[str, int]]]  # kind -> cell label -> counters
EMPTY_BUCKET = {"n": 0, "pass": 0, "n_first": 0, "pass_first": 0}


def _bucket(data: Counts, kind: str, label: str) -> dict[str, int]:
    return data.setdefault(kind, {}).setdefault(label, dict(EMPTY_BUCKET))


def _merge(base: Counts, deltas: Counts) -> Counts:
    """Add `deltas` into `base` in place and return it; pass counts stay within [0, n]."""
    for kind, cells in deltas.items():
        for label, d in cells.items():
            b = _bucket(base, kind, label)
            b["n"] += d["n"]
            b["n_first"] += d["n_first"]
            b["pass"] = max(0, min(b["n"], b["pass"] + d["pass"]))
            b["pass_first"] = max(0, min(b["n_first"], b["pass_first"] + d["pass_first"]))
    return base


class FeedbackStore:
    """Pass rates per (kind, cell) learned from verdicts, persisted as JSON when given a path. A kind's
    start cell moves only after `min_n` first attempts; `explore` is the fraction of tasks sent one step
    down when nearly everything passes.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        min_n: int = 10,
        demote_below: float = 0.6,
        promote_above: float = 0.9,
        explore: float = 0.0,
        rng: random.Random | None = None,
    ) -> None:
        self.path = path
        self.min_n = min_n
        self.demote_below = demote_below
        self.promote_above = promote_above
        self.explore = explore
        self.rng = rng or random.Random()
        self.unreadable = False  # the file exists but is not a JSON object: the store starts empty
        self.data: Counts = self._read()
        self._deltas: Counts = {}  # what this store added since it read the file; merged in on save

    def _read(self) -> Counts:
        if self.path is None or not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        if not isinstance(data, dict):
            self.unreadable = True
            return {}
        return data

    def _bucket(self, kind: str, cell: Cell) -> dict[str, int]:
        return _bucket(self.data, kind, cell.label)

    def record(self, kind: str, cell: Cell, passed: bool, *, first: bool = True) -> None:
        """Count one verdict for `cell` on `kind`; `first` marks a first attempt, kept apart from escalated ones."""
        for b in (self._bucket(kind, cell), _bucket(self._deltas, kind, cell.label)):
            b["n"] += 1
            b["pass"] += int(passed)
            if first:
                b["n_first"] += 1
                b["pass_first"] += int(passed)

    def first_attempt_stats(self, kind: str, cell: Cell) -> tuple[int, float | None]:
        """Return (first attempts seen, first-attempt pass rate) for `cell` on `kind`; (0, None) without data."""
        b = self.data.get(kind, {}).get(cell.label)
        if not b or not b["n_first"]:
            return 0, None
        return b["n_first"], b["pass_first"] / b["n_first"]

    def suggest_start(
        self, kind: str, default: Cell, cells: list[Cell], *, break_even: float | None = None
    ) -> tuple[Cell, str | None]:
        """Return the cell `kind` should start on instead of `default`, and the reason when it differs
        (None otherwise). With at least `min_n` first attempts, a pass rate under `break_even`
        moves one step up and a rate over `promote_above` explores one step down on an `explore`
        fraction of tasks. `break_even` is the rate below which starting on `default` costs more than
        starting one step up, verification included; without it the flat `demote_below` applies.
        """
        n, rate = self.first_attempt_stats(kind, default)
        if n < self.min_n or rate is None:
            return default, None
        threshold = self.demote_below if break_even is None else break_even
        if rate < threshold:
            up = step_up(default, cells)
            if up is not None:
                return up, (f"feedback: {default} passes first time on {rate:.0%} of {n} '{kind}' tasks "
                            f"(threshold {threshold:.0%}) -> start on {up}")
        elif rate >= self.promote_above and self.explore > 0 and self.rng.random() < self.explore:
            down = step_down(default, cells)
            if down is not None:
                return down, f"feedback: {default} passes first time on {rate:.0%} of {n} '{kind}'; exploring {down}"
        return default, None

    def apply_override(self, kind: str, cell_label: str, passed: bool, *, first: bool) -> None:
        """Apply a human correction of a recorded verdict: move one count from pass to fail or back. The
        caller makes sure the verdict really changes; applying the same correction twice moves two counts.
        """
        cell = parse_cell(cell_label)
        delta = 1 if passed else -1
        b, d = self._bucket(kind, cell), _bucket(self._deltas, kind, cell.label)
        b["pass"] = max(0, min(b["n"], b["pass"] + delta))
        d["pass"] += delta
        if first:
            b["pass_first"] = max(0, min(b["n_first"], b["pass_first"] + delta))
            d["pass_first"] += delta

    def rows(self) -> list[list[object]]:
        """Return one row per (kind, cell) for `router stats`: first-attempt and total counts and pass rates."""
        out = []
        for kind, cells in sorted(self.data.items()):
            for label, b in sorted(cells.items(), key=lambda kv: -kv[1]["n"]):
                first = f"{b['pass_first'] / b['n_first']:.0%}" if b["n_first"] else "-"
                total = f"{b['pass'] / b['n']:.0%}" if b["n"] else "-"
                out.append([kind, label, b["n_first"], first, b["n"], total])
        return out

    def save(self) -> None:
        """Merge what this store added into its file, under a lock and with an atomic write, so additions
        other processes made since the file was read are kept; a store without a path stays in memory.
        """
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(".lock").open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                merged = _merge(self._read(), self._deltas)
                tmp = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
                tmp.write_text(json.dumps(merged, indent=1, ensure_ascii=False), encoding="utf-8")
                tmp.replace(self.path)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
        self.data, self._deltas, self.unreadable = merged, {}, False
