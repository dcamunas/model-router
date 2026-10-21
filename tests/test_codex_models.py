import json

from model_router.codex_models import OPENAI_PRICES, listed_models, price_note, propose


def cache(home, *models, raw=None):
    home.mkdir(exist_ok=True)
    data = raw if raw is not None else {"fetched_at": "x", "models": list(models)}
    (home / "models_cache.json").write_text(json.dumps(data) if not isinstance(data, str) else data)
    return home


def m(slug, visibility="list"):
    return {"slug": slug, "visibility": visibility, "supported_reasoning_levels": [{"effort": "low"}]}


def test_the_home_lists_only_the_models_its_picker_shows_and_a_strange_file_lists_nothing(tmp_path):
    home = cache(tmp_path / "h", m("gpt-6-sol"), m("gpt-reserve", "hide"), m("gpt-6-luna"), {"no": "slug"}, "junk",
                 {"slug": 7})
    assert listed_models(home) == ["gpt-6-sol", "gpt-6-luna"]
    assert listed_models(home, include_hidden=True) == ["gpt-6-sol", "gpt-reserve", "gpt-6-luna"]
    assert listed_models(cache(tmp_path / "empty")) is None  # an empty list says nothing about what runs
    assert listed_models(tmp_path / "missing") is None
    assert listed_models(cache(tmp_path / "bad", raw="{not json")) is None
    assert listed_models(cache(tmp_path / "shape", raw={"models": "nope"})) is None


def test_proposals_name_the_newest_of_a_family_plainly_price_it_and_respect_what_is_declared():
    ids = ["gpt-6-astra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-7-nova"]
    by_id = {p.model_id: p for p in propose(ids, {"gpt-6-luna": ("luna", "codex")})}
    assert by_id["gpt-6-astra"].declaration == "astra=codex/gpt-6-astra@10/50/3"
    assert by_id["gpt-6-sol"].declaration == "sol=codex/gpt-6-sol@2/10/1"
    assert by_id["gpt-5.6-sol"].declaration == "sol-56=codex/gpt-5.6-sol@4/20/1"
    assert by_id["gpt-5.6-terra"].declaration == "terra=codex/gpt-5.6-terra@2/12/1"
    assert by_id["gpt-5.6-luna"].declaration == "luna-56=codex/gpt-5.6-luna@0.2/1.2/0"
    assert by_id["gpt-6-luna"].declared_as == "luna" and by_id["gpt-6-luna"].declaration is None
    nova = by_id["gpt-7-nova"]
    assert nova.name == "nova" and nova.declaration is None  # no list price here: declared by hand or not at all


def test_versions_compare_as_numbers_and_no_two_proposals_or_an_anthropic_name_collide(monkeypatch):
    from model_router import codex_models

    names = {p.model_id: p.name for p in propose(["gpt-6.1-sol", "gpt-6.10-sol", "gpt-6.9-sol"], {})}
    assert names == {"gpt-6.10-sol": "sol", "gpt-6.9-sol": "sol-69", "gpt-6.1-sol": "sol-61"}
    names = [p.name for p in propose(["gpt-6-sol", "gpt-6.0-sol"], {})]
    assert len(set(names)) == 2  # 6 and 6.0 are the same version: the second one gets its own name
    monkeypatch.setitem(codex_models.OPENAI_PRICES, "gpt-7-opus", (1.0, 2.0))
    opus = propose(["gpt-7-opus"], {})[0]
    assert opus.name != "opus" and opus.declaration is not None and "/2" in opus.declaration  # unknown family: tier 2
    taken = propose(["gpt-6-sol"], {"gpt-5.5": ("sol", "codex"), "x": ("sol-gpt6sol", "openai")})
    assert taken[0].name == "sol-gpt6sol-2"


def test_a_name_another_model_holds_is_kept_apart_and_every_proposal_parses():
    from model_router.catalog import parse_external

    proposals = propose(["gpt-6-sol"], {"gpt-5.5": ("sol", "openai")})  # someone already called gpt-5.5 "sol"
    assert proposals[0].name == "sol-gpt6sol" and proposals[0].declaration is not None
    for p in propose(list(OPENAI_PRICES), {}):
        assert p.declaration is not None
        name, spec = parse_external(p.declaration)
        assert spec is not None and name == p.name and spec.id == p.model_id


def test_price_note_flags_a_declared_price_that_is_not_the_list_one():
    assert price_note("gpt-6-luna", 0.10, 0.50) is None
    note = price_note("gpt-6-luna", 2.5, 12.5)
    assert note is not None and "declared at 2.5/12.5, OpenAI lists 0.1/0.5" in note
    assert price_note("deepseek-reasoner", 1, 2) is None  # not an OpenAI model this router knows a price for
