"""Tests for app/auditor/risk_scoring.py — public surface only.

Public API:
  classify_gate_decision, classify_risk, get_rule_weight, compute_audit_risk,
  SBCRiskScore, RuleRiskComponent, DEFAULT_RULE_WEIGHTS, DEFAULT_OMEGA,
  LAMBDA_TRADE_SIZE_SCALING, SBC_GATE_AUTO, SBC_GATE_ESCALATE

Branch coverage for the private helpers (_compute_trade_size_factor,
_compute_composite) is achieved indirectly through compute_audit_risk.
"""

import math

import pytest

from app.auditor.models import ConstraintDelta, FailedRuleDetail
from app.auditor.risk_scoring import (
    DEFAULT_OMEGA,
    DEFAULT_RULE_WEIGHTS,
    INTENT_BASED_RULES,
    LAMBDA_TRADE_SIZE_SCALING,
    RuleRiskComponent,
    SBC_GATE_AUTO,
    SBC_GATE_ESCALATE,
    SBCRiskScore,
    TSF_INTENT_FLOOR,
    classify_gate_decision,
    classify_risk,
    compute_audit_risk,
    get_rule_weight,
)


# ---------------------------------------------------------------------------
# Module-level constants.
# ---------------------------------------------------------------------------

def test_lambda_matches_documented_value():
    """CLAUDE.md and the in-code rationale both reference λ=5."""
    assert LAMBDA_TRADE_SIZE_SCALING == 5.0


def test_gate_thresholds():
    assert SBC_GATE_AUTO == 0.20
    assert SBC_GATE_ESCALATE == 0.80


def test_critical_rule_weights_above_escalate_floor():
    """All CRITICAL absolutes must escalate on a single-rule fire."""
    criticals = {"SEC_10b5", "FINRA_2090", "SEC_144", "FINRA_3280",
                 "FINRA_3240", "STATIC_PORTFOLIO"}
    for rule_id, omega in DEFAULT_RULE_WEIGHTS.items():
        if rule_id in criticals:
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
# compute_audit_risk — exercises _compute_trade_size_factor and
# _compute_composite by observing their effects on the returned SBCRiskScore.
# ---------------------------------------------------------------------------

def test_compute_audit_risk_no_failures_is_auto_approve():
    delta = ConstraintDelta(allow=True)
    risk = compute_audit_risk(delta, detected_signals=[], trade_size_usd=0.0,
                              total_equity_usd=100.0)
    assert risk.composite_score == 0.0
    assert risk.gate_decision == "AUTO_APPROVE"
    assert risk.components == []
    assert risk.trade_size_factor == 0.0


def test_compute_audit_risk_no_failures_returns_zero_composite():
    """Empty failed_details → _compute_composite([]) returns 0.0."""
    risk = compute_audit_risk(ConstraintDelta(allow=True), [], 1.0, 100.0)
    assert risk.composite_score == 0.0


def test_compute_audit_risk_summary_tsf_zero_equity_positive_trade():
    """Summary TSF = 1.0 when total_equity=0 and trade_size>0."""
    risk = compute_audit_risk(ConstraintDelta(allow=True), [],
                              trade_size_usd=100.0, total_equity_usd=0.0)
    assert risk.trade_size_factor == 1.0


def test_compute_audit_risk_summary_tsf_zero_equity_zero_trade():
    """Summary TSF = 0.0 when both are zero."""
    risk = compute_audit_risk(ConstraintDelta(allow=True), [], 0.0, 0.0)
    assert risk.trade_size_factor == 0.0


def test_compute_audit_risk_summary_tsf_normal_case():
    """Summary TSF follows the 1 - e^(-λ·S_norm) formula."""
    risk = compute_audit_risk(ConstraintDelta(allow=True), [],
                              trade_size_usd=10.0, total_equity_usd=100.0)
    expected = 1.0 - math.exp(-LAMBDA_TRADE_SIZE_SCALING * 0.1)
    assert risk.trade_size_factor == pytest.approx(expected, abs=1e-4)


def test_compute_audit_risk_critical_rule_bypasses_tsf():
    """bypass_tsf=True forces TSF=1.0 even for a tiny trade."""
    detail = FailedRuleDetail(rule_id="FINRA_2090", clause_id="X",
                              bypass_tsf=True, description="KYC bypass")
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[detail]),
                              [], trade_size_usd=1.0, total_equity_usd=1e9)
    assert risk.components[0].trade_size_factor == 1.0
    assert risk.gate_decision == "HUMAN_ESCALATION"


def test_compute_audit_risk_graded_rule_tsf_scales_with_trade_size():
    """Two equal-omega graded rules at different trade sizes produce
    different scores via TSF."""
    detail = FailedRuleDetail(rule_id="FINRA_2111", clause_id="X",
                              description="small", missing_evidence_id="EV")
    tiny = compute_audit_risk(
        ConstraintDelta(allow=False, failed_details=[detail]), [],
        trade_size_usd=10.0, total_equity_usd=100_000.0,
    )
    huge = compute_audit_risk(
        ConstraintDelta(allow=False, failed_details=[detail]), [],
        trade_size_usd=80_000.0, total_equity_usd=100_000.0,
    )
    assert huge.composite_score > tiny.composite_score


def test_compute_audit_risk_evidence_cures_rule():
    """Full evidence (C_ev=1.0) drops R_i to zero."""
    detail = FailedRuleDetail(rule_id="FINRA_2111", clause_id="X",
                              description="s", missing_evidence_id="EV")
    risk = compute_audit_risk(
        ConstraintDelta(allow=False, failed_details=[detail]), [],
        trade_size_usd=80_000.0, total_equity_usd=100_000.0,
        evidence_scores={"EV": 1.0},
    )
    assert risk.composite_score == pytest.approx(0.0, abs=1e-4)
    assert risk.gate_decision == "AUTO_APPROVE"


def test_compute_audit_risk_multi_evidence_takes_min_weakest_link():
    """When a rule has multiple evidence items, C_ev = min(scores)."""
    d1 = FailedRuleDetail(rule_id="R", clause_id="c1", description="d",
                          missing_evidence_id="A", weight=0.6)
    d2 = FailedRuleDetail(rule_id="R", clause_id="c2", description="d",
                          missing_evidence_id="B", weight=0.6)
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[d1, d2]),
                              [], 100_000, 100_000, {"A": 0.9, "B": 0.1})
    assert risk.components[0].evidence_coverage == pytest.approx(0.1, abs=1e-6)


def test_compute_audit_risk_detail_without_evidence_id_anchors_to_zero():
    """A failure with no missing_evidence_id contributes 0.0 → C_ev=0."""
    detail = FailedRuleDetail(rule_id="R", clause_id="c", description="d", weight=0.6)
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[detail]),
                              [], 10.0, 100.0, {"X": 1.0})
    assert risk.components[0].evidence_coverage == 0.0


def test_compute_audit_risk_omega_takes_max_across_details():
    """Per-detail max ω: rule-level for some, dynamic for others."""
    d1 = FailedRuleDetail(rule_id="STATIC_PORTFOLIO", clause_id="c1",
                          bypass_tsf=True, description="funds")
    d2 = FailedRuleDetail(rule_id="STATIC_PORTFOLIO", clause_id="c2",
                          description="conc", weight=0.92)
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[d1, d2]),
                              [], 10.0, 100.0)
    assert risk.components[0].omega == 0.92


def test_compute_audit_risk_omega_falls_back_to_rule_weight():
    """Detail with no weight → uses DEFAULT_RULE_WEIGHTS."""
    d = FailedRuleDetail(rule_id="STATIC_PORTFOLIO", clause_id="c",
                         bypass_tsf=True, description="d")
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[d]),
                              [], 10.0, 100.0)
    assert risk.components[0].omega == DEFAULT_RULE_WEIGHTS["STATIC_PORTFOLIO"]


def test_compute_audit_risk_combined_description_joined_with_pipe():
    d1 = FailedRuleDetail(rule_id="R", clause_id="c1", description="first")
    d2 = FailedRuleDetail(rule_id="R", clause_id="c2", description="second")
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[d1, d2]),
                              [], 1.0, 100.0)
    assert " | " in risk.components[0].description
    assert "first" in risk.components[0].description
    assert "second" in risk.components[0].description


def test_compute_audit_risk_single_detail_no_pipe():
    d = FailedRuleDetail(rule_id="R", clause_id="c1", description="solo")
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[d]),
                              [], 1.0, 100.0)
    assert risk.components[0].description == "solo"


def test_compute_audit_risk_complement_product_combines_components():
    """Two rules at 0.5 each → composite = 1 - (1-0.5)(1-0.5) = 0.75."""
    d1 = FailedRuleDetail(rule_id="R1", clause_id="c1",
                          bypass_tsf=True, description="d", weight=0.5)
    d2 = FailedRuleDetail(rule_id="R2", clause_id="c2",
                          bypass_tsf=True, description="d", weight=0.5)
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[d1, d2]),
                              [], 10.0, 100.0)
    # Each component scores TSF(=1.0 bypassed) * (1 - 0) * 0.5 = 0.5
    assert risk.composite_score == pytest.approx(0.75, abs=1e-3)


def test_compute_audit_risk_defaults_evidence_scores_to_empty():
    detail = FailedRuleDetail(rule_id="R", clause_id="c", description="d",
                              missing_evidence_id="X")
    risk = compute_audit_risk(ConstraintDelta(allow=False, failed_details=[detail]),
                              [], 10.0, 100.0, evidence_scores=None)
    assert risk.components[0].evidence_coverage == 0.0


# ---------------------------------------------------------------------------
# Intent-based TSF floor — guards against the regression where the natural
# TSF collapses to ~0 when the Proposer outputs a placeholder $1 trade size
# on an inherently intent-based violation (conflict of interest, suitability,
# wash-sale intent).  Also covers the branch on line 433 — a previous version
# of this code referenced an undefined `is_binary` variable and would crash
# whenever an INTENT_BASED rule with bypass_tsf=False reached this path.
# ---------------------------------------------------------------------------

def test_intent_based_rule_floors_tiny_tsf():
    """Tiny trade on an INTENT_BASED rule (bypass_tsf=False) → TSF clamps to
    TSF_INTENT_FLOOR even though the natural value would be ~0."""
    rule_id = next(iter(INTENT_BASED_RULES))
    detail = FailedRuleDetail(rule_id=rule_id, clause_id="X", description="d",
                              missing_evidence_id="EV")
    risk = compute_audit_risk(
        ConstraintDelta(allow=False, failed_details=[detail]),
        [], trade_size_usd=1.0, total_equity_usd=500_000.0,
    )
    assert risk.components[0].trade_size_factor == TSF_INTENT_FLOOR


def test_intent_based_rule_no_floor_when_natural_tsf_above_floor():
    """Large trade on an INTENT_BASED rule → natural TSF (already > floor)
    is preserved, the inner `tsf < TSF_INTENT_FLOOR` branch is False."""
    rule_id = next(iter(INTENT_BASED_RULES))
    detail = FailedRuleDetail(rule_id=rule_id, clause_id="X", description="d",
                              missing_evidence_id="EV")
    risk = compute_audit_risk(
        ConstraintDelta(allow=False, failed_details=[detail]),
        [], trade_size_usd=80_000.0, total_equity_usd=100_000.0,
    )
    tsf = risk.components[0].trade_size_factor
    assert tsf > TSF_INTENT_FLOOR
    assert tsf == pytest.approx(1.0 - math.exp(-LAMBDA_TRADE_SIZE_SCALING * 0.8), abs=1e-4)


def test_intent_based_rule_with_bypass_tsf_skips_floor_branch():
    """bypass_tsf=True short-circuits the outer condition before
    `rule_id in INTENT_BASED_RULES` is evaluated — TSF stays at 1.0."""
    rule_id = next(iter(INTENT_BASED_RULES))
    detail = FailedRuleDetail(rule_id=rule_id, clause_id="X", description="d",
                              bypass_tsf=True)
    risk = compute_audit_risk(
        ConstraintDelta(allow=False, failed_details=[detail]),
        [], trade_size_usd=1.0, total_equity_usd=500_000.0,
    )
    assert risk.components[0].trade_size_factor == 1.0


def test_non_intent_rule_does_not_floor_tsf():
    """A rule outside INTENT_BASED_RULES → outer condition False on the
    `rule_id in …` clause, no floor applied."""
    rule_id = "UNRELATED_RULE_NOT_IN_INTENT_SET"
    assert rule_id not in INTENT_BASED_RULES
    detail = FailedRuleDetail(rule_id=rule_id, clause_id="X", description="d",
                              missing_evidence_id="EV", weight=0.5)
    risk = compute_audit_risk(
        ConstraintDelta(allow=False, failed_details=[detail]),
        [], trade_size_usd=1.0, total_equity_usd=500_000.0,
    )
    tsf = risk.components[0].trade_size_factor
    assert tsf < TSF_INTENT_FLOOR
    assert tsf == pytest.approx(1.0 - math.exp(-LAMBDA_TRADE_SIZE_SCALING * 1.0 / 500_000.0), abs=1e-6)


def test_intent_based_floor_logs_application(caplog):
    """When the floor binds, a logger.info line records the lift."""
    rule_id = next(iter(INTENT_BASED_RULES))
    detail = FailedRuleDetail(rule_id=rule_id, clause_id="X", description="d",
                              missing_evidence_id="EV")
    with caplog.at_level("INFO", logger="app.auditor.risk_scoring"):
        compute_audit_risk(
            ConstraintDelta(allow=False, failed_details=[detail]),
            [], trade_size_usd=1.0, total_equity_usd=500_000.0,
        )
    assert any("Intent-based TSF floor applied" in r.message for r in caplog.records)


def test_compute_audit_risk_logs_weakest_link_for_multi_evidence(caplog):
    import logging
    caplog.set_level(logging.INFO, logger="app.auditor.risk_scoring")
    d1 = FailedRuleDetail(rule_id="R", clause_id="c1", description="d",
                          missing_evidence_id="A")
    d2 = FailedRuleDetail(rule_id="R", clause_id="c2", description="d",
                          missing_evidence_id="B")
    compute_audit_risk(ConstraintDelta(allow=False, failed_details=[d1, d2]),
                       [], 100.0, 100.0, {"A": 0.5, "B": 0.3})
    assert any("multi-evidence C_ev" in r.message for r in caplog.records)


def test_compute_audit_risk_iteration_passed_through():
    risk = compute_audit_risk(ConstraintDelta(allow=True), [], iteration=4)
    assert risk.iteration == 4


# ---------------------------------------------------------------------------
# SBCRiskScore.to_dict (public method on a public class)
# ---------------------------------------------------------------------------

def test_sbc_risk_score_to_dict_serialization():
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
    assert d["lambda"] == LAMBDA_TRADE_SIZE_SCALING
    assert d["components"][0]["evidence_coverage"] == 0.987
