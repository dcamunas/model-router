"""Offline simulation: the same tasks through several strategies at real prices, with assumed
capabilities. Nothing here calls the API and nothing here proves anything about a real workload.
What it shows is how the router's economics move with verifier accuracy, task difficulty and the
hard tail, and it exercises the real Router, Policy and Verifier code paths end to end.

The worker's answer carries its own ground truth (`SIM|task|cell|ok`), so a programmatic checker is
perfect and a judge is wrong with the configured false-pass / false-fail rates.
"""

from __future__ import annotations

import hashlib
import math
import random
import re
from dataclasses import dataclass, field

from ._fmt import money, table
from .catalog import EFFORT_OUTPUT_MULT, Cell, Usage, parse_cell
from .client import NON_STREAMING_MAX_TOKENS, Completion
from .feedback import FeedbackStore
from .ledger import Ledger, summarize
from .policy import FULL_VERIFY_GAIN, LadderPolicy, Policy, PolicyProtocol
from .runner import Router
from .task import CheckResult, Task
from .verify import Verdict

# kind: (mean difficulty, input tokens, output tokens at high effort, has a programmatic checker)
KIND_PROFILE: dict[str, tuple[float, int, int, bool]] = {
    "classify": (0.25, 800, 60, True),
    "extract": (0.35, 3000, 400, True),
    "summarize": (0.35, 6000, 500, False),
    "search": (0.30, 4000, 400, False),
    "chat": (0.30, 1200, 300, False),
    "write": (0.45, 1500, 900, False),
    "code": (0.55, 4000, 1800, True),
    "review": (0.55, 8000, 1200, False),
    "analysis": (0.60, 5000, 1500, False),
    "agentic": (0.72, 12000, 4000, True),
}
KIND_MIX: dict[str, float] = {
    "classify": 0.12, "extract": 0.12, "summarize": 0.08, "search": 0.10, "chat": 0.08, "write": 0.10,
    "code": 0.15, "review": 0.08, "analysis": 0.10, "agentic": 0.07,
}
RISK_MIX: dict[str, float] = {"low": 0.30, "medium": 0.40, "high": 0.20, "critical": 0.10}
TAIL_SHARE = 0.15  # share of tasks that get a difficulty bump: the tail that decides the bill
TAIL_BUMP = 0.25
DIFFICULTY_SPREAD = 8.0  # concentration of the beta distribution around the kind's mean
STEEPNESS = 12.0  # how sharply pass probability falls once difficulty exceeds capability
JUDGE_ERROR: dict[str, tuple[float, float]] = {"light": (0.10, 0.05), "full": (0.03, 0.03)}  # (false pass, false fail)


@dataclass(frozen=True)
class SimTask:
    id: str
    kind: str
    risk: str
    difficulty: float
    input_tokens: int
    output_high: int
    has_checker: bool


def _weighted(rng: random.Random, mix: dict[str, float]) -> str:
    return rng.choices(list(mix), weights=list(mix.values()))[0]


def make_tasks(n: int, seed: int) -> list[SimTask]:
    rng = random.Random(seed)
    out = []
    for i in range(n):
        kind = _weighted(rng, KIND_MIX)
        risk = _weighted(rng, RISK_MIX)
        mean, inp, outp, checker = KIND_PROFILE[kind]
        difficulty = rng.betavariate(mean * DIFFICULTY_SPREAD, (1 - mean) * DIFFICULTY_SPREAD)
        if rng.random() < TAIL_SHARE:
            difficulty = min(1.0, difficulty + TAIL_BUMP)
        out.append(SimTask(
            f"t{i + 1:03d}", kind, risk, round(difficulty, 3),
            round(inp * rng.uniform(0.6, 1.6)), round(outp * rng.uniform(0.7, 1.4)), checker,
        ))
    return out


def pass_probability(cell: Cell, difficulty: float) -> float:
    return 1.0 / (1.0 + math.exp(-STEEPNESS * (cell.capability - difficulty)))


def _draw(seed: int, *parts: object) -> float:
    digest = hashlib.sha256("|".join([str(seed), *map(str, parts)]).encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


MARK = re.compile(r"SIM\|(\S+?)\|(\S+?)\|(ok|ko)")


def truth_of(answer: str) -> bool | None:
    m = MARK.search(answer)
    return None if m is None else m.group(3) == "ok"


def sim_checker(answer: str) -> CheckResult:
    return CheckResult(bool(truth_of(answer)), "sim checker")


class SimClient:
    """Implements ClaudeClient. Worker prompts are task ids; judge prompts contain the answer mark."""

    def __init__(self, tasks: list[SimTask], seed: int, judge_error: dict[str, tuple[float, float]] | None = None) -> None:
        self.tasks = {t.id: t for t in tasks}
        self.seed = seed
        self.judge_error = judge_error or JUDGE_ERROR

    def complete(
        self, cell: Cell, *, prompt: str,
        system: str | None = None,  # noqa: ARG002 - client protocol signature, unused by the simulation
        max_tokens: int = NON_STREAMING_MAX_TOKENS,  # noqa: ARG002 - same
        output_format: type | None = None,
    ) -> Completion:
        if output_format is Verdict:
            return self._judge(cell, prompt)
        task = self.tasks[prompt]
        ok = _draw(self.seed, "work", task.id, cell.label) < pass_probability(cell, task.difficulty)
        noise = 0.8 + 0.4 * _draw(self.seed, "out", task.id, cell.label)
        out = max(1, round(task.output_high * EFFORT_OUTPUT_MULT[cell.effort] * noise))
        return Completion(text=f"SIM|{task.id}|{cell.label}|{'ok' if ok else 'ko'}",
                          usage=Usage(task.input_tokens, out), stop_reason="end_turn", model=cell.model)

    def _judge(self, cell: Cell, prompt: str) -> Completion:
        m = MARK.search(prompt)
        if m is None:
            raise ValueError("judge prompt without a simulated answer")
        task, worker, truth = self.tasks[m.group(1)], parse_cell(m.group(2)), m.group(3) == "ok"
        mode = "full" if cell.capability >= worker.capability + FULL_VERIFY_GAIN - 1e-9 else "light"
        false_pass, false_fail = self.judge_error[mode]
        r = _draw(self.seed, "judge", task.id, cell.label, worker.label)
        passed = (r >= false_fail) if truth else (r < false_pass)
        usage = Usage(task.input_tokens + round(task.output_high * 0.5) + 300, 120)
        return Completion(text="{}", usage=usage, stop_reason="end_turn", model=cell.model,
                          parsed=Verdict(passed=passed, confidence=0.9, reason=f"sim {mode} judge"))


def default_strategies() -> dict[str, PolicyProtocol]:
    return {
        "J fable/xhigh": LadderPolicy([parse_cell("fable/xhigh")]),
        "M opus/high": LadderPolicy([parse_cell("opus/high")]),
        "S sonnet/high": LadderPolicy([parse_cell("sonnet/high")]),
        "F fable/low": LadderPolicy([parse_cell("fable/low")]),
        "S>O rerun": LadderPolicy([parse_cell("sonnet/medium"), parse_cell("opus/high")], verify="light"),
        "G router": Policy(),
        "G+fb router": Policy(feedback=FeedbackStore(min_n=10)),
    }


@dataclass
class StrategyReport:
    name: str
    tasks: int = 0
    accepted: int = 0  # what the strategy believed it solved
    solved: int = 0  # ground truth
    false_accepts: int = 0
    attempts: int = 0
    retries: int = 0
    tokens: int = 0
    work_cost: float = 0.0
    verify_cost: float = 0.0
    tail_cost: float = 0.0
    ledger: Ledger = field(default_factory=lambda: Ledger(None))

    @property
    def total_cost(self) -> float:
        return self.work_cost + self.verify_cost

    @property
    def cost_per_solved(self) -> float | None:
        return self.total_cost / self.solved if self.solved else None


def run_simulation(n_tasks: int = 60, seed: int = 1, strategies: dict[str, PolicyProtocol] | None = None,
                   judge_error: dict[str, tuple[float, float]] | None = None) -> tuple[list[SimTask], list[StrategyReport]]:
    tasks = make_tasks(n_tasks, seed)
    client = SimClient(tasks, seed, judge_error)
    tail = {t.id for t in sorted(tasks, key=lambda t: -t.difficulty)[: max(1, n_tasks // 10)]}
    reports = []
    for name, policy in (strategies or default_strategies()).items():
        rep = StrategyReport(name, n_tasks)
        router = Router(client, policy=policy, ledger=rep.ledger, feedback=getattr(policy, "feedback", None))
        for t in tasks:
            res = router.run(Task(prompt=t.id, kind=t.kind, risk=t.risk, id=t.id,
                                  checker=sim_checker if t.has_checker else None))
            truth = bool(truth_of(res.answer))
            rep.accepted += res.solved
            rep.solved += truth
            rep.false_accepts += res.solved and not truth
            rep.attempts += len(res.attempts)
            rep.retries += res.retries
            rep.tokens += res.usage.total
            rep.work_cost += res.work_cost_usd
            rep.verify_cost += res.verify_cost_usd
            if t.id in tail:
                rep.tail_cost += res.total_cost_usd
        reports.append(rep)
    return tasks, reports


def format_report(tasks: list[SimTask], reports: list[StrategyReport], seed: int, detail: bool = False) -> str:
    base = reports[0]
    rows = []
    for r in reports:
        ratio = (f"{r.cost_per_solved / base.cost_per_solved:.2f}x"
                 if r.cost_per_solved is not None and base.cost_per_solved else "-")
        rows.append([
            r.name, f"{r.solved}/{r.tasks}", r.false_accepts, r.retries, f"{r.tokens:,}", money(r.total_cost),
            money(r.verify_cost), money(r.cost_per_solved), ratio,
            f"{r.tail_cost / r.total_cost:.0%}" if r.total_cost else "-",
        ])
    kinds: dict[str, int] = {}
    risks: dict[str, int] = {}
    for t in tasks:
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
        risks[t.risk] = risks.get(t.risk, 0) + 1
    head = [
        f"SIMULACION · {len(tasks)} tareas · semilla {seed} · precios reales, capacidades supuestas (catalog.py)",
        "tipos: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())),
        "riesgos: " + ", ".join(
            f"{k} {v}" for k, v in sorted(risks.items(), key=lambda kv: ["low", "medium", "high", "critical"].index(kv[0]))
        ),
        f"juez: falso OK light {JUDGE_ERROR['light'][0]:.0%} / full {JUDGE_ERROR['full'][0]:.0%}; "
        f"falso KO light {JUDGE_ERROR['light'][1]:.0%} / full {JUDGE_ERROR['full'][1]:.0%}",
        "",
    ]
    tbl = table(["estrategia", "resueltas", "falsos OK", "reintentos", "tokens", "coste", "verif.", "$/resuelta",
                 f"vs {base.name.split()[0]}", "cola 10%"], rows)
    foot = [
        "",
        "resueltas = verdad del simulador; falsos OK = la estrategia dio por buena una respuesta mala;",
        "cola 10% = parte del coste que se llevan las 10% tareas mas dificiles.",
    ]
    out = head + [tbl] + foot
    if detail:
        out.append("")
        out.append("detalle por estrategia (aqui 'resueltas' = aceptadas por la estrategia; la tabla de arriba usa la verdad)")
        for r in reports:
            out += ["", f"== {r.name} ==", summarize(r.ledger.tasks()).text()]
    return "\n".join(out)
