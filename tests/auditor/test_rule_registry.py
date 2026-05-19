"""Tests for app/auditor/rule_registry.py — cached AST access."""

from app.auditor import rule_registry


def test_get_regulations_caches_after_first_call(seeded_db):
    first = rule_registry.get_regulations()
    second = rule_registry.get_regulations()
    assert first is second  # same list object — cached


def test_get_regulations_loads_seeded_rules(seeded_db):
    regs = rule_registry.get_regulations()
    rule_ids = {r.rule_id for r in regs}
    assert "FINRA_2111" in rule_ids
    assert "FINRA_2090" in rule_ids
    assert "STATIC_PORTFOLIO" in rule_ids


def test_get_rule_index_keyed_by_rule_id(seeded_db):
    idx = rule_registry.get_rule_index()
    assert "FINRA_2111" in idx
    assert idx["FINRA_2111"].rule_id == "FINRA_2111"
    # Cached on subsequent calls
    assert rule_registry.get_rule_index() is idx


def test_get_regulation_returns_named_rule(seeded_db):
    reg = rule_registry.get_regulation("FINRA_2090")
    assert reg is not None
    assert reg.bypass_tsf is True


def test_get_regulation_unknown_returns_none(seeded_db):
    assert rule_registry.get_regulation("NOT_A_RULE") is None


def test_reload_regulations_drops_and_rebuilds_cache(seeded_db):
    first = rule_registry.get_regulations()
    rule_registry.reload_regulations()
    second = rule_registry.get_regulations()
    # Different objects after reload (cache rebuilt)
    assert first is not second
    assert {r.rule_id for r in first} == {r.rule_id for r in second}
