"""Guess a task's kind and risk when the caller does not say. Rules first (free, deterministic,
bilingual es/en); an optional Haiku call for free text the rules cannot place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pydantic import BaseModel

from .catalog import Cell, cost_usd
from .client import ClaudeClient
from .task import KINDS, RISKS

# Order matters: the first rule that matches wins.
KIND_RULES: list[tuple[str, str]] = [
    ("review", r"\b(revisa|revisar|review|audita|auditar|audit|code review|busca (bugs|errores)|find bugs|bug)\b"),
    ("agentic", r"\b(paso a paso|multi-?step|pipeline|migra(r|me|lo|la|los|las)?|migrate|orquest\w*|orchestrat\w*|end-to-end|de principio a fin|varios ficheros|several files)\b"),
    ("classify", r"\b(clasifica\w*|classif\w*|etiqueta\w*|label\w*|categor[ií]za\w*|categori[sz]e|sentimiento|sentiment|spam|triage|triaje)\b"),
    ("extract", r"\b(extrae\w*|extract\w*|parsea\w*|parse|campos|fields|a json|to json|en json|as json|csv|tabla de|table of)\b"),
    ("summarize", r"\b(resume\w*|resumen|summar\w*|tl;?dr|s[ií]ntesis)\b"),
    ("search", r"\b(explora\w*|explore|busca\w*|buscar|search (for|in|the)|find (the|all|every|where|which)|encuentra\w*|localiza\w*|locate|lista (los|las|todos|todas|qu[eé])|list (the|all|every|which)|d[oó]nde est[aá]\w*|where (is|are)|qu[eé] ficheros|which files|inventar\w*|inventory)\b"),
    ("code", r"\b(c[oó]digo|code|funci[oó]n|function|script|implementa\w*|implement\w*|refactor\w*|tests?|sql|python|typescript|javascript|bash|regex|query|consulta|bug ?fix|arregla|fix)\b"),
    ("analysis", r"\b(analiza\w*|analy[sz]\w*|compara\w*|compar\w*|investiga\w*|research|eval[uú]a\w*|evaluat\w*|por qu[eé]|why|estrategia|strategy|riesgos?|risks?|pros y contras|trade-?offs?)\b"),
    ("write", r"\b(escribe\w*|write|redacta\w*|draft|post|email|correo|art[ií]culo|article|carta|letter|descripci[oó]n|description|copy|gui[oó]n)\b"),
]
HIGH_RISK = r"\b(producci[oó]n|production|dinero|money|pago\w*|payment\w*|factura\w*|invoice\w*|legal|contrato|contract|cliente\w*|customer\w*|borra\w*|delete|elimina\w*|irreversible|m[eé]dic\w*|medical|salud|health|seguridad|security|credencial\w*|password|contrase[ñn]a)\b"
CRITICAL_RISK = r"\b(cr[ií]tic\w*|critical|urgente|urgent|sin vuelta atr[aá]s|no undo)\b"


@dataclass(frozen=True)
class Classification:
    kind: str
    risk: str
    reason: str
    cost_usd: float = 0.0


def classify_rules(prompt: str) -> Classification:
    text = prompt.lower()
    kind, why = "unknown", "no rule matched"
    for candidate, pattern in KIND_RULES:
        m = re.search(pattern, text)
        if m:
            kind, why = candidate, f"rule '{m.group(0)}'"
            break
    if kind == "unknown" and len(prompt) < 200 and prompt.rstrip().endswith("?"):
        kind, why = "chat", "short question"
    if re.search(CRITICAL_RISK, text):
        risk, risk_why = "critical", "critical keyword"
    elif re.search(HIGH_RISK, text):
        risk, risk_why = "high", "high-risk keyword"
    elif kind in ("classify", "summarize", "chat") and len(prompt) < 400:
        risk, risk_why = "low", "cheap kind, short prompt"
    else:
        risk, risk_why = "medium", "default"
    return Classification(kind, risk, f"{why}; risk: {risk_why}")


class LlmClassification(BaseModel):
    kind: str
    risk: str
    reason: str


CLASSIFIER_SYSTEM = f"""Classify a task for a model router. Reply with the JSON object requested.

kind, one of: {", ".join(k for k in KINDS if k != "unknown")}.
- classify: assign labels or categories. extract: pull fields out of text into a structure.
- summarize: condense text. search: find or list things in a codebase or corpus without changing
  them. chat: a short answer to a question. write: produce prose or copy.
- code: write or change code. review: find bugs or audit code or a document. analysis: reason,
  compare, investigate, recommend. agentic: a long multi-step job across several artefacts.

risk, one of: {", ".join(RISKS)}. How bad a wrong answer is: low (easily noticed and redone),
medium (default), high (touches money, customers, production, legal, health or irreversible
actions), critical (a wrong answer is very costly and hard to detect).

reason: one short sentence."""

CLASSIFIER_CELL = Cell("claude-haiku-5-5", "low")


def classify_llm(client: ClaudeClient, prompt: str) -> Classification:
    completion = client.complete(CLASSIFIER_CELL, prompt=f"<task>\n{prompt}\n</task>", system=CLASSIFIER_SYSTEM,
                                 max_tokens=400, output_format=LlmClassification)
    cost = cost_usd(CLASSIFIER_CELL.model, completion.usage)
    parsed = completion.parsed
    if not isinstance(parsed, LlmClassification) or parsed.kind not in KINDS or parsed.risk not in RISKS:
        fallback = classify_rules(prompt)
        return Classification(fallback.kind, fallback.risk, f"llm unusable, rules: {fallback.reason}", cost)
    return Classification(parsed.kind, parsed.risk, f"llm: {parsed.reason}", cost)
