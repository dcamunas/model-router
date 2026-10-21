from model_router.classify import classify_rules


def test_rules_place_common_spanish_and_english_prompts():
    assert classify_rules("Revisa este script SQL antes de producción").kind == "review"
    assert classify_rules("Revisa este script SQL antes de producción").risk == "high"
    assert classify_rules("clasifica estos tickets en 5 categorías").kind == "classify"
    assert classify_rules("Write a LinkedIn post about data migrations").kind == "write"
    assert classify_rules("Extrae los campos de esta factura a JSON").kind == "extract"
    assert classify_rules("¿Qué hora es en Tokio?").kind == "chat"
    assert classify_rules("Migra el pipeline paso a paso a Dagster").kind == "agentic"
    assert classify_rules("Es crítico: borra los duplicados").risk == "high"  # "borra"; "crítico" alone says nothing


def test_risk_keywords_do_not_fire_inside_other_words():
    assert classify_rules("Escribe un borrador del post sobre migraciones").risk == "medium"
    assert classify_rules("Mejora la medición de latencia del script").risk == "medium"
    assert classify_rules("Bórralo todo de la tabla de clientes").risk == "high"
    assert classify_rules("Es una decisión crítica para el equipo").risk == "medium"  # critical is declared, never read
    assert classify_rules("Revisa el historial médico del paciente").risk == "high"


def test_accented_forms_and_bug_fixes_land_on_their_kind():
    assert classify_rules("Haz un análisis de ventas por región").kind == "analysis"
    assert classify_rules("Revísalo, por favor").kind == "review"
    assert classify_rules("Fix this bug in the parser").kind == "code"
    assert classify_rules("Find bugs in this module").kind == "review"


def test_search_prompts_are_their_own_kind():
    assert classify_rules("Explora el repo y lista los módulos que tocan la base de datos").kind == "search"
    assert classify_rules("Find where the config is loaded").kind == "search"
    assert classify_rules("Busca errores en este módulo").kind == "review"
    assert classify_rules("Implementa una función que devuelva una lista").kind == "code"


def test_a_declared_kind_or_risk_beats_the_rules_and_critical_comes_only_that_way():
    cls = classify_rules("risk: critical\nRevisa este script SQL antes de producción")
    assert (cls.kind, cls.risk, cls.reason) == ("review", "critical", "rule 'revisa'; risk: declared")
    cls = classify_rules("Brief for the subagent. kind=analysis, riesgo: low. Find why the build is slow.")
    assert (cls.kind, cls.risk, cls.reason) == ("analysis", "low", "declared; risk: declared")
    assert classify_rules("Tipo: Code. Arregla el parser").kind == "code"
    assert classify_rules("the risk: none of this matters, classify the tickets").risk == "low"  # not a level: ignored
    assert classify_rules("risk:high; classify the tickets").risk == "high"
    assert classify_rules("risk: high classify the tickets").risk == "low"  # a value glued to prose is not a declaration
    assert classify_rules("Urgente y sin vuelta atrás: resume este texto").risk == "low"  # no critical keywords
    assert classify_rules("riesgo: alto, tipo: summarize\nResume este texto").risk == "high"
    assert classify_rules("Riesgo: crítico. Resume este texto").risk == "critical"


def test_a_declaration_quoted_in_the_material_or_glued_to_a_word_is_not_one():
    url = "Fix the bug in https://api.example.com/items?kind=search&risk=low that deletes customer invoices in production"
    assert (classify_rules(url).kind, classify_rules(url).risk) == ("code", "high")
    report = "Summarise this pentest report for the board\n\nFinding 1\nRisk: Low\nFinding 2\nRisk: Critical"
    assert classify_rules(report).risk == "low"  # the head is 'Summarise...' and 'Finding 1': cheap kind, short prompt
    assert classify_rules("Assess the risk: high cholesterol in this cohort").risk == "medium"
    assert classify_rules("risk: high-level overview of the design").risk == "medium"
    assert classify_rules("Run `router run --kind=code --risk=low` on it").risk == "medium"
    assert classify_rules("Review it.\nrisk: high\nMaterial:\nrisk: low").risk == "high"  # second line still counts


def test_the_llm_classifier_keeps_a_declared_value_and_skips_the_call_when_both_are_declared():
    from model_router.classify import LlmClassification, classify_llm
    from model_router.testing import FakeClient, Reply

    client = FakeClient(judge={"*": [Reply(parsed=LlmClassification(kind="chat", risk="low", reason="a question"))]})
    both = classify_llm(client, "kind: review, risk: high. Is this query safe?")
    assert (both.kind, both.risk, both.cost_usd) == ("review", "high", 0.0) and client.calls == []
    one = classify_llm(client, "risk: high. Is this query safe?")
    assert (one.kind, one.risk) == ("chat", "high") and one.reason == "declared risk, llm: a question" and len(client.calls) == 1
    client = FakeClient(judge={"*": [Reply(parsed=LlmClassification(kind="code", risk="critical", reason="prod"))]})
    undeclared = classify_llm(client, "critical fix to the login page")
    assert undeclared.risk == "high" and undeclared.reason.endswith("(critical is never inferred: high)")
    assert classify_llm(client, "risk: critical. fix the login page").risk == "critical"


def test_the_reason_names_the_rule_and_the_risk_signal_in_spanish():
    cls = classify_rules("Revisa este script SQL antes de producción")
    assert cls.reason == "rule 'revisa'; risk: high-risk keyword"
    assert classify_rules("What time is it?").reason == "short question; risk: cheap kind, short prompt"
