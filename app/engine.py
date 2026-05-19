"""
SBC Orchestrator - LangGraph state machine for the governance loop.

Implements the Stochastic Boundary Control loop:
  Proposer (LLM) → Deterministic Auditor → SBC Gate Decision

Routing (score-driven):
  AUTO_APPROVE     (score < 0.20) → Execute trade, end
  REFINEMENT       (0.20 ≤ score < 0.80) → Feed constraint_delta back to proposer
  HUMAN_ESCALATION (score ≥ 0.80) → Hard block, HITL escalation, end

After MAX_ITERATIONS without convergence below AUTO_APPROVE → escalate.
Also supports unsupervised mode (no auditing) for benchmarking.
"""

import logging
from typing import TypedDict

from dotenv import load_dotenv
load_dotenv()  # Ensure GOOGLE_API_KEY is available from .env

# Note: numerical-warning suppression lives in signal_detector.py, scoped to
# the specific call sites (Apple Silicon MPS NaN/inf) via np.errstate — we
# deliberately don't blanket-silence FutureWarning / RuntimeWarning here.

from langgraph.graph import StateGraph, START, END

from app.proposer import (
    generate_proposal,
    generate_proposal_freeform,
    create_proposer_llm,
)
from app.auditor.rule_engine import evaluate_proposal
from app.database.client_db import get_client_state

logger = logging.getLogger(__name__)

# Maximum number of BUY/SELL proposal attempts before the session is terminated.
# REVIEW rounds (information-gathering) do NOT count toward this limit so that
# normal multi-step conversations are not cut short before the user can provide
# required evidence or clarifications.
MAX_ITERATIONS = 6


# ---------------------------------------------------------------------------
# State Definition
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    """State flowing through the LangGraph."""
    client_id: str
    client_data: dict
    prompt: str
    # Proposer outputs
    proposal: str               # Human-readable proposal text
    proposal_json: dict         # Structured proposal dict
    # Auditor outputs
    critique: str               # Human-readable audit result
    constraint_delta: dict      # Structured delta for re-proposal
    status: str                 # PENDING | CERTIFIED_COMPLIANT | NEEDS_REVISION | CRITICAL_BLOCK
    revision_count: int
    supervisor_enabled: bool    # True = audited, False = unsupervised
    is_real_time: bool          # Flag for interactive terminal mode
    # Audit trail
    fired_rules: list           # Which rules were triggered
    history_log: str            # Full cycle-by-cycle log
    risk_scores: list           # SBC risk scores per iteration
    # User Simulation (for testing)
    additional_info: str        # Info to inject if revision requested
    info_injected: bool         # Track if simulator has already fired


# ---------------------------------------------------------------------------
# Shared LLM instance
# ---------------------------------------------------------------------------

_llm = None

def _get_llm():
    global _llm
    if _llm is None:
        _llm = create_proposer_llm()
    return _llm


# ---------------------------------------------------------------------------
# Node: Proposer
# ---------------------------------------------------------------------------

def proposer_node(state: AgentState) -> dict:
    """
    LLM Proposer - generates or revises a trade proposal.
    """
    iteration = state.get("revision_count", 0) + 1
    delta = state.get("constraint_delta")

    if state.get("supervisor_enabled", True):
        # Build a context string from the previous proposal so the LLM retains
        # the ticker, instrument type, and trade size when reformulating.
        prev_pj = state.get("proposal_json") or {}
        prev_context = ""
        if prev_pj.get("asset_ticker") and prev_pj.get("asset_ticker") not in ("", "UNKNOWN", "UNKNOW"):
            prev_context = (
                f"  asset_ticker   : {prev_pj.get('asset_ticker', '')}\n"
                f"  instrument_type: {prev_pj.get('instrument_type', 'EQUITY')}\n"
                f"  trade_size_usd : {prev_pj.get('trade_size_usd', 0)}\n"
                f"  missing_evidence: {state.get('constraint_delta', {}).get('missing_evidence_ids', [])}"
            )

        from app.auditor.typo_filter import correct_typos
        corrected_prompt = correct_typos(state["prompt"])

        # Structured mode: generate JSON proposal for auditor
        proposal = generate_proposal(
            client_id=state["client_id"],
            client_data=state["client_data"],
            prompt=corrected_prompt,
            constraint_delta=delta if delta else None,
            iteration=iteration,
            llm=_get_llm(),
            previous_proposal_context=prev_context,
        )

        proposal_text = (
            f"**Action:** {proposal.action}\n"
            f"**Asset:** {proposal.asset_ticker} ({proposal.instrument_type})\n"
            f"**Size:** ${proposal.trade_size_usd:,.2f}\n"
            f"**Rationale:** {proposal.rationale}"
        )

        revision_label = f"Round {iteration}"
        proposal_entry = (
            f"\n{'─'*60}\n"
            f"[PROPOSER — {revision_label}]\n"
            f"  Action     : {proposal.action}\n"
            f"  Asset      : {proposal.asset_ticker}\n"
            f"  Instrument : {proposal.instrument_type}\n"
            f"  Size       : ${proposal.trade_size_usd:,.2f}\n"
            f"  Evidence IDs: {[e.evidence_id for e in proposal.provided_evidence]}\n"
            f"  Evidence Quotes (SCRAPS): {[e.scrap for e in proposal.provided_evidence]}\n"
            f"  Evidence Path: {[getattr(e, 'evidence_path', '') for e in proposal.provided_evidence]}\n"
            f"  Rationale  : {proposal.rationale}\n"
        )
        prev_log = state.get("history_log") or ""

        return {
            "prompt": corrected_prompt,
            "proposal": proposal_text,
            "proposal_json": {
                "proposal_id": proposal.proposal_id,
                "client_id": proposal.client_id,
                "action": proposal.action,
                "asset_ticker": proposal.asset_ticker,
                "instrument_type": proposal.instrument_type,
                "trade_size_usd": proposal.trade_size_usd,
                "rationale": proposal.rationale,
                "user_question": proposal.user_question,
                "provided_evidence": [
                    {"evidence_id": e.evidence_id, "value": e.value, "scrap": e.scrap}
                    for e in proposal.provided_evidence
                ],
            },
            "revision_count": iteration,
            "history_log": prev_log + proposal_entry,
            "status": "NEEDS_REVISION" if proposal.action == "REVIEW" else "PENDING"
        }
    else:
        # Unsupervised mode: free-form text proposal
        critique = state.get("critique", "")
        proposal_text = generate_proposal_freeform(
            client_id=state["client_id"],
            client_data=state["client_data"],
            prompt=state["prompt"],
            critique=critique if critique else None,
            llm=_get_llm(),
        )

        return {
            "proposal": proposal_text,
            "revision_count": iteration,
        }


# ---------------------------------------------------------------------------
# Node: Deterministic Auditor
# ---------------------------------------------------------------------------

def auditor_node(state: AgentState) -> dict:
    """
    Deterministic compliance auditor - evaluates the proposal against
    regulatory rules using pure logic (no LLM calls).
    """
    from app.auditor.models import TradeProposal

    # Rehydrate the TradeProposal from the JSON-serialised state form.  The
    # reconstruction lives on the dataclass (TradeProposal.from_proposal_json)
    # so any other consumer that pulls a proposal off the wire uses the same
    # defaults and field names.
    trade_proposal = TradeProposal.from_proposal_json(
        state.get("proposal_json", {}), state["client_id"]
    )

    # Get normalized client state
    client_state = get_client_state(state["client_id"], state.get("client_data"))

    # Run the deterministic auditor.  ``iteration`` records which loop cycle
    # produced this score, so SBCRiskScore.iteration is right in the audit trail.
    iteration = state.get("revision_count", 1)
    user_prompt = state.get("prompt", "")
    delta, audit_risk = evaluate_proposal(
        trade_proposal, client_state, user_prompt, iteration=iteration
    )

    # Build human-readable critique with SBC gate decision
    risk_header = (
        f"[SBC] Audit Risk: {audit_risk.composite_score:.3f} ({audit_risk.risk_level}) "
        f"→ Gate: {delta.gate_decision}\n"
    )

    if delta.gate_decision == "AUTO_APPROVE":
        critique = risk_header + "COMPLIANT: SBC gate auto-approved. No violations detected."
        status = "CERTIFIED_COMPLIANT"
    elif delta.gate_decision == "HUMAN_ESCALATION":
        critique = risk_header + f"HUMAN ESCALATION (score={audit_risk.composite_score:.3f}):\n"
        for d in delta.failed_details:
            critique += f"  - [{d.rule_id}] {d.description}\n"
        critique += "\nTrade BLOCKED. Escalating to human compliance officer."
        status = "CRITICAL_BLOCK"
    else:  # REFINEMENT
        critique = risk_header + f"REFINEMENT REQUIRED (score={audit_risk.composite_score:.3f}):\n"
        for d in delta.failed_details:
            critique += f"  - [{d.rule_id}] {d.description}\n"
        if delta.missing_evidence_ids:
            critique += f"\nRequired evidence: {delta.missing_evidence_ids}"
        critique += "\nProposer must revise or provide evidence to reduce risk score."
        status = "NEEDS_REVISION"

    # REVIEW = proposer asking a clarifying question, no trade proposed.
    # The audit_risk above scores any signals in the user prompt for the
    # audit trail, but a clean prompt would route to AUTO_APPROVE → END,
    # which would terminate the session before the user has even answered
    # the question.  Force NEEDS_REVISION so the loop continues through
    # user_simulator_node; HUMAN_ESCALATION still wins (a signal in the
    # prompt that warrants escalation should still block).
    if trade_proposal.action == "REVIEW" and status == "CERTIFIED_COMPLIANT":
        status = "NEEDS_REVISION"

    if state.get("is_real_time"):
        print(f"\n\033[1;35m{critique}\033[0m")

    # Append risk scores to history
    prev_scores = state.get("risk_scores", []) or []
    new_scores = prev_scores + [audit_risk.to_dict()]

    # Append auditor decision to history_log (reusing ``iteration`` from above
    # — there's exactly one notion of "current round" per audit call).
    audit_entry = (
        f"[AUDITOR — Round {iteration}] Status: {status}\n"
        f"  Audit Risk  : {audit_risk.composite_score:.3f} ({audit_risk.risk_level})\n"
        f"  SBC Gate    : {delta.gate_decision} (score={audit_risk.composite_score:.4f})\n"
        f"  Fired Rules : {delta.failed_rules or 'none'}\n"
    )
    for comp in audit_risk.components:
        audit_entry += (
            f"  ↳ [{comp.rule_id}|{'ABS' if comp.bypass_tsf else 'GRD'}] "
            f"R={comp.score:.3f} TSF={comp.trade_size_factor:.4f} "
            f"C_ev={comp.evidence_coverage:.3f} ω={comp.omega}\n"
        )
    for d in delta.failed_details:
        audit_entry += f"  ↳ [{d.rule_id}|{'ABS' if d.bypass_tsf else 'GRD'}] {d.description}\n"
    if delta.missing_evidence_ids:
        audit_entry += f"  Missing Evidence: {delta.missing_evidence_ids}\n"
    prev_log = state.get("history_log") or ""

    return {
        "critique": critique,
        "constraint_delta": delta.to_dict(),
        "status": status,
        "fired_rules": delta.failed_rules,
        "risk_scores": new_scores,
        "history_log": prev_log + audit_entry,
    }


def user_simulator_node(state: AgentState) -> dict:
    """
    Simulates a human user providing additional info if the auditor requests revision.
    """
    if state.get("is_real_time"):
        print(f"\n\033[1;33m[REVISION REQUIRED — Round {state['revision_count']}]\033[0m")
        # Display the direct question from the Proposer
        question = state.get("proposal_json", {}).get("user_question", "How would you like to proceed?")
        print(f"\n\033[1;36mPROPOSER ASKS: {question}\033[0m")
        user_input = input("\n\033[1;32mProvide your response (or 'quit' to exit):\033[0m ")
        if user_input.lower() == 'quit':
            return {"status": "CRITICAL_BLOCK"}
        
        new_prompt = f"{state['prompt']}\n\n[USER UPDATE]: {user_input}"
        return {"prompt": new_prompt, "info_injected": True}

    # Automated mode logic
    status = state.get("status")
    extra = state.get("additional_info")
    injected = state.get("info_injected", False)

    if status == "NEEDS_REVISION" and extra and not injected:
        new_prompt = f"{state['prompt']}\n\n[USER UPDATE]: {extra}"
        
        # Log the injection
        iteration = state.get("revision_count", 0)
        entry = f"\n[USER SIMULATOR — Round {iteration}] Injected additional info: {extra}\n"
        prev_log = state.get("history_log") or ""
        
        return {
            "prompt": new_prompt,
            "info_injected": True,
            "history_log": prev_log + entry
        }
    
    return {}


# ---------------------------------------------------------------------------
# Router Edges
# ---------------------------------------------------------------------------

def router_node(state: AgentState) -> str:
    """Route after proposer.

    Always send to the auditor — the audit trail records every round,
    including REVIEW (question) rounds.  Real-time and unsupervised
    modes are intentionally identical here; the only mode-conditional
    behavior in the graph is inside ``user_simulator_node`` (human
    input vs. test-data injection).
    """
    if not state.get("supervisor_enabled", True):
        return END

    # Convergence safety valve: after MAX_ITERATIONS, escalate to human review
    if state.get("revision_count", 0) >= MAX_ITERATIONS:
        return END

    return "auditor_node"


def compliance_router(state: AgentState) -> str:
    """Route after auditor: approve, retry, or block.

    Identical in real-time and unsupervised modes.  Terminal statuses
    (CERTIFIED_COMPLIANT, CRITICAL_BLOCK) always END regardless of
    action type — escalation on a REVIEW round (e.g., INSIDER_TIP in
    the user's question) must still hard-block the session.

    Non-terminal outcomes route through ``user_simulator_node`` so info
    can be injected (test) or solicited (real-time) before the next
    proposer attempt.  user_simulator_node is a no-op when neither
    applies, and user_simulator_router falls through to proposer_node.

    Note: REVIEW + AUTO_APPROVE was rewritten to NEEDS_REVISION in
    ``auditor_node`` so that pure question rounds don't terminate
    before the user has answered.  Only HUMAN_ESCALATION can end a
    REVIEW round here, which is the intended behavior.
    """
    status = state.get("status", "")

    if status == "CERTIFIED_COMPLIANT":
        return END
    if status == "CRITICAL_BLOCK":
        return END  # Hard block, no retry — even for REVIEW

    # Convergence safety valve: after MAX_ITERATIONS, escalate to human review
    if state.get("revision_count", 0) >= MAX_ITERATIONS:
        return END

    # Non-terminal: route through user node.  Applies uniformly to
    # REVIEW (proposer asked a question) and REFINEMENT (proposer
    # proposed a trade that needs revision).
    return "user_simulator_node"


def user_simulator_router(state: AgentState) -> str:
    """Route after user simulator: check if user quit."""
    if state.get("status") == "CRITICAL_BLOCK":
        return END
    return "proposer_node"


# ---------------------------------------------------------------------------
# Graph Assembly
# ---------------------------------------------------------------------------

builder = StateGraph(AgentState)
builder.add_node("proposer_node", proposer_node)
builder.add_node("auditor_node", auditor_node)
builder.add_node("user_simulator_node", user_simulator_node)
builder.add_edge(START, "proposer_node")

builder.add_conditional_edges(
    "proposer_node",
    router_node,
    {
        "auditor_node": "auditor_node",
        "user_simulator_node": "user_simulator_node",
        END: END,
    },
)

builder.add_conditional_edges(
    "auditor_node",
    compliance_router,
    {
        END: END,
        "proposer_node": "proposer_node",
        "user_simulator_node": "user_simulator_node",
    },
)

builder.add_conditional_edges(
    "user_simulator_node",
    user_simulator_router,
    {
        END: END,
        "proposer_node": "proposer_node",
    },
)

graph = builder.compile()
