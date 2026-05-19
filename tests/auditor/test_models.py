"""Tests for app/auditor/models.py — dataclasses + serialisation helpers."""

from app.auditor.models import (
    ConstraintDelta,
    DERIVATIVE_INSTRUMENT_TYPES,
    EvidenceFallback,
    FailedRuleDetail,
    INSTRUMENT_TYPES,
    KYCOperator,
    KYCRequirement,
    ProvidedEvidence,
    Regulation,
    RuleClause,
    TradeProposal,
    TriggerCondition,
    TriggerOperator,
)


def test_instrument_type_vocab():
    assert "EQUITY" in INSTRUMENT_TYPES
    assert "CALL_OPTION" in INSTRUMENT_TYPES
    assert "OPTION" not in INSTRUMENT_TYPES  # alias, not in vocab
    assert "CALL_OPTION" in DERIVATIVE_INSTRUMENT_TYPES
    assert "OPTION" in DERIVATIVE_INSTRUMENT_TYPES  # legacy alias retained
    assert "EQUITY" not in DERIVATIVE_INSTRUMENT_TYPES


def test_enum_string_values():
    assert TriggerOperator.EQUALS.value == "=="
    assert TriggerOperator.IN.value == "IN"
    assert TriggerOperator.CONTAINS.value == "CONTAINS"
    assert TriggerOperator.EXISTS.value == "EXISTS"

    assert KYCOperator.LESS_THAN.value == "<"
    assert KYCOperator.GREATER_THAN.value == ">"
    assert KYCOperator.EQUALS.value == "=="
    assert KYCOperator.NOT_EQUALS.value == "!="
    assert KYCOperator.NOT_CONTAINS.value == "NOT_CONTAINS"
    assert KYCOperator.SEMANTIC_SIMILAR.value == "SEMANTIC_SIMILAR"


def test_provided_evidence_defaults():
    ev = ProvidedEvidence(evidence_id="X", value=True)
    assert ev.scrap == ""
    assert ev.evidence_path == ""


def test_provided_evidence_from_dict_with_all_fields():
    ev = ProvidedEvidence.from_dict({
        "evidence_id": "EVID_X",
        "value": True,
        "scrap": "yes I agree",
        "evidence_path": "GraphRAG/FINRA_2111",
    })
    assert ev.evidence_id == "EVID_X"
    assert ev.value is True
    assert ev.scrap == "yes I agree"
    assert ev.evidence_path == "GraphRAG/FINRA_2111"


def test_provided_evidence_from_dict_with_missing_fields():
    ev = ProvidedEvidence.from_dict({})
    assert ev.evidence_id == ""
    assert ev.value is False
    assert ev.scrap == ""
    assert ev.evidence_path == ""


def test_trade_proposal_defaults():
    tp = TradeProposal(
        proposal_id="p1", client_id="c1", action="BUY", asset_ticker="SPY",
    )
    assert tp.instrument_type == "EQUITY"
    assert tp.trade_size_usd == 0.0
    assert tp.rationale == ""
    assert tp.user_question == ""
    assert tp.provided_evidence == []
    assert tp.prompt_signals == []


def test_trade_proposal_from_proposal_json_full():
    tp = TradeProposal.from_proposal_json({
        "proposal_id": "pid",
        "action": "buy",
        "asset_ticker": "AAPL",
        "instrument_type": "EQUITY",
        "trade_size_usd": 5000.0,
        "rationale": "long-term hold",
        "provided_evidence": [
            {"evidence_id": "EV1", "value": True, "scrap": "yes"},
            {"evidence_id": "EV2", "value": False, "scrap": ""},
        ],
    }, client_id="CLIENT1")
    assert tp.client_id == "CLIENT1"
    assert tp.action == "buy"
    assert tp.asset_ticker == "AAPL"
    assert tp.instrument_type == "EQUITY"
    assert tp.trade_size_usd == 5000.0
    assert tp.rationale == "long-term hold"
    assert len(tp.provided_evidence) == 2
    assert tp.provided_evidence[0].evidence_id == "EV1"
    assert tp.provided_evidence[1].value is False


def test_trade_proposal_from_proposal_json_empty_string_instrument():
    tp = TradeProposal.from_proposal_json(
        {"action": "BUY", "asset_ticker": "SPY", "instrument_type": ""},
        client_id="C",
    )
    assert tp.instrument_type == "EQUITY"


def test_trade_proposal_from_proposal_json_none_instrument():
    tp = TradeProposal.from_proposal_json(
        {"action": "BUY", "asset_ticker": "SPY", "instrument_type": None},
        client_id="C",
    )
    assert tp.instrument_type == "EQUITY"


def test_trade_proposal_from_proposal_json_defaults_on_empty_dict():
    tp = TradeProposal.from_proposal_json({}, client_id="X")
    assert tp.proposal_id == ""
    assert tp.action == "REVIEW"
    assert tp.asset_ticker == ""
    assert tp.instrument_type == "EQUITY"
    assert tp.trade_size_usd == 0.0
    assert tp.rationale == ""
    assert tp.provided_evidence == []


def test_failed_rule_detail_defaults():
    d = FailedRuleDetail(rule_id="R", clause_id="C", description="d")
    assert d.missing_evidence_id is None
    assert d.is_ack is False
    assert d.weight is None
    assert d.bypass_tsf is False


def test_constraint_delta_to_dict_round_trip():
    delta = ConstraintDelta(
        allow=False,
        audit_risk_score=0.123456,
        gate_decision="REFINEMENT",
        failed_rules=["FINRA_2111"],
        missing_evidence_ids=["EV1"],
        failed_details=[
            FailedRuleDetail(
                rule_id="FINRA_2111", clause_id="2111.01",
                description="bad", missing_evidence_id="EV1", bypass_tsf=False,
            )
        ],
    )
    d = delta.to_dict()
    assert d["allow"] is False
    assert d["status"] == "REJECT"
    assert d["audit_risk_score"] == 0.1235  # rounded to 4
    assert d["gate_decision"] == "REFINEMENT"
    assert d["failed_rules"] == ["FINRA_2111"]
    assert d["missing_evidence_ids"] == ["EV1"]
    assert d["failed_details"][0]["rule_id"] == "FINRA_2111"
    assert d["failed_details"][0]["bypass_tsf"] is False


def test_constraint_delta_to_dict_allow_true_status_is_allow():
    delta = ConstraintDelta(allow=True)
    assert delta.to_dict()["status"] == "ALLOW"


def test_ast_dataclass_defaults():
    """RuleClause / TriggerCondition / Regulation defaults populate properly."""
    fb = EvidenceFallback(evidence_id="E", description="d")
    assert fb.is_ack is False

    kyc = KYCRequirement(
        kyc_id="K", condition_id="C", client_field="f",
        operator=KYCOperator.EQUALS, threshold="t", domain="profile",
    )
    assert kyc.fallback is None

    cond = TriggerCondition(
        condition_id="C", clause_id="L", trigger_field="f",
        trigger_operator=TriggerOperator.EQUALS, trigger_value="v",
    )
    assert cond.kyc_requirements == []

    clause = RuleClause(clause_id="L", rule_id="R", description="d")
    assert clause.conditions == []

    reg = Regulation(rule_id="R", rule_name="N", bypass_tsf=True, description="d")
    assert reg.clauses == []
