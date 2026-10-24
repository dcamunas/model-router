"""Guess a task's kind and risk when the caller does not say. A declaration in the text first
(`risk: high`, `kind: review`, or `riesgo:` / `tipo:`; a kind may be written by another of its names, `kind:
research` is `search`): whoever writes the task knows what the answer is for, and the risk is not in the wording. Then rules (free, deterministic, bilingual es/en). Critical risk is
never inferred: a text that says "critical" is not a critical task for it, only a declaration or the caller makes
one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .task import KINDS, RISKS

# Order matters: the first rule that matches wins. A match right after a negation ("do not run tests",
# "sin código", "never migrate") is skipped: the text says what the task is not.
KIND_RULES: list[tuple[str, str]] = [
    ("review", r"\b(rev[ií]sa\w*|revisar|review|audita|auditar|audit|code review|busca (bugs|errores)|find bugs)\b"),
    ("agentic", r"\b(paso a paso|multi-?step|pipeline|migra(r|me|lo|la|los|las)?|migrate|orquest\w*|orchestrat\w*|end-to-end|de principio a fin|varios ficheros|several files)\b"),
    ("classify", r"\b(clasifica\w*|classif\w*|etiqueta\w*|label\w*|categor[ií]za\w*|categori[sz]e|sentimiento|sentiment|spam|triage|triaje)\b"),
    ("extract", r"\b(extrae\w*|extract\w*|parsea\w*|parse|campos|fields|a json|to json|en json|as json|csv|tabla de|table of)\b"),
    ("summarize", r"\b(resume\w*|resumen|summar\w*|tl;?dr|s[ií]ntesis)\b"),
    ("search", r"\b(explora\w*|explore|busca\w*|buscar|search (for|in|the)|find (the|all|every|where|which)|encuentra\w*|localiza\w*|locate|lista (los|las|todos|todas|qu[eé])|list (the|all|every|which)|d[oó]nde est[aá]\w*|where (is|are)|qu[eé] ficheros|which files|inventar\w*|inventory)\b"),
    ("code", r"\b(c[oó]digo|code|funci[oó]n|function|script|implementa\w*|implement\w*|refactor\w*|tests?|sql|python|typescript|javascript|bash|regex|query|consulta|bug ?fix|arregla|fix)\b"),
    ("analysis", r"\b(an[aá]lisis|analiza\w*|analy[sz]\w*|compara\w*|compar\w*|investiga\w*|research|eval[uú]a\w*|evaluat\w*|por qu[eé]|why|estrategia|strategy|riesgos?|risks?|pros y contras|trade-?offs?)\b"),
    ("write", r"\b(escribe\w*|write|redacta\w*|draft|post|email|correo|art[ií]culo|article|carta|letter|descripci[oó]n|description|copy|gui[oó]n)\b"),
]
# What a negation looks like in the NEGATION_WINDOW characters before a kind keyword, within the same clause.
NEGATION = re.compile(r"\b(?:do not|don'?t|does not|doesn'?t|never|not|no|without|nunca|sin|ni|tampoco)\b[^.;:!?\n]*$", re.I)
NEGATION_WINDOW = 40
# Other names people give a kind, on a declaration (`kind: research`) or as a word in the text.
KIND_ALIASES: dict[str, str] = {
    "research": "search", "investigation": "search", "investigacion": "search", "investigación": "search",
    "writing": "write", "redaccion": "write", "redacción": "write",
    "extraction": "extract", "extraccion": "extract", "extracción": "extract",
    "classification": "classify", "clasificacion": "classify", "clasificación": "classify",
    "summary": "summarize", "resumen": "summarize",
    "analyse": "analysis", "analyze": "analysis", "analisis": "analysis", "análisis": "analysis",
    "coding": "code", "programacion": "code", "programación": "code",
    "reviewing": "review", "revision": "review", "revisión": "review",
}
HIGH_RISK = r"\b(producci[oó]n|production|dinero|money|pago\w*|payment\w*|factura\w*|invoice\w*|legal|contrato|contract|cliente\w*|customer\w*|b[oó]rra(r|rl[oa]s?|l[oa]s?|d[oa]s?)?|delete|elimina\w*|irreversible|m[eé]dic[oa]s?|medicina|medicamento\w*|medical|salud|health|seguridad|security|credencial\w*|password|contrase[ñn]a)\b"
# A declaration in the head of the text (its first DECLARATION_LINES non-empty lines, where the orchestrator or the
# user writes, never the material pasted below): `risk: high`, `kind=review`, `riesgo: alto`, `tipo: code`. The
# value is a whole token (`risk: high-level` and `?risk=low` are not declarations). The first one wins.
DECLARATION_LINES = 2
SHORT_QUESTION_CHARS = 200  # a question this short with no other signal is chat
CHEAP_PROMPT_CHARS = 400  # a cheap kind of task this short is low risk unless it says otherwise
CHEAP_KINDS = frozenset({"classify", "summarize", "chat"})
SPANISH_RISK = {"bajo": "low", "medio": "medium", "alto": "high", "crítico": "critical", "critico": "critical"}
_RISK_VALUES = "|".join(list(RISKS) + list(SPANISH_RISK))
_KIND_VALUES = "|".join(list(k for k in KINDS if k != "unknown") + list(KIND_ALIASES))
DECLARED_KIND = re.compile(r"(?:^|[\s,;(\[])(?:kind|tipo)\s*[:=]\s*(" + _KIND_VALUES + r")(?=\s*(?:$|[,;.)\]]))", re.I | re.M)
DECLARED_RISK = re.compile(r"(?:^|[\s,;(\[])(?:risk|riesgo)\s*[:=]\s*(" + _RISK_VALUES + r")(?=\s*(?:$|[,;.)\]]))", re.I | re.M)
# A line made of declarations only, e.g. `kind: review, risk: low, access: read`: where `access: read` counts.
DECLARATION_LINE = re.compile(
    r"^\s*(?:(?:kind|tipo|risk|riesgo|access|acceso)\s*[:=]\s*[\w-]+\s*[,;.]?\s*)+$", re.I)
DECLARED_ACCESS = re.compile(r"(?:^|[\s,;(\[])(?:access|acceso)\s*[:=]\s*(read|write|lectura|escritura)(?=\s*(?:$|[,;.)\]]))",
                             re.I | re.M)


@dataclass(frozen=True)
class Classification:
    """Kind and risk guessed for a prompt, and the rule that decided."""

    kind: str
    risk: str
    reason: str


def declared_access(prompt: str) -> str | None:
    """Return the access the head of the text declares (`access: read` or `access: write`, also `acceso: lectura`
    or `escritura`), as `read` or `write`, or None.
    """
    m = DECLARED_ACCESS.search(head(prompt))
    if not m:
        return None
    return "write" if m.group(1).lower() in ("write", "escritura") else "read"


def read_only(prompt: str, description: str = "") -> tuple[bool, str]:
    """Say whether a subagent may go to the read-only agent, and why. Only a declaration decides: `access: read`
    (or `acceso: lectura`) on the prompt's first non-empty line, a line made of declarations only (`kind: review,
    risk: low, access: read`). A sentence that happens to end in "access: read", material pasted below, or the
    Agent tool's description never count; `access: write` in the prompt's head or the description keeps the general
    agent. The wording is never enough: write requests are often phrased like reading work ("find where the
    timeout is set and set it to 30").
    """
    if declared_access(description) == "write" or declared_access(prompt) == "write":
        return False, "declared access: write"
    first = next((line for line in prompt.splitlines() if line.strip()), "")
    if DECLARATION_LINE.match(first) and declared_access(first) == "read":
        return True, "declared access: read"
    return False, "access not declared: the general agent"


def declared(prompt: str) -> tuple[str | None, str | None]:
    """Return the (kind, risk) the head of the text declares (`kind: review`, `risk: high`, or `tipo:` /
    `riesgo: alto`), None for each one it does not. Only the first DECLARATION_LINES non-empty lines count,
    so a declaration quoted in the material (a URL, a pasted report, an eval file) never sets the risk.
    """
    lines = head(prompt)
    kind = DECLARED_KIND.search(lines)
    risk = DECLARED_RISK.search(lines)
    risk_value = risk.group(1).lower() if risk else None
    kind_value = kind.group(1).lower() if kind else None
    return (KIND_ALIASES.get(kind_value, kind_value) if kind_value else None), (SPANISH_RISK.get(risk_value, risk_value) if risk_value else None)


def head(prompt: str) -> str:
    """Return the first DECLARATION_LINES non-empty lines of `prompt`, where a declaration counts."""
    return "\n".join([line for line in prompt.splitlines() if line.strip()][:DECLARATION_LINES])


def negated(text: str, start: int) -> bool:
    """Say whether the keyword at `start` of `text` follows a negation in its clause ("do not run tests")."""
    return NEGATION.search(text[max(0, start - NEGATION_WINDOW):start]) is not None


def _first_match(pattern: str, text: str) -> re.Match[str] | None:
    """Return the first match of `pattern` in `text` that no negation precedes, or None."""
    return next((m for m in re.finditer(pattern, text) if not negated(text, m.start())), None)


def classify_rules(prompt: str, *, description: str = "") -> Classification:
    """Classify `prompt`: a declared kind or risk first (see `declared`, the prompt's head only); then the first
    kind rule that matches the prompt or the `description` (the Agent tool's few words); the risk from the
    high-risk keywords, else low for cheap kinds with short prompts, else medium. Critical is never inferred.
    """
    text = f"{prompt}\n{description}".lower()
    kind, risk = declared(prompt)
    why, risk_why = "declared", "declared"
    if kind is None:
        kind, why = "unknown", "no rule matched"
        for candidate, pattern in KIND_RULES:
            m = _first_match(pattern, text)
            if m:
                kind, why = candidate, f"rule '{m.group(0)}'"
                break
        if kind == "unknown" and len(prompt) < SHORT_QUESTION_CHARS and prompt.rstrip().endswith("?"):
            kind, why = "chat", "short question"
    if risk is None:
        if re.search(HIGH_RISK, text):
            risk, risk_why = "high", "high-risk keyword"
        elif kind in CHEAP_KINDS and len(prompt) < CHEAP_PROMPT_CHARS:
            risk, risk_why = "low", "cheap kind, short prompt"
        else:
            risk, risk_why = "medium", "default"
    return Classification(kind, risk, f"{why}; risk: {risk_why}")
