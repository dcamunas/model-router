"""What the router receives: a task with its kind and its risk. The hook builds one from a subagent's brief to pick
the start cell; a judge reads its prompt and its instructions.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

KINDS: tuple[str, ...] = (
    "classify", "extract", "summarize", "search", "chat", "write", "code", "review", "analysis", "agentic", "unknown",
)
RISKS: tuple[str, ...] = ("low", "medium", "high", "critical")


@dataclass
class Task:
    """What the router receives. Only `prompt` is required; `kind` and `risk` pick the start cell, and `system` holds
    the instructions the answer had to follow, which a judge reads with it.
    """

    prompt: str
    kind: str = "unknown"
    risk: str = "medium"
    system: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}; known: {', '.join(KINDS)}")
        if self.risk not in RISKS:
            raise ValueError(f"unknown risk {self.risk!r}; known: {', '.join(RISKS)}")
        if not self.prompt.strip():
            raise ValueError("empty prompt")


FENCED_BLOCK = re.compile(r"```[\w-]*[ \t]*\r?\n(.*?)\r?\n?[ \t]*```", re.DOTALL)


def json_payload(answer: str) -> object:
    """Parse the JSON in `answer`: the first fenced block when there is one, else the whole text.

    Raises:
        json.JSONDecodeError: it is not JSON.
    """
    text = answer.strip()
    fenced = FENCED_BLOCK.search(text)
    if fenced:
        text = fenced.group(1)
    return json.loads(text)
