"""
Pydantic models for the deterministic compliance auditor.

Defines the canonical data structures used across the SBC framework:
- TradeProposal: The LLM proposer's structured output
- ProvidedEvidence: Evidence items extracted from client transcript
- ConstraintDelta: The auditor's deterministic verdict
- RuleClause / TriggerCondition / KYCRequirement: In-memory AST nodes
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Severity(str, Enum):
    """Severity classification for regulatory violations."""
    CRITICAL = "CRITICAL"
    RECOVERABLE = "RECOVERABLE"


class TriggerOperator(str, Enum):
    """Operators used in trigger condition matching."""
    EQUALS = "=="
    IN = "IN"
    CONTAINS = "CONTAINS"
    EXISTS = "EXISTS"


class KYCOperator(str, Enum):
    """Operators used in KYC requirement evaluation."""
    LESS_THAN = "<"
    GREATER_THAN = ">"
    EQUALS = "=="
    NOT_EQUALS = "!="
    NOT_CONTAINS = "NOT_CONTAINS"
    SEMANTIC_SIMILAR = "SEMANTIC_SIMILAR"


# ---------------------------------------------------------------------------
# Instrument-type vocabulary
# ---------------------------------------------------------------------------
# Single source of truth for the proposer schema, the auditor's
# derivative-detection check, and any future consumers.  Kept here (rather
# than in proposer/agent.py) so the auditor can import it without depending
# on the proposer module.

INSTRUMENT_TYPES: tuple[str, ...] = (
    "EQUITY", "CALL_OPTION", "PUT_OPTION", "FUTURES", "ETF", "BOND", "OTHER",
)

# Members of INSTRUMENT_TYPES that are derivatives.  "OPTION" is included as
# a backward-compat alias — older LLM emissions used the generic form before
# the schema was tightened to CALL_OPTION / PUT_OPTION.
DERIVATIVE_INSTRUMENT_TYPES: frozenset[str] = frozenset({
    "CALL_OPTION", "PUT_OPTION", "FUTURES", "OPTION",
})


# ---------------------------------------------------------------------------
# Proposal & Evidence (LLM output / auditor input)
# ---------------------------------------------------------------------------

@dataclass
class ProvidedEvidence:
    """A single piece of evidence extracted from a client transcript."""
    evidence_id: str
    value: bool
    scrap: str = ""
    evidence_path: str = ""


@dataclass
class TradeProposal:
    """
    Structured trade proposal emitted by the Proposer LLM.
    This is the canonical payload that the auditor evaluates.
    """
    proposal_id: str
    client_id: str
    action: str                                   # BUY | SELL | HOLD | REVIEW
    asset_ticker: str                             # Underlying symbol ONLY (e.g. SPY)
    instrument_type: str = "EQUITY"               # one of INSTRUMENT_TYPES
    trade_size_usd: float = 0.0
    rationale: str = ""
    user_question: str = ""
    provided_evidence: list[ProvidedEvidence] = field(default_factory=list)

    # Enriched signal flags (set by the signal detector, not the LLM)
    prompt_signals: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Constraint Delta (Auditor output)
# ---------------------------------------------------------------------------

@dataclass
class FailedRuleDetail:
    """Human-readable detail about a single rule failure."""
    rule_id: str
    clause_id: str
    severity: Severity
    description: str
    missing_evidence_id: Optional[str] = None
    # True when missing_evidence_id is a user acknowledgment that an affirmative
    # reply ("yes", "agree", …) is sufficient to cure.  Used by the C_ev
    # fast-path so the auditor doesn't have to recognize ACK evidence by its ID.
    is_ack: bool = False
    # Per-instance weight override (the SBC ω for this specific violation).
    # When None, compute_audit_risk falls back to the rule-level constant in
    # DEFAULT_RULE_WEIGHTS[rule_id].  When set, it lets a single static check
    # express continuous severity — e.g. concentration at 51% vs 99% can emit
    # the same FailedRuleDetail with different `weight` values, smoothing the
    # 50% knife-edge into a gradient under the unified SBC formula:
    #     R = TSF · (1 − C_ev) · ω
    weight: Optional[float] = None


@dataclass
class ConstraintDelta:
    """
    The deterministic verdict produced by the auditor.

    Routing is driven by the SBC audit_risk_score and gate_decision:
      - gate_decision='AUTO_APPROVE'      (score < 0.20) → trade proceeds
      - gate_decision='REFINEMENT'        (0.20 ≤ score < 0.80) → proposer retries
      - gate_decision='HUMAN_ESCALATION'  (score ≥ 0.80) → hard block, HITL

    The severity field is derived from the gate decision for backward compatibility.
    """
    allow: bool
    severity: Optional[Severity] = None
    audit_risk_score: float = 0.0
    gate_decision: str = "AUTO_APPROVE"
    failed_rules: list[str] = field(default_factory=list)
    missing_evidence_ids: list[str] = field(default_factory=list)
    failed_details: list[FailedRuleDetail] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize for JSON transport to the proposer agent."""
        return {
            "allow": self.allow,
            "status": "ALLOW" if self.allow else "REJECT",
            "severity": self.severity.value if self.severity else None,
            "audit_risk_score": round(self.audit_risk_score, 4),
            "gate_decision": self.gate_decision,
            "failed_rules": self.failed_rules,
            "missing_evidence_ids": self.missing_evidence_ids,
            "failed_details": [
                {
                    "rule_id": d.rule_id,
                    "clause_id": d.clause_id,
                    "severity": d.severity.value,
                    "description": d.description,
                    "missing_evidence_id": d.missing_evidence_id,
                }
                for d in self.failed_details
            ],
        }


# ---------------------------------------------------------------------------
# In-memory AST nodes (loaded from SQLite at startup)
# ---------------------------------------------------------------------------

@dataclass
class EvidenceFallback:
    """An evidence item that can cure a RECOVERABLE KYC failure."""
    evidence_id: str
    kyc_id: str
    description: str
    # True when this evidence is a user acknowledgment (e.g. "I accept the
    # risk").  An affirmative reply qualifies as a perfect-coverage scrap;
    # the auditor's C_ev step uses this instead of pattern-matching on the
    # evidence_id naming convention.
    is_ack: bool = False


@dataclass
class KYCRequirement:
    """A client-state check attached to a trigger condition."""
    kyc_id: str
    condition_id: str
    client_field: str
    operator: KYCOperator
    threshold: str
    domain: str                 # 'profile', 'holdings', 'account_state', 'relational'
    fallback: Optional[EvidenceFallback] = None


@dataclass
class TriggerCondition:
    """
    A condition on the *proposal* or *prompt* that activates a rule clause.
    If the condition matches, the associated KYC requirements are evaluated.
    """
    condition_id: str
    clause_id: str
    trigger_field: str          # 'proposal.action', 'prompt_signal', 'proposal.asset_ticker'
    trigger_operator: TriggerOperator
    trigger_value: str          # The value to match against
    kyc_requirements: list[KYCRequirement] = field(default_factory=list)


@dataclass
class RuleClause:
    """A single evaluable clause within a regulation."""
    clause_id: str
    rule_id: str
    description: str
    conditions: list[TriggerCondition] = field(default_factory=list)


@dataclass
class Regulation:
    """A top-level regulation containing one or more clauses."""
    rule_id: str
    rule_name: str
    severity: Severity
    description: str
    clauses: list[RuleClause] = field(default_factory=list)
