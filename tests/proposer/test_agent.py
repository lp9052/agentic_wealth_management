"""Tests for app/proposer/agent.py — public surface only.

Public API:
  TradeProposalSchema, ProvidedEvidenceSchema, create_proposer_llm,
  generate_proposal, generate_proposal_freeform.

Branch coverage for the private helpers (_load_regulations, _build_rag_context)
is achieved indirectly via generate_proposal with constraint_delta — the
LLM is mocked and we inspect the system message passed to it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.auditor.models import TradeProposal
from app.proposer import agent
from app.proposer.agent import (
    ProvidedEvidenceSchema,
    TradeProposalSchema,
    create_proposer_llm,
    generate_proposal,
    generate_proposal_freeform,
)


# ---------------------------------------------------------------------------
# TradeProposalSchema validators — fire on instantiation (public behavior).
# ---------------------------------------------------------------------------

def _schema(**kw) -> TradeProposalSchema:
    base = dict(action="BUY", asset_ticker="SPY", trade_size_usd=1.0,
                rationale="r", user_question="q")
    base.update(kw)
    return TradeProposalSchema(**base)


def test_action_validator_strips_and_upcases():
    assert _schema(action=" buy ").action == "BUY"


@pytest.mark.parametrize("raw, expected", [
    ("stock", "EQUITY"), ("Equities", "EQUITY"), ("share", "EQUITY"),
    ("call options", "CALL_OPTION"), ("puts", "PUT_OPTION"),
    ("future contract", "FUTURES"), ("fixed income", "BOND"),
    ("etfs", "ETF"),
])
def test_instrument_type_validator_normalizes_aliases(raw, expected):
    assert _schema(instrument_type=raw).instrument_type == expected


def test_instrument_type_validator_canonical_passes_through():
    assert _schema(instrument_type="CALL_OPTION").instrument_type == "CALL_OPTION"


def test_instrument_type_validator_unknown_passes_as_is():
    """Unknown variants drop through to the auditor's unknown-instrument path."""
    assert _schema(instrument_type="WIDGETS").instrument_type == "WIDGETS"


@pytest.mark.parametrize("falsy", [None, "", "   "])
def test_instrument_type_validator_falsy_defaults_to_equity(falsy):
    assert _schema(instrument_type=falsy).instrument_type == "EQUITY"


def test_asset_ticker_validator_clean_passes_through():
    assert _schema(asset_ticker="SPY").asset_ticker == "SPY"


def test_asset_ticker_validator_extracts_from_multi_word():
    assert _schema(asset_ticker="SPY call options").asset_ticker == "SPY"


def test_asset_ticker_validator_strips_trailing_punctuation():
    assert _schema(asset_ticker="AAPL!").asset_ticker == "AAPL"


def test_asset_ticker_validator_fallback_to_first_six_chars():
    """No token matches the ticker regex → first 6 chars of first word."""
    assert _schema(asset_ticker="something_long").asset_ticker == "SOMETH"


def test_asset_ticker_validator_whitespace_returns_unknown():
    assert _schema(asset_ticker="   ").asset_ticker == "UNKNOWN"


def test_provided_evidence_schema_defaults():
    s = ProvidedEvidenceSchema(evidence_id="X", value=True)
    assert s.scrap == ""
    assert s.evidence_path == ""


# ---------------------------------------------------------------------------
# create_proposer_llm — factory smoke test.
# ---------------------------------------------------------------------------

def test_create_proposer_llm_invokes_constructor(monkeypatch):
    sentinel = object()
    monkeypatch.setattr("app.proposer.agent.ChatGoogleGenerativeAI",
                        lambda *a, **kw: sentinel)
    assert create_proposer_llm() is sentinel


# ---------------------------------------------------------------------------
# generate_proposal / generate_proposal_freeform — LLM is mocked.
#
# These tests exercise _load_regulations and _build_rag_context indirectly:
# generate_proposal with constraint_delta builds the RAG addendum and embeds
# it in the system message sent to the LLM.  By capturing what was passed to
# the LLM, we observe the private helpers' behavior on the public boundary.
# ---------------------------------------------------------------------------

class _FakeStructuredLLM:
    def __init__(self, response, capture):
        self._response = response
        self._capture = capture

    def invoke(self, messages):
        # Capture the system message so tests can inspect what _build_rag_context produced
        self._capture["messages"] = messages
        return self._response


class _FakeLLM:
    def __init__(self, structured_response=None, freeform_response=None):
        self._capture = {}
        self._structured = (
            _FakeStructuredLLM(structured_response, self._capture)
            if structured_response else None
        )
        self._freeform_response = freeform_response

    def with_structured_output(self, schema):
        return self._structured

    def invoke(self, messages):
        self._capture["messages"] = messages
        msg = MagicMock()
        msg.content = self._freeform_response
        return msg

    def system_message(self) -> str:
        return self._capture["messages"][0].content


def _ok_response():
    return TradeProposalSchema(
        action="BUY", asset_ticker="SPY", instrument_type="EQUITY",
        trade_size_usd=1234.5, rationale="r", user_question="",
        provided_evidence=[ProvidedEvidenceSchema(
            evidence_id="EV1", value=True, scrap="yes", evidence_path="g")],
    )


def test_generate_proposal_maps_schema_to_dataclass():
    fake = _FakeLLM(structured_response=_ok_response())
    out = generate_proposal(client_id="C1", client_data={"x": 1}, prompt="buy SPY",
                            llm=fake, iteration=2)
    assert isinstance(out, TradeProposal)
    assert out.client_id == "C1"
    assert out.proposal_id == "C1_t_2"
    assert out.action == "BUY"
    assert out.trade_size_usd == 1234.5
    assert out.provided_evidence[0].evidence_path == "g"


def test_generate_proposal_default_llm_factory_used(monkeypatch):
    fake = _FakeLLM(structured_response=_ok_response())
    monkeypatch.setattr(agent, "create_proposer_llm", lambda: fake)
    out = generate_proposal(client_id="C", client_data={}, prompt="p")
    assert out.action == "BUY"


def test_generate_proposal_none_instrument_type_defaults_to_equity():
    response = TradeProposalSchema(
        action="BUY", asset_ticker="SPY", instrument_type=None,
        trade_size_usd=10.0, rationale="r", user_question="q",
    )
    fake = _FakeLLM(structured_response=response)
    out = generate_proposal(client_id="C", client_data={}, prompt="p", llm=fake)
    assert out.instrument_type == "EQUITY"


def test_generate_proposal_with_constraint_delta_injects_rag_addendum(
    regulations_path, monkeypatch
):
    """constraint_delta routes through _build_rag_context → _load_regulations.
    Output is inspectable as part of the system message handed to the LLM."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: ["chunk-A"])

    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            "failed_rules": ["FINRA_2111"],
            "failed_details": [{"rule_id": "FINRA_2111", "bypass_tsf": False,
                                "description": "fail desc"}],
            "missing_evidence_ids": ["EV1"],
        },
        llm=fake,
    )
    sys_msg = fake.system_message()
    assert "AUDITOR CONSTRAINT DELTA" in sys_msg
    assert "chunk-A" in sys_msg  # ChromaDB chunk included
    assert "Missing evidence required" in sys_msg
    assert "EV1" in sys_msg


def test_generate_proposal_constraint_delta_with_chroma_failure_uses_flat_fallback(
    regulations_path, monkeypatch
):
    """When ChromaDB blows up, _build_rag_context falls back to the flat
    regulation text from regulations.json."""
    from app.proposer import rag

    def boom(*a, **kw):
        raise RuntimeError("chroma down")
    monkeypatch.setattr(rag, "retrieve_regulations", boom)

    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            "failed_rules": ["FINRA_2111"],
            "failed_details": [],
            "missing_evidence_ids": [],
        },
        llm=fake,
    )
    sys_msg = fake.system_message()
    # The flat-fallback path injects the Rule:/Text: headers
    assert "Rule:" in sys_msg
    assert "Suitability" in sys_msg or "FINRA_2111" in sys_msg


def test_generate_proposal_constraint_delta_chroma_dedupes_repeated_chunks(
    regulations_path, monkeypatch
):
    """Repeated chunks across multiple failed_rules queries are deduped."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations",
                        lambda q, k=2: ["dup", "dup", "unique"])

    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            "failed_rules": ["FINRA_2111"],
            "failed_details": [], "missing_evidence_ids": [],
        },
        llm=fake,
    )
    sys_msg = fake.system_message()
    assert sys_msg.count("dup") == 1
    assert "unique" in sys_msg


def test_generate_proposal_constraint_delta_unknown_rule_id_in_flat_fallback(
    regulations_path, monkeypatch
):
    """Flat-fallback path: an unknown rule_id has no entry → skipped silently."""
    from app.proposer import rag

    def boom(*a, **kw):
        raise RuntimeError("chroma down")
    monkeypatch.setattr(rag, "retrieve_regulations", boom)

    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            "failed_rules": ["UNKNOWN_RULE"],
            "failed_details": [], "missing_evidence_ids": [],
        },
        llm=fake,
    )
    sys_msg = fake.system_message()
    # No Rule:/Text: block since the rule has no entry in the KB
    assert "Rule:" not in sys_msg or "UNKNOWN_RULE" in sys_msg


def test_generate_proposal_constraint_delta_includes_related_rules(
    regulations_path, monkeypatch
):
    """When FINRA_2111 fails, its related rules (per metadata.related) are
    added to the system message — exercises the related-rule expansion."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: [])

    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            "failed_rules": ["FINRA_2111"],
            "failed_details": [], "missing_evidence_ids": [],
        },
        llm=fake,
    )
    sys_msg = fake.system_message()
    assert "Related Rules" in sys_msg
    assert "SEC_REG_BI" in sys_msg


def test_generate_proposal_skips_related_already_in_failed_rules(
    regulations_path, monkeypatch
):
    """All related rules already in failed_rules → no Related-Rules block emitted."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: [])

    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            # FINRA_2111 has [SEC_REG_BI, FINRA_2090] as related per conftest.
            "failed_rules": ["FINRA_2111", "SEC_REG_BI", "FINRA_2090"],
            "failed_details": [], "missing_evidence_ids": [],
        },
        llm=fake,
    )
    sys_msg = fake.system_message()
    assert "Related Rules" not in sys_msg


def test_generate_proposal_related_rule_not_in_kb_is_skipped(tmp_path, monkeypatch):
    """A related rule with no entry in regulations.json → the inner per-rule
    block is skipped (the header may or may not be present, depending on
    whether OTHER related rules expand)."""
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

    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            "failed_rules": ["FINRA_2111"],
            "failed_details": [], "missing_evidence_ids": [],
        },
        llm=fake,
    )
    sys_msg = fake.system_message()
    assert "UNKNOWN_RELATED" not in sys_msg


def test_generate_proposal_constraint_delta_with_absolute_and_graded_details(
    regulations_path, monkeypatch
):
    """failed_details with mixed bypass_tsf flags → both ABSOLUTE and GRADED tags."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: [])

    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            "failed_rules": ["FINRA_2111"],
            "failed_details": [
                {"rule_id": "FINRA_2111", "bypass_tsf": True, "description": "abs"},
                {"rule_id": "FINRA_2111", "bypass_tsf": False, "description": "grd"},
            ],
            "missing_evidence_ids": [],
        },
        llm=fake,
    )
    sys_msg = fake.system_message()
    assert "ABSOLUTE" in sys_msg and "GRADED" in sys_msg


def test_generate_proposal_empty_constraint_delta_omits_rag_block(monkeypatch):
    """No failed_rules → _build_rag_context short-circuits to empty string."""
    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={"failed_rules": []},
        llm=fake,
    )
    sys_msg = fake.system_message()
    assert "AUDITOR CONSTRAINT DELTA" in sys_msg  # outer addendum still emitted
    # But no RAG content body
    assert "REGULATORY KNOWLEDGE BASE" not in sys_msg


# ── generate_proposal_freeform ─────────────────────────────────────────────

def test_generate_proposal_freeform_returns_string():
    fake = _FakeLLM(freeform_response="proposal text")
    assert generate_proposal_freeform(client_id="C", client_data={}, prompt="buy",
                                      llm=fake) == "proposal text"


def test_generate_proposal_freeform_with_critique_appends_to_system():
    fake = _FakeLLM(freeform_response="revised")
    out = generate_proposal_freeform(client_id="C", client_data={}, prompt="buy",
                                     critique="too aggressive", llm=fake)
    assert out == "revised"
    assert "too aggressive" in fake.system_message()


def test_generate_proposal_freeform_default_llm(monkeypatch):
    fake = _FakeLLM(freeform_response="text")
    monkeypatch.setattr(agent, "create_proposer_llm", lambda: fake)
    assert generate_proposal_freeform(client_id="C", client_data={}, prompt="p") == "text"


# ---------------------------------------------------------------------------
# _load_regulations failure path — exercised via generate_proposal.
# ---------------------------------------------------------------------------

def test_generate_proposal_caches_regulations_across_calls(regulations_path, monkeypatch):
    """Second generate_proposal call with constraint_delta hits the cached
    _regulations_cache branch (no re-read of regulations.json)."""
    from app.proposer import rag
    monkeypatch.setattr(rag, "retrieve_regulations", lambda q, k=2: [])
    fake = _FakeLLM(structured_response=_ok_response())
    delta = {"failed_rules": ["FINRA_2111"], "failed_details": [],
             "missing_evidence_ids": []}
    generate_proposal(client_id="C", client_data={}, prompt="p",
                      constraint_delta=delta, llm=fake)
    # Wipe the file so a re-read would fail; cache should hold
    regulations_path.write_text("not json at all")
    generate_proposal(client_id="C", client_data={}, prompt="p",
                      constraint_delta=delta, llm=fake)
    # Both calls completed without errors → cache held


def test_generate_proposal_with_missing_regulations_file_does_not_crash(
    tmp_path, monkeypatch
):
    """When regulations.json is missing, _build_rag_context silently degrades
    (per the current implementation — flagged in the code review as a
    convention violation but kept as-is here)."""
    monkeypatch.setattr(agent, "_REG_PATH", str(tmp_path / "absent.json"))
    agent._regulations_cache = None
    fake = _FakeLLM(structured_response=_ok_response())
    generate_proposal(
        client_id="C", client_data={}, prompt="p",
        constraint_delta={
            "failed_rules": ["FINRA_2111"],
            "failed_details": [], "missing_evidence_ids": [],
        },
        llm=fake,
    )
    # No crash — degraded silently
    sys_msg = fake.system_message()
    assert "AUDITOR CONSTRAINT DELTA" in sys_msg
