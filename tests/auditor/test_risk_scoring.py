"""Tests for app/auditor/risk_scoring.py — the SBC math layer."""

import math

import pytest

from app.auditor.models import ConstraintDelta, FailedRuleDetail
from app.auditor.risk_scoring import (
    DEFAULT_OMEGA,
    DEFAULT_RULE_WEIGHTS,
    LAMBDA_TRADE_SIZE_SCALING,
    RuleRiskComponent,
    SBC_GATE_AUTO,
    SBC_GATE_ESCALATE,
    SBCRiskScore,
    _compute_composite,
    _compute_trade_size_factor,
    classify_gate_decision,
    classify_risk,
    compute_audit_risk,
    get_rule_weight,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

def test_lambda_matches_documented_value():
    """CLAUDE.md and the in-code rationale both reference λ=5."""
    assert LAMBDA_TRADE_SIZE_SCALING == 5.0


def test_gate_thresholds_have_expected_values():
    assert SBC_GATE_AUTO == 0.20
    assert SBC_GATE_ESCALATE == 0.80


def test_critical_rule_weights_above_escalate_floor():
    for rule_id, omega in DEFAULT_RULE_WEIGHTS.items():
        # All four CRITICAL absolutes plus STATIC_PORTFOLIO must escalate alone.
        if rule_id in {"SEC_10b5", "FINRA_2090", "SEC_144", "FINRA_3280",
                       "FINRA_3240", "STATIC_PORTFOLIO"}:
            assert omega >= SBC_GATE_ESCALATE, (rule_id, omega)


# ---------------------------------------------------------------------------
# classify_gate_decision / classify_risk
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("score, gate", [
    (0.0, "AUTO_APPROVE"),
    (SBC_GATE_AUTO - 0.001, "AUTO_APPROVE"),
    (SBC_GATE_AUTO, "REFINEMENT"),
    (0.5, "REFINEMENT"),
    (SBC_GATE_ESCALATE - 0.001, "REFINEMENT"),
    (SBC_GATE_ESCALATE, "HUMAN_ESCALATION"),
    (1.0, "HUMAN_ESCALATION"),
])
def test_classify_gate_decision_at_boundaries(score, gate):
    assert classify_gate_decision(score) == gate


@pytest.mark.parametrize("score, level", [
    (0.0, "LOW"), (0.19, "LOW"),
    (0.2, "MODERATE"), (0.49, "MODERATE"),
    (0.5, "HIGH"), (0.79, "HIGH"),
    (0.8, "CRITICAL"), (1.0, "CRITICAL"),
])
def test_classify_risk_buckets(score, level):
    assert classify_risk(score) == level


# ---------------------------------------------------------------------------
# get_rule_weight
# ---------------------------------------------------------------------------

def test_get_rule_weight_known():
    assert get_rule_weight("SEC_10b5") == DEFAULT_RULE_WEIGHTS["SEC_10b5"]


def test_get_rule_weight_unknown_falls_back_to_default_omega():
    assert get_rule_weight("UNKNOWN_RULE") == DEFAULT_OMEGA


# ---------------------------------------------------------------------------
# _compute_trade_size_factor
# ---------------------------------------------------------------------------

def test_tsf_bypass_always_one():
    assert _compute_trade_size_factor(10.0, 100.0, bypass=True) == 1.0
    # Zero equity is fine when bypass=True (no division performed).
    assert _compute_trade_size_factor(10.0, 0.0001, bypass=True) == 1.0


def test_tsf_small_trade_small_factor():
    # 1% trade, λ=5 → TSF ≈ 1 − e^(−0.05) ≈ 0.0488
    tsf = _compute_trade_size_factor(1.0, 100.0)
    assert tsf == pytest.approx(1.0 - math.exp(-0.05))
    assert 0.0 < tsf < 0.1


def test_tsf_saturates_for_huge_trades():
    tsf = _compute_trade_size_factor(1e9, 1.0)  # S_norm enormous
    assert tsf == pytest.approx(1.0, abs=1e-9)


def test_tsf_custom_lambda():
    tsf5 = _compute_trade_size_factor(10.0, 100.0, lam=5.0)
    tsf20 = _compute_trade_size_factor(10.0, 100.0, lam=20.0)
    # Higher λ → more aggressive ramp → larger TSF for the same trade
    assert tsf20 > tsf5


# ---------------------------------------------------------------------------
# _compute_composite
# ---------------------------------------------------------------------------

def test_compute_composite_empty_returns_zero():
    assert _compute_composite([]) == 0.0


def test_compute_composite_single_component_returns_its_score():
    comp = RuleRiskComponent(
        rule_id="R", bypass_tsf=False, score=0.4,
        trade_size_factor=1.0, evidence_coverage=0.0, omega=0.4,
        description="d", fired=True,
    )
    assert _compute_composite([comp]) == pytest.approx(0.4, abs=1e-6)


def test_compute_composite_complement_product():
    # R = 1 − (1 − 0.5)(1 − 0.5) = 0.75
    c = RuleRiskComponent(
        rule_id="R", bypass_tsf=False, score=0.5,
        trade_size_factor=1.0, evidence_coverage=0.0, omega=0.5,
        description="", fired=True,
    )
    assert _compute_composite([c, c]) == pytest.approx(0.75, abs=1e-6)


# ---------------------------------------------------------------------------
# compute_audit_risk
# ---------------------------------------------------------------------------

def test_compute_audit_risk_no_failures_is_auto_approve():
    delta = ConstraintDelta(allow=True)
    risk = compute_audit_risk(delta, detected_signals=[], trade_size_usd=0.0,
                              total_equity_usd=100.0)
    assert risk.composite_score == 0.0
    assert risk.gate_decision == "AUTO_APPROVE"
    assert risk.components == []
    assert risk.trade_size_factor == 0.0  # zero trade


def test_compute_audit_risk_zero_equity_positive_trade_summary_tsf_one():
    delta = ConstraintDelta(allow=True)
    risk = compute_audit_risk(delta, [], trade_size_usd=100.0, total_equity_usd=0.0)
    assert risk.trade_size_factor == 1.0


def test_compute_audit_risk_zero_equity_zero_trade_summary_tsf_zero():
    delta = ConstraintDelta(allow=True)
    risk = compute_audit_risk(delta, [], trade_size_usd=0.0, total_equity_usd=0.0)
    assert risk.trade_size_factor == 0.0


def test_compute_audit_risk_critical_rule_bypass_escalates():
    detail = FailedRuleDetail(
        rule_id="FINRA_2090", clause_id="2090.01",
        bypass_tsf=True, description="KYC bypass",
    )
    delta = ConstraintDelta(allow=False, failed_details=[detail])
    risk = compute_audit_risk(delta, [], trade_size_usd=1.0, total_equity_usd=1e9)
    assert risk.gate_decision == "HUMAN_ESCALATION"
    assert risk.components[0].trade_size_factor == 1.0  # TSF bypassed
    assert risk.components[0].bypass_tsf is True


def test_compute_audit_risk_evidence_cures():
    detail = FailedRuleDetail(
        rule_id="FINRA_2111", clause_id="2111.01",
        description="Suitability", missing_evidence_id="EV1",
    )
    delta = ConstraintDelta(allow=False, failed_details=[detail])
    risk = compute_audit_risk(delta, [], trade_size_usd=100_000, total_equity_usd=100_000,
                              evidence_scores={"EV1": 1.0})
    assert risk.composite_score == pytest.approx(0.0, abs=1e-6)
    assert risk.gate_decision == "AUTO_APPROVE"


def test_compute_audit_risk_multi_evidence_takes_min_weakest_link():
    # Two details in the same rule, two evidence IDs, one scored 0.9 one 0.1.
    # min() should win → c_ev = 0.1, R = TSF · 0.9 · ω
    d1 = FailedRuleDetail(rule_id="R", clause_id="c1", description="d1",
                          missing_evidence_id="A", weight=0.6)
    d2 = FailedRuleDetail(rule_id="R", clause_id="c2", description="d2",
                          missing_evidence_id="B", weight=0.6)
    delta = ConstraintDelta(allow=False, failed_details=[d1, d2])
    risk = compute_audit_risk(delta, [], trade_size_usd=100_000, total_equity_usd=100_000,
                              evidence_scores={"A": 0.9, "B": 0.1})
    assert risk.components[0].evidence_coverage == pytest.approx(0.1, abs=1e-6)


def test_compute_audit_risk_detail_without_evidence_id_anchors_c_ev_zero():
    detail = FailedRuleDetail(rule_id="R", clause_id="c", description="d", weight=0.6)
    delta = ConstraintDelta(allow=False, failed_details=[detail])
    risk = compute_audit_risk(delta, [], trade_size_usd=10.0, total_equity_usd=100.0,
                              evidence_scores={"X": 1.0})
    assert risk.components[0].evidence_coverage == 0.0


def test_compute_audit_risk_omega_takes_max_across_details():
    """Per-detail max: insufficient-funds + concentration → max(0.85, dynamic_0.92)."""
    d1 = FailedRuleDetail(rule_id="STATIC_PORTFOLIO", clause_id="STATIC.01",
                          bypass_tsf=True, description="funds")
    d2 = FailedRuleDetail(rule_id="STATIC_PORTFOLIO", clause_id="STATIC.05",
                          description="conc", weight=0.92)
    delta = ConstraintDelta(allow=False, failed_details=[d1, d2])
    risk = compute_audit_risk(delta, [], trade_size_usd=10.0, total_equity_usd=100.0)
    assert risk.components[0].omega == 0.92  # max(rule-level 0.85, dynamic 0.92)


def test_compute_audit_risk_omega_fallback_to_rule_weight():
    d = FailedRuleDetail(rule_id="STATIC_PORTFOLIO", clause_id="c",
                         bypass_tsf=True, description="d")
    delta = ConstraintDelta(allow=False, failed_details=[d])
    risk = compute_audit_risk(delta, [], trade_size_usd=10.0, total_equity_usd=100.0)
    assert risk.components[0].omega == DEFAULT_RULE_WEIGHTS["STATIC_PORTFOLIO"]


def test_compute_audit_risk_combined_description_uses_pipe():
    d1 = FailedRuleDetail(rule_id="R", clause_id="c1", description="first")
    d2 = FailedRuleDetail(rule_id="R", clause_id="c2", description="second")
    delta = ConstraintDelta(allow=False, failed_details=[d1, d2])
    risk = compute_audit_risk(delta, [], trade_size_usd=1.0, total_equity_usd=100.0)
    assert "first" in risk.components[0].description
    assert "second" in risk.components[0].description
    assert " | " in risk.components[0].description


def test_compute_audit_risk_single_detail_no_pipe_in_description():
    d = FailedRuleDetail(rule_id="R", clause_id="c1", description="solo")
    delta = ConstraintDelta(allow=False, failed_details=[d])
    risk = compute_audit_risk(delta, [], trade_size_usd=1.0, total_equity_usd=100.0)
    assert risk.components[0].description == "solo"


def test_compute_audit_risk_defaults_evidence_scores_to_empty(caplog):
    detail = FailedRuleDetail(rule_id="R", clause_id="c", description="d",
                              missing_evidence_id="X")
    delta = ConstraintDelta(allow=False, failed_details=[detail])
    risk = compute_audit_risk(delta, [], trade_size_usd=10.0, total_equity_usd=100.0,
                              evidence_scores=None)
    # Missing-from-dict evidence default → 0.0
    assert risk.components[0].evidence_coverage == 0.0


def test_compute_audit_risk_logs_weakest_link_with_multi_evidence(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="app.auditor.risk_scoring")
    d1 = FailedRuleDetail(rule_id="R", clause_id="c1", description="d1",
                          missing_evidence_id="A")
    d2 = FailedRuleDetail(rule_id="R", clause_id="c2", description="d2",
                          missing_evidence_id="B")
    delta = ConstraintDelta(allow=False, failed_details=[d1, d2])
    compute_audit_risk(delta, [], trade_size_usd=100.0, total_equity_usd=100.0,
                       evidence_scores={"A": 0.5, "B": 0.3})
    assert any("multi-evidence C_ev" in r.message for r in caplog.records)


def test_compute_audit_risk_iteration_passed_through():
    delta = ConstraintDelta(allow=True)
    risk = compute_audit_risk(delta, [], iteration=4)
    assert risk.iteration == 4


# ---------------------------------------------------------------------------
# SBCRiskScore.to_dict
# ---------------------------------------------------------------------------

def test_sbc_risk_score_to_dict_round_trip():
    comp = RuleRiskComponent(
        rule_id="R", bypass_tsf=False, score=0.12345,
        trade_size_factor=0.5, evidence_coverage=0.987,
        omega=0.6, description="d", fired=True,
    )
    s = SBCRiskScore(
        composite_score=0.12345, risk_level="LOW",
        gate_decision="AUTO_APPROVE", step_name="rule_evaluation", iteration=2,
        trade_size_factor=0.5, signal_count=3, components=[comp],
    )
    d = s.to_dict()
    assert d["composite_score"] == 0.1235
    assert d["risk_level"] == "LOW"
    assert d["gate_decision"] == "AUTO_APPROVE"
    assert d["step_name"] == "rule_evaluation"
    assert d["iteration"] == 2
    assert d["trade_size_factor"] == 0.5
    assert d["lambda"] == LAMBDA_TRADE_SIZE_SCALING
    assert d["signal_count"] == 3
    assert d["components"][0]["rule_id"] == "R"
    assert d["components"][0]["score"] == 0.1235
    assert d["components"][0]["evidence_coverage"] == 0.987
