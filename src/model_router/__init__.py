"""model-router: picks a Claude model and effort per task, verifies by risk, escalates on failure,
and keeps the ledger that says what each solved task really cost."""

from .catalog import MODELS, Cell, Usage, cost_usd, lattice, parse_cell
from .feedback import FeedbackStore
from .ledger import Ledger, summarize
from .policy import LadderPolicy, Plan, Policy
from .runner import Attempt, Router, TaskResult
from .task import Task, contains_checker, json_checker, pydantic_checker, regex_checker

__all__ = [
    "MODELS", "Cell", "Usage", "cost_usd", "lattice", "parse_cell", "FeedbackStore", "Ledger", "summarize",
    "LadderPolicy", "Plan", "Policy", "Attempt", "Router", "TaskResult", "Task", "contains_checker",
    "json_checker", "pydantic_checker", "regex_checker",
]
