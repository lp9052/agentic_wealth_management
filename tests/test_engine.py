"""Tests for app/engine.py — public surface only.

Public API:
  proposer_node, auditor_node, user_simulator_node, router_node,
  compliance_router, user_simulator_router, graph, MAX_ITERATIONS, AgentState.

The private _get_llm cache is exercised indirectly through proposer_node
(supervised path).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langgraph.graph import END

import app.engine as engine
from app.auditor.models import (
    ConstraintDelta,
    FailedRuleDetail,
    ProvidedEvidence,
    TradeProposal,
)
from app.auditor.risk_scoring import RuleRiskComponent, SBCRiskScore
from app.engine import (
    MAX_ITERATIONS,
    auditor_node,
    compliance_router,
    proposer_node,
    router_node,
    user_simulator_node,
    user_simulator_router,
)


# ---------------------------------------------------------------------------
# proposer_node — supervised + unsupervised.
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_proposal():
    return TradeProposal(
        proposal_id="p1", client_id="C1",
        action="BUY", asset_ticker="SPY", instrument_type="EQUITY",
        trade_size_usd=10_000.0, rationale="r", user_question="",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EV", value=True, scrap="s", evidence_path="g",
        )],
    )


def test_proposer_node_supervised_basic(monkeypatch, fake_proposal):
    """First call also exercises the _get_llm lazy-init branch."""
    sentinel = MagicMock()
    engine._llm = None  # force the cached-None branch on first call
    monkeypatch.setattr(engine, "create_proposer_llm", lambda: sentinel)
    monkeypatch.setattr(engine, "generate_proposal", lambda **kw: fake_proposal)
    monkeypatch.setattr("app.auditor.typo_filter.correct_typos", lambda t: t)

    state = {
        "client_id": "C1", "client_data": {}, "prompt": "buy SPY",
        "supervisor_enabled": True, "revision_count": 0,
    }
    out = proposer_node(state)
    assert out["proposal_json"]["action"] == "BUY"
    assert out["status"] == "PENDING"
    # Second call exercises the cached-LLM branch
    proposer_node(state)
    assert engine._llm is sentinel


def test_proposer_node_preserves_evidence_path_through_state(monkeypatch, fake_proposal):
    """Regression: evidence_path must survive the proposal_json round-trip.

    proposer_node serialises the proposal into AgentState as JSON; auditor_node
    rehydrates it via TradeProposal.from_proposal_json.  If evidence_path is
    dropped in serialisation, _score_evidence_coverage sees an empty path and
    scores every non-ACK evidence item C_ev=0 — making graded rules incurable.
    """
    engine._llm = MagicMock()
    monkeypatch.setattr(engine, "generate_proposal", lambda **kw: fake_proposal)
    monkeypatch.setattr("app.auditor.typo_filter.correct_typos", lambda t: t)

    state = {
        "client_id": "C1", "client_data": {}, "prompt": "buy SPY",
        "supervisor_enabled": True, "revision_count": 0,
    }
    out = proposer_node(state)

    # Serialised form carries the path
    serialised_ev = out["proposal_json"]["provided_evidence"][0]
    assert serialised_ev["evidence_path"] == "g"

    # ...and the auditor's rehydration preserves it
    rehydrated = TradeProposal.from_proposal_json(out["proposal_json"], "C1")
    assert rehydrated.provided_evidence[0].evidence_path == "g"


def test_proposer_node_review_action_sets_needs_revision(monkeypatch, fake_proposal):
    fake_proposal.action = "REVIEW"
    fake_proposal.user_question = "Are you sure?"
    engine._llm = MagicMock()
    monkeypatch.setattr(engine, "generate_proposal", lambda **kw: fake_proposal)
    monkeypatch.setattr("app.auditor.typo_filter.correct_typos", lambda t: t)

    state = {
        "client_id": "C", "client_data": {}, "prompt": "p",
        "supervisor_enabled": True, "revision_count": 0,
    }
    out = proposer_node(state)
    assert out["status"] == "NEEDS_REVISION"


def test_proposer_node_supervised_with_previous_context(monkeypatch, fake_proposal):
    engine._llm = MagicMock()
    captured = {}
    def gen(**kw):
        captured["prev_context"] = kw.get("previous_proposal_context")
        return fake_proposal
    monkeypatch.setattr(engine, "generate_proposal", gen)
    monkeypatch.setattr("app.auditor.typo_filter.correct_typos", lambda t: t)

    state = {
        "client_id": "C", "client_data": {}, "prompt": "p",
        "supervisor_enabled": True, "revision_count": 1,
        "proposal_json": {
            "asset_ticker": "AAPL", "instrument_type": "EQUITY", "trade_size_usd": 5000.0,
        },
        "constraint_delta": {"missing_evidence_ids": ["EV1"]},
    }
    proposer_node(state)
    assert "AAPL" in captured["prev_context"]


def test_proposer_node_skips_prev_context_when_ticker_is_unknown(monkeypatch, fake_proposal):
    engine._llm = MagicMock()
    captured = {}
    def gen(**kw):
        captured["prev_context"] = kw.get("previous_proposal_context")
        return fake_proposal
    monkeypatch.setattr(engine, "generate_proposal", gen)
    monkeypatch.setattr("app.auditor.typo_filter.correct_typos", lambda t: t)

    state = {
        "client_id": "C", "client_data": {}, "prompt": "p",
        "supervisor_enabled": True, "revision_count": 1,
        "proposal_json": {"asset_ticker": "UNKNOWN"},
    }
    proposer_node(state)
    assert captured["prev_context"] == ""


def test_proposer_node_unsupervised(monkeypatch):
    engine._llm = MagicMock()
    monkeypatch.setattr(engine, "generate_proposal_freeform",
                        lambda **kw: "free-form text")
    state = {
        "client_id": "C", "client_data": {}, "prompt": "p",
        "supervisor_enabled": False, "revision_count": 0,
    }
    out = proposer_node(state)
    assert out["proposal"] == "free-form text"
    assert out["revision_count"] == 1


def test_proposer_node_unsupervised_with_critique(monkeypatch):
    engine._llm = MagicMock()
    captured = {}
    def freeform(**kw):
        captured["critique"] = kw.get("critique")
        return "rev"
    monkeypatch.setattr(engine, "generate_proposal_freeform", freeform)
    state = {
        "client_id": "C", "client_data": {}, "prompt": "p",
        "supervisor_enabled": False, "revision_count": 1,
        "critique": "too aggressive",
    }
    proposer_node(state)
    assert captured["critique"] == "too aggressive"


# ---------------------------------------------------------------------------
# auditor_node — every gate decision branch.
# ---------------------------------------------------------------------------

def _proposal_json(action="BUY"):
    return {
        "proposal_id": "p1", "action": action, "asset_ticker": "SPY",
        "instrument_type": "EQUITY", "trade_size_usd": 10_000.0,
        "rationale": "r", "user_question": "",
        "provided_evidence": [],
    }


def _fake_delta_risk(gate, score, failed=None):
    components = [RuleRiskComponent(
        rule_id="R", bypass_tsf=False, score=score,
        trade_size_factor=0.5, evidence_coverage=0.0,
        omega=0.6, description="d", fired=True,
    )]
    delta = ConstraintDelta(
        allow=(gate == "AUTO_APPROVE"),
        audit_risk_score=score,
        gate_decision=gate,
        failed_rules=failed or [],
        missing_evidence_ids=["EV1"] if gate == "REFINEMENT" else [],
        failed_details=[
            FailedRuleDetail(rule_id="R", clause_id="C", description="d",
                             bypass_tsf=(gate == "HUMAN_ESCALATION"))
        ] if gate != "AUTO_APPROVE" else [],
    )
    risk = SBCRiskScore(
        composite_score=score, risk_level="HIGH",
        gate_decision=gate, step_name="rule_evaluation", iteration=1,
        trade_size_factor=0.5, signal_count=1, components=components,
    )
    return delta, risk


def test_auditor_node_auto_approve(monkeypatch):
    monkeypatch.setattr(engine, "evaluate_proposal",
                        lambda *a, **kw: _fake_delta_risk("AUTO_APPROVE", 0.05))
    monkeypatch.setattr(engine, "get_client_state", lambda cid, data: {})
    state = {"client_id": "C", "client_data": {}, "prompt": "p",
            "proposal_json": _proposal_json()}
    out = auditor_node(state)
    assert out["status"] == "CERTIFIED_COMPLIANT"
    assert "COMPLIANT" in out["critique"]


def test_auditor_node_human_escalation(monkeypatch):
    monkeypatch.setattr(engine, "evaluate_proposal",
                        lambda *a, **kw: _fake_delta_risk("HUMAN_ESCALATION", 0.9, failed=["R"]))
    monkeypatch.setattr(engine, "get_client_state", lambda cid, data: {})
    state = {"client_id": "C", "client_data": {}, "prompt": "p",
            "proposal_json": _proposal_json()}
    out = auditor_node(state)
    assert out["status"] == "CRITICAL_BLOCK"
    assert "BLOCKED" in out["critique"]


def test_auditor_node_refinement_with_missing_evidence(monkeypatch):
    monkeypatch.setattr(engine, "evaluate_proposal",
                        lambda *a, **kw: _fake_delta_risk("REFINEMENT", 0.5, failed=["R"]))
    monkeypatch.setattr(engine, "get_client_state", lambda cid, data: {})
    state = {"client_id": "C", "client_data": {}, "prompt": "p",
            "proposal_json": _proposal_json()}
    out = auditor_node(state)
    assert out["status"] == "NEEDS_REVISION"
    assert "REFINEMENT" in out["critique"]
    assert "EV1" in out["critique"]


def test_auditor_node_review_with_auto_approve_forced_to_revision(monkeypatch):
    """A REVIEW action that would auto-approve must force NEEDS_REVISION
    so the loop continues into user_simulator_node."""
    monkeypatch.setattr(engine, "evaluate_proposal",
                        lambda *a, **kw: _fake_delta_risk("AUTO_APPROVE", 0.05))
    monkeypatch.setattr(engine, "get_client_state", lambda cid, data: {})
    state = {"client_id": "C", "client_data": {}, "prompt": "p",
            "proposal_json": _proposal_json(action="REVIEW")}
    out = auditor_node(state)
    assert out["status"] == "NEEDS_REVISION"


def test_auditor_node_review_with_escalation_blocks(monkeypatch, capsys):
    """REVIEW + HUMAN_ESCALATION must still hard-block (not override to revision)."""
    monkeypatch.setattr(engine, "evaluate_proposal",
                        lambda *a, **kw: _fake_delta_risk("HUMAN_ESCALATION", 0.9, failed=["R"]))
    monkeypatch.setattr(engine, "get_client_state", lambda cid, data: {})
    state = {"client_id": "C", "client_data": {}, "prompt": "p",
            "proposal_json": _proposal_json(action="REVIEW"),
            "is_real_time": True}  # exercise the print branch
    out = auditor_node(state)
    assert out["status"] == "CRITICAL_BLOCK"
    captured = capsys.readouterr()
    # Real-time mode prints to stdout
    assert "HUMAN ESCALATION" in captured.out


def test_auditor_node_refinement_without_missing_evidence(monkeypatch):
    """REFINEMENT can fire with no missing evidence (e.g. a graded rule with no
    fallback) — the critique then omits the 'Required evidence' line."""
    def fake(*a, **kw):
        delta, risk = _fake_delta_risk("REFINEMENT", 0.5, failed=["R"])
        delta.missing_evidence_ids = []
        return delta, risk

    monkeypatch.setattr(engine, "evaluate_proposal", fake)
    monkeypatch.setattr(engine, "get_client_state", lambda cid, data: {})
    state = {"client_id": "C", "client_data": {}, "prompt": "p",
            "proposal_json": _proposal_json()}
    out = auditor_node(state)
    assert "Required evidence" not in out["critique"]
    assert out["status"] == "NEEDS_REVISION"


def test_auditor_node_accumulates_risk_scores_and_history(monkeypatch):
    monkeypatch.setattr(engine, "evaluate_proposal",
                        lambda *a, **kw: _fake_delta_risk("REFINEMENT", 0.5, failed=["R"]))
    monkeypatch.setattr(engine, "get_client_state", lambda cid, data: {})
    state = {
        "client_id": "C", "client_data": {}, "prompt": "p",
        "proposal_json": _proposal_json(),
        "risk_scores": [{"prior": "score"}],
        "history_log": "prior log\n",
    }
    out = auditor_node(state)
    assert len(out["risk_scores"]) == 2  # appended
    assert "prior log" in out["history_log"]
    assert "[AUDITOR" in out["history_log"]


# ---------------------------------------------------------------------------
# user_simulator_node — real-time + automated.
# ---------------------------------------------------------------------------

def test_user_simulator_real_time_quit(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "quit")
    state = {
        "is_real_time": True, "revision_count": 1,
        "proposal_json": {"user_question": "Sure?"},
        "prompt": "p",
    }
    out = user_simulator_node(state)
    assert out["status"] == "CRITICAL_BLOCK"


def test_user_simulator_real_time_with_input(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a: "yes I agree")
    state = {
        "is_real_time": True, "revision_count": 1,
        "proposal_json": {"user_question": "Sure?"},
        "prompt": "buy SPY",
    }
    out = user_simulator_node(state)
    assert "yes I agree" in out["prompt"]
    assert out["info_injected"] is True


def test_user_simulator_real_time_default_question(monkeypatch):
    """When proposal_json lacks user_question, falls back to default text."""
    monkeypatch.setattr("builtins.input", lambda *a: "ok")
    state = {
        "is_real_time": True, "revision_count": 1,
        "proposal_json": {},  # no user_question
        "prompt": "p",
    }
    out = user_simulator_node(state)
    assert out["info_injected"] is True


def test_user_simulator_automated_injects_additional_info():
    state = {
        "status": "NEEDS_REVISION",
        "additional_info": "yes I agree",
        "info_injected": False,
        "revision_count": 1,
        "prompt": "buy SPY",
    }
    out = user_simulator_node(state)
    assert "yes I agree" in out["prompt"]
    assert out["info_injected"] is True
    assert "[USER SIMULATOR" in out["history_log"]


def test_user_simulator_automated_skips_when_no_extra_info():
    state = {"status": "NEEDS_REVISION", "info_injected": False, "prompt": "p"}
    assert user_simulator_node(state) == {}


def test_user_simulator_automated_skips_when_already_injected():
    state = {
        "status": "NEEDS_REVISION", "additional_info": "x",
        "info_injected": True, "prompt": "p",
    }
    assert user_simulator_node(state) == {}


def test_user_simulator_automated_skips_when_not_needs_revision():
    state = {"status": "CERTIFIED_COMPLIANT", "additional_info": "x",
            "info_injected": False, "prompt": "p"}
    assert user_simulator_node(state) == {}


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

def test_router_node_unsupervised_ends():
    assert router_node({"supervisor_enabled": False}) == END


def test_router_node_max_iterations_ends():
    assert router_node({"supervisor_enabled": True,
                        "revision_count": MAX_ITERATIONS}) == END


def test_router_node_default_routes_to_auditor():
    assert router_node({"supervisor_enabled": True,
                        "revision_count": 1}) == "auditor_node"


def test_compliance_router_certified_compliant_ends():
    assert compliance_router({"status": "CERTIFIED_COMPLIANT"}) == END


def test_compliance_router_critical_block_ends():
    assert compliance_router({"status": "CRITICAL_BLOCK"}) == END


def test_compliance_router_max_iterations_ends():
    assert compliance_router({"status": "NEEDS_REVISION",
                              "revision_count": MAX_ITERATIONS}) == END


def test_compliance_router_continues_to_user_simulator():
    out = compliance_router({"status": "NEEDS_REVISION", "revision_count": 1})
    assert out == "user_simulator_node"


def test_user_simulator_router_quit_ends():
    assert user_simulator_router({"status": "CRITICAL_BLOCK"}) == END


def test_user_simulator_router_default_back_to_proposer():
    assert user_simulator_router({"status": "NEEDS_REVISION"}) == "proposer_node"


# ---------------------------------------------------------------------------
# Graph assembly — smoke test that the compiled graph exists.
# ---------------------------------------------------------------------------

def test_graph_is_compiled():
    assert engine.graph is not None
    # The compiled graph is a callable
    assert hasattr(engine.graph, "invoke")
