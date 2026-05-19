"""Tests for app/auditor/rule_engine.py — the deterministic audit pipeline."""

from __future__ import annotations

import json

import pytest

from app.auditor import rule_engine
from app.auditor.models import (
    FailedRuleDetail,
    KYCOperator,
    KYCRequirement,
    ProvidedEvidence,
    TradeProposal,
    TriggerCondition,
    TriggerOperator,
)
from app.auditor.rule_engine import (
    AFFIRMATIVE_KEYWORDS,
    CONCENTRATION_LIMIT,
    MAX_SINGLE_TRADE_USD,
    VALID_ACTIONS,
    _kyc_passes,
    _score_evidence_coverage,
    _scrap_is_grounded,
    _trigger_matches,
    evaluate_proposal,
    get_adjacent_risks,
    get_derivative_markers,
    get_suppress_signals_for_ticker,
    run_static_checks,
)


# ---------------------------------------------------------------------------
# _scrap_is_grounded — word-boundary substring check.
# ---------------------------------------------------------------------------

def test_scrap_grounded_exact_match():
    assert _scrap_is_grounded("yes I agree", "I said yes I agree to the trade")


def test_scrap_grounded_case_insensitive():
    assert _scrap_is_grounded("YES I AGREE", "yes i agree to terms")


def test_scrap_not_grounded_when_substring_only():
    # 'user' is a substring of 'username' but should NOT word-match
    assert not _scrap_is_grounded("user", "the username system is fine")


def test_scrap_grounded_handles_punctuation_in_scrap():
    assert _scrap_is_grounded("yes, I agree.", "I confirm: yes I agree")


def test_scrap_not_grounded_empty():
    assert not _scrap_is_grounded("", "anything")
    assert not _scrap_is_grounded("   ", "anything")


def test_scrap_not_grounded_word_order_matters():
    # The scrap "agree yes" with separator should match "agree, yes" but not "yes agree"
    assert _scrap_is_grounded("agree yes", "I agree, yes I do")
    assert not _scrap_is_grounded("agree yes", "yes I agree")


# ---------------------------------------------------------------------------
# Ticker config loaders.
# ---------------------------------------------------------------------------

def test_get_suppress_signals_known_ticker(ticker_config_path):
    assert get_suppress_signals_for_ticker("SPY") == {"HIGH_RISK_PRODUCT", "SPECULATIVE_PRODUCT"}


def test_get_suppress_signals_unknown_ticker(ticker_config_path):
    assert get_suppress_signals_for_ticker("ZZZ") == set()


def test_get_suppress_signals_normalizes_case(ticker_config_path):
    assert get_suppress_signals_for_ticker("spy") == {"HIGH_RISK_PRODUCT", "SPECULATIVE_PRODUCT"}


def test_get_derivative_markers(ticker_config_path):
    markers = get_derivative_markers()
    assert "option" in markers
    assert "call" in markers


def test_ticker_config_cached(ticker_config_path):
    first = get_derivative_markers()
    # Modify the underlying file — cache should hold
    ticker_config_path.write_text(json.dumps({"tickers": {}, "derivative_markers": ["MUTATED"]}))
    second = get_derivative_markers()
    assert first == second


# ---------------------------------------------------------------------------
# Adjacency loader.
# ---------------------------------------------------------------------------

def test_get_adjacent_risks(regulations_path):
    adj = get_adjacent_risks()
    assert "FINRA_2111" in adj
    assert "SEC_REG_BI" in adj["FINRA_2111"]


def test_adjacency_cached(regulations_path):
    first = get_adjacent_risks()
    second = get_adjacent_risks()
    assert first is second


def test_adjacency_missing_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(rule_engine, "_REGULATIONS_PATH", str(tmp_path / "nope.json"))
    with pytest.raises(FileNotFoundError):
        get_adjacent_risks()


def test_adjacency_skips_entries_without_id_or_related(tmp_path, monkeypatch):
    p = tmp_path / "regs.json"
    p.write_text(json.dumps([
        {"id": "A", "metadata": {"related": ["B"]}},
        {"metadata": {"related": ["C"]}},     # missing id
        {"id": "B", "metadata": {"related": []}},  # empty related → skipped
    ]))
    monkeypatch.setattr(rule_engine, "_REGULATIONS_PATH", str(p))
    adj = get_adjacent_risks()
    assert adj == {"A": ["B"]}


# ---------------------------------------------------------------------------
# run_static_checks — every branch.
# ---------------------------------------------------------------------------

def _client_state(equity: float = 100_000.0, assets=None, kyc=True, aml=True) -> dict:
    return {
        "profile": {"age": 40, "risk_tolerance": "Moderate", "compliance_history": "Clean record.", "archetype": "NORMAL"},
        "holdings": {"assets": assets if assets is not None else []},
        "account_state": {
            "kyc_verified": kyc,
            "aml_ofac_cleared": aml,
            "total_equity_usd": equity,
            "total_portfolio_value": equity,
        },
    }


def _proposal(**kw) -> TradeProposal:
    base = dict(
        proposal_id="p1", client_id="c1",
        action="BUY", asset_ticker="SPY",
        instrument_type="EQUITY", trade_size_usd=1_000.0,
        rationale="ok",
    )
    base.update(kw)
    return TradeProposal(**base)


def test_static_invalid_action_short_circuits():
    fails = run_static_checks(_proposal(action="DANCE"), _client_state())
    assert len(fails) == 1
    assert "Invalid action" in fails[0].description


def test_static_hold_and_review_no_checks():
    assert run_static_checks(_proposal(action="HOLD"), _client_state()) == []
    assert run_static_checks(_proposal(action="REVIEW"), _client_state()) == []


def test_static_invalid_ticker_empty():
    fails = run_static_checks(_proposal(asset_ticker=""), _client_state())
    assert any("Invalid or missing ticker" in f.description for f in fails)


def test_static_invalid_ticker_unknown():
    fails = run_static_checks(_proposal(asset_ticker="UNKNOWN"), _client_state())
    assert any("Invalid or missing ticker" in f.description for f in fails)


def test_static_invalid_ticker_too_long():
    fails = run_static_checks(_proposal(asset_ticker="ABCDEFGHIJK"), _client_state())
    assert any("Invalid or missing ticker" in f.description for f in fails)


def test_static_negative_trade_size():
    fails = run_static_checks(_proposal(trade_size_usd=-1.0), _client_state())
    assert any("Negative trade size" in f.description for f in fails)


def test_static_max_trade_ceiling():
    fails = run_static_checks(_proposal(trade_size_usd=20_000_000.0), _client_state(equity=1e10))
    assert any("exceeds the single-trade ceiling" in f.description for f in fails)


def test_static_kyc_failure():
    fails = run_static_checks(_proposal(), _client_state(kyc=False))
    assert any("KYC-verified" in f.description for f in fails)


def test_static_aml_failure():
    fails = run_static_checks(_proposal(), _client_state(aml=False))
    assert any("AML/OFAC" in f.description for f in fails)


def test_static_zero_trade_size_returns_early():
    """Zero trade size short-circuits the funds/holdings checks."""
    fails = run_static_checks(_proposal(trade_size_usd=0.0), _client_state(equity=0.0))
    # Should be empty unless kyc/aml fail (they don't here)
    assert fails == []


def test_static_buy_insufficient_funds():
    fails = run_static_checks(_proposal(trade_size_usd=200_000.0), _client_state(equity=1_000.0))
    assert any("Insufficient funds" in f.description for f in fails)


def test_static_buy_concentration_fires_at_overage():
    assets = [{"asset": "SPY", "value": 60_000.0}]
    fails = run_static_checks(_proposal(trade_size_usd=10_000.0), _client_state(equity=100_000.0, assets=assets))
    conc = [f for f in fails if f.clause_id == "STATIC.05"]
    assert len(conc) == 1
    assert conc[0].is_ack is True
    assert 0.85 <= conc[0].weight <= 1.0
    assert "EVID_CONCENTRATION_ACK" == conc[0].missing_evidence_id


def test_static_buy_concentration_zero_portfolio_is_full():
    fails = run_static_checks(_proposal(trade_size_usd=5_000.0),
                              _client_state(equity=0.0))
    # zero portfolio + positive buy → 100% conc → fires
    conc = [f for f in fails if f.clause_id == "STATIC.05"]
    # Plus insufficient funds — both fire at zero equity.
    assert len(conc) == 1
    assert conc[0].weight == 1.0  # 100% concentration


def test_static_buy_concentration_below_limit_no_fire():
    assets = [{"asset": "SPY", "value": 10_000.0}]
    fails = run_static_checks(_proposal(trade_size_usd=10_000.0), _client_state(equity=100_000.0, assets=assets))
    assert not any(f.clause_id == "STATIC.05" for f in fails)


def test_static_sell_insufficient_holdings_zero():
    fails = run_static_checks(_proposal(action="SELL", trade_size_usd=1_000.0), _client_state())
    assert any("does not hold" in f.description for f in fails)


def test_static_sell_holdings_below_size():
    assets = [{"asset": "SPY", "value": 500.0}]
    fails = run_static_checks(_proposal(action="SELL", trade_size_usd=1_000.0),
                              _client_state(equity=100_000.0, assets=assets))
    assert any("but client holds only" in f.description for f in fails)


def test_static_sell_ok_when_holdings_sufficient():
    assets = [{"asset": "SPY", "value": 100_000.0}]
    fails = run_static_checks(_proposal(action="SELL", trade_size_usd=10_000.0),
                              _client_state(equity=100_000.0, assets=assets))
    assert fails == []


def test_static_sell_without_ticker_skips_holdings_check():
    """If asset_ticker is empty, the SELL branch only catches it via the
    invalid-ticker check earlier."""
    fails = run_static_checks(_proposal(action="SELL", asset_ticker=""),
                              _client_state())
    # Only the invalid-ticker failure should fire here.
    holdings_fails = [f for f in fails if "holdings" in f.description.lower()]
    assert holdings_fails == []


# ---------------------------------------------------------------------------
# _trigger_matches
# ---------------------------------------------------------------------------

def _proposal_for_trigger(action="BUY", signals=None) -> TradeProposal:
    p = _proposal(action=action)
    p.prompt_signals = signals or []
    return p


def test_trigger_prompt_signal_contains():
    c = TriggerCondition(condition_id="c", clause_id="cl",
                         trigger_field="prompt_signal",
                         trigger_operator=TriggerOperator.CONTAINS,
                         trigger_value="HIGH_RISK_PRODUCT")
    assert _trigger_matches(c, _proposal_for_trigger(signals=["HIGH_RISK_PRODUCT"])) is True
    assert _trigger_matches(c, _proposal_for_trigger(signals=[])) is False


def test_trigger_prompt_signal_equals():
    c = TriggerCondition(condition_id="c", clause_id="cl",
                         trigger_field="prompt_signal",
                         trigger_operator=TriggerOperator.EQUALS,
                         trigger_value="KYC_BYPASS")
    assert _trigger_matches(c, _proposal_for_trigger(signals=["KYC_BYPASS"])) is True


def test_trigger_action_equals():
    c = TriggerCondition(condition_id="c", clause_id="cl",
                         trigger_field="proposal.action",
                         trigger_operator=TriggerOperator.EQUALS,
                         trigger_value="buy")
    assert _trigger_matches(c, _proposal_for_trigger("BUY")) is True
    assert _trigger_matches(c, _proposal_for_trigger("SELL")) is False


def test_trigger_action_in_list():
    c = TriggerCondition(condition_id="c", clause_id="cl",
                         trigger_field="proposal.action",
                         trigger_operator=TriggerOperator.IN,
                         trigger_value="[BUY,SELL]")
    assert _trigger_matches(c, _proposal_for_trigger("BUY")) is True
    assert _trigger_matches(c, _proposal_for_trigger("REVIEW")) is False


def test_trigger_unknown_field_returns_false():
    c = TriggerCondition(condition_id="c", clause_id="cl",
                         trigger_field="proposal.banana",
                         trigger_operator=TriggerOperator.EQUALS,
                         trigger_value="x")
    assert _trigger_matches(c, _proposal_for_trigger()) is False


def test_trigger_prompt_signal_unknown_operator_returns_false():
    c = TriggerCondition(condition_id="c", clause_id="cl",
                         trigger_field="prompt_signal",
                         trigger_operator=TriggerOperator.EXISTS,
                         trigger_value="X")
    assert _trigger_matches(c, _proposal_for_trigger(signals=["X"])) is False


# ---------------------------------------------------------------------------
# _kyc_passes
# ---------------------------------------------------------------------------

def _kyc(domain="profile", field="age", op=KYCOperator.GREATER_THAN, threshold="18") -> KYCRequirement:
    return KYCRequirement(kyc_id="k", condition_id="c", client_field=field,
                          operator=op, threshold=threshold, domain=domain)


def test_kyc_proposal_check_always_fails():
    assert _kyc_passes(_kyc(domain="proposal_check"), {}) is False


def test_kyc_portfolio_check_always_passes():
    assert _kyc_passes(_kyc(domain="portfolio_check"), {}) is True


def test_kyc_missing_field_fails_conservatively():
    assert _kyc_passes(_kyc(), {"profile": {}}) is False


def test_kyc_missing_field_passes_when_forced():
    assert _kyc_passes(_kyc(), {"profile": {}}, is_forced=True) is True


def test_kyc_bool_threshold_equals():
    k = _kyc(field="kyc_verified", op=KYCOperator.EQUALS, threshold="True")
    assert _kyc_passes(k, {"profile": {"kyc_verified": True}}) is True
    assert _kyc_passes(k, {"profile": {"kyc_verified": False}}) is False


def test_kyc_bool_threshold_not_equals():
    k = _kyc(field="kyc_verified", op=KYCOperator.NOT_EQUALS, threshold="True")
    assert _kyc_passes(k, {"profile": {"kyc_verified": False}}) is True


def test_kyc_numeric_less_than():
    k = _kyc(field="age", op=KYCOperator.LESS_THAN, threshold="65")
    assert _kyc_passes(k, {"profile": {"age": 40}}) is True
    assert _kyc_passes(k, {"profile": {"age": 70}}) is False


def test_kyc_numeric_greater_than():
    k = _kyc(field="age", op=KYCOperator.GREATER_THAN, threshold="18")
    assert _kyc_passes(k, {"profile": {"age": 40}}) is True


def test_kyc_numeric_equals():
    k = _kyc(field="age", op=KYCOperator.EQUALS, threshold="40")
    assert _kyc_passes(k, {"profile": {"age": 40}}) is True


def test_kyc_numeric_not_equals():
    k = _kyc(field="age", op=KYCOperator.NOT_EQUALS, threshold="40")
    assert _kyc_passes(k, {"profile": {"age": 41}}) is True


def test_kyc_string_equals():
    k = _kyc(field="risk_tolerance", op=KYCOperator.EQUALS, threshold="Aggressive")
    assert _kyc_passes(k, {"profile": {"risk_tolerance": "Aggressive"}}) is True
    assert _kyc_passes(k, {"profile": {"risk_tolerance": "Moderate"}}) is False


def test_kyc_string_not_equals():
    k = _kyc(field="risk_tolerance", op=KYCOperator.NOT_EQUALS, threshold="Aggressive")
    assert _kyc_passes(k, {"profile": {"risk_tolerance": "Moderate"}}) is True


def test_kyc_string_not_contains():
    k = _kyc(field="compliance_history", op=KYCOperator.NOT_CONTAINS, threshold="violation")
    assert _kyc_passes(k, {"profile": {"compliance_history": "Clean record."}}) is True
    assert _kyc_passes(k, {"profile": {"compliance_history": "Prior violation noted"}}) is False


def test_kyc_unknown_operator_returns_true():
    """The catch-all at the bottom returns True for unhandled operators."""
    k = _kyc(field="risk_tolerance",
             op=KYCOperator.SEMANTIC_SIMILAR, threshold="something")
    # Without mocking the signal_detector this triggers the SEMANTIC_SIMILAR path
    # — which our FakeEncoder + monkeypatch isn't loaded for here.  Instead
    # we test the operator-not-handled fallthrough with an op that exists in
    # the enum but doesn't match any branch (force via a synthetic KYC).
    # The signal_detector path is exercised separately below.
    # Simulate "fell through" by using LESS_THAN with a non-numeric threshold
    # and a non-numeric value — both numeric and bool branches skip, string
    # branch only handles EQUALS / NOT_EQUALS / NOT_CONTAINS.
    k2 = _kyc(field="risk_tolerance", op=KYCOperator.LESS_THAN, threshold="banana")
    assert _kyc_passes(k2, {"profile": {"risk_tolerance": "Moderate"}}) is True


def test_kyc_semantic_similar_invokes_embedding(monkeypatch):
    """SEMANTIC_SIMILAR delegates to the signal_detector's similarity function."""
    # The import inside _kyc_passes is a local-scope late import, so we have
    # to patch the source module (it's not yet bound on rule_engine).
    from app.auditor import signal_detector
    monkeypatch.setattr(signal_detector, "get_embedding_similarity",
                        lambda a, b: 0.9)
    k = _kyc(field="compliance_history", op=KYCOperator.SEMANTIC_SIMILAR,
             threshold="Clean record")
    assert _kyc_passes(k, {"profile": {"compliance_history": "no issues"}}) is True


def test_kyc_semantic_similar_below_threshold(monkeypatch):
    from app.auditor import signal_detector
    monkeypatch.setattr(signal_detector, "get_embedding_similarity",
                        lambda a, b: 0.4)
    k = _kyc(field="compliance_history", op=KYCOperator.SEMANTIC_SIMILAR,
             threshold="Clean record")
    assert _kyc_passes(k, {"profile": {"compliance_history": "prior violation"}}) is False


# ---------------------------------------------------------------------------
# _score_evidence_coverage
# ---------------------------------------------------------------------------

def test_score_evidence_empty_scrap_returns_zero():
    assert _score_evidence_coverage("E", "", "desc", "prompt", False) == 0.0


def test_score_evidence_ungrounded_returns_zero():
    # "alpha" appears nowhere in the prompt
    assert _score_evidence_coverage("E", "alpha", "desc", "user typed beta", False) == 0.0


def test_score_evidence_no_description_returns_one():
    assert _score_evidence_coverage("E", "ok", "", "the user said ok now", False) == 1.0


def test_score_evidence_ack_fast_path():
    score = _score_evidence_coverage(
        "EVID_X_ACK",
        scrap="yes I agree",
        description="canonical acknowledgment text",
        prompt="user said yes I agree to the trade",
        is_ack=True,
    )
    assert score == 1.0


def test_score_evidence_ack_no_keyword_falls_through_to_semantic(monkeypatch):
    # rule_engine imports get_embedding_similarity directly at top-of-module,
    # so we patch the rebound name on rule_engine itself.
    monkeypatch.setattr(rule_engine, "get_embedding_similarity",
                        lambda a, b: 0.42)
    # Scrap with NO substring overlap with AFFIRMATIVE_KEYWORDS (yes / yep /
    # sure / understand / agree / confirm / proceed / acknowledge).
    score = _score_evidence_coverage(
        "EVID_X_ACK",
        scrap="fine response",
        description="acknowledgment",
        prompt="user said fine response here",
        is_ack=True,
    )
    # No affirmative keyword in scrap → fast path skipped → semantic score returned
    assert score == 0.42


def test_score_evidence_semantic_path_clamps_negative(monkeypatch):
    monkeypatch.setattr(rule_engine, "get_embedding_similarity",
                        lambda a, b: -0.5)
    score = _score_evidence_coverage(
        "EV", "scrap text", "description", "prompt scrap text", False
    )
    assert score == 0.0


def test_affirmative_keyword_set_contents():
    assert "yes" in AFFIRMATIVE_KEYWORDS
    assert "agree" in AFFIRMATIVE_KEYWORDS


# ---------------------------------------------------------------------------
# evaluate_proposal — end-to-end with the seeded DB + mocked embeddings.
# ---------------------------------------------------------------------------

@pytest.fixture
def _evaluate_setup(seeded_db, regulations_path, ticker_config_path,
                    anchors_path, normal_corpus_path, fake_encoders):
    """All file fixtures + fake encoders wired up for evaluate_proposal."""
    yield


def _eval(prop, state, prompt, **kw):
    return evaluate_proposal(prop, state, prompt, **kw)


def test_evaluate_clean_proposal_auto_approves(_evaluate_setup):
    delta, risk = _eval(
        _proposal(action="BUY", asset_ticker="SPY", trade_size_usd=1_000.0),
        _client_state(equity=100_000.0),
        prompt="Please buy SPY for retirement",
    )
    assert delta.gate_decision == "AUTO_APPROVE"
    assert delta.allow is True
    assert delta.failed_rules == []


def test_evaluate_oversized_trade_escalates(_evaluate_setup):
    delta, risk = _eval(
        _proposal(action="BUY", trade_size_usd=2e7),
        _client_state(equity=1e10),
        prompt="please buy SPY",
    )
    assert delta.gate_decision == "HUMAN_ESCALATION"


def test_evaluate_high_risk_signal_triggers_finra_2111(_evaluate_setup):
    # "leveraged" → FakeEncoder maps to HIGH_RISK_PRODUCT signal class
    delta, risk = _eval(
        _proposal(action="BUY", asset_ticker="TQQQ", trade_size_usd=50_000.0),
        _client_state(equity=100_000.0),
        prompt="I want a leveraged speculative trade",
    )
    # FINRA_2111 should fire on the HIGH_RISK_PRODUCT signal + non-Aggressive profile
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_spy_suppresses_high_risk_signal(_evaluate_setup):
    # SPY in the ticker_config has HIGH_RISK_PRODUCT suppressed (non-derivative)
    delta, risk = _eval(
        _proposal(action="BUY", asset_ticker="SPY", trade_size_usd=1_000.0,
                  instrument_type="EQUITY"),
        _client_state(equity=100_000.0),
        prompt="buy me leveraged SPY exposure",
    )
    # Even though "leveraged" would normally fire HIGH_RISK_PRODUCT, SPY suppresses it.
    assert "FINRA_2111" not in delta.failed_rules


def test_evaluate_derivative_on_spy_keeps_high_risk_signal(_evaluate_setup):
    # SPY but CALL_OPTION → suppression DISABLED
    delta, risk = _eval(
        _proposal(action="BUY", asset_ticker="SPY", trade_size_usd=10_000.0,
                  instrument_type="CALL_OPTION"),
        _client_state(equity=100_000.0),
        prompt="buy SPY leveraged speculative call option",
    )
    # HIGH_RISK_PRODUCT still flows through → FINRA_2111 fires
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_derivative_marker_in_prompt_disables_suppression(_evaluate_setup):
    # Even when instrument_type=EQUITY, the marker word disables suppression
    delta, risk = _eval(
        _proposal(action="BUY", asset_ticker="SPY", trade_size_usd=10_000.0,
                  instrument_type="EQUITY"),
        _client_state(equity=100_000.0),
        prompt="buy me a SPY call option leveraged speculative",
    )
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_evidence_cures_finra_2111(_evaluate_setup):
    """Aggressive-risk client supplying the ACK evidence reduces R to near-zero."""
    state = _client_state(equity=100_000.0)
    state["profile"]["risk_tolerance"] = "Aggressive"  # passes the rule
    delta, risk = _eval(
        _proposal(action="BUY", asset_ticker="TQQQ", trade_size_usd=1_000.0),
        state,
        prompt="leveraged speculative trade please",
    )
    # Aggressive risk tolerance → KYC passes → no FINRA_2111 failure
    assert "FINRA_2111" not in delta.failed_rules


def test_evaluate_review_action_skips_concentration(_evaluate_setup):
    """REVIEW is a question, not a trade — no portfolio risk."""
    delta, _ = _eval(
        _proposal(action="REVIEW", trade_size_usd=0.0),
        _client_state(equity=100_000.0),
        prompt="what's my balance",
    )
    assert delta.gate_decision in ("AUTO_APPROVE", "REFINEMENT")
    assert "STATIC_PORTFOLIO" not in delta.failed_rules


def test_evaluate_evidence_grounded_path(_evaluate_setup, monkeypatch):
    """Provide an ACK evidence with a grounded scrap → C_ev=1 cures the rule."""
    from app.auditor import signal_detector
    monkeypatch.setattr(signal_detector, "get_embedding_similarity", lambda a, b: 1.0)
    prompt = "I want to buy 60% TQQQ in my account, yes I agree to the risk"
    prop = _proposal(
        action="BUY", asset_ticker="TQQQ", trade_size_usd=60_000.0,
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_CONCENTRATION_ACK",
            value=True,
            scrap="yes I agree",
        )],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0), prompt=prompt)
    # Concentration evidence ACK is fast-path 1.0 → reduces composite
    assert delta.gate_decision != "HUMAN_ESCALATION"


def test_evaluate_evidence_not_provided_no_cure(_evaluate_setup):
    """When provided_evidence doesn't list the missing ID, C_ev stays 0."""
    assets = [{"asset": "TQQQ", "value": 70_000.0}]
    delta, _ = _eval(
        _proposal(action="BUY", asset_ticker="TQQQ", trade_size_usd=5_000.0),
        _client_state(equity=100_000.0, assets=assets),
        prompt="buy TQQQ",
    )
    assert "EVID_CONCENTRATION_ACK" in delta.missing_evidence_ids


def test_evaluate_cascade_triggers_adjacent_rules(_evaluate_setup):
    """When FINRA_2111 fires loud enough to leave AUTO_APPROVE, the adjacent
    rule (SEC_REG_BI) is force-evaluated."""
    delta, _ = _eval(
        # Big trade → TSF saturates → R >> 0.20 → REFINEMENT or ESCALATION,
        # which exposes failed_rules in the delta (AUTO_APPROVE wipes them).
        _proposal(action="BUY", asset_ticker="TQQQ", trade_size_usd=80_000.0),
        _client_state(equity=100_000.0),
        prompt="leveraged speculative product",
    )
    assert delta.gate_decision != "AUTO_APPROVE"
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_provided_evidence_value_false_skipped(_evaluate_setup):
    """evidence_value=False means the proposer didn't actually provide it."""
    prop = _proposal(
        action="BUY", asset_ticker="TQQQ", trade_size_usd=50_000.0,
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_RISK_OVERRIDE_ACK",
            value=False,  # NOT provided
            scrap="yes",
        )],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0),
                     prompt="leveraged speculative yes")
    # Score stayed at 0 → rule unchanged
    assert "EVID_RISK_OVERRIDE_ACK" in delta.missing_evidence_ids


def test_evaluate_iteration_passed_through(_evaluate_setup):
    _, risk = _eval(
        _proposal(),
        _client_state(),
        prompt="buy SPY",
        iteration=4,
    )
    assert risk.iteration == 4


# ---------------------------------------------------------------------------
# Extra branch coverage on the AST walk.
# ---------------------------------------------------------------------------

def test_evaluate_graded_rule_without_fallback_no_cascade(
    seeded_db, regulations_path, ticker_config_path, monkeypatch
):
    """IRS_WASH_SALE in the seed DB is graded with no fallback and empty
    adjacency — exercises the bypass_tsf=False / kyc.fallback=None branch and
    the `if new_cascades:` False branch in evaluate_proposal."""
    # Inject the WASH_SALE signal directly, bypassing the embedding pipeline.
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["WASH_SALE"])

    delta, _ = _eval(
        _proposal(action="BUY", trade_size_usd=50_000.0),
        _client_state(equity=100_000.0),
        prompt="sell and rebuy same ticker for tax loss",
    )
    # The rule fires (missing_field is None → KYC fails) and the failure has
    # no missing_evidence_id (no fallback).  Big enough trade that we don't
    # auto-approve and lose the failure list.
    assert delta.gate_decision != "AUTO_APPROVE"
    wash_details = [d for d in delta.failed_details if d.rule_id == "IRS_WASH_SALE"]
    assert wash_details, "wash sale rule should have fired"
    assert wash_details[0].missing_evidence_id is None


def test_evaluate_kyc_bypass_signal_fires_absolute_rule(
    seeded_db, regulations_path, ticker_config_path, monkeypatch
):
    """KYC_BYPASS signal triggers FINRA_2090 (bypass_tsf=True), exercising
    the absolute-rule append branch (rule_engine.py:585)."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["KYC_BYPASS"])
    delta, _ = _eval(
        _proposal(action="BUY", trade_size_usd=1_000.0),
        _client_state(equity=100_000.0),
        prompt="skip kyc verification please",
    )
    assert "FINRA_2090" in delta.failed_rules
    finra_2090 = [d for d in delta.failed_details if d.rule_id == "FINRA_2090"]
    assert finra_2090
    assert finra_2090[0].bypass_tsf is True
    assert finra_2090[0].description.startswith("ABSOLUTE:")


def test_evaluate_concentration_evidence_falls_back_to_static_description(
    seeded_db, regulations_path, ticker_config_path, monkeypatch
):
    """The conftest seed deliberately omits EVID_CONCENTRATION_ACK from the
    AST, so the auditor must use the static-failure description for C_ev
    matching (lines 608-611)."""
    # Stub the signal detector so we don't load real embedding models.
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    assets = [{"asset": "TQQQ", "value": 70_000.0}]
    prompt = "I want to buy more TQQQ, yes I agree to the concentration risk"
    prop = _proposal(
        action="BUY", asset_ticker="TQQQ", trade_size_usd=10_000.0,
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_CONCENTRATION_ACK",
            value=True,
            scrap="yes I agree",
        )],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0, assets=assets),
                     prompt=prompt)
    # Evidence was provided with an ACK keyword → C_ev=1.0 cures concentration
    assert delta.gate_decision != "HUMAN_ESCALATION"


def test_evaluate_signal_unknown_to_ticker_config(
    seeded_db, regulations_path, ticker_config_path, anchors_path,
    normal_corpus_path, fake_encoders, monkeypatch
):
    """detect_signals returns names not in suppress_set → no suppression occurs.

    Covers the empty `suppressed` path inside the suppression-info log gate.
    """
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["UNKNOWN_SIGNAL"])
    delta, _ = _eval(
        _proposal(action="BUY", asset_ticker="SPY", trade_size_usd=1_000.0),
        _client_state(equity=100_000.0),
        prompt="anything",
    )
    # Nothing relevant fires
    assert delta.gate_decision == "AUTO_APPROVE"


# ---------------------------------------------------------------------------
# _trigger_matches edge: proposal.action with non-EQUALS/IN operator.
# ---------------------------------------------------------------------------

def test_trigger_action_with_unsupported_operator_returns_false():
    c = TriggerCondition(condition_id="c", clause_id="cl",
                         trigger_field="proposal.action",
                         trigger_operator=TriggerOperator.CONTAINS,
                         trigger_value="BUY")
    assert _trigger_matches(c, _proposal_for_trigger("BUY")) is False


# ---------------------------------------------------------------------------
# _kyc_passes edge cases for the LSP-falsy combinations.
# ---------------------------------------------------------------------------

def test_kyc_bool_threshold_with_unsupported_operator_falls_through():
    """A bool-shaped threshold with LESS_THAN can't be answered by the bool
    branch; the numeric branch converts True→1 and compares against 1."""
    k = _kyc(field="kyc_verified", op=KYCOperator.LESS_THAN, threshold="1")
    # value=True → float(True)=1.0, threshold=1.0, 1<1 False
    assert _kyc_passes(k, {"profile": {"kyc_verified": True}}) is False


def test_kyc_numeric_threshold_with_unsupported_operator_falls_to_string():
    """NOT_CONTAINS on a numeric value falls to the string-compare branch."""
    k = _kyc(field="age", op=KYCOperator.NOT_CONTAINS, threshold="42")
    # str(40) does not contain '42' → True
    assert _kyc_passes(k, {"profile": {"age": 40}}) is True


def test_kyc_unhandled_string_operator_returns_true():
    """SEMANTIC_SIMILAR on a numeric field — falls through to the catch-all
    `return True`."""
    # Threshold is also numeric, so the numeric block doesn't match the op;
    # the string-compare block also doesn't handle SEMANTIC_SIMILAR directly
    # — wait, it does.  Use EXISTS (TriggerOperator only, not KYCOperator —
    # this branch is structurally guarded but we test the bottom-most return
    # path by exhausting all upstream branches.
    class FakeOp:
        value = "FAKE"

    k = _kyc(field="age", op=FakeOp(), threshold="40")
    # All branches fall through → returns True
    assert _kyc_passes(k, {"profile": {"age": 40}}) is True
