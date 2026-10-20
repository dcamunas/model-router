"""Model catalogue: prices, effort levels, a capability estimate per (model, effort) cell and the
rules that walk between cells.

Prices are Anthropic first-party API rates in USD per million tokens as cached by the claude-api
skill on PRICES_AS_OF. Cache writes cost 1.25x input on every model; cache reads are the documented
per-model rate (Haiku 5.5's is an assumption: 10% of input). Haiku 5.5 bills 5x above 100K prompt
tokens. The ledger always prices the real `usage` returned by the API.

Two tables are assumptions, not measurements. They drive planning (expected cost of a cell, which
cell to escalate to) and the offline simulation, and the feedback store is there to overrule them
with real verdicts:

* EFFORT_OUTPUT_MULT - output tokens (thinking + answer) at each effort relative to `high`.
* CAPABILITY_TIER / CAPABILITY_EFFORT - a 0-1 capability score per cell. The model tier dominates
  and effort fine-tunes within the tier, following Anthropic's published guidance that a frontier
  model at low effort often beats a lower tier at high effort.

Models of other providers (OpenAI, DeepSeek, xAI) are not shipped here: their ids and prices change
and a wrong number is worse than none. The user declares them in config.json with `router config
--external NAME=PROVIDER/MODEL_ID@INPUT/OUTPUT` and `load_external` registers them at start-up. They
judge and arbitrate; they never do the work (the routing lattice is Anthropic only), so their tier is
a fixed assumption (EXTERNAL_TIER) and their cached tokens are priced at the input rate.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .paths import MODELS_ENV, read_config

PRICES_AS_OF = "2026-10-06"
KNOWN_PROVIDERS: tuple[str, ...] = ("anthropic", "openai", "deepseek", "xai")
EXTERNAL_TIER = 2  # an external judge is assumed as capable as Opus; it is never routed, so this only orders cells
TIER_CAPABILITY: dict[int, float] = {0: 0.40, 1: 0.60, 2: 0.75, 3: 0.90}
EXTERNAL_SYNTAX = re.compile(r"^([a-z][a-z0-9-]*)=(?:([a-z]+)/([^@\s]+)@([0-9.]+)/([0-9.]+))?$")

EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
EFFORT_OUTPUT_MULT: dict[str, float] = {"low": 0.5, "medium": 0.75, "high": 1.0, "xhigh": 1.6, "max": 2.5}
CAPABILITY_TIER: dict[str, float] = {"haiku": 0.40, "sonnet": 0.60, "opus": 0.75, "fable": 0.90}
CAPABILITY_EFFORT: dict[str, float] = {"low": -0.06, "medium": -0.03, "high": 0.0, "xhigh": 0.03, "max": 0.05}

# Reference request used to compare cells with each other (planning only).
PLAN_INPUT_TOKENS = 2000
PLAN_OUTPUT_TOKENS_AT_HIGH = 1500


@dataclass(frozen=True)
class ModelSpec:
    """One model of the catalogue: ids, list prices in USD per million tokens and the API traits the router
    relies on (default effort, server-side fallbacks, output cap, long-prompt pricing).
    """

    id: str
    short: str  # the name Claude Code uses in agent frontmatter and in the Agent tool
    tier: int  # capability order between models: 0 Haiku .. 3 Fable
    input: float  # USD per million tokens
    output: float
    cache_read: float
    cache_write: float
    default_effort: str  # what the API applies when `effort` is omitted
    server_fallbacks: bool  # accepts fallbacks="default" (Haiku does not)
    max_output: int = 128_000
    long_prompt_from: int | None = None  # prompt tokens above which `long_prompt_mult` applies
    long_prompt_mult: float = 1.0
    provider: str = "anthropic"  # who serves it; only Anthropic models do the work, the rest judge


MODELS: dict[str, ModelSpec] = {
    "claude-haiku-5-5": ModelSpec(
        "claude-haiku-5-5", "haiku", 0, 0.10, 0.50, 0.01, 0.125, "medium", False,
        long_prompt_from=100_000, long_prompt_mult=5.0,
    ),
    "claude-sonnet-5-5": ModelSpec("claude-sonnet-5-5", "sonnet", 1, 2.00, 10.00, 0.20, 2.50, "high", True),
    "claude-opus-5-5": ModelSpec("claude-opus-5-5", "opus", 2, 4.00, 20.00, 0.20, 5.00, "medium", True),
    "claude-fable-5-1": ModelSpec("claude-fable-5-1", "fable", 3, 10.00, 50.00, 0.25, 12.50, "high", True),
}
SHORT_TO_ID: dict[str, str] = {spec.short: spec.id for spec in MODELS.values()}


def parse_external(text: str) -> tuple[str, ModelSpec | None]:
    """Parse one `router config --external` value, `NAME=PROVIDER/MODEL_ID@INPUT/OUTPUT` (prices in USD per
    million tokens), into the short name and its spec; `NAME=` alone means remove, and returns None.

    Raises:
        ValueError: the syntax, the provider or the name is not acceptable.
    """
    m = EXTERNAL_SYNTAX.match(text.strip())
    if m is None:
        raise ValueError(f"expected NAME=PROVIDER/MODEL_ID@INPUT/OUTPUT (or NAME= to remove), got {text!r}")
    name, provider, model_id, price_in, price_out = m.groups()
    if any(spec.short == name and spec.provider == "anthropic" for spec in MODELS.values()):
        raise ValueError(f"{name!r} is an Anthropic model; pick another short name")
    if provider is None:
        return name, None
    if provider not in KNOWN_PROVIDERS or provider == "anthropic":
        raise ValueError(f"unknown provider {provider!r}; known: {', '.join(p for p in KNOWN_PROVIDERS if p != 'anthropic')}")
    cost_in, cost_out = float(price_in), float(price_out)
    return name, ModelSpec(model_id, name, EXTERNAL_TIER, cost_in, cost_out, cost_in, cost_in, "medium", False,
                           provider=provider)


def load_external(config: dict[str, Any]) -> list[str]:
    """Register the external models declared in `config["external"]` (as `router config --external` writes
    them) in the catalogue, replacing any registered before, and return their short names. Entries that
    do not parse are skipped.
    """
    for model_id in [m for m, spec in MODELS.items() if spec.provider != "anthropic"]:
        SHORT_TO_ID.pop(MODELS.pop(model_id).short, None)
    loaded = []
    declared = config.get("external")
    for name, entry in (declared.items() if isinstance(declared, dict) else []):
        if not isinstance(entry, dict):
            continue
        declared_as = f"{name}={entry.get('provider')}/{entry.get('id')}@{entry.get('input')}/{entry.get('output')}"
        try:
            _, spec = parse_external(declared_as)
        except ValueError:
            continue
        if spec is not None:
            MODELS[spec.id] = spec
            SHORT_TO_ID[spec.short] = spec.id
            loaded.append(spec.short)
    return loaded


def external_models() -> list[ModelSpec]:
    """Return the registered models of other providers, the ones that may judge but never do the work."""
    return [spec for spec in MODELS.values() if spec.provider != "anthropic"]


class UnknownModelError(ValueError):
    """A model name outside the catalogue. `name` is the offending value and `known` the accepted short names."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.known = list(SHORT_TO_ID)
        super().__init__(f"unknown model {name!r}; known: {', '.join(self.known)}")


@dataclass(frozen=True)
class Usage:
    """Token counts of one API call as billed: input, output and prompt-cache reads and writes."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input": self.input_tokens,
            "output": self.output_tokens,
            "cache_read": self.cache_read_tokens,
            "cache_write": self.cache_write_tokens,
        }


def cost_usd(model_id: str, usage: Usage) -> float:
    """Return the list-price cost in USD of `usage` on `model_id`, with the long-prompt multiplier applied
    when the prompt (input plus cache tokens) exceeds the model's threshold.
    """
    spec = MODELS[model_id]
    prompt_tokens = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    mult = spec.long_prompt_mult if spec.long_prompt_from and prompt_tokens > spec.long_prompt_from else 1.0
    raw = (
        usage.input_tokens * spec.input
        + usage.output_tokens * spec.output
        + usage.cache_read_tokens * spec.cache_read
        + usage.cache_write_tokens * spec.cache_write
    )
    return mult * raw / 1_000_000


def resolve_model(name: str) -> str:
    """Return the catalogue id for `name`, a short name ('opus') or an id ('claude-opus-5-5').

    Raises:
        UnknownModelError: `name` is not in the catalogue.
    """
    if name in MODELS:
        return name
    if name in SHORT_TO_ID:
        return SHORT_TO_ID[name]
    raise UnknownModelError(name)


def parse_models(text: str | Iterable[str]) -> list[str]:
    """Return the model ids named in `text` ('haiku,sonnet,opus', short names or ids, any order, or
    an iterable of them) in catalogue order.

    Raises:
        UnknownModelError: a name is not in the catalogue, so a typo never silently changes the lattice.
        ValueError: the list is empty.
    """
    names = [n.strip() for n in text.split(",")] if isinstance(text, str) else [str(n).strip() for n in text]
    ids = {resolve_model(n) for n in names if n}
    if not ids:
        raise ValueError("empty model list")
    return [m for m in MODELS if m in ids]


def available_models() -> list[str]:
    """Return the models this installation may route to, in catalogue order: Anthropic models only. The
    MODEL_ROUTER_MODELS variable wins, then `models` in config.json (written by `router install --models`),
    else every Anthropic model in the catalogue. Use it when a plan or an account lacks a model (no Fable, say).
    """
    raw = os.environ.get(MODELS_ENV)
    configured = read_config().get("models")
    ids = parse_models(raw) if raw else parse_models(configured) if configured else list(MODELS)
    return [m for m in ids if MODELS[m].provider == "anthropic"]


@dataclass(frozen=True)
class Cell:
    """One (model, effort) combination: the unit the router chooses between."""

    model: str
    effort: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "model", resolve_model(self.model))
        if self.effort not in EFFORTS:
            raise ValueError(f"unknown effort {self.effort!r}; known: {', '.join(EFFORTS)}")

    @property
    def spec(self) -> ModelSpec:
        return MODELS[self.model]

    @property
    def tier(self) -> int:
        return self.spec.tier

    @property
    def effort_rank(self) -> int:
        return EFFORTS.index(self.effort)

    @property
    def label(self) -> str:
        return f"{self.spec.short}/{self.effort}"

    @property
    def capability(self) -> float:
        base = CAPABILITY_TIER.get(self.spec.short, TIER_CAPABILITY[self.spec.tier])
        return round(base + CAPABILITY_EFFORT[self.effort], 4)

    def claude_code(self) -> dict[str, str]:
        """Return the `model` and `effort` values Claude Code's Agent tool and agent frontmatter accept."""
        return {"model": self.spec.short, "effort": self.effort}

    def expected_cost(
        self, input_tokens: int = PLAN_INPUT_TOKENS, output_tokens_at_high: int = PLAN_OUTPUT_TOKENS_AT_HIGH
    ) -> float:
        """Return the list-price cost of a request of `input_tokens` on this cell, with the output sized as
        `output_tokens_at_high` scaled by the effort's multiplier.
        """
        out = round(output_tokens_at_high * EFFORT_OUTPUT_MULT[self.effort])
        return cost_usd(self.model, Usage(input_tokens=input_tokens, output_tokens=out))

    def __str__(self) -> str:
        return self.label


def parse_cell(text: str) -> Cell:
    """Parse 'opus/medium', 'claude-opus-5-5/high' or just 'sonnet' (the model's default effort) into a Cell."""
    name, _, effort = text.strip().partition("/")
    model = resolve_model(name)
    return Cell(model, effort or MODELS[model].default_effort)


def lattice(models: Iterable[str] | None = None, efforts: Iterable[str] | None = None) -> list[Cell]:
    """Return every (model, effort) cell of the available models (or of `models`), cheapest first on the
    reference request.
    """
    cells = [Cell(m, e) for m in (models or available_models()) for e in (efforts or EFFORTS)]
    return sorted(cells, key=lambda c: (c.expected_cost(), c.capability))


def nearest_available(cell: Cell, models: Iterable[str] | None = None) -> Cell:
    """Return `cell` itself when its model is available (an external judge always is: its availability is
    its API key, checked when it is called); otherwise the same effort on the most capable available
    model below it, or the least capable one above it when nothing is below. The ladder then escalates
    from there as usual.
    """
    if cell.spec.provider != "anthropic":
        return cell
    avail = [resolve_model(m) for m in (models or available_models())]
    if cell.model in avail:
        return cell
    specs = sorted((MODELS[m] for m in avail), key=lambda spec: spec.tier)
    below = [spec for spec in specs if spec.tier < cell.tier]
    return Cell((below[-1] if below else specs[0]).id, cell.effort)


def step_up(current: Cell, cells: Iterable[Cell], *, min_gain: float = 0.08, tie: float = 0.10) -> Cell | None:
    """Return the cheapest cell at least `min_gain` more capable than `current`. Cells whose expected cost
    is within `tie` of that cheapest one compete on capability, so a frontier model at low effort
    wins over a lower tier at max effort when they cost about the same.
    """
    cands = [c for c in cells if c.capability >= current.capability + min_gain - 1e-9]
    if not cands:
        return None
    floor = min(c.expected_cost() for c in cands)
    close = [c for c in cands if c.expected_cost() <= floor * (1 + tie)]
    return max(close, key=lambda c: (c.capability, -c.expected_cost()))


def step_down(current: Cell, cells: Iterable[Cell], *, max_loss: float = 0.08, min_saving: float = 0.20) -> Cell | None:
    """Return the most capable cell that gives up at most `max_loss` capability and saves at least
    `min_saving` of the expected cost. Used only for exploration by the feedback store.
    """
    cands = [
        c
        for c in cells
        if current.capability - max_loss - 1e-9 <= c.capability < current.capability
        and c.expected_cost() <= current.expected_cost() * (1 - min_saving)
    ]
    if not cands:
        return None
    return max(cands, key=lambda c: (c.capability, -c.expected_cost()))


def ladder(start: Cell, cells: Iterable[Cell], steps: int, *, min_gain: float = 0.08) -> list[Cell]:
    """Return `start` followed by up to `steps - 1` escalations, each the cell `step_up` picks from `cells`."""
    cells = list(cells)
    out = [start]
    while len(out) < steps:
        nxt = step_up(out[-1], cells, min_gain=min_gain)
        if nxt is None:
            break
        out.append(nxt)
    return out
