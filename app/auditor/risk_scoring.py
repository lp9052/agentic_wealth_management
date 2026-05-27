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
    TSF(S_norm) = 1 - e^(-λ · S_norm)   for graded rules (the default)
    TSF(S_norm) = 1.0                    when bypass_tsf=True (absolutes)

    λ        = Trade size scaling sensitivity (institution-level parameter).
               Controls how quickly trade size ramps up risk contribution.
               Default: 5 (see LAMBDA_TRADE_SIZE_SCALING below for the
               operating-band tradeoff).  Higher → smaller trades start
               getting scrutiny.  This is the CDF of an exponential
               distribution — a natural model for "what fraction of the
               portfolio is at risk."

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

from app.auditor.models import ConstraintDelta, FailedRuleDetail

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
# Controls how quickly small trades ramp up risk via TSF = 1 − e^(−λ·S_norm).
#   λ=5   → responsive across 0.5 % – 50 % of portfolio  (50% risk at ~14%)
#   λ=10  → responsive across 0.5 % – 25 % of portfolio  (50% risk at ~7%)
#   λ=20  → very strict on tiny trades; saturates by 10%  (50% risk at ~3.5%)
#
# We use λ=5 so trade_size remains a live SBC dial across the realistic
# operating band.  At λ=20 the TSF saturates before 10 % of portfolio,
# effectively turning trade size into a binary "is this trade visible at
# all?" signal — undermining SBC's continuous-control design.
LAMBDA_TRADE_SIZE_SCALING: float = 5.0

# ω — Per-rule institution risk weights.
#
# Each rule has a base weight reflecting institutional risk appetite.
# FailedRuleDetail.weight can override this per-instance (see
# concentration's dynamic weight in rule_engine.py).
#
# Calibration principle:
#   ω = SBC_GATE_ESCALATE + severity_premium
#   where SBC_GATE_ESCALATE = 0.80 (escalation threshold) and
#   severity_premium ∈ [0.05, 0.15] for ABSOLUTE rules (bypass_tsf=True)
#   reflects the regulatory blast radius of the violation.
#
# ABSOLUTE band (≥ 0.85, all escalate at TSF=1 in the absence of
# evidence).  Ordering by severity_premium creates a meaningful gap
# between criminal exposure (SEC_10b5) and operational failures
# (STATIC_PORTFOLIO) without changing gate behavior — these matter
# when multiple absolutes co-fire and the composite needs to reflect
# the WORST violation, not an average:
#
#   premium  rule              consequence                           ω
#   ─────────────────────────────────────────────────────────────────────
#   +0.15    SEC_10b5          criminal liability (insider trading)  0.95
#   +0.10    FINRA_2090        legal + reputational (KYC bypass)     0.90
#   +0.08    SEC_144           SEC enforcement (restricted stock)    0.88
#   +0.07    FINRA_3280        registration violation (selling away) 0.87
#   +0.06    FINRA_3240        FINRA sanction (borrow/lend w/client) 0.86
#   +0.05    STATIC_PORTFOLIO  operational (funds, AML, KYC status)  0.85
#
# GRADED band (< 0.80, TSF applies — small trades naturally auto-approve;
# evidence cures medium-sized ones).  Ordering reflects how recoverable
# the violation is with reasonable client engagement:
#
#   rule              cure path                                     ω
#   ─────────────────────────────────────────────────────────────────────
#   SEC_REG_BI        disclosure can cure (broadest duty)           0.65
#   FINRA_2111        suitability — evidence can cure               0.60
#   IRS_WASH_SALE     simple pivot (different ticker / wait 30d)    0.55
DEFAULT_RULE_WEIGHTS: dict[str, float] = {
    # ── ABSOLUTE band (bypass_tsf=True; always escalate without evidence) ──

    # Insider trading.  Section 10(b) + Rule 10b-5 carry CRIMINAL
    # liability for the firm AND the individual broker (fines + prison).
    # Highest possible severity premium — no other regulatory failure
    # in this set exposes the firm to criminal prosecution.  Top of band.
    "SEC_10b5":         0.95,   # +0.15  criminal liability

    # KYC bypass.  Trading without verified Know-Your-Customer is a
    # legal exposure (AML/BSA) and a reputational hit, but cure path
    # exists: re-do KYC.  Above the FINRA-3000s because the violation
    # blocks ALL future activity for the client, not just one trade.
    "FINRA_2090":       0.90,   # +0.10  legal + reputational

    # Restricted-stock (Rule 144) violations.  SEC enforcement — civil
    # penalties + disgorgement.  Severity below KYC because it's
    # ticker-specific (one position, not the whole account) and the
    # cure is mechanical (wait out the holding period).
    "SEC_144":          0.88,   # +0.08  SEC civil enforcement

    # Selling away — broker transacting outside their B/D's books.
    # FINRA registration violation; broker can be barred but firm
    # exposure is bounded (no client funds at risk if discovered early).
    "FINRA_3280":       0.87,   # +0.07  registration violation

    # Borrowing/lending arrangements with clients.  FINRA sanction +
    # fiduciary breach exposure.  Just below 3280 because the underlying
    # trade is usually legitimate; the structure of the relationship is
    # what triggers the rule.
    "FINRA_3240":       0.86,   # +0.06  FINRA sanction

    # Operational failures: insufficient funds, AML/OFAC flag, KYC
    # status, invalid action/ticker, MAX_TRADE ceiling.  These are
    # firm-side discipline issues, not regulatory violations in the
    # external sense.  Floor of the ABSOLUTE band — still escalates at
    # TSF=1, but contributes the smallest premium to co-fire composites.
    # (Concentration violations live here too but use a per-instance
    # dynamic weight that lerps up to 1.0 at full concentration.)
    "STATIC_PORTFOLIO": 0.85,   # +0.05  operational

    # ── GRADED band (TSF applies; evidence + small trade size cure) ──

    # Best Interest (Reg BI, Form CRS).  Broadest duty in the set —
    # covers any recommendation to a retail client.  Slightly above
    # suitability because it's a higher fiduciary standard AND because
    # the disclosure cure is straightforward (signed acknowledgement).
    "SEC_REG_BI":       0.65,

    # Suitability (FINRA 2111).  Common, often curable with KYC-linked
    # evidence (risk tolerance, investment objectives).  Standard graded
    # weight — middle of the band.
    "FINRA_2111":       0.60,

    # Wash sale (IRS).  Tax-loss harvesting violation; consequences are
    # tax-domain (disallowed loss) not regulatory.  Easiest cure: pivot
    # to a non-substantially-identical ticker or wait 30 days.  Floor
    # of the GRADED band.
    "IRS_WASH_SALE":    0.55,
}

# Fallback weight for rules not in the config
DEFAULT_OMEGA: float = 0.70

# ── Intent-based rules ──────────────────────────────────────────────────────
# These rules detect violations in the *nature of the request* — conflict of
# interest, suitability mismatch, or wash-sale timing patterns — rather than
# portfolio-level risk that scales with trade size.  When the Proposer outputs
# a placeholder trade size ($1), the normal TSF formula collapses the risk
# score to zero.  A minimum TSF floor ensures that signal-detected intent
# violations always produce a meaningful risk score.
#
# This does NOT affect:
#   - CRITICAL rules (already bypass TSF with TSF=1.0)
#   - STATIC_PORTFOLIO (portfolio-risk: TSF correctly scales with trade size)
INTENT_BASED_RULES: set[str] = {"SEC_REG_BI", "FINRA_2111", "IRS_WASH_SALE"}
TSF_INTENT_FLOOR: float = 0.40


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
    bypass_tsf: bool            # True → TSF held at 1.0 for this rule
    score: float                # R_i = TSF · (1 − C_ev) · ω  ∈ [0, 1]
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
            "components": [
                {
                    "rule_id": c.rule_id,
                    "bypass_tsf": c.bypass_tsf,
                    "score": round(c.score, 4),
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
    bypass: bool = False,
) -> float:
    """
    Compute the trade size scaling factor: TSF(S_norm).

    For graded rules (the default):
        TSF = 1 - e^(-λ · S_norm)
    This is the CDF of an exponential distribution:
        - S_norm → 0: TSF → 0 (negligible trade, negligible risk)
        - S_norm → ∞: TSF → 1 (full portfolio at risk)

    When `bypass=True` (the rule is an absolute — KYC/AML/insufficient funds/…):
        TSF = 1.0 always (trade size doesn't discount the violation)

    S_norm = trade_size / total_equity.  No cap — math.exp handles arbitrarily
    negative inputs without overflow, so large ratios just saturate cleanly to
    1.0.  total_equity_usd > 0 and trade_size_usd ≥ 0 are invariants enforced
    by the upstream static checks (insufficient funds and negative-trade-size
    are CRITICAL with bypass_tsf=True, so they fire before this function is
    asked to evaluate a graded rule on a degenerate state).
    """
    if bypass:
        return 1.0

    s_norm = trade_size_usd / total_equity_usd
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

    for rule_id, details in rule_groups.items():
        # ω resolution: each detail contributes either its per-instance
        # dynamic weight (used by the concentration check, which fuses the
        # rule-level weight with the post-trade portfolio percentage) or
        # the rule-level default from DEFAULT_RULE_WEIGHTS.  Take max across
        # all details — the worst-case violation in the group sets the
        # rule's institutional weight.
        #
        # IMPORTANT: this is per-detail, NOT "max(dynamic) when any dynamic
        # exists else rule-level."  A previous version of this code did the
        # latter, which silently dropped the rule-level weight of every
        # non-dynamic detail in the group (e.g. insufficient-funds + a
        # smaller-dynamic-weight concentration got concentration's weight
        # for both, downgrading insufficient-funds from ESCALATION to
        # REFINEMENT).
        omega = max(
            d.weight if d.weight is not None else get_rule_weight(rule_id)
            for d in details
        )

        # If ANY detail in this group requests TSF bypass, the rule's TSF is
        # held at 1.0 — trade size doesn't discount this violation.  Used for
        # absolutes that don't admit a "tiny version" (insufficient funds,
        # KYC, AML, invalid action/ticker, MAX_TRADE).
        bypass_tsf = any(d.bypass_tsf for d in details)
        tsf = _compute_trade_size_factor(
            trade_size_usd, total_equity_usd, bypass=bypass_tsf
        )

        # Intent-based floor: for rules where the violation is in the *nature*
        # of the request (not the trade size), ensure TSF never collapses to
        # zero just because the Proposer output a placeholder trade size.
        # Only applies to RECOVERABLE (non-binary) rules in the intent set.
        if not is_binary and rule_id in INTENT_BASED_RULES:
            if tsf < TSF_INTENT_FLOOR:
                logger.info(
                    "Intent-based TSF floor applied for '%s': %.4f → %.4f",
                    rule_id, tsf, TSF_INTENT_FLOOR,
                )
                tsf = TSF_INTENT_FLOOR

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

        # R_i = TSF · (1 - C_ev) · ω_i.  All three factors are independently
        # bounded to [0, 1] by their construction (TSF via 1 − e^(−λ·S_norm),
        # C_ev via embedding similarity, ω by DEFAULT_RULE_WEIGHTS + the
        # per-instance dynamic weight cap), so the product is naturally
        # in [0, 1] — no clamp needed.
        score = tsf * (1.0 - c_ev) * omega

        # Build description from all details in the group
        descriptions = [d.description for d in details]
        combined_desc = " | ".join(descriptions) if len(descriptions) > 1 else descriptions[0]

        components.append(RuleRiskComponent(
            rule_id=rule_id,
            bypass_tsf=bypass_tsf,
            score=score,
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

    # Summary-level TSF for the audit log — "what fraction of portfolio is
    # this trade?"  Zero equity with a positive trade is conceptually full
    # saturation (1.0); zero trade is 0.0.  Per-rule TSFs already handle
    # this implicitly via bypass=True on the upstream insufficient-funds
    # failure, so this only matters for the audit dict.
    if total_equity_usd > 0:
        summary_tsf = _compute_trade_size_factor(trade_size_usd, total_equity_usd, bypass=False)
    else:
        summary_tsf = 1.0 if trade_size_usd > 0 else 0.0

    return SBCRiskScore(
        composite_score=composite, risk_level=classify_risk(composite),
        gate_decision=gate,
        step_name="rule_evaluation", iteration=iteration,
        trade_size_factor=summary_tsf, signal_count=len(detected_signals),
        components=components,
    )


def _compute_composite(components: list[RuleRiskComponent]) -> float:
    """
    Composite risk via complement-product:
        R = 1 - ∏(1 - R_i)
    Each R_i is in [0, 1] by construction (see compute_audit_risk), so the
    complement-product is also in [0, 1] — no clamp needed.
    """
    if not components:
        return 0.0

    product = 1.0
    for comp in components:
        product *= (1.0 - comp.score)

    return round(1.0 - product, 4)
