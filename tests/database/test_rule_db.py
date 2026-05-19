"""Tests for app/database/rule_db.py — AST hydration from SQLite."""

import pytest

from app.auditor.models import KYCOperator, TriggerOperator
from app.database.rule_db import load_all_regulations


def test_load_all_regulations_hydrates_full_tree(seeded_db):
    regs = load_all_regulations(seeded_db)
    assert {r.rule_id for r in regs} >= {"FINRA_2090", "FINRA_2111", "SEC_REG_BI"}


def test_load_all_regulations_attaches_clauses(seeded_db):
    regs = load_all_regulations(seeded_db)
    finra2111 = next(r for r in regs if r.rule_id == "FINRA_2111")
    assert len(finra2111.clauses) >= 1
    clause = finra2111.clauses[0]
    assert clause.rule_id == "FINRA_2111"


def test_load_all_regulations_attaches_conditions(seeded_db):
    regs = load_all_regulations(seeded_db)
    finra2111 = next(r for r in regs if r.rule_id == "FINRA_2111")
    cond = finra2111.clauses[0].conditions[0]
    assert cond.trigger_operator == TriggerOperator.CONTAINS
    assert cond.trigger_value == "HIGH_RISK_PRODUCT"


def test_load_all_regulations_attaches_kyc_with_fallback(seeded_db):
    regs = load_all_regulations(seeded_db)
    finra2111 = next(r for r in regs if r.rule_id == "FINRA_2111")
    kyc = finra2111.clauses[0].conditions[0].kyc_requirements[0]
    assert kyc.operator == KYCOperator.EQUALS
    assert kyc.fallback is not None
    assert kyc.fallback.evidence_id == "EVID_RISK_OVERRIDE_ACK"
    assert kyc.fallback.is_ack is True


def test_load_all_regulations_handles_kyc_without_fallback(seeded_db):
    """IRS_WASH_SALE in the seed has a KYC with no evidence_fallback row."""
    regs = load_all_regulations(seeded_db)
    wash = next(r for r in regs if r.rule_id == "IRS_WASH_SALE")
    kyc = wash.clauses[0].conditions[0].kyc_requirements[0]
    assert kyc.fallback is None


def test_load_all_regulations_bypass_tsf_flag(seeded_db):
    regs = load_all_regulations(seeded_db)
    assert next(r for r in regs if r.rule_id == "FINRA_2090").bypass_tsf is True
    assert next(r for r in regs if r.rule_id == "FINRA_2111").bypass_tsf is False
