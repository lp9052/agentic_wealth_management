"""Tests for app/proposer/agent.py — LLM proposer (LLM call mocked)."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.auditor.models import ProvidedEvidence, TradeProposal
from app.proposer import agent
from app.proposer.agent import (
    ProvidedEvidenceSchema,
    TradeProposalSchema,
    _build_rag_context,
    _load_regulations,
    create_proposer_llm,
    generate_proposal,
    generate_proposal_freeform,
)


# ---------------------------------------------------------------------------
# Pydantic validators on TradeProposalSchema.
# ---------------------------------------------------------------------------

def test_action_validator_uppercases_and_strips():
    m = TradeProposalSchema(action=" buy ", asset_ticker="SPY", trade_size_usd=1,
                            rationale="r", user_question="q")
    assert m.action == "BUY"


def test_instrument_type_validator_normalizes_aliases():
    cases = {
        "stock": "EQUITY", "Equities": "EQUITY", "share": "EQUITY",
        "call options": "CALL_OPTION", "puts": "PUT_OPTION",
        "future contract": "FUTURES", "fixed income": "BOND", "etfs": "ETF",
    }
    for raw, expected in cases.items():
        m = TradeProposalSchema(action="BUY", asset_ticker="SPY",
                                trade_size_usd=1.0, rationale="r",
                                user_question="q", instrument_type=raw)
        assert m.instrument_type == expected, f"{raw!r} → {m.instrument_type!r}"


def test_instrument_type_validator_passes_canonical_through():
    m = TradeProposalSchema(action="BUY", asset_ticker="SPY", trade_size_usd=1,
                            rationale="r", user_question="q",
                            instrument_type="CALL_OPTION")
    assert m.instrument_type == "CALL_OPTION"


def test_instrument_type_validator_unknown_passes_as_is():
    m = TradeProposalSchema(action="BUY", asset_ticker="SPY", trade_size_usd=1,
                            rationale="r", user_question="q",
                            instrument_type="WIDGETS")
    assert m.instrument_type == "WIDGETS"


def test_instrument_type_validator_none_defaults_to_equity():
    m = TradeProposalSchema(action="BUY", asset_ticker="SPY", trade_size_usd=1,
                            rationale="r", user_question="q",
                            instrument_type=None)
    assert m.instrument_type == "EQUITY"


def test_instrument_type_validator_empty_string_defaults_to_equity():
    m = TradeProposalSchema(action="BUY", asset_ticker="SPY", trade_size_usd=1,
                            rationale="r", user_question="q",
                            instrument_type="")
    assert m.instrument_type == "EQUITY"


def test_instrument_type_validator_whitespace_defaults_to_equity():
    m = TradeProposalSchema(action="BUY", asset_ticker="SPY", trade_size_usd=1,
                            rationale="r", user_question="q",
                            instrument_type="   ")
    assert m.instrument_type == "EQUITY"


def test_asset_ticker_validator_clean_passes_through():
    m = TradeProposalSchema(action="BUY", asset_ticker="SPY",
                            trade_size_usd=1, rationale="r", user_question="q")
    assert m.asset_ticker == "SPY"


def test_asset_ticker_validator_extracts_from_multi_word():
    m = TradeProposalSchema(action="BUY", asset_ticker="SPY call options",
                            trade_size_usd=1, rationale="r", user_question="q")
    assert m.asset_ticker == "SPY"


def test_asset_ticker_validator_strips_trailing_punctuation():
    m = TradeProposalSchema(action="BUY", asset_ticker="AAPL!",
                            trade_size_usd=1, rationale="r", user_question="q")
    assert m.asset_ticker == "AAPL"


def test_asset_ticker_validator_falls_back_to_first_six_chars():
    """When no token looks like a ticker, take the first 6 chars of the first word."""
    m = TradeProposalSchema(action="BUY", asset_ticker="something_long",
                            trade_size_usd=1, rationale="r", user_question="q")
    # First word is "something_long"; first 6 chars are "SOMETH"
    assert m.asset_ticker == "SOMETH"


def test_asset_ticker_validator_empty_string_returns_unknown():
    m = TradeProposalSchema(action="BUY", asset_ticker="   ",
                            trade_size_usd=1, rationale="r", user_question="q")
    assert m.asset_ticker == "UNKNOWN"


# ---------------------------------------------------------------------------
# ProvidedEvidenceSchema default values
# ---------------------------------------------------------------------------

def test_provided_evidence_schema_defaults():
    s = ProvidedEvidenceSchema(evidence_id="X", value=True)
    assert s.scrap == ""
    assert s.evidence_path == ""


# ---------------------------------------------------------------------------
# _load_regulations
# ---------------------------------------------------------------------------

def test_load_regulations_reads_and_caches(regulations_path):
    first = _load_regulations()
    assert any(r["id"] == "FINRA_2111" for r in first)
    # Mutate file — cache should hold
    regulations_path.write_text(json.dumps([]))
    second = _load_regulations()
    assert second is first


def test_load_regulations_failure_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(agent, "_REG_PATH", str(tmp_path / "missing.json"))
    agent._regulations_cache = None
    out = _load_regulations()
    assert out == []


# ---------------------------------------------------------------------------
# _build_rag_context
# ---------------------------------------------------------------------------

def test_build_rag_context_empty_when_no_failed_rules():
    assert _build_rag_context({}) == ""
    assert _build_rag_context({"failed_rules": []}) == ""


def test_build_rag_context_uses_flat_fallback_when_chroma_unavailable(
    regulations_path, monkeypatch
):
    # Force the chroma retrieve_regulations path to error
    from app.proposer import rag
    def boom(*a, **kw):
        raise RuntimeError("chroma down")
    monkeypatch.setattr(rag, "retrieve_regulations", boom)

    delta = {
        "failed_rules": ["FINRA_2111"],
        "failed_details": [
            {"rule_id": "FINRA_2111", "bypass_tsf": False,
             "description": "suitability failure"},
        ],
        "missing_evidence_ids": ["EV1"],
    }
    out = _build_rag_context(delta)
    assert "FINRA_2111" in out
    # The flat-fallback branch injects Rule/Text headers
    assert "Rule:" in out or "Suitability" in out


def test_build_rag_context_uses_chroma_when_available(regulations_path, monkeypatch):
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations",
                        lambda q, k=2: ["chunk-A", "chunk-A", "chunk-B"])

    delta = {
        "failed_rules": ["FINRA_2111"],
        "failed_details": [{"rule_id": "FINRA_2111", "bypass_tsf": True,
                            "description": "x"}],
        "missing_evidence_ids": [],
    }
    out = _build_rag_context(delta)
    # Duplicate chunk-A should be deduped
    assert out.count("chunk-A") == 1
    assert "chunk-B" in out


def test_build_rag_context_includes_related_rule_block(regulations_path, monkeypatch):
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: [])

    delta = {
        "failed_rules": ["FINRA_2111"],
        "failed_details": [],
        "missing_evidence_ids": [],
    }
    out = _build_rag_context(delta)
    # FINRA_2111 has SEC_REG_BI as related in MINIMAL_REGULATIONS
    assert "Related Rules" in out
    assert "SEC_REG_BI" in out


def test_build_rag_context_unknown_rule_id_in_failed_rules(regulations_path, monkeypatch):
    """A failed_rules entry with no matching entry in regulations.json
    falls back to the rule_id as the query (no related expansion)."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: ["fallback"])

    delta = {
        "failed_rules": ["UNKNOWN_RULE_ID"],
        "failed_details": [],
        "missing_evidence_ids": [],
    }
    out = _build_rag_context(delta)
    assert "UNKNOWN_RULE_ID" in out


def test_build_rag_context_flat_fallback_skips_unknown_rule(regulations_path, monkeypatch):
    """Flat fallback path: unknown rule_id has no entry in reg_by_id → skipped."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations",
                        MagicMock(side_effect=RuntimeError("chroma down")))
    delta = {
        "failed_rules": ["UNKNOWN_IN_FLAT_FALLBACK"],
        "failed_details": [],
        "missing_evidence_ids": [],
    }
    out = _build_rag_context(delta)
    # Should not include a Rule:/Text: block for the unknown rule
    assert "Rule:" not in out


def test_build_rag_context_skips_related_already_in_failed_rules(regulations_path, monkeypatch):
    """When every related rule is itself in failed_rules, skip the expansion."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: [])
    delta = {
        # FINRA_2111 has [SEC_REG_BI, FINRA_2090] as related (per conftest).
        # All three in failed_rules → no related-rules block to emit.
        "failed_rules": ["FINRA_2111", "SEC_REG_BI", "FINRA_2090"],
        "failed_details": [],
        "missing_evidence_ids": [],
    }
    out = _build_rag_context(delta)
    assert "Related Rules" not in out


def test_build_rag_context_related_rule_not_in_kb_is_skipped(tmp_path, monkeypatch):
    """When metadata.related references a rule with no entry in regulations.json,
    the inner `if entry:` skip path is exercised."""
    from app.proposer import rag

    regs = [{
        "id": "FINRA_2111",
        "metadata": {"rule": "Suitability", "related": ["UNKNOWN_RELATED"], "tags": []},
        "text": "Suitability rule.",
    }]
    p = tmp_path / "regulations.json"
    p.write_text(json.dumps(regs))
    monkeypatch.setattr(agent, "_REG_PATH", str(p))
    agent._regulations_cache = None
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: [])

    delta = {"failed_rules": ["FINRA_2111"], "failed_details": [], "missing_evidence_ids": []}
    out = _build_rag_context(delta)
    # The header is still emitted (related_ids was non-empty before filtering),
    # but no per-rule entry follows because the only related rule has no body.
    assert "Related Rules" in out
    assert "UNKNOWN_RELATED" not in out
    assert "GraphRAG ID:" not in out.split("[Related Rules")[1]


def test_build_rag_context_failure_details_with_missing_evidence(regulations_path, monkeypatch):
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: [])
    delta = {
        "failed_rules": ["FINRA_2111"],
        "failed_details": [
            {"rule_id": "FINRA_2111", "bypass_tsf": True, "description": "abs failure"},
            {"rule_id": "FINRA_2111", "bypass_tsf": False, "description": "grd failure"},
        ],
        "missing_evidence_ids": ["EV1", "EV2"],
    }
    out = _build_rag_context(delta)
    assert "ABSOLUTE" in out and "GRADED" in out
    assert "EV1" in out and "EV2" in out
    assert "Missing evidence required" in out


# ---------------------------------------------------------------------------
# create_proposer_llm — exercise the factory.
# ---------------------------------------------------------------------------

def test_create_proposer_llm_returns_chat_instance(monkeypatch):
    sentinel = object()

    def fake_ctor(*a, **kw):
        return sentinel

    from langchain_google_genai import ChatGoogleGenerativeAI
    monkeypatch.setattr("app.proposer.agent.ChatGoogleGenerativeAI", fake_ctor)
    assert create_proposer_llm() is sentinel


# ---------------------------------------------------------------------------
# generate_proposal / generate_proposal_freeform — LLM mocked.
# ---------------------------------------------------------------------------

class _FakeStructuredLLM:
    def __init__(self, response):
        self._response = response

    def invoke(self, messages):
        return self._response


class _FakeLLM:
    def __init__(self, structured_response=None, freeform_response=None):
        self._structured = _FakeStructuredLLM(structured_response) if structured_response else None
        self._freeform_response = freeform_response

    def with_structured_output(self, schema):
        return self._structured

    def invoke(self, messages):
        msg = MagicMock()
        msg.content = self._freeform_response
        return msg


def test_generate_proposal_maps_schema_to_dataclass(monkeypatch):
    response = TradeProposalSchema(
        action="BUY", asset_ticker="SPY", instrument_type="EQUITY",
        trade_size_usd=1234.5, rationale="r", user_question="q",
        provided_evidence=[ProvidedEvidenceSchema(
            evidence_id="EV1", value=True, scrap="yes", evidence_path="g"
        )],
    )
    fake_llm = _FakeLLM(structured_response=response)
    out = generate_proposal(
        client_id="C1", client_data={"x": 1}, prompt="buy SPY",
        llm=fake_llm, iteration=2,
    )
    assert isinstance(out, TradeProposal)
    assert out.client_id == "C1"
    assert out.proposal_id == "C1_t_2"
    assert out.action == "BUY"
    assert out.trade_size_usd == 1234.5
    assert out.provided_evidence[0].evidence_id == "EV1"
    assert out.provided_evidence[0].evidence_path == "g"


def test_generate_proposal_with_constraint_delta_invokes_rag(monkeypatch, regulations_path):
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: ["chunk"])

    response = TradeProposalSchema(
        action="BUY", asset_ticker="SPY", trade_size_usd=1.0,
        rationale="r", user_question="q",
    )
    fake_llm = _FakeLLM(structured_response=response)
    out = generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={"failed_rules": ["FINRA_2111"], "failed_details": [],
                          "missing_evidence_ids": []},
        llm=fake_llm,
    )
    assert out.action == "BUY"


def test_generate_proposal_default_llm_used(monkeypatch):
    """When llm=None, the function calls create_proposer_llm()."""
    response = TradeProposalSchema(
        action="HOLD", asset_ticker="SPY", trade_size_usd=0.0,
        rationale="r", user_question="q",
    )
    fake_llm = _FakeLLM(structured_response=response)
    monkeypatch.setattr(agent, "create_proposer_llm", lambda: fake_llm)
    out = generate_proposal(client_id="C", client_data={}, prompt="hold")
    assert out.action == "HOLD"


def test_generate_proposal_handles_none_instrument_type(monkeypatch):
    response = TradeProposalSchema(
        action="BUY", asset_ticker="SPY", instrument_type=None,
        trade_size_usd=10.0, rationale="r", user_question="q",
    )
    fake_llm = _FakeLLM(structured_response=response)
    out = generate_proposal(client_id="C", client_data={}, prompt="p", llm=fake_llm)
    assert out.instrument_type == "EQUITY"


def test_generate_proposal_freeform_returns_string(monkeypatch):
    fake_llm = _FakeLLM(freeform_response="proposal text")
    out = generate_proposal_freeform(client_id="C", client_data={}, prompt="buy",
                                     llm=fake_llm)
    assert out == "proposal text"


def test_generate_proposal_freeform_with_critique(monkeypatch):
    fake_llm = _FakeLLM(freeform_response="revised")
    out = generate_proposal_freeform(
        client_id="C", client_data={}, prompt="buy",
        critique="too aggressive", llm=fake_llm,
    )
    assert out == "revised"


def test_generate_proposal_freeform_default_llm(monkeypatch):
    fake_llm = _FakeLLM(freeform_response="text")
    monkeypatch.setattr(agent, "create_proposer_llm", lambda: fake_llm)
    assert generate_proposal_freeform(client_id="C", client_data={}, prompt="p") == "text"
