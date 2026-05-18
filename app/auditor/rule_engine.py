"""
Deterministic Rule Engine — Core of the compliance pipeline.

Pipeline stages handled here:
  Step 2 : Static portfolio checks   — funds, holdings, AML/KYC, concentration
  Step 3 : Signal detection          — semantic embeddings vs anchors (imported)
  Step 4 : Ticker-context tagging    — tag is_derivative & suppress false-positive signals
  Step 5 : Cascading audit setup     — load adjacency map (force-evaluations are populated
                                       later, when a regulation actually fails)
  Step 6 : Regulatory evaluation     — trigger match → KYC gate → collect evidence reqs
  Step 7 : Evidence scoring          — continuous C_ev via ensemble embedding model
  Step 8 : Audit risk scoring        — SBC authoritative composite score (TSF)
  Step 9 : SBC Gate routing          — score-driven: AUTO_APPROVE / REFINEMENT / HUMAN_ESCALATION

No LLM calls. No probabilistic reasoning. Pure logic + semantic math.
"""

import json
import logging
import os
from typing import Optional

from app.auditor.models import (
    ConstraintDelta, FailedRuleDetail, TradeProposal, Severity,
    TriggerOperator, KYCOperator, TriggerCondition, KYCRequirement,
    DERIVATIVE_INSTRUMENT_TYPES,
)
from app.auditor.rule_registry import get_regulations
from app.auditor.risk_scoring import (
    compute_audit_risk, SBCRiskScore,
)
from app.auditor.signal_detector import detect_signals, get_embedding_similarity

logger = logging.getLogger(__name__)

# ── Paths ────────────────────────────────────────────────────────────────────
_TICKER_CONFIG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "ticker_config.json")
)
_REGULATIONS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "regulations.json")
)


# ===========================================================================
# Step 8a: Ticker Configuration  (loaded once, cached)
# ===========================================================================

_ticker_config_cache: Optional[dict] = None


def _load_ticker_config() -> dict:
    """
    Load data/ticker_config.json.

    Returns the full config dict.  Crashes loudly on failure — a missing
    config is a deployment error, not a runtime condition to swallow silently.
    """
    global _ticker_config_cache
    if _ticker_config_cache is not None:
        return _ticker_config_cache

    with open(_TICKER_CONFIG_PATH, "r") as f:
        _ticker_config_cache = json.load(f)
    logger.info(
        "Loaded ticker config: %d tickers, %d derivative markers.",
        len(_ticker_config_cache.get("tickers", {})),
        len(_ticker_config_cache.get("derivative_markers", [])),
    )
    return _ticker_config_cache


def get_suppress_signals_for_ticker(ticker: str) -> set[str]:
    """Return the set of signal names to suppress for this ticker (may be empty)."""
    cfg = _load_ticker_config()
    entry = cfg.get("tickers", {}).get(ticker.upper(), {})
    return set(entry.get("suppress_signals", []))


def get_derivative_markers() -> list[str]:
    """Return the list of derivative-intent keywords from ticker_config.json."""
    return _load_ticker_config().get("derivative_markers", [])





# ===========================================================================
# Step 12a: Adjacency / Cascading Audit  (loaded once from regulations.json)
# ===========================================================================

_adjacent_risks_cache: Optional[dict[str, list[str]]] = None


def get_adjacent_risks() -> dict[str, list[str]]:
    """
    Load the rule adjacency map exclusively from data/regulations.json.

    Each regulation's "related" field defines which adjacent rules to
    force-evaluate when that regulation fires.  No hardcoded fallback —
    if the file is missing or malformed, we raise so the operator knows.
    """
    global _adjacent_risks_cache
    if _adjacent_risks_cache is not None:
        return _adjacent_risks_cache

    with open(_REGULATIONS_PATH, "r") as f:
        regs = json.load(f)

    adjacency: dict[str, list[str]] = {}
    for r in regs:
        rule_id = r.get("id")
        related = r.get("metadata", {}).get("related", [])
        if rule_id and related:
            adjacency[rule_id] = related

    _adjacent_risks_cache = adjacency
    logger.info(
        "Adjacency map loaded from regulations.json: %d rules with related links.",
        len(adjacency),
    )
    return _adjacent_risks_cache


# ===========================================================================
# Step 11: Static Portfolio & Proposal Validation Checks
# ===========================================================================

VALID_ACTIONS = {"BUY", "SELL", "HOLD", "REVIEW"}
MAX_SINGLE_TRADE_USD = 10_000_000   # $10 M single-trade ceiling
CONCENTRATION_LIMIT = 0.50          # Max 50 % of portfolio in one position


def run_static_checks(
    proposal: TradeProposal,
    client_state: dict,
) -> list[FailedRuleDetail]:
    """
    Validate basic portfolio and proposal constraints — pure arithmetic, no embeddings.

    Checks:
      STATIC.00  Invalid action
      STATIC.00  Invalid / empty ticker symbol
      STATIC.00  Negative trade size
      STATIC.03  Trade size > $10 M ceiling
      STATIC.04  Account not KYC-verified
      STATIC.04  Account not AML/OFAC cleared
      STATIC.01  Insufficient funds for BUY
      STATIC.05  Concentration risk > 50 % for BUY
      STATIC.02  Insufficient holdings for SELL
    """
    failures: list[FailedRuleDetail] = []
    action = proposal.action.upper()

    # STATIC.00 — Invalid action (must evaluate first; can't continue without a valid action)
    if action not in VALID_ACTIONS:
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.00",
            severity=Severity.CRITICAL,
            description=f"Invalid action '{proposal.action}'. Must be one of: {VALID_ACTIONS}",
        ))
        return failures

    # HOLD and REVIEW carry no portfolio risk
    if action in ("HOLD", "REVIEW"):
        return failures

    # STATIC.00 — Invalid ticker
    ticker = (proposal.asset_ticker or "").strip().upper()
    if not ticker or ticker == "UNKNOWN" or len(ticker) > 10:
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.00",
            severity=Severity.CRITICAL,
            description=f"Invalid or missing ticker symbol: '{proposal.asset_ticker}'",
        ))

    # STATIC.00 — Negative trade size
    if proposal.trade_size_usd < 0:
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.00",
            severity=Severity.CRITICAL,
            description=f"Negative trade size: ${proposal.trade_size_usd:,.2f}",
        ))

    # STATIC.03 — Absurdly large trade
    if proposal.trade_size_usd > MAX_SINGLE_TRADE_USD:
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.03",
            severity=Severity.CRITICAL,
            description=(
                f"Trade size ${proposal.trade_size_usd:,.2f} exceeds the single-trade "
                f"ceiling of ${MAX_SINGLE_TRADE_USD:,.2f}"
            ),
        ))

    # STATIC.04 — Account status (frozen accounts block everything)
    acct = client_state.get("account_state", {})
    if not acct.get("kyc_verified", True):
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.04",
            severity=Severity.CRITICAL,
            description="Account not KYC-verified. All trading is blocked until verification completes.",
        ))
    if not acct.get("aml_ofac_cleared", True):
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.04",
            severity=Severity.CRITICAL,
            description="Account has not passed AML/OFAC screening. All trading is blocked.",
        ))

    if proposal.trade_size_usd <= 0:
        return failures

    holdings = client_state.get("holdings", {})
    assets = holdings.get("assets", [])
    # total_equity_usd is used for the available cash check (buying power)
    total_equity = acct.get("total_equity_usd", 0)
    # total_portfolio_value is used for the concentration check denominator
    total_portfolio = acct.get("total_portfolio_value", total_equity)

    if action == "BUY":
        # STATIC.01 — Insufficient funds
        if total_equity > 0 and proposal.trade_size_usd > total_equity:
            failures.append(FailedRuleDetail(
                rule_id="STATIC_PORTFOLIO", clause_id="STATIC.01",
                severity=Severity.CRITICAL,
                description=(
                    f"Insufficient funds: trade requires ${proposal.trade_size_usd:,.2f} "
                    f"but client equity is ${total_equity:,.2f}"
                ),
            ))

        # STATIC.05 — Concentration risk
        if total_portfolio > 0 and ticker:
            existing = sum(
                h.get("value", 0) for h in assets
                if ticker in h.get("asset", "").upper()
            )
            # No new cash is being deposited, so denominator is just total_portfolio
            post_pct = (existing + proposal.trade_size_usd) / total_portfolio
            if post_pct > CONCENTRATION_LIMIT:
                failures.append(FailedRuleDetail(
                    rule_id="STATIC_PORTFOLIO", clause_id="STATIC.05",
                    severity=Severity.RECOVERABLE,
                    description=(
                        f"Concentration risk: post-trade {ticker} position would be "
                        f"{post_pct:.0%} of the total portfolio value (limit: {CONCENTRATION_LIMIT:.0%})"
                    ),
                    missing_evidence_id="EVID_CONCENTRATION_ACK",
                    is_ack=True,
                ))

    elif action == "SELL" and ticker:
        # STATIC.02 — Insufficient holdings
        held = sum(
            h.get("value", 0) for h in assets
            if ticker in h.get("asset", "").upper()
        )
        if held <= 0:
            failures.append(FailedRuleDetail(
                rule_id="STATIC_PORTFOLIO", clause_id="STATIC.02",
                severity=Severity.CRITICAL,
                description=(
                    f"Insufficient holdings: client does not hold {ticker} "
                    f"or has zero value in this position"
                ),
            ))
        elif proposal.trade_size_usd > held:
            failures.append(FailedRuleDetail(
                rule_id="STATIC_PORTFOLIO", clause_id="STATIC.02",
                severity=Severity.CRITICAL,
                description=(
                    f"Insufficient holdings: trade requires ${proposal.trade_size_usd:,.2f} "
                    f"but client holds only ${held:,.2f} of {ticker}"
                ),
            ))

    return failures


# ===========================================================================
# Step 13: Rule Evaluation Helpers
# ===========================================================================

def _trigger_matches(condition: TriggerCondition, proposal: TradeProposal) -> bool:
    """Check if a trigger condition matches the proposal fields."""
    if condition.trigger_field == "prompt_signal":
        if condition.trigger_operator in (TriggerOperator.CONTAINS, TriggerOperator.EQUALS):
            return condition.trigger_value in proposal.prompt_signals
    elif condition.trigger_field == "proposal.action":
        if condition.trigger_operator == TriggerOperator.EQUALS:
            return proposal.action.upper() == condition.trigger_value.upper()
        elif condition.trigger_operator == TriggerOperator.IN:
            allowed = [v.strip().strip("'\"") for v in condition.trigger_value.strip("[]").split(",")]
            return proposal.action.upper() in [a.upper() for a in allowed]
    return False


def _kyc_passes(kyc: KYCRequirement, client_state: dict, is_forced: bool = False) -> bool:
    """
    Evaluate a single KYC requirement against client state.
    Returns True if the check PASSES (i.e., no violation detected).
    """
    # intent-based domain: always fails when triggered (no state to check)
    if kyc.domain == "proposal_check":
        return False

    # portfolio_check: handled entirely by static checks (Step 11)
    if kyc.domain == "portfolio_check":
        return True

    domain_state = client_state.get(kyc.domain, {})
    value = domain_state.get(kyc.client_field)
    if value is None:
        if is_forced:
            return True
        return False  # Field not present → conservative failure

    threshold = kyc.threshold

    # Boolean comparison
    if threshold in ("0", "1", "true", "false", "True", "False"):
        bool_threshold = threshold in ("1", "true", "True")
        bool_value = bool(value) if not isinstance(value, bool) else value
        if kyc.operator == KYCOperator.EQUALS:
            return bool_value == bool_threshold
        elif kyc.operator == KYCOperator.NOT_EQUALS:
            return bool_value != bool_threshold

    # Numeric comparison
    try:
        num_t = float(threshold)
        num_v = float(value)
        if kyc.operator == KYCOperator.LESS_THAN:
            return num_v < num_t
        elif kyc.operator == KYCOperator.GREATER_THAN:
            return num_v > num_t
        elif kyc.operator == KYCOperator.EQUALS:
            return num_v == num_t
        elif kyc.operator == KYCOperator.NOT_EQUALS:
            return num_v != num_t
    except (ValueError, TypeError):
        pass

    # String comparison
    str_v = str(value)
    if kyc.operator == KYCOperator.EQUALS:
        return str_v == threshold
    elif kyc.operator == KYCOperator.NOT_EQUALS:
        return str_v != threshold
    elif kyc.operator == KYCOperator.NOT_CONTAINS:
        return threshold not in str_v
    elif kyc.operator == KYCOperator.SEMANTIC_SIMILAR:
        from app.auditor.signal_detector import get_embedding_similarity as _sim
        return _sim(str_v, threshold) >= 0.60

    return True


# ===========================================================================
# Main Evaluation Entry Point
# ===========================================================================

def evaluate_proposal(
    proposal: TradeProposal,
    client_state: dict,
    prompt: str,
    iteration: int = 1,
) -> tuple[ConstraintDelta, SBCRiskScore]:
    """
    Run the full 15-step compliance evaluation.

    Args:
        proposal   : Structured trade proposal from the LLM Proposer.
        client_state: Normalized client KYC / portfolio state dict.
        prompt     : Original (typo-corrected) user prompt — used for signal detection.
        iteration  : Current SBC loop round (1-indexed).  Passed through to the risk
                     score so the audit trail correctly records which revision cycle
                     produced each score.  Defaults to 1.

    Returns:
        (constraint_delta, audit_risk_score)
    """
    trade_size = proposal.trade_size_usd
    total_equity = client_state.get("account_state", {}).get("total_equity_usd", 0.0)

    # ── Step 1: Static portfolio checks (Fail Fast) ──────────────────────────
    static_failures = run_static_checks(proposal, client_state)

    # ── Steps 3–7: Run signal detection (Typo filter is handled upstream) ────
    # detect_signals calls detect_signals_semantic which runs Steps 1–7.
    signals = detect_signals(prompt)

    # Determine if the trade involves a derivative using both the LLM's structured output
    # AND the user prompt as a fallback safety net.  The instrument_type membership
    # covers every derivative the proposer can emit (CALL_OPTION, PUT_OPTION, FUTURES,
    # plus the legacy generic "OPTION"); the prompt-marker scan catches the case where
    # the LLM tagged it EQUITY but the prompt clearly describes a derivative.
    ticker = proposal.asset_ticker.upper()
    derivative_markers = get_derivative_markers()
    is_derivative = (
        proposal.instrument_type.upper().strip() in DERIVATIVE_INSTRUMENT_TYPES or
        any(m in prompt.lower() for m in derivative_markers)
    )

    # ── Step 9: Signal suppression (single unified pass) ─────────────────────
    # Load per-ticker suppress list from config; apply only when NOT derivative.
    suppress_set = get_suppress_signals_for_ticker(ticker)
    if suppress_set and not is_derivative:
        before = set(signals)
        signals = [s for s in signals if s not in suppress_set]
        suppressed = before - set(signals)
        if suppressed:
            logger.info(
                "Signal suppression for %s (non-derivative): removed %s",
                ticker, suppressed,
            )

    proposal.prompt_signals = signals

    # ── Step 12: Cascading audit setup ───────────────────────────────────────
    # Adjacency is consulted *after* a regulation actually fails (second-level
    # cascade further down).  Signals themselves activate their owning rules
    # via the natural TriggerCondition(prompt_signal == …) match, so no
    # signal-keyed force-eval is needed here.  Load the map (also used below).
    adjacent_risks = get_adjacent_risks()

    forced_evaluations: set[str] = set()   # Populated by second-level cascade

    # ── Steps 13: Regulatory rule evaluation ─────────────────────────────────
    regulations = get_regulations()

    # Build a lookup of evidence_id → fallback description text during rule traversal.
    # This avoids a second iteration of the regulations list later in Step 14.
    # evidence_is_ack carries the AST-declared "this evidence is a client
    # acknowledgment" flag — used by the C_ev fast-path so we don't infer ACK
    # status from a naming convention on the evidence_id.
    evidence_descriptions: dict[str, str] = {}
    evidence_is_ack: dict[str, bool] = {}
    for reg in regulations:
        for clause in reg.clauses:
            for cond in clause.conditions:
                for kyc in cond.kyc_requirements:
                    if kyc.fallback:
                        evidence_descriptions[kyc.fallback.evidence_id] = kyc.fallback.description
                        evidence_is_ack[kyc.fallback.evidence_id] = kyc.fallback.is_ack
    
    # ── Map provided evidence ────────────────────────────────────────────────
    evidence_map = {e.evidence_id: e.scrap for e in proposal.provided_evidence}
    provided_ev_ids = {e.evidence_id for e in proposal.provided_evidence if e.value}

    # ── Process static failures ─────────────────────────────────────────────
    # Static failures are collected as-is.  Evidence curing is handled
    # via the continuous C_ev score in the risk computation — not by
    # removing failures from the list.
    failed_details: list[FailedRuleDetail] = list(static_failures)

    # Extract missing evidence IDs from static failures
    all_missing_evidence: list[str] = [
        f.missing_evidence_id for f in failed_details if f.missing_evidence_id
    ]

    all_failed_rules: set[str] = set()

    if failed_details:
        all_failed_rules.add("STATIC_PORTFOLIO")

    for reg in regulations:
        if reg.rule_id == "STATIC_PORTFOLIO":
            continue  # Already handled in Step 11

        is_forced = reg.rule_id in forced_evaluations

        for clause in reg.clauses:
            for condition in clause.conditions:
                if not (is_forced or _trigger_matches(condition, proposal)):
                    continue

                for kyc in condition.kyc_requirements:
                    # Force-evals skip intent-based checks (proposal_check domain)
                    # — those only make sense when triggered by actual user content
                    if is_forced and kyc.domain == "proposal_check":
                        continue

                    if _kyc_passes(kyc, client_state, is_forced=is_forced):
                        continue

                    # KYC failed → violation
                    all_failed_rules.add(reg.rule_id)

                    # Second-level cascade
                    if reg.rule_id in adjacent_risks:
                        new_cascades = [
                            r for r in adjacent_risks[reg.rule_id]
                            if r not in forced_evaluations
                        ]
                        forced_evaluations.update(new_cascades)
                        if new_cascades:
                            logger.info(
                                "Rule '%s' failed → cascading to: %s",
                                reg.rule_id, new_cascades,
                            )

                    if reg.severity == Severity.CRITICAL:
                        failed_details.append(FailedRuleDetail(
                            rule_id=reg.rule_id, clause_id=clause.clause_id,
                            severity=Severity.CRITICAL,
                            description=f"CRITICAL: {clause.description}",
                        ))
                    else:
                        if kyc.fallback:
                            all_missing_evidence.append(kyc.fallback.evidence_id)
                            failed_details.append(FailedRuleDetail(
                                rule_id=reg.rule_id, clause_id=clause.clause_id,
                                severity=Severity.RECOVERABLE,
                                description=(
                                    f"RECOVERABLE: {clause.description}. "
                                    f"Required: {kyc.fallback.description}"
                                ),
                                missing_evidence_id=kyc.fallback.evidence_id,
                            ))
                        else:
                            failed_details.append(FailedRuleDetail(
                                rule_id=reg.rule_id, clause_id=clause.clause_id,
                                severity=Severity.RECOVERABLE,
                                description=f"RECOVERABLE: {clause.description}",
                            ))

    # ── Step 14: Compute continuous evidence scores (C_ev) ───────────────
    # For each missing evidence item, compute the semantic similarity between
    # the provided scrap and the evidence fallback description using the
    # ensemble embedding model.  This produces a continuous C_ev ∈ [0, 1]
    # instead of a binary provided/not-provided check.
    #
    # The evidence_scores dict maps evidence_id → similarity score.
    # When a rule requires multiple evidence items, compute_audit_risk
    # uses min(scores) as the rule's C_ev (weakest link).
    evidence_scores: dict[str, float] = {}

    # Also add static check evidence descriptions not covered by regulations.
    # The AST-loaded evidence_is_ack flag takes precedence; only fall back to the
    # FailedRuleDetail.is_ack when the evidence isn't declared in the AST at all.
    for f in static_failures:
        if f.missing_evidence_id and f.missing_evidence_id not in evidence_descriptions:
            # Use the failure description as a proxy
            evidence_descriptions[f.missing_evidence_id] = f.description
            evidence_is_ack[f.missing_evidence_id] = f.is_ack

    for ev_id in set(all_missing_evidence):
        if ev_id in provided_ev_ids:
            scrap = evidence_map.get(ev_id, "").strip()
            if scrap:
                # Verify scrap is grounded in the user prompt
                if scrap.lower() in prompt.lower():
                    # Compute semantic similarity against the evidence description
                    desc = evidence_descriptions.get(ev_id, "")
                    if desc:
                        # Fast-path for explicit user acknowledgments.
                        # Comparing "yes" to a 20-word legal description yields very low semantic similarity,
                        # so when the AST has marked this evidence as an ACK we accept any affirmative reply.
                        affirmative_keywords = {"yes", "yep", "sure", "understand", "agree", "confirm", "proceed", "acknowledge"}
                        is_ack_evidence = evidence_is_ack.get(ev_id, False)
                        if is_ack_evidence and any(k in scrap.lower() for k in affirmative_keywords):
                            sim = 1.0
                            logger.info("Evidence '%s': Fast-path ACK match for scrap=%r", ev_id, scrap)
                        else:
                            sim = get_embedding_similarity(scrap, desc)

                        evidence_scores[ev_id] = max(sim, 0.0)
                        if sim != 1.0 or not is_ack_evidence:
                            logger.info(
                                "Evidence '%s': C_ev = %.3f (scrap=%r vs desc=%r)",
                                ev_id, evidence_scores[ev_id],
                                scrap[:60], desc[:60],
                            )
                    else:
                        # No description to compare against — treat as binary
                        evidence_scores[ev_id] = 1.0
                else:
                    logger.warning(
                        "Evidence grounding FAILED for %s: scrap=%r not in prompt",
                        ev_id, scrap,
                    )
                    evidence_scores[ev_id] = 0.0
            else:
                evidence_scores[ev_id] = 0.0

    # ── Step 15: Audit risk scoring ──────────────────────────────────────────
    # Build a preliminary delta with all violations (before gate routing).
    # This is needed by compute_audit_risk to iterate over failed_details.
    prelim_delta = ConstraintDelta(
        allow=False,
        failed_rules=sorted(all_failed_rules),
        missing_evidence_ids=all_missing_evidence,
        failed_details=failed_details,
    )

    audit_risk = compute_audit_risk(
        prelim_delta, signals,
        trade_size_usd=trade_size,
        total_equity_usd=total_equity,
        evidence_scores=evidence_scores,
        iteration=iteration,
    )

    # ── Step 16: SBC Gate routing ────────────────────────────────────────────
    # The audit_risk score is the SOLE determinant of the routing decision.
    # No static severity labels — the math decides.
    gate = audit_risk.gate_decision
    score = audit_risk.composite_score

    if gate == "AUTO_APPROVE":
        delta = ConstraintDelta(
            allow=True,
            audit_risk_score=score,
            gate_decision="AUTO_APPROVE",
        )
    elif gate == "HUMAN_ESCALATION":
        delta = ConstraintDelta(
            allow=False, severity=Severity.CRITICAL,
            audit_risk_score=score,
            gate_decision="HUMAN_ESCALATION",
            failed_rules=sorted(all_failed_rules),
            missing_evidence_ids=all_missing_evidence,
            failed_details=failed_details,
        )
    else:  # REFINEMENT
        delta = ConstraintDelta(
            allow=False, severity=Severity.RECOVERABLE,
            audit_risk_score=score,
            gate_decision="REFINEMENT",
            failed_rules=sorted(all_failed_rules),
            missing_evidence_ids=all_missing_evidence,
            failed_details=failed_details,
        )

    logger.info(
        "SBC Gate: %s (score=%.4f) | TSF: %.4f | Rules: %s | Evidence C_ev: %s",
        gate, score, audit_risk.trade_size_factor,
        sorted(all_failed_rules),
        {k: round(v, 3) for k, v in evidence_scores.items()},
    )

    return delta, audit_risk
