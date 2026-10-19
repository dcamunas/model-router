from model_router.classify import classify_rules


def test_rules_place_common_spanish_and_english_prompts():
    assert classify_rules("Revisa este script SQL antes de producción").kind == "review"
    assert classify_rules("Revisa este script SQL antes de producción").risk == "high"
    assert classify_rules("clasifica estos tickets en 5 categorías").kind == "classify"
    assert classify_rules("Write a LinkedIn post about data migrations").kind == "write"
    assert classify_rules("Extrae los campos de esta factura a JSON").kind == "extract"
    assert classify_rules("¿Qué hora es en Tokio?").kind == "chat"
    assert classify_rules("Migra el pipeline paso a paso a Dagster").kind == "agentic"
    assert classify_rules("Es crítico: borra los duplicados").risk == "critical"


def test_search_prompts_are_their_own_kind():
    assert classify_rules("Explora el repo y lista los módulos que tocan la base de datos").kind == "search"
    assert classify_rules("Find where the config is loaded").kind == "search"
    assert classify_rules("Busca errores en este módulo").kind == "review"
    assert classify_rules("Implementa una función que devuelva una lista").kind == "code"


def test_the_reason_names_the_rule_and_the_risk_signal_in_spanish():
    cls = classify_rules("Revisa este script SQL antes de producción")
    assert cls.reason == "rule 'revisa'; risk: high-risk keyword"
    assert classify_rules("What time is it?").reason == "short question; risk: cheap kind, short prompt"
