"""
Stochastic Boundary Control (SBC) — Non-Linear Risk Scoring Module.

Implements a non-linear, institution-configurable risk scoring framework
that produces a composite risk score at each pipeline step.  The risk score
is the SOLE determinant of routing — no static severity labels.

## SBC Gate Function

The gate function maps the composite audit risk score to a routing decision:

    AUTO_APPROVE:       [0.00, 0.20)  — Trade executes, no intervention
    REFINEMENT:         [0.20, 0.80)  — Loop back: proposer adds evidence / adjusts
    HUMAN_ESCALATION:   [0.80, 1.00]  — Hard block, compliance officer review

After MAX_ITERATIONS without convergence below AUTO_APPROVE → HUMAN_ESCALATION.

## Non-Linear Risk Model

The core risk formula for each triggered rule i:

    R_i = TSF(S_norm) · (1 - C_ev_i) · ω_i

Where:
    TSF(S_norm) = 1 - e^(-λ · S_norm)   for evidence-based (RECOVERABLE) rules
    TSF(S_norm) = 1.0                    for binary violations (CRITICAL rules)

    λ        = Trade size scaling sensitivity (institution-level parameter).
               Controls how quickly trade size ramps up risk contribution.
               Default: 20. Higher → smaller trades start getting scrutiny.
               This is the CDF of an exponential distribution — a natural
               model for "what fraction of the portfolio is at risk."

    S_norm   = Normalized trade size = trade_size_usd / total_equity_usd.
               Ranges from 0 (tiny trade) to 1+ (leveraged/oversized).
               When S_norm→0, TSF→0 (negligible trade, negligible risk).
               When S_norm→∞, TSF→1 (full portfolio at risk).

    C_ev_i   = Evidence coverage for rule i ∈ [0.0, 1.0].
               This is the SEMANTIC SIMILARITY SCORE between the provided
               evidence scrap and the required evidence description, computed
               by the ensemble embedding model (fin-mpnet-base + bge-base).
               C_ev=0 → no evidence → full risk contribution.
               C_ev=1 → perfect evidence match → rule contribution is zero.
               Continuous values (e.g. 0.7) give proportional attenuation.

    ω_i      = Institution risk weight for rule i.
               Each institution can configure per-rule weights to reflect
               their own risk appetite. Higher ω → more conservative.

## Composite Score

Individual rule scores are combined via the complement-product formula:

    R_composite = 1 - ∏(1 - clamp(R_i, 0, 1))

Then clamped to [0.0, 1.0].
"""

from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Optional

from app.auditor.models import ConstraintDelta, Severity, FailedRuleDetail

logger = logging.getLogger(__name__)


# ===========================================================================
# SBC Gate Thresholds (Institution-Configurable)
# ===========================================================================

# Below this score → trade auto-approves, no human intervention
SBC_GATE_AUTO: float = 0.20

# At or above this score → hard block, compliance officer review
SBC_GATE_ESCALATE: float = 0.80


def classify_gate_decision(score: float) -> str:
    """
    Map a composite risk score to an SBC gate decision.

    Returns one of: 'AUTO_APPROVE', 'REFINEMENT', 'HUMAN_ESCALATION'
    """
    if score < SBC_GATE_AUTO:
        return "AUTO_APPROVE"
    elif score >= SBC_GATE_ESCALATE:
        return "HUMAN_ESCALATION"
    return "REFINEMENT"


# ===========================================================================
# Institution-Configurable Parameters
# ===========================================================================

# λ — Trade size scaling sensitivity.
# Controls how quickly small trades ramp up risk.
#   λ=5   → lenient (50% risk at ~14% of equity)
#   λ=20  → default (50% risk at ~3.5% of equity)
#   λ=50  → aggressive (50% risk at ~1.4% of equity)
LAMBDA_TRADE_SIZE_SCALING: float = 20.0

# ω — Per-rule institution risk weights.
# Each rule can have a custom weight reflecting the institution's risk flavor.
# Default weights are based on regulatory severity; institutions can override.
DEFAULT_RULE_WEIGHTS: dict[str, float] = {
    # Binary / CRITICAL regulations — highest institutional risk
    # These bypass TSF (always TSF=1.0) so their weight IS the minimum score.
    # Weights ≥ 0.85 guarantee they land in the HUMAN_ESCALATION band.
    "FINRA_2090":       0.90,   # KYC bypass — reputational & legal
    "SEC_10b5":         0.95,   # Insider trading — criminal liability
    "SEC_144":          0.85,   # Restricted stock — SEC enforcement
    "FINRA_3280":       0.85,   # Selling away — FINRA sanctions
    "FINRA_3240":       0.85,   # Borrowing/lending — FINRA sanctions

    # Static portfolio checks with CRITICAL severity (AML/KYC) bypass TSF.
    # The weight must be ≥ SBC_GATE_ESCALATE to guarantee escalation.
    "STATIC_PORTFOLIO": 0.85,   # KYC/AML/frozen account — binary gate

    # RECOVERABLE regulations — can be mitigated with evidence.
    # These USE TSF scaling, so small trades will naturally score low.
    "FINRA_2111":       0.60,   # Suitability — common, often curable
    "SEC_REG_BI":       0.65,   # Best interest — disclosure can cure
    "IRS_WASH_SALE":    0.55,   # Wash sale — pivot can cure
}

# Fallback weight for rules not in the config
DEFAULT_OMEGA: float = 0.70


def get_rule_weight(rule_id: str) -> float:
    """Get the institution-configured weight for a rule."""
    return DEFAULT_RULE_WEIGHTS.get(rule_id, DEFAULT_OMEGA)


# ===========================================================================
# Data Structures
# ===========================================================================

@dataclass
class RuleRiskComponent:
    """Risk contribution from a single rule evaluation."""
    rule_id: str
    severity: Optional[Severity]
    raw_score: float            # R_i before clamping
    clamped_score: float        # R_i after clamp to [0, 1]
    trade_size_factor: float    # TSF(S_norm)
    evidence_coverage: float    # C_ev_i ∈ [0, 1] (semantic similarity)
    omega: float                # ω_i (institution weight)
    description: str
    fired: bool


@dataclass
class SBCRiskScore:
    """
    Complete risk assessment at a specific pipeline step.

    Attributes:
        composite_score:    R ∈ [0.0, 1.0] — overall risk probability
        risk_level:         Human-readable classification
        gate_decision:      SBC gate routing: AUTO_APPROVE / REFINEMENT / HUMAN_ESCALATION
        step_name:          'signal_detection' | 'rule_evaluation'
        iteration:          Current SBC loop iteration
        trade_size_factor:  TSF(S_norm) applied to this evaluation
        lambda_param:       The λ parameter used
        components:         Per-rule risk breakdown
    """
    composite_score: float
    risk_level: str
    gate_decision: str
    step_name: str
    iteration: int
    trade_size_factor: float = 0.0
    lambda_param: float = LAMBDA_TRADE_SIZE_SCALING
    signal_count: int = 0
    critical_count: int = 0
    recoverable_count: int = 0
    components: list[RuleRiskComponent] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize for logging and transport."""
        return {
            "composite_score": round(self.composite_score, 4),
            "risk_level": self.risk_level,
            "gate_decision": self.gate_decision,
            "step_name": self.step_name,
            "iteration": self.iteration,
            "trade_size_factor": round(self.trade_size_factor, 4),
            "lambda": self.lambda_param,
            "signal_count": self.signal_count,
            "critical_count": self.critical_count,
            "recoverable_count": self.recoverable_count,
            "components": [
                {
                    "rule_id": c.rule_id,
                    "severity": c.severity.value if c.severity else None,
                    "raw_score": round(c.raw_score, 4),
                    "clamped_score": round(c.clamped_score, 4),
                    "trade_size_factor": round(c.trade_size_factor, 4),
                    "evidence_coverage": round(c.evidence_coverage, 3),
                    "omega": c.omega,
                    "fired": c.fired,
                    "description": c.description,
                }
                for c in self.components
            ],
        }


# ===========================================================================
# Core Computation
# ===========================================================================

def classify_risk(score: float) -> str:
    """Classify a composite risk score into a human-readable level."""
    if score >= 0.8:
        return "CRITICAL"
    elif score >= 0.5:
        return "HIGH"
    elif score >= 0.2:
        return "MODERATE"
    return "LOW"


def _compute_trade_size_factor(
    trade_size_usd: float,
    total_equity_usd: float,
    lam: float = LAMBDA_TRADE_SIZE_SCALING,
    is_binary: bool = False,
) -> float:
    """
    Compute the trade size scaling factor: TSF(S_norm).

    For evidence-based (RECOVERABLE) rules:
        TSF = 1 - e^(-λ · S_norm)
    This is the CDF of an exponential distribution:
        - S_norm → 0: TSF → 0 (negligible trade, negligible risk)
        - S_norm → ∞: TSF → 1 (full portfolio at risk)

    For binary violations (CRITICAL rules):
        TSF = 1.0 always (trade size is irrelevant for KYC/AML checks)

    S_norm = trade_size / total_equity (capped at 2.0 for sanity).
    """
    if is_binary:
        return 1.0

    if total_equity_usd <= 0 or trade_size_usd <= 0:
        return 0.0

    s_norm = min(trade_size_usd / total_equity_usd, 2.0)
    return 1.0 - math.exp(-lam * s_norm)





def compute_audit_risk(
    delta: ConstraintDelta,
    detected_signals: list[str],
    trade_size_usd: float = 0.0,
    total_equity_usd: float = 0.0,
    evidence_scores: Optional[dict[str, float]] = None,
    iteration: int = 1,
) -> SBCRiskScore:
    """
    Authoritative composite risk score for the full proposal evaluation.

    Violations are GROUPED BY RULE.  Each rule produces ONE risk component:

        R_i = TSF(S_norm) · (1 - C_ev_i) · ω_i

    Where:
        TSF = 1 - e^(-λ·S_norm)   for RECOVERABLE rules (trade-size-dependent)
        TSF = 1.0                  for CRITICAL rules (binary violations)

        C_ev_i = evidence coverage for rule i, computed as:
                 min(similarity scores across ALL evidence items for this rule)
                 This is the "weakest link" — the rule is only as covered as
                 its least-supported evidence requirement.
                 Each individual score comes from the ensemble embedding model's
                 semantic similarity between the provided scrap and the
                 evidence description.  Range: [0.0, 1.0].

    Args:
        evidence_scores: Dict mapping evidence_id → C_ev (semantic similarity
                         score from ensemble model). 0.0 = no evidence,
                         1.0 = perfect semantic match. If None, defaults to
                         empty dict (all C_ev = 0).
    """
    if evidence_scores is None:
        evidence_scores = {}

    # ── Group failed details by rule_id ───────────────────────────────────
    # Each rule produces ONE risk component.  If a rule has multiple
    # evidence requirements, C_ev = min(all evidence scores) — weakest link.
    from collections import OrderedDict
    rule_groups: OrderedDict[str, list[FailedRuleDetail]] = OrderedDict()
    for detail in delta.failed_details:
        rule_groups.setdefault(detail.rule_id, []).append(detail)

    components = []
    critical_count = 0
    recoverable_count = 0

    for rule_id, details in rule_groups.items():
        omega = get_rule_weight(rule_id)

        # Determine severity: if ANY detail in this group is CRITICAL, the
        # rule is binary (TSF bypassed).
        has_critical = any(d.severity == Severity.CRITICAL for d in details)
        is_binary = has_critical
        tsf = _compute_trade_size_factor(
            trade_size_usd, total_equity_usd, is_binary=is_binary
        )

        # Compute C_ev: min() across all evidence items for this rule.
        # If a detail has no missing_evidence_id (CRITICAL, no fallback),
        # it contributes 0.0 — anchoring the min to zero.
        per_evidence_scores: list[float] = []
        for d in details:
            if d.missing_evidence_id:
                per_evidence_scores.append(
                    evidence_scores.get(d.missing_evidence_id, 0.0)
                )
            else:
                per_evidence_scores.append(0.0)

        c_ev = min(per_evidence_scores) if per_evidence_scores else 0.0

        # R_i = TSF · (1 - C_ev) · ω_i
        raw = tsf * (1.0 - c_ev) * omega
        clamped = min(max(raw, 0.0), 1.0)

        if has_critical:
            critical_count += 1
        else:
            recoverable_count += 1

        # Build description from all details in the group
        descriptions = [d.description for d in details]
        combined_desc = " | ".join(descriptions) if len(descriptions) > 1 else descriptions[0]

        components.append(RuleRiskComponent(
            rule_id=rule_id,
            severity=Severity.CRITICAL if has_critical else Severity.RECOVERABLE,
            raw_score=raw, clamped_score=clamped,
            trade_size_factor=tsf, evidence_coverage=c_ev,
            omega=omega, description=combined_desc, fired=True,
        ))

        if per_evidence_scores and len(per_evidence_scores) > 1:
            logger.info(
                "Rule '%s': multi-evidence C_ev = min(%s) = %.3f (weakest link)",
                rule_id, [round(s, 3) for s in per_evidence_scores], c_ev,
            )

    composite = _compute_composite(components)
    gate = classify_gate_decision(composite)

    # Use the RECOVERABLE TSF for the summary-level trade_size_factor
    summary_tsf = _compute_trade_size_factor(trade_size_usd, total_equity_usd, is_binary=False)

    return SBCRiskScore(
        composite_score=composite, risk_level=classify_risk(composite),
        gate_decision=gate,
        step_name="rule_evaluation", iteration=iteration,
        trade_size_factor=summary_tsf, signal_count=len(detected_signals),
        critical_count=critical_count, recoverable_count=recoverable_count,
        components=components,
    )


def _compute_composite(components: list[RuleRiskComponent]) -> float:
    """
    Composite risk via complement-product:
        R = 1 - ∏(1 - R_i_clamped)
    Clamped to [0.0, 1.0].
    """
    if not components:
        return 0.0

    product = 1.0
    for comp in components:
        product *= (1.0 - comp.clamped_score)

    return round(min(max(1.0 - product, 0.0), 1.0), 4)
