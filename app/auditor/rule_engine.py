"""
Deterministic Rule Engine — Core of the compliance pipeline.

Flow of evaluate_proposal():
  1. Static portfolio checks      — deterministic arithmetic, fail-fast
                                    (funds, holdings, AML/KYC, concentration)
  2. Signal detection             — semantic embeddings vs anchors
                                    (delegated to signal_detector)
  3. Derivative classification    — instrument_type membership + prompt markers,
                                    used to gate per-ticker signal suppression
  4. Regulatory rule evaluation   — single walk over the regulations AST:
                                      * harvests evidence_descriptions /
                                        evidence_is_ack lookup map
                                      * matches TriggerConditions against the
                                        proposal + detected signals
                                      * evaluates KYCRequirements against
                                        client_state, collects failures
                                      * second-level cascade via adjacency map
  5. Evidence coverage (C_ev)     — for each provided evidence scrap, semantic
                                    similarity vs the canonical description;
                                    ACK fast-path for affirmative replies
  6. Composite audit risk         — TSF · (1 − C_ev) · ω, complement-product
                                    (delegated to risk_scoring)
  7. SBC gate routing             — score → AUTO_APPROVE / REFINEMENT /
                                    HUMAN_ESCALATION → ConstraintDelta

No LLM calls. No probabilistic reasoning. Pure logic + semantic math.
"""

import json
import logging
import os
import re
from typing import Optional

from app.auditor.models import (
    ConstraintDelta, FailedRuleDetail, TradeProposal,
    TriggerOperator, KYCOperator, TriggerCondition, KYCRequirement,
    DERIVATIVE_INSTRUMENT_TYPES,
)
from app.auditor.rule_registry import get_regulations
from app.auditor.risk_scoring import (
    compute_audit_risk, SBCRiskScore, get_rule_weight,
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


# Affirmative replies that satisfy a client-acknowledgment evidence item
# (used by _score_evidence_coverage's ACK fast-path).  Module-level so we
# don't reallocate the set on every C_ev iteration.
AFFIRMATIVE_KEYWORDS: frozenset[str] = frozenset({
    "yes", "yep", "sure", "understand", "agree", "confirm", "proceed", "acknowledge",
})


def _scrap_is_grounded(scrap: str, prompt: str) -> bool:
    """
    Word-boundary check that the scrap actually appears in the prompt.

    Plain substring matching is too loose — a scrap of "user" matches inside
    "username".  We compare on a normalised, word-boundary-tokenised form:
    every contiguous run of word characters in the scrap must appear as a
    word-boundary match in the prompt, in order.  Non-word chars in the
    scrap (punctuation, whitespace) don't have to match anything.
    """
    scrap_words = re.findall(r"\w+", scrap.lower())
    if not scrap_words:
        return False
    pattern = r"\b" + r"\W+".join(re.escape(w) for w in scrap_words) + r"\b"
    return re.search(pattern, prompt.lower()) is not None


# ===========================================================================
# Ticker Configuration  (loaded once, cached)
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
# Adjacency / Cascading Audit  (loaded once from regulations.json)
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
# Static Portfolio & Proposal Validation Checks
# ===========================================================================

VALID_ACTIONS = {"BUY", "SELL", "HOLD", "REVIEW"}
MAX_SINGLE_TRADE_USD = 10_000_000   # $10 M single-trade ceiling (bypass_tsf — firm-level cap)
CONCENTRATION_LIMIT = 0.50          # Concentration trigger threshold (51 %+ fires the rule)

# Concentration dynamic-weight fusion: linear lerp from the rule-level
# institutional weight (the floor) to 1.0 (full concentration ceiling).
#     ω(post_pct) = ω_rule + (1.0 − ω_rule) · overage_fraction
#       overage_fraction = (post_pct − CONCENTRATION_LIMIT) / (1 − CONCENTRATION_LIMIT)
# The floor IS the rule weight — no magic constant.  When an institution
# tightens DEFAULT_RULE_WEIGHTS["STATIC_PORTFOLIO"], the concentration
# baseline moves with it.  With ω_rule = 0.85 today:
#   At 51 %: ω ≈ 0.85 → REFINEMENT (TSF<1 keeps R below ESCALATE for most trades).
#   At 75 %: ω ≈ 0.925 → REFINEMENT / ESCALATION at TSF=1.
#   At 100 %: ω = 1.00 → ESCALATION at any TSF.


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
            bypass_tsf=True,
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
            bypass_tsf=True,
            description=f"Invalid or missing ticker symbol: '{proposal.asset_ticker}'",
        ))

    # STATIC.00 — Negative trade size
    if proposal.trade_size_usd < 0:
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.00",
            bypass_tsf=True,
            description=f"Negative trade size: ${proposal.trade_size_usd:,.2f}",
        ))

    # STATIC.03 — Absurdly large trade
    if proposal.trade_size_usd > MAX_SINGLE_TRADE_USD:
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.03",
            bypass_tsf=True,
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
            bypass_tsf=True,
            description="Account not KYC-verified. All trading is blocked until verification completes.",
        ))
    if not acct.get("aml_ofac_cleared", True):
        failures.append(FailedRuleDetail(
            rule_id="STATIC_PORTFOLIO", clause_id="STATIC.04",
            bypass_tsf=True,
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
        # STATIC.01 — Insufficient funds.  No `total_equity > 0` guard:
        # a zero-equity client buying anything positive IS insufficient
        # funds, and silently skipping the check would let the trade
        # cascade into a div-by-zero in the TSF math downstream.
        if proposal.trade_size_usd > total_equity:
            failures.append(FailedRuleDetail(
                rule_id="STATIC_PORTFOLIO", clause_id="STATIC.01",
                bypass_tsf=True,
                description=(
                    f"Insufficient funds: trade requires ${proposal.trade_size_usd:,.2f} "
                    f"but client equity is ${total_equity:,.2f}"
                ),
            ))

        # STATIC.05 — Concentration risk (continuous SBC severity).
        # Fires when post-trade fraction crosses CONCENTRATION_LIMIT; the
        # dynamic weight fuses the institutional rule weight (floor) with
        # the post-trade concentration (lerps toward 1.0 at full
        # concentration).  See module-level constants for the formula.
        # Zero-portfolio is treated as full concentration (post_pct=1.0):
        # any positive trade on nothing IS 100% concentration, and that's
        # what the math should say.  Insufficient funds also fires and
        # dominates the composite, but concentration scores correctly too.
        if ticker:
            existing = sum(
                h.get("value", 0) for h in assets
                if ticker in h.get("asset", "").upper()
            )
            if total_portfolio > 0:
                post_pct = (existing + proposal.trade_size_usd) / total_portfolio
            else:
                post_pct = 1.0
            if post_pct > CONCENTRATION_LIMIT:
                overage_fraction = min(
                    (post_pct - CONCENTRATION_LIMIT) / (1.0 - CONCENTRATION_LIMIT),
                    1.0,
                )
                rule_weight = get_rule_weight("STATIC_PORTFOLIO")
                dynamic_weight = rule_weight + (1.0 - rule_weight) * overage_fraction
                failures.append(FailedRuleDetail(
                    rule_id="STATIC_PORTFOLIO", clause_id="STATIC.05",
                    description=(
                        f"Concentration risk: post-trade {ticker} position would be "
                        f"{post_pct:.0%} of the total portfolio value "
                        f"(limit: {CONCENTRATION_LIMIT:.0%}, ω={dynamic_weight:.2f})"
                    ),
                    missing_evidence_id="EVID_CONCENTRATION_ACK",
                    is_ack=True,
                    weight=dynamic_weight,
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
                bypass_tsf=True,
                description=(
                    f"Insufficient holdings: client does not hold {ticker} "
                    f"or has zero value in this position"
                ),
            ))
        elif proposal.trade_size_usd > held:
            failures.append(FailedRuleDetail(
                rule_id="STATIC_PORTFOLIO", clause_id="STATIC.02",
                bypass_tsf=True,
                description=(
                    f"Insufficient holdings: trade requires ${proposal.trade_size_usd:,.2f} "
                    f"but client holds only ${held:,.2f} of {ticker}"
                ),
            ))

    return failures


# ===========================================================================
# Rule Evaluation Helpers
# ===========================================================================

def _trigger_matches(condition: TriggerCondition, proposal: TradeProposal) -> bool:
    """Check if a trigger condition matches the proposal fields.

    Supported (trigger_field, trigger_operator) combinations:
      prompt_signal × {CONTAINS, EQUALS} → trigger_value must appear in
                                            proposal.prompt_signals
      prompt_signal × EXISTS              → any signal at all is present;
                                            trigger_value is ignored
      proposal.action × EQUALS            → exact match (case-insensitive)
      proposal.action × IN                → action ∈ comma-list of allowed
      proposal.action × EXISTS            → action is non-empty
      proposal.asset_ticker × EXISTS      → ticker is non-empty
    """
    if condition.trigger_field == "prompt_signal":
        if condition.trigger_operator in (TriggerOperator.CONTAINS, TriggerOperator.EQUALS):
            return condition.trigger_value in proposal.prompt_signals
        elif condition.trigger_operator == TriggerOperator.EXISTS:
            # "Any signal fired" — useful for catch-all rules that escalate
            # whenever the semantic detector flags anything at all.
            return len(proposal.prompt_signals) > 0
    elif condition.trigger_field == "proposal.action":
        if condition.trigger_operator == TriggerOperator.EQUALS:
            return proposal.action.upper() == condition.trigger_value.upper()
        elif condition.trigger_operator == TriggerOperator.IN:
            allowed = [v.strip().strip("'\"") for v in condition.trigger_value.strip("[]").split(",")]
            return proposal.action.upper() in [a.upper() for a in allowed]
        elif condition.trigger_operator == TriggerOperator.EXISTS:
            return bool(proposal.action)
    elif condition.trigger_field == "proposal.asset_ticker":
        if condition.trigger_operator == TriggerOperator.EXISTS:
            return bool((proposal.asset_ticker or "").strip())
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

    # Boolean comparison.  Only literal true/false thresholds route here —
    # "0"/"1" are intentionally excluded so numeric KYCs like `field == 0`
    # take the numeric path below (0.0 == 0.0) rather than being coerced to
    # bool(value) == False, which silently mis-handles non-zero numeric values
    # (e.g. 0.5 → True → False).  Booleans still compare correctly because
    # float(True)/float(False) == 1.0/0.0 matches a "1"/"0" threshold there.
    if threshold in ("true", "false", "True", "False"):
        bool_threshold = threshold in ("true", "True")
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

    # Reachable when an op + value-type combination has no handler.  Example:
    # LESS_THAN / GREATER_THAN on a non-numeric value — bool block skips
    # (operator isn't EQUALS/NOT_EQUALS), numeric block catches the
    # ValueError on float() and falls through, string block doesn't define
    # < / >.  The conservative default is "pass" (no violation): a
    # misconfigured rule shouldn't trigger false escalations, and the
    # misconfig will surface in the audit log because the rule's other
    # KYCs will continue to be evaluated.
    return True


def _score_evidence_coverage(
    ev_id: str,
    scrap: str,
    description: str,
    prompt: str,
    is_ack: bool,
    evidence_path: str = "",
) -> float:
    """
    Compute C_ev ∈ [0, 1] for a single provided evidence item.

    Decision order:
      * empty scrap                        → 0.0
      * scrap not grounded in user prompt  → 0.0  (warned — possible LLM hallucination)
      * non-ACK AND empty evidence_path    → 0.0  (cite-your-source: any
                                                   semantic cure must point at
                                                   a known rule via its GraphRAG
                                                   id; ACKs don't need a citation
                                                   since the user's "yes" IS the
                                                   evidence)
      * is_ack AND scrap is affirmative    → 1.0  (ACK fast-path — "yes" wouldn't
                                                   embed-match a 20-word legal phrase)
      * otherwise                          → semantic similarity from the ensemble model
    """
    if not scrap:
        return 0.0

    if not _scrap_is_grounded(scrap, prompt):
        logger.warning("Evidence grounding FAILED for %s: scrap=%r not in prompt", ev_id, scrap)
        return 0.0

    # description is guaranteed non-empty: rule_db.load_all_regulations
    # rejects empty AST descriptions; run_static_checks always emits a
    # human-readable description on every FailedRuleDetail that carries a
    # missing_evidence_id.

    if is_ack and any(k in scrap.lower() for k in AFFIRMATIVE_KEYWORDS):
        logger.info("Evidence '%s': Fast-path ACK match for scrap=%r", ev_id, scrap)
        return 1.0

    # Cite-your-source: semantic-similarity cures require a GraphRAG citation
    # so the audit trail can verify the proposer pulled from the right rule.
    # An empty path on a non-ACK evidence is a sign the LLM hallucinated the
    # cure without consulting the knowledge base.
    if not evidence_path.strip():
        logger.warning(
            "Evidence '%s': non-ACK evidence has empty evidence_path; "
            "rejecting as unsupported (scrap=%r)",
            ev_id, scrap,
        )
        return 0.0

    sim = max(get_embedding_similarity(scrap, description), 0.0)
    logger.info(
        "Evidence '%s': C_ev = %.3f (scrap=%r vs desc=%r, path=%r)",
        ev_id, sim, scrap[:60], description[:60], evidence_path,
    )
    return sim


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
    Run the full compliance evaluation — see module docstring for the 7-step flow.

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

    # ── 1. Static portfolio checks (deterministic arithmetic) ────────────────
    static_failures = run_static_checks(proposal, client_state)

    # ── 2. Signal detection (semantic embeddings vs anchors) ─────────────────
    signals = detect_signals(prompt)

    # ── 3. Derivative classification + per-ticker signal suppression ─────────
    # is_derivative gates suppression: blue-chip tickers like SPY suppress
    # noisy HIGH_RISK_PRODUCT / SPECULATIVE_PRODUCT signals, but a derivative
    # on the same ticker (e.g. SPY 0DTE calls) is genuinely risky and must
    # keep those signals.  Two paths cover the proposer's structured field
    # and a prompt-keyword fallback for cases where the LLM mis-tagged it.
    ticker = proposal.asset_ticker.upper()
    is_derivative = (
        proposal.instrument_type.upper().strip() in DERIVATIVE_INSTRUMENT_TYPES
        or any(m in prompt.lower() for m in get_derivative_markers())
    )
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

    # ── 4. Regulatory rule evaluation (single AST walk) ──────────────────────
    # One pass does three things at once:
    #   * harvests the evidence-description / is_ack lookup map used by C_ev
    #   * matches trigger conditions against the proposal + detected signals
    #   * evaluates KYC requirements, emitting FailedRuleDetail per violation
    # STATIC_PORTFOLIO clauses are visited only for their evidence fallbacks
    # — the checks themselves live in run_static_checks above.  Adjacency
    # cascades populate forced_evaluations mid-walk; rules later in the
    # iteration order see them as is_forced=True.
    adjacent_risks = get_adjacent_risks()
    forced_evaluations: set[str] = set()
    failed_details: list[FailedRuleDetail] = list(static_failures)
    all_failed_rules: set[str] = {"STATIC_PORTFOLIO"} if failed_details else set()
    evidence_descriptions: dict[str, str] = {}
    evidence_is_ack: dict[str, bool] = {}

    for reg in get_regulations():
        is_static = (reg.rule_id == "STATIC_PORTFOLIO")
        is_forced = reg.rule_id in forced_evaluations

        for clause in reg.clauses:
            for condition in clause.conditions:
                # Harvest evidence map for every clause (incl. STATIC_PORTFOLIO,
                # whose EVID_CONCENTRATION_ACK fallback we need below).
                for kyc in condition.kyc_requirements:
                    if kyc.fallback:
                        evidence_descriptions[kyc.fallback.evidence_id] = kyc.fallback.description
                        evidence_is_ack[kyc.fallback.evidence_id] = kyc.fallback.is_ack

                if is_static:
                    continue
                if not (is_forced or _trigger_matches(condition, proposal)):
                    continue

                for kyc in condition.kyc_requirements:
                    # Forced rules skip intent-based checks (proposal_check
                    # domain) — those only make sense when triggered by actual
                    # user content, not by adjacency.
                    if is_forced and kyc.domain == "proposal_check":
                        continue
                    if _kyc_passes(kyc, client_state, is_forced=is_forced):
                        continue

                    all_failed_rules.add(reg.rule_id)

                    # Second-level cascade — adds rules to forced_evaluations
                    # to be picked up later in this same walk.
                    new_cascades = [
                        r for r in adjacent_risks.get(reg.rule_id, [])
                        if r not in forced_evaluations
                    ]
                    if new_cascades:
                        forced_evaluations.update(new_cascades)
                        logger.info("Rule '%s' failed → cascading to: %s", reg.rule_id, new_cascades)

                    if reg.bypass_tsf:
                        failed_details.append(FailedRuleDetail(
                            rule_id=reg.rule_id, clause_id=clause.clause_id,
                            bypass_tsf=True,
                            description=f"ABSOLUTE: {clause.description}",
                        ))
                    elif kyc.fallback:
                        failed_details.append(FailedRuleDetail(
                            rule_id=reg.rule_id, clause_id=clause.clause_id,
                            description=(
                                f"GRADED: {clause.description}. "
                                f"Required: {kyc.fallback.description}"
                            ),
                            missing_evidence_id=kyc.fallback.evidence_id,
                        ))
                    else:
                        failed_details.append(FailedRuleDetail(
                            rule_id=reg.rule_id, clause_id=clause.clause_id,
                            description=f"GRADED: {clause.description}",
                        ))

    # Static-check evidence proxy: when a static failure references an
    # evidence_id that the AST doesn't declare, fall back to the failure's
    # own description for similarity comparison.
    for f in static_failures:
        if f.missing_evidence_id and f.missing_evidence_id not in evidence_descriptions:
            evidence_descriptions[f.missing_evidence_id] = f.description
            evidence_is_ack[f.missing_evidence_id] = f.is_ack

    # Derived from failed_details — single source of truth, no parallel list.
    all_missing_evidence = [f.missing_evidence_id for f in failed_details if f.missing_evidence_id]

    # ── 5. Evidence coverage (C_ev) ──────────────────────────────────────────
    # For each missing-evidence item the proposer claims to have provided,
    # compute a continuous C_ev ∈ [0, 1].  When a rule has multiple required
    # evidence items, compute_audit_risk takes min(scores) as the rule's
    # C_ev (weakest link).
    evidence_map = {e.evidence_id: (e.scrap, e.evidence_path)
                    for e in proposal.provided_evidence}
    provided_ev_ids = {e.evidence_id for e in proposal.provided_evidence if e.value}

    evidence_scores: dict[str, float] = {}
    for ev_id in set(all_missing_evidence):
        if ev_id not in provided_ev_ids:
            continue
        scrap, ev_path = evidence_map.get(ev_id, ("", ""))
        evidence_scores[ev_id] = _score_evidence_coverage(
            ev_id,
            scrap=scrap.strip(),
            description=evidence_descriptions.get(ev_id, ""),
            prompt=prompt,
            is_ack=evidence_is_ack.get(ev_id, False),
            evidence_path=ev_path,
        )

    # ── 6. Composite audit risk (TSF · (1 − C_ev) · ω, complement-product) ──
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

    # ── 7. SBC gate routing ──────────────────────────────────────────────────
    # The audit_risk composite score is the SOLE routing input — no parallel
    # severity taxonomy.  AUTO_APPROVE hides the failure list (the trade
    # proceeds, the proposer doesn't need to revise); REFINEMENT and
    # HUMAN_ESCALATION expose it for the proposer / human reviewer.
    gate = audit_risk.gate_decision
    allow = (gate == "AUTO_APPROVE")
    delta = ConstraintDelta(
        allow=allow,
        audit_risk_score=audit_risk.composite_score,
        gate_decision=gate,
        failed_rules=sorted(all_failed_rules) if not allow else [],
        missing_evidence_ids=all_missing_evidence if not allow else [],
        failed_details=failed_details if not allow else [],
    )

    logger.info(
        "SBC Gate: %s (score=%.4f) | TSF: %.4f | Rules: %s | Evidence C_ev: %s",
        gate, audit_risk.composite_score, audit_risk.trade_size_factor,
        sorted(all_failed_rules),
        {k: round(v, 3) for k, v in evidence_scores.items()},
    )

    return delta, audit_risk
