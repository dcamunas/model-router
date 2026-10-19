"""What the router learns: pass rate per (kind, cell), separated into first attempts and escalated
attempts because escalated attempts only see the tasks a cheaper cell already failed. Enough
first-attempt evidence moves a kind's start cell up (too many failures) or, when exploration is on,
down (nearly everything passes, try the next cheaper cell on a fraction of tasks).

Verdicts come from checkers and judges, so this store is only as good as the verification. Human
overrides through the ledger correct it.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from .catalog import Cell, parse_cell, step_down, step_up


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
        self.data: dict[str, dict[str, dict[str, int]]] = {}
        if path is not None and path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def _bucket(self, kind: str, cell: Cell) -> dict[str, int]:
        return self.data.setdefault(kind, {}).setdefault(cell.label, {"n": 0, "pass": 0, "n_first": 0, "pass_first": 0})

    def record(self, kind: str, cell: Cell, passed: bool, *, first: bool = True) -> None:
        """Count one verdict for `cell` on `kind`; `first` marks a first attempt, kept apart from escalated ones."""
        b = self._bucket(kind, cell)
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
        """Return the cell `kind` should start on instead of `default`, and the reason in Spanish when it
        differs (None otherwise). With at least `min_n` first attempts, a pass rate under `break_even`
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
                return up, (f"feedback: {default} resuelve a la primera {rate:.0%} de {n} tareas '{kind}' "
                            f"(umbral {threshold:.0%}) -> empieza en {up}")
        elif rate >= self.promote_above and self.explore > 0 and self.rng.random() < self.explore:
            down = step_down(default, cells)
            if down is not None:
                return down, f"feedback: {default} resuelve a la primera {rate:.0%} de {n} '{kind}'; explorando {down}"
        return default, None

    def apply_override(self, kind: str, cell_label: str, passed: bool, *, first: bool) -> None:
        """Apply a human correction of a recorded verdict: move one count from pass to fail or back."""
        b = self._bucket(kind, parse_cell(cell_label))
        delta = 1 if passed else -1
        b["pass"] = max(0, min(b["n"], b["pass"] + delta))
        if first:
            b["pass_first"] = max(0, min(b["n_first"], b["pass_first"] + delta))

    def rows(self) -> list[list[Any]]:
        """Return one row per (kind, cell) for `router stats`: first-attempt and total counts and pass rates."""
        out = []
        for kind, cells in sorted(self.data.items()):
            for label, b in sorted(cells.items(), key=lambda kv: -kv[1]["n"]):
                first = f"{b['pass_first'] / b['n_first']:.0%}" if b["n_first"] else "-"
                total = f"{b['pass'] / b['n']:.0%}" if b["n"] else "-"
                out.append([kind, label, b["n_first"], first, b["n"], total])
        return out

    def save(self) -> None:
        """Write the store to its path; a store without a path stays in memory."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, ensure_ascii=False), encoding="utf-8")
