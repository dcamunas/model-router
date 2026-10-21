"""Calibrations: a start table measured from the ledger instead of assumed. `measure` takes task records
(as the index or the ledger return them), keeps the first attempt of each one when a checker or a judge
gave its verdict, and computes per kind and cell the first-attempt pass rate with its confidence
interval, the mean cost of an attempt and the expected cost per solved task; from those it proposes a
start cell per kind and risk, and changes the current table only on evidence: a cheaper cell that was
measured and passes, or a current cell that was measured and loses. `router calibrate` prints the
report; `--apply` saves the calibration under `~/.model-router/calibrations/` and names it in
config.json; `Policy` loads the active one over the tables in the code, and every record it decides
carries its id. Without a calibration everything decides as before. The ladder still climbs by the
catalogue's assumed capability: a calibration moves where a task starts, not how it escalates.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ._fmt import money, table
from .catalog import Cell, UnknownModelError, lattice, parse_cell, step_up
from .classify import Classification, classify_llm, classify_rules
from .client import ClaudeClient
from .paths import CALIBRATION_ENV, MAIN_LEDGER, home, read_config
from .task import KINDS, RISKS, VERIFIED_SOURCES, Task
from .version import router_version

MIN_N = 6  # first attempts a (kind, cell) needs before its rate may choose a start cell
MIN_RATE = 0.5  # a start cell passes at least half of its first attempts, whatever the cost per solved says
MIN_SAVING = 0.10  # a measured start cell is replaced only by one this much cheaper per solved task: noise moves nothing
Z95 = 1.96  # normal quantile of a 95 % interval
ACTIVE = "active"  # `Policy(calibration=ACTIVE)`: the one MODEL_ROUTER_CALIBRATION or config.json name
NO_CALIBRATION = ("", "none", "builtin")  # values of MODEL_ROUTER_CALIBRATION or config.json that mean the code tables
BUILTIN = "builtin"


def wilson(passes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Return the Wilson score interval of a rate of `passes` in `n` trials; (0, 1) when there are none."""
    if n == 0:
        return 0.0, 1.0
    p = passes / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


@dataclass
class Measure:
    """What the records say about one cell on one kind: first attempts with an independent verdict, how many
    passed, the cost of those attempts (work plus verification) and, once priced, the expected cost per
    solved task: the attempt, plus the next cell up on the share that fails.
    """

    n: int = 0
    passes: int = 0
    cost: float = 0.0
    cost_per_solved: float | None = None

    @property
    def rate(self) -> float:
        return self.passes / self.n if self.n else 0.0

    @property
    def interval(self) -> tuple[float, float]:
        return wilson(self.passes, self.n)

    @property
    def mean_cost(self) -> float:
        return self.cost / self.n if self.n else 0.0

    def to_dict(self) -> dict[str, float | int | None]:
        low, high = self.interval
        solved = None if self.cost_per_solved is None else round(self.cost_per_solved, 6)
        return {"n": self.n, "passes": self.passes, "rate": round(self.rate, 4), "low": round(low, 4),
                "high": round(high, 4), "cost_per_attempt": round(self.mean_cost, 6), "cost_per_solved": solved}


@dataclass
class Calibration:
    """A measured start table and what it was measured from. `start` is the whole table (kind x risk), the
    current entry wherever the records gave no reason to change it; `measured` keeps every (kind, cell)
    the records covered; `changes` lists the entries that differ from `against`, with the evidence.
    """

    id: str
    created: str
    router_version: str
    eval_id: str | None
    min_n: int
    min_rate: float
    min_saving: float
    records: int  # task records looked at
    evidence: int  # first attempts with an independent verdict among them (see `_independent`)
    sources: dict[str, int]  # records from benches and from the ledger
    against: str  # the table the changes compare with: `builtin`, or a calibration id
    start: dict[str, dict[str, str]]
    measured: dict[str, dict[str, dict[str, float | int | None]]]
    changes: list[dict[str, Any]]
    no_data: list[str]  # kinds with no cell at `min_n` first attempts
    path: Path | None = field(default=None, compare=False)
    self_judged: int = 0  # verified first attempts left out because only the worker's own model backed the verdict

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if k != "path"}

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> Calibration:
        """Build a calibration from its JSON form.

        Raises:
            ValueError: a field is missing or has the wrong shape.
        """
        try:
            cal = cls(**{k: data[k] for k in cls.__dataclass_fields__ if k not in ("path", "self_judged")},
                  path=path, self_judged=int(data.get("self_judged", 0)))
        except (KeyError, TypeError) as exc:
            raise ValueError(f"not a calibration file: {exc}") from exc
        _check_table(cal.start)
        return cal

    def save(self, directory: Path) -> Path:
        """Write the calibration as `<id>.json` under `directory`; when that name is taken, the id gets a counter."""
        directory.mkdir(parents=True, exist_ok=True)
        base, n = self.id, 1
        while (directory / f"{self.id}.json").exists():
            n += 1
            self.id = f"{base}-{n}"
        self.path = directory / f"{self.id}.json"
        self.path.write_text(json.dumps(self.to_dict(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        return self.path


def _check_table(start: object) -> None:
    """Make sure `start` is a start table this version can plan with: every kind this router knows, every risk
    in each row, and a cell the catalogue can parse in each entry.

    Raises:
        ValueError: it is not.
    """
    if not isinstance(start, dict) or not all(isinstance(row, dict) for row in start.values()):
        raise ValueError("not a calibration file: `start` is not a table")
    missing = [k for k in KINDS if k not in start]
    if missing:
        raise ValueError(f"not a calibration file: `start` lacks the kinds {', '.join(missing)}")
    for kind, row in start.items():
        if kind not in KINDS:
            raise ValueError(f"not a calibration file: unknown kind {kind!r} in `start`")
        for risk in RISKS:
            cell = row.get(risk)
            if not isinstance(cell, str):
                raise ValueError(f"not a calibration file: `start` has no cell for {kind} x {risk}")
            try:
                parse_cell(cell)
            except (UnknownModelError, ValueError) as exc:
                raise ValueError(f"not a calibration file: {kind} x {risk} names {cell!r}: {exc}") from exc


def calibrations_dir() -> Path:
    """Return where calibrations are kept: `calibrations/` inside the state directory."""
    return home() / "calibrations"


def active_calibration_name() -> str | None:
    """Return the id (or file) of the calibration that should decide: MODEL_ROUTER_CALIBRATION, else
    `calibration` in config.json; None when neither names one, or names `none` or `builtin`.
    """
    raw = os.environ.get(CALIBRATION_ENV)
    if raw is None:
        raw = read_config().get("calibration")
    name = str(raw or "").strip()
    return None if name.lower() in NO_CALIBRATION else name


def load_calibration(name: str) -> Calibration:
    """Load the calibration `name` names: a file, or an id under `calibrations_dir()`.

    Raises:
        OSError: no such file.
        ValueError: the file is not a calibration.
    """
    path = Path(name).expanduser()
    if not path.exists():
        path = calibrations_dir() / f"{name}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"not a calibration file: {path}")
    return Calibration.from_dict(data, path)


def _independent(attempt: dict[str, Any], worker: Cell) -> bool:
    """Say whether the verdict on `attempt` is evidence: a checker decided, or a judge on a model other than
    `worker`'s backed the final verdict (the arbiter's ruling counts as such a judge). A worker judging itself
    in a fresh context passed a quarter of the answers an independent judge failed, so its lone verdict is not.
    """
    if attempt.get("verdict_source") == "checker":
        return True
    final = bool(attempt["verdict"])
    votes = attempt.get("verdicts")
    votes = [v for v in votes if isinstance(v, dict)] if isinstance(votes, list) else []
    if not votes:  # a record from before verdicts were kept: the single verifier is all there is
        votes = [{"cell": attempt.get("verifier"), "passed": final}]
    for vote in votes:
        label = str(vote.get("cell") or "")
        try:
            other = parse_cell(label).model != worker.model
        except (UnknownModelError, ValueError):  # a judge no longer declared: its name still says whose it was
            other = bool(label) and label.split("/")[0] != worker.spec.short
        if other and vote.get("passed") is final:
            return True
    return False


def _collect(records: Iterable[dict[str, Any]], eval_id: str | None) -> tuple[dict[tuple[str, str], Measure], Calibration]:
    """Bucket the first attempts of `records` by (kind, cell); return the buckets and a calibration shell
    with the counts filled in. A verdict that only the worker's own model backed is counted in
    `self_judged` and left out (see `_independent`).
    """
    buckets: dict[tuple[str, str], Measure] = {}
    shell = Calibration("", "", "", eval_id, 0, 0.0, 0.0, 0, 0, {"bench": 0, "ledger": 0}, "", {}, {}, [], [])
    for r in records:
        if eval_id is not None and r.get("eval_id") != eval_id:
            continue
        shell.records += 1
        bench = bool(r.get("eval_id")) or bool(r.get("source") and r.get("source") != MAIN_LEDGER)
        shell.sources["bench" if bench else "ledger"] += 1
        attempts = r.get("attempts") or []
        first = attempts[0] if attempts else None
        if not first or first.get("verdict") is None or first.get("verdict_source") not in VERIFIED_SOURCES:
            continue
        if first.get("cost_usd") is None:  # a record this router did not write: without a cost it cannot be priced
            continue
        if first.get("stop_reason") == "external":  # `router judge`: an answer made elsewhere, at no cost here
            continue
        label = str(first.get("cell", ""))
        try:
            worker = parse_cell(label)
        except (UnknownModelError, ValueError):
            continue
        passed = bool(first["verdict"])
        if r.get("overridden") and len(attempts) == 1:  # a human corrected the only verdict there is
            passed = r.get("status") == "solved"
        elif not _independent(first, worker):
            shell.self_judged += 1
            continue
        m = buckets.setdefault((str(r.get("kind", "unknown")), label), Measure())
        m.n += 1
        m.passes += passed
        m.cost += float(first.get("cost_usd") or 0.0) + float(first.get("verify_cost_usd") or 0.0)
        shell.evidence += 1
    return buckets, shell


def _price(buckets: dict[tuple[str, str], Measure], cells: list[Cell]) -> None:
    """Fill `cost_per_solved`: the mean attempt cost plus, on the share that fails, one attempt on the next cell up
    (its measured cost on the same kind, else its price scaled as this cell's measured cost scales its own
    reference price); a cell with nothing above it counts a second attempt on itself.
    """
    for (kind, label), m in buckets.items():
        cell = parse_cell(label)
        up = step_up(cell, cells)
        above = buckets.get((kind, up.label)) if up is not None else None
        if up is None:
            c_up = m.mean_cost
        elif above is not None and above.n:
            c_up = above.mean_cost
        else:
            reference = cell.expected_cost()
            scale = m.mean_cost / reference if reference and m.mean_cost else 1.0
            c_up = up.expected_cost() * scale
        m.cost_per_solved = m.mean_cost + (1 - m.rate) * c_up


def _propose(now_label: str, good: dict[str, Measure], floor: str | None, min_rate: float, min_saving: float) -> str:
    """Return the start cell for one kind and risk: the measured cell above the floor with the lowest expected
    cost per solved task, `now_label` kept unless there is evidence: a cheaper measured cell that passes, or
    the current cell measured and beaten by at least `min_saving` (or under `min_rate`).
    """
    now = parse_cell(now_label)
    floor_capability = parse_cell(floor).capability if floor else 0.0
    ok = {label: m for label, m in good.items()
          if m.rate >= min_rate and m.cost_per_solved is not None and parse_cell(label).capability >= floor_capability}
    pool = ok if now_label in good else {label: m for label, m in ok.items()
                                         if parse_cell(label).expected_cost() < now.expected_cost()}
    if not pool:
        return now_label
    best = min(pool, key=lambda label: (pool[label].cost_per_solved or 0.0, parse_cell(label).expected_cost(), label))
    if now_label in ok and best != now_label:
        beaten = (pool[best].cost_per_solved or 0.0) <= (ok[now_label].cost_per_solved or 0.0) * (1 - min_saving)
        return best if beaten else now_label
    return best


def measure(
    records: Iterable[dict[str, Any]], *, current: dict[str, dict[str, str]], floors: dict[str, str],
    min_n: int = MIN_N, min_rate: float = MIN_RATE, min_saving: float = MIN_SAVING, cells: list[Cell] | None = None,
    eval_id: str | None = None, against: str = BUILTIN,
) -> Calibration:
    """Measure `records` and propose a start table from `current` (the table in force, whose name is `against`),
    with `floors` (risk -> the lowest cell allowed) respected; `cells` are the ones a task may escalate to. Records
    tagged with their `source` file (see `Store.task_records`) are counted as bench or ledger by it.
    """
    cells = cells or lattice()
    buckets, cal = _collect(records, eval_id)
    _price(buckets, cells)
    cal.start = {kind: dict(row) for kind, row in current.items()}
    for kind, row in current.items():
        good = {label: m for (k, label), m in buckets.items() if k == kind and m.n >= min_n}
        if not good:
            cal.no_data.append(kind)
            continue
        for risk, now_label in row.items():
            best = _propose(now_label, good, floors.get(risk), min_rate, min_saving)
            if best != now_label:
                cal.start[kind][risk] = best
                m, was = good[best], good.get(now_label)
                cal.changes.append({"kind": kind, "risk": risk, "from": now_label, "to": best, "n": m.n,
                                    "rate": round(m.rate, 4), "cost_per_solved": m.cost_per_solved,
                                    "from_n": was.n if was else 0, "from_rate": round(was.rate, 4) if was else None,
                                    "from_cost_per_solved": was.cost_per_solved if was else None})
    for (kind, label), m in sorted(buckets.items()):
        cal.measured.setdefault(kind, {})[label] = m.to_dict()
    stamp = datetime.now(UTC)
    cal.id = f"{stamp:%Y-%m-%d}-{eval_id or 'all'}"
    cal.created = stamp.isoformat(timespec="seconds")
    cal.router_version = router_version()
    cal.min_n, cal.min_rate, cal.min_saving, cal.against = min_n, min_rate, min_saving, against
    return cal


def report(cal: Calibration) -> str:
    """Render the calibration as `router calibrate` prints it: the evidence, the measured cells, the proposed
    table with the changes marked, and the kinds that gave no evidence.
    """
    lines = [
        f"calibration {cal.id} · {cal.records} task records ({cal.sources.get('bench', 0)} from benches, "
        f"{cal.sources.get('ledger', 0)} from the ledger) · {cal.evidence} first attempts with an independent verdict"
        + (f" ({cal.self_judged} left out: only the worker's own model judged them)" if cal.self_judged else "")
        + f" · min n {cal.min_n}, min pass rate {cal.min_rate:.0%}, min saving {cal.min_saving:.0%} · compared with "
        + ("the built-in table" if cal.against == BUILTIN else f"calibration {cal.against}"),
    ]
    rows: list[list[object]] = []
    for kind, cells in cal.measured.items():
        for label, m in sorted(cells.items(), key=lambda kv: (kv[1]["cost_per_solved"] is None, kv[1]["cost_per_solved"] or 0)):
            n = int(m["n"] or 0)
            rows.append([kind, label, n, f"{float(m['rate'] or 0):.0%}",
                         f"{float(m['low'] or 0):.0%}-{float(m['high'] or 0):.0%}", money(float(m["cost_per_attempt"] or 0)),
                         money(m["cost_per_solved"]) if m["cost_per_solved"] is not None else "-",
                         "" if n >= cal.min_n else "n too small"])
    if rows:
        lines += ["", "measured (first attempts with an independent verdict; $/solved = the attempt plus one on the next "
                      "cell up when it fails, or on itself at the top)",
                  table(["kind", "cell", "n", "pass", "95% CI", "$/attempt", "$/solved", "note"], rows)]
    else:
        lines += ["", "nothing measured: no first attempt with a checker's or a judge's verdict in these records"]
    risks = list(next(iter(cal.start.values()), {}))
    changed = {(c["kind"], c["risk"]) for c in cal.changes}
    starts = [[kind] + [row[r] + ("*" if (kind, r) in changed else "") for r in risks] for kind, row in cal.start.items()]
    lines += ["", "start table (* = changed, see below)", table(["kind", *risks], starts)]
    if cal.changes:
        lines += ["", f"changes ({len(cal.changes)}):"]
        for c in cal.changes:
            was = (f"was {c['from_rate']:.0%} of {c['from_n']}, {money(c['from_cost_per_solved'])}"
                   if c.get("from_n") else "was not measured")
            lines.append(f"  {c['kind']} x {c['risk']}: {c['from']} -> {c['to']} ({c['rate']:.0%} of {c['n']}, "
                         f"{money(c['cost_per_solved'])} per solved; {was})")
    else:
        lines += ["", "no changes: the records give no reason to move a start cell"]
    if cal.no_data:
        lines.append(f"without enough data (no cell at n >= {cal.min_n}): {', '.join(cal.no_data)}")
    return "\n".join(lines)


def _confusion(pairs: list[tuple[str, str]]) -> str:
    """Render expected (rows) against predicted (columns) counts."""
    labels = sorted({e for e, _ in pairs} | {p for _, p in pairs})
    counts: dict[tuple[str, str], int] = {}
    for e, p in pairs:
        counts[(e, p)] = counts.get((e, p), 0) + 1
    rows = [[e] + [counts.get((e, p), 0) or "" for p in labels] for e in labels if any(x == e for x, _ in pairs)]
    return table(["expected \\ got", *labels], rows)


def classifier_report(
    specs: Iterable[dict[str, Any]], *, start: Callable[[Task], Cell], client: ClaudeClient | None = None
) -> str:
    """Compare the rule classifier (and, with `client`, the Haiku one) with the kind and risk the eval file gives
    each task; say how often a miss would change the start cell `start` picks.
    """
    labelled = [s for s in specs if s.get("kind") and s.get("risk")]
    if not labelled:
        return "no task in the file has both kind and risk; nothing to compare"
    sections = [_classifier_section("rules", labelled, start, classify_rules)]
    if client is not None:
        cost: list[float] = []

        def llm(prompt: str) -> Classification:
            c = classify_llm(client, prompt)
            cost.append(c.cost_usd)
            return c

        sections.append(_classifier_section("haiku", labelled, start, llm) + f"\ncost of the Haiku calls: {money(sum(cost))}")
    return "\n\n".join(sections)


def _classifier_section(
    name: str, labelled: list[dict[str, Any]], start: Callable[[Task], Cell], classify: Callable[[str], Classification]
) -> str:
    kinds: list[tuple[str, str]] = []
    risks: list[tuple[str, str]] = []
    misses: list[list[object]] = []
    for s in labelled:
        got = classify(str(s["prompt"]))
        kinds.append((str(s["kind"]), got.kind))
        risks.append((str(s["risk"]), got.risk))
        if got.kind != s["kind"] or got.risk != s["risk"]:
            want = start(Task(prompt=str(s["prompt"]), kind=str(s["kind"]), risk=str(s["risk"])))
            would = start(Task(prompt=str(s["prompt"]), kind=got.kind, risk=got.risk))
            misses.append([s.get("id", "?"), f"{s['kind']}/{s['risk']}", f"{got.kind}/{got.risk}",
                           f"{want} -> {would}" if want != would else "same", got.reason])
    n = len(labelled)
    kind_ok = sum(e == p for e, p in kinds)
    risk_ok = sum(e == p for e, p in risks)
    moved = sum(row[3] != "same" for row in misses)
    lines = [f"{name}: kind right on {kind_ok}/{n}, risk right on {risk_ok}/{n}; {len(misses)} misses, "
             f"{moved} of them change the start cell", "", "kind", _confusion(kinds), "", "risk", _confusion(risks)]
    if misses:
        lines += ["", table(["id", "expected", "got", "start cell", "why"], misses)]
    return "\n".join(lines)
