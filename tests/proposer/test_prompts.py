"""Tests for app/proposer/prompts.py — pure template builders."""

from app.proposer.prompts import (
    PROPOSER_FREEFORM_SYSTEM_PROMPT,
    PROPOSER_SYSTEM_PROMPT,
    build_constraint_delta_addendum,
    build_critique_addendum,
    build_freeform_user_prompt,
    build_proposer_user_prompt,
)


def test_system_prompts_are_non_empty():
    assert PROPOSER_SYSTEM_PROMPT.strip()
    assert PROPOSER_FREEFORM_SYSTEM_PROMPT.strip()


def test_system_prompt_instructs_evidence_path_for_non_ack():
    """The auditor rejects non-ACK evidence with empty evidence_path
    (C_ev=0).  The system prompt must tell the proposer to populate it
    for every non-ACK cure, otherwise live trades will silently fail to
    cure even on perfect semantic matches."""
    p = PROPOSER_SYSTEM_PROMPT
    # Mentions the field name and the GraphRAG citation format
    assert "evidence_path" in p
    assert "GraphRAG ID" in p
    # Calls out the ACK exemption (either by listing the three IDs verbatim
    # or by the generic `_ACK` suffix rule)
    assert "_ACK" in p


def test_build_proposer_user_prompt_basic():
    out = build_proposer_user_prompt("{}", "buy SPY", 1)
    assert "{}" in out
    assert "buy SPY" in out
    assert "Iteration: 1" in out


def test_build_proposer_user_prompt_with_previous_context():
    out = build_proposer_user_prompt("{}", "buy SPY", 2,
                                     previous_proposal_context="prev_ticker: SPY")
    assert "Previous Proposal Context" in out
    assert "prev_ticker: SPY" in out


def test_build_proposer_user_prompt_omits_previous_context_when_empty():
    out = build_proposer_user_prompt("{}", "buy SPY", 1, previous_proposal_context="")
    assert "Previous Proposal Context" not in out


def test_build_freeform_user_prompt():
    out = build_freeform_user_prompt('{"id":"c"}', "rebalance")
    assert "rebalance" in out
    assert '{"id":"c"}' in out


def test_build_constraint_delta_addendum_with_rag_context():
    delta = {"failed_rules": ["FINRA_2111"], "missing_evidence_ids": ["EV1"]}
    out = build_constraint_delta_addendum(delta, regulatory_context="EXTRA TEXT")
    assert "AUDITOR CONSTRAINT DELTA" in out
    assert "FINRA_2111" in out
    assert "EXTRA TEXT" in out
    assert "REGULATORY KNOWLEDGE BASE" in out


def test_build_constraint_delta_addendum_without_rag_context():
    delta = {"failed_rules": ["X"]}
    out = build_constraint_delta_addendum(delta)
    assert "AUDITOR CONSTRAINT DELTA" in out
    assert "REGULATORY KNOWLEDGE BASE" not in out


def test_build_critique_addendum():
    out = build_critique_addendum("the trade was too risky")
    assert "the trade was too risky" in out
