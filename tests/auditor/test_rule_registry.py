"""Tests for app/auditor/rule_registry.py — public surface only.

Public API: get_regulations, get_rule_index.
"""

from app.auditor import rule_registry


def test_get_regulations_caches_after_first_call(seeded_db):
    first = rule_registry.get_regulations()
    second = rule_registry.get_regulations()
    assert first is second


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
