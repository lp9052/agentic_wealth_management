"""Tests for app/auditor/rule_engine.py — public surface only.

Public API:
  evaluate_proposal, run_static_checks, get_adjacent_risks,
  get_suppress_signals_for_ticker, get_derivative_markers,
  AFFIRMATIVE_KEYWORDS, VALID_ACTIONS, MAX_SINGLE_TRADE_USD, CONCENTRATION_LIMIT

Branch coverage for the private helpers (_trigger_matches, _kyc_passes,
_score_evidence_coverage, _scrap_is_grounded, _load_ticker_config) is achieved
indirectly through evaluate_proposal with appropriately seeded rules — see
the OP_COVERAGE block in tests/conftest.py.
"""

from __future__ import annotations

import json

import pytest

from app.auditor import rule_engine
from app.auditor.models import ProvidedEvidence, TradeProposal
from app.auditor.rule_engine import (
    AFFIRMATIVE_KEYWORDS,
    CONCENTRATION_LIMIT,
    MAX_SINGLE_TRADE_USD,
    VALID_ACTIONS,
    evaluate_proposal,
    get_adjacent_risks,
    get_derivative_markers,
    get_suppress_signals_for_ticker,
    run_static_checks,
)


# ---------------------------------------------------------------------------
# Module-level constants.
# ---------------------------------------------------------------------------

def test_valid_actions_contents():
    assert VALID_ACTIONS == {"BUY", "SELL", "HOLD", "REVIEW"}


def test_max_single_trade_ceiling():
    assert MAX_SINGLE_TRADE_USD == 10_000_000


def test_concentration_limit():
    assert CONCENTRATION_LIMIT == 0.50


def test_affirmative_keywords_contents():
    assert "yes" in AFFIRMATIVE_KEYWORDS
    assert "agree" in AFFIRMATIVE_KEYWORDS
    assert "acknowledge" in AFFIRMATIVE_KEYWORDS


# ---------------------------------------------------------------------------
# Ticker config loader (cached file read).
# ---------------------------------------------------------------------------

def test_get_suppress_signals_known_ticker(ticker_config_path):
    assert get_suppress_signals_for_ticker("SPY") == {"HIGH_RISK_PRODUCT", "SPECULATIVE_PRODUCT"}


def test_get_suppress_signals_unknown_ticker(ticker_config_path):
    assert get_suppress_signals_for_ticker("ZZZ") == set()


def test_get_suppress_signals_normalizes_case(ticker_config_path):
    assert get_suppress_signals_for_ticker("spy") == get_suppress_signals_for_ticker("SPY")


def test_get_derivative_markers(ticker_config_path):
    markers = get_derivative_markers()
    assert "option" in markers
    assert "call" in markers


def test_ticker_config_cached(ticker_config_path):
    first = get_derivative_markers()
    ticker_config_path.write_text(json.dumps({"tickers": {}, "derivative_markers": ["MUTATED"]}))
    second = get_derivative_markers()
    assert first == second  # cache holds


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


def test_adjacency_missing_file_raises_loudly(tmp_path, monkeypatch):
    """Per CLAUDE.md: missing regulations.json is a deployment error, not
    a runtime fallback."""
    monkeypatch.setattr(rule_engine, "_REGULATIONS_PATH", str(tmp_path / "absent.json"))
    with pytest.raises(FileNotFoundError):
        get_adjacent_risks()


def test_adjacency_skips_entries_without_id_or_empty_related(tmp_path, monkeypatch):
    p = tmp_path / "regs.json"
    p.write_text(json.dumps([
        {"id": "A", "metadata": {"related": ["B"]}},
        {"metadata": {"related": ["C"]}},          # missing id
        {"id": "B", "metadata": {"related": []}},  # empty related
    ]))
    monkeypatch.setattr(rule_engine, "_REGULATIONS_PATH", str(p))
    assert get_adjacent_risks() == {"A": ["B"]}


# ---------------------------------------------------------------------------
# Test helpers.
# ---------------------------------------------------------------------------

def _client_state(equity: float = 100_000.0, assets=None, kyc=True, aml=True,
                  age: int = 40, risk_tolerance: str = "Moderate",
                  archetype: str = "NORMAL",
                  compliance_history: str = "Clean record.") -> dict:
    return {
        "profile": {"age": age, "risk_tolerance": risk_tolerance,
                    "compliance_history": compliance_history, "archetype": archetype,
                    "kyc_verified": kyc},
        "holdings": {"assets": assets if assets is not None else []},
        "account_state": {
            "kyc_verified": kyc, "aml_ofac_cleared": aml,
            "total_equity_usd": equity, "total_portfolio_value": equity,
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


# ---------------------------------------------------------------------------
# run_static_checks — public, exhaustive branch coverage.
# ---------------------------------------------------------------------------

def test_static_invalid_action_short_circuits():
    fails = run_static_checks(_proposal(action="DANCE"), _client_state())
    assert len(fails) == 1
    assert "Invalid action" in fails[0].description


def test_static_hold_and_review_skip_portfolio_checks():
    assert run_static_checks(_proposal(action="HOLD"), _client_state()) == []
    assert run_static_checks(_proposal(action="REVIEW"), _client_state()) == []


@pytest.mark.parametrize("ticker", ["", "UNKNOWN", "ABCDEFGHIJK"])
def test_static_invalid_ticker(ticker):
    fails = run_static_checks(_proposal(asset_ticker=ticker), _client_state())
    assert any("Invalid or missing ticker" in f.description for f in fails)


def test_static_negative_trade_size():
    fails = run_static_checks(_proposal(trade_size_usd=-1.0), _client_state())
    assert any("Negative trade size" in f.description for f in fails)


def test_static_trade_above_ceiling():
    fails = run_static_checks(_proposal(trade_size_usd=20_000_000.0), _client_state(equity=1e10))
    assert any("exceeds the single-trade ceiling" in f.description for f in fails)


def test_static_kyc_failure():
    fails = run_static_checks(_proposal(), _client_state(kyc=False))
    assert any("KYC-verified" in f.description for f in fails)


def test_static_aml_failure():
    fails = run_static_checks(_proposal(), _client_state(aml=False))
    assert any("AML/OFAC" in f.description for f in fails)


def test_static_zero_trade_size_returns_early():
    """Zero trade size short-circuits funds/holdings/concentration checks."""
    fails = run_static_checks(_proposal(trade_size_usd=0.0), _client_state(equity=0.0))
    assert fails == []


def test_static_buy_insufficient_funds():
    fails = run_static_checks(_proposal(trade_size_usd=200_000.0), _client_state(equity=1_000.0))
    assert any("Insufficient funds" in f.description for f in fails)


def test_static_buy_concentration_fires_above_limit():
    assets = [{"asset": "SPY", "value": 60_000.0}]
    fails = run_static_checks(_proposal(trade_size_usd=10_000.0),
                              _client_state(equity=100_000.0, assets=assets))
    conc = [f for f in fails if f.clause_id == "STATIC.05"]
    assert len(conc) == 1
    assert conc[0].is_ack is True
    assert conc[0].missing_evidence_id == "EVID_CONCENTRATION_ACK"
    assert 0.85 <= conc[0].weight <= 1.0


def test_static_buy_concentration_at_full_portfolio():
    """Zero portfolio + positive trade = 100% concentration."""
    fails = run_static_checks(_proposal(trade_size_usd=5_000.0), _client_state(equity=0.0))
    conc = [f for f in fails if f.clause_id == "STATIC.05"]
    assert conc[0].weight == 1.0


def test_static_buy_concentration_below_limit():
    assets = [{"asset": "SPY", "value": 10_000.0}]
    fails = run_static_checks(_proposal(trade_size_usd=10_000.0),
                              _client_state(equity=100_000.0, assets=assets))
    assert not any(f.clause_id == "STATIC.05" for f in fails)


def test_static_sell_zero_holdings():
    fails = run_static_checks(_proposal(action="SELL", trade_size_usd=1_000.0),
                              _client_state())
    assert any("does not hold" in f.description for f in fails)


def test_static_sell_partial_holdings():
    assets = [{"asset": "SPY", "value": 500.0}]
    fails = run_static_checks(_proposal(action="SELL", trade_size_usd=1_000.0),
                              _client_state(equity=100_000.0, assets=assets))
    assert any("but client holds only" in f.description for f in fails)


def test_static_sell_sufficient_holdings():
    assets = [{"asset": "SPY", "value": 100_000.0}]
    fails = run_static_checks(_proposal(action="SELL", trade_size_usd=10_000.0),
                              _client_state(equity=100_000.0, assets=assets))
    assert fails == []


def test_static_sell_empty_ticker_skips_holdings_check():
    """Empty ticker for SELL only emits the invalid-ticker failure."""
    fails = run_static_checks(_proposal(action="SELL", asset_ticker=""),
                              _client_state())
    holdings_fails = [f for f in fails if "holdings" in f.description.lower()]
    assert holdings_fails == []


# ---------------------------------------------------------------------------
# evaluate_proposal — covers _trigger_matches, _kyc_passes,
# _score_evidence_coverage, _scrap_is_grounded indirectly.
# ---------------------------------------------------------------------------

@pytest.fixture
def evalsetup(seeded_db, regulations_path, ticker_config_path):
    """Wire DB + config; tests stub detect_signals individually for control."""
    yield


def _eval(proposal, state, prompt, **kw):
    return evaluate_proposal(proposal, state, prompt, **kw)


# ── happy path ──────────────────────────────────────────────────────────────

def test_evaluate_clean_proposal_auto_approves(evalsetup, monkeypatch):
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0), prompt="buy SPY")
    assert delta.gate_decision == "AUTO_APPROVE"
    assert delta.allow is True


def test_evaluate_iteration_propagates_to_risk_score(evalsetup, monkeypatch):
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    _, risk = _eval(_proposal(), _client_state(), prompt="p", iteration=7)
    assert risk.iteration == 7


# ── _trigger_matches branch coverage via OP_COVERAGE rules ─────────────────

def test_evaluate_trigger_unknown_field_does_not_fire(evalsetup, monkeypatch):
    """OP.unknown_field uses trigger_field='proposal.asset_ticker' which the
    engine doesn't handle → trigger returns False → rule doesn't fire."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0), prompt="p")
    assert "OP_COVERAGE" not in delta.failed_rules


def test_evaluate_trigger_prompt_signal_with_exists_op_does_not_fire(evalsetup, monkeypatch):
    """OP.sig_exists uses prompt_signal with EXISTS operator (not handled) →
    even if the signal X is present, the trigger returns False."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["X"])
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0), prompt="p")
    # Both OP.sig_exists and OP.act_contains use unhandled operators → no fire
    # (OP_COVERAGE has other clauses but none of their signals are present)


def test_evaluate_trigger_proposal_action_contains_does_not_fire(evalsetup, monkeypatch):
    """OP.act_contains uses proposal.action with CONTAINS (unhandled)."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    delta, _ = _eval(_proposal(action="BUY", trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0), prompt="p")
    # BUY would match an EQUALS or IN check but CONTAINS on action is dead
    # — no fire from OP.act_contains.


def test_evaluate_trigger_action_equals_and_in_both_work(evalsetup, monkeypatch):
    """Both proposal.action EQUALS and IN paths are exercised by the live
    rules (STATIC.05 uses EQUALS, REGBI.01 uses IN).  A normal BUY visits
    both during the AST walk."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    # REGBI.01 KYC age>0 passes for our 40-year-old → SEC_REG_BI doesn't fire.
    delta, _ = _eval(_proposal(action="BUY", trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0, age=40), prompt="p")
    assert delta.gate_decision == "AUTO_APPROVE"


# ── _kyc_passes branch coverage via OP_COVERAGE rules ──────────────────────

def _fire(monkeypatch, signal):
    """Helper: stub detect_signals to return a single signal."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [signal])


def test_evaluate_kyc_bool_equals_pass(evalsetup, monkeypatch):
    """bool == True, kyc_verified=True → KYC passes → rule does NOT fire."""
    _fire(monkeypatch, "SIG_BOOL_EQ")
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0, kyc=True), prompt="p")
    failures = [d for d in delta.failed_details if d.clause_id == "OP.bool_eq"]
    assert failures == []


def test_evaluate_kyc_bool_equals_fail(evalsetup, monkeypatch):
    _fire(monkeypatch, "SIG_BOOL_EQ")
    # kyc=False → KYC bool == True fails AND static KYC check fires.  Either
    # way the rule fires.
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0, kyc=False), prompt="p")
    assert delta.gate_decision != "AUTO_APPROVE"


def test_evaluate_kyc_bool_not_equals(evalsetup, monkeypatch):
    """bool != False with kyc=True → True != False → True (KYC passes)."""
    _fire(monkeypatch, "SIG_BOOL_NEQ")
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0, kyc=True), prompt="p")
    failures = [d for d in delta.failed_details if d.clause_id == "OP.bool_neq"]
    assert failures == []


def test_evaluate_kyc_bool_threshold_with_gt_operator_falls_through(evalsetup, monkeypatch):
    """threshold='True' but operator is GREATER_THAN.

    The bool block is entered (threshold is a bool literal) but neither EQUALS
    nor NOT_EQUALS matches, so _kyc_passes falls through to the numeric path
    (float('True') raises) and then the string path (no > handler), landing on
    the conservative `return True`.  Regression guard for the bool-detection
    narrowing that removed '0'/'1' from the bool set."""
    _fire(monkeypatch, "SIG_BOOL_GT")
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0, kyc=True), prompt="p")
    assert not any(d.clause_id == "OP.bool_gt" for d in delta.failed_details)


def test_evaluate_kyc_num_less_than_pass(evalsetup, monkeypatch):
    """age < 30, client age=25 → KYC passes."""
    _fire(monkeypatch, "SIG_NUM_LT")
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0, age=25), prompt="p")
    assert not any(d.clause_id == "OP.num_lt" for d in delta.failed_details)


def test_evaluate_kyc_num_less_than_fail(evalsetup, monkeypatch):
    """age < 30, client age=40 → KYC fails → rule fires."""
    _fire(monkeypatch, "SIG_NUM_LT")
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0, age=40), prompt="p")
    assert delta.gate_decision != "AUTO_APPROVE"
    assert "OP_COVERAGE" in delta.failed_rules


def test_evaluate_kyc_num_equals(evalsetup, monkeypatch):
    """age == 40, client age=40 → KYC passes."""
    _fire(monkeypatch, "SIG_NUM_EQ")
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0, age=40), prompt="p")
    assert not any(d.clause_id == "OP.num_eq" for d in delta.failed_details)


def test_evaluate_kyc_num_not_equals(evalsetup, monkeypatch):
    """age != 40, client age=41 → KYC passes."""
    _fire(monkeypatch, "SIG_NUM_NEQ")
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0, age=41), prompt="p")
    assert not any(d.clause_id == "OP.num_neq" for d in delta.failed_details)


def test_evaluate_kyc_str_equals(evalsetup, monkeypatch):
    _fire(monkeypatch, "SIG_STR_EQ")
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0, archetype="WHALE"), prompt="p")
    assert not any(d.clause_id == "OP.str_eq" for d in delta.failed_details)


def test_evaluate_kyc_str_not_equals(evalsetup, monkeypatch):
    _fire(monkeypatch, "SIG_STR_NEQ")
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0, archetype="WHALE"), prompt="p")
    # archetype != NORMAL → "WHALE" != "NORMAL" → passes
    assert not any(d.clause_id == "OP.str_neq" for d in delta.failed_details)


def test_evaluate_kyc_str_not_contains_pass(evalsetup, monkeypatch):
    _fire(monkeypatch, "SIG_STR_NC")
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0,
                                   compliance_history="Clean record."),
                     prompt="p")
    assert not any(d.clause_id == "OP.str_nc" for d in delta.failed_details)


def test_evaluate_kyc_str_not_contains_fail(evalsetup, monkeypatch):
    _fire(monkeypatch, "SIG_STR_NC")
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0,
                                   compliance_history="prior violation"),
                     prompt="p")
    assert delta.gate_decision != "AUTO_APPROVE"


def test_evaluate_kyc_semantic_similar_high_sim_passes(evalsetup, monkeypatch):
    """SEMANTIC_SIMILAR delegates to signal_detector.get_embedding_similarity."""
    from app.auditor import signal_detector
    monkeypatch.setattr(signal_detector, "get_embedding_similarity",
                        lambda a, b: 0.9)
    _fire(monkeypatch, "SIG_SEM")
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0), prompt="p")
    assert not any(d.clause_id == "OP.sem" for d in delta.failed_details)


def test_evaluate_kyc_semantic_similar_low_sim_fails(evalsetup, monkeypatch):
    from app.auditor import signal_detector
    monkeypatch.setattr(signal_detector, "get_embedding_similarity",
                        lambda a, b: 0.3)
    _fire(monkeypatch, "SIG_SEM")
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0), prompt="p")
    assert delta.gate_decision != "AUTO_APPROVE"


def test_evaluate_kyc_numeric_value_with_not_contains_falls_to_string(evalsetup, monkeypatch):
    """age=40 with NOT_CONTAINS '42' → str(40) does NOT contain '42' → pass."""
    _fire(monkeypatch, "SIG_NUM_STR_NC")
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0, age=40), prompt="p")
    assert not any(d.clause_id == "OP.num_str_nc" for d in delta.failed_details)


def test_evaluate_kyc_misconfigured_op_returns_conservative_pass(
    evalsetup, monkeypatch
):
    """A misconfigured KYC (LESS_THAN on a string field) reaches the
    bottom-of-function `return True` catch-all and silently passes.

    Documented behavior: this is the conservative default — a malformed
    rule shouldn't escalate.  The fact that the test exists means we
    consciously chose 'pass' over 'fail-closed' for this edge."""
    _fire(monkeypatch, "SIG_MISCONFIG")
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0,
                                   risk_tolerance="Moderate"),
                     prompt="p")
    # KYC returns True → no failure from this clause
    assert not any(d.clause_id == "OP.misconfig_op" for d in delta.failed_details)


def test_evaluate_kyc_missing_field_direct_trigger_fails(evalsetup, monkeypatch):
    """Direct trigger (is_forced=False) + missing client field → KYC fails."""
    _fire(monkeypatch, "SIG_MISSING")
    delta, _ = _eval(_proposal(trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0), prompt="p")
    assert delta.gate_decision != "AUTO_APPROVE"


def test_evaluate_kyc_missing_field_when_forced_passes(evalsetup, monkeypatch):
    """When OP.missing_field is force-evaluated via cascade, missing client
    field returns True (conservative-pass for cascaded checks).

    We trip FINRA_2111 first; its adjacency cascades to SEC_REG_BI + FINRA_2090
    in the conftest seed.  To exercise the is_forced + missing-field path,
    extend the cascade.  Achieved here by extending FINRA_2111's adjacency to
    include OP_COVERAGE for this test only.
    """
    # Patch adjacency to cascade FINRA_2111 → OP_COVERAGE
    rule_engine._adjacent_risks_cache = {"FINRA_2111": ["OP_COVERAGE"]}
    monkeypatch.setattr(rule_engine, "detect_signals",
                        lambda p: ["HIGH_RISK_PRODUCT"])
    # OP_COVERAGE's SEMANTIC_SIMILAR clause will be force-evaluated too;
    # short-circuit the embedding call so we don't load the model.
    from app.auditor import signal_detector
    monkeypatch.setattr(signal_detector, "get_embedding_similarity",
                        lambda a, b: 1.0)
    # TQQQ: not in the SPY suppress list, so HIGH_RISK_PRODUCT survives the
    # per-ticker filter → FINRA_2111 fires → cascade to OP_COVERAGE → forced
    # eval of OP.missing_field's KYC (missing field) → `if is_forced: return True`.
    delta, _ = _eval(_proposal(asset_ticker="TQQQ", trade_size_usd=10_000.0),
                     _client_state(equity=100_000.0), prompt="leveraged")
    failures_op = [d for d in delta.failed_details if d.clause_id == "OP.missing_field"]
    assert failures_op == []


def test_evaluate_cascade_proposal_check_skipped_when_forced(evalsetup, monkeypatch):
    """Cascade target with proposal_check KYC: skipped via is_forced+proposal_check guard.

    FINRA_2111's adjacency includes FINRA_2090, which has a proposal_check KYC.
    When FINRA_2111 fires, FINRA_2090 is force-evaluated; its proposal_check
    KYC is skipped (only fires when triggered by real signals)."""
    monkeypatch.setattr(rule_engine, "detect_signals",
                        lambda p: ["HIGH_RISK_PRODUCT"])
    # TQQQ + held=0 → no concentration on top of FINRA_2111, so the failure
    # list reflects what the AST walk produced (not just STATIC_PORTFOLIO).
    delta, _ = _eval(_proposal(asset_ticker="TQQQ", trade_size_usd=10_000.0),
                     _client_state(equity=100_000.0), prompt="leveraged")
    assert "FINRA_2111" in delta.failed_rules
    assert "FINRA_2090" not in delta.failed_rules


# ── Absolute rule append (bypass_tsf branch in the AST walk) ────────────────

def test_evaluate_absolute_rule_fires(evalsetup, monkeypatch):
    """KYC_BYPASS triggers FINRA_2090 (bypass_tsf=True) — exercises the
    absolute-rule append in the AST walk."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["KYC_BYPASS"])
    delta, _ = _eval(_proposal(trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0), prompt="skip kyc")
    assert "FINRA_2090" in delta.failed_rules
    finra = [d for d in delta.failed_details if d.rule_id == "FINRA_2090"]
    assert finra[0].bypass_tsf is True
    assert finra[0].description.startswith("ABSOLUTE:")


# ── Graded-with-fallback vs Graded-without-fallback ────────────────────────

def test_evaluate_graded_rule_with_fallback(evalsetup, monkeypatch):
    """FINRA_2111 fires → graded failure with EVID_RISK_OVERRIDE_ACK fallback."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    # TQQQ isn't in the ticker_config suppress list → HIGH_RISK_PRODUCT survives
    delta, _ = _eval(_proposal(asset_ticker="TQQQ", trade_size_usd=80_000.0),
                     _client_state(equity=100_000.0), prompt="leveraged")
    finra = [d for d in delta.failed_details if d.rule_id == "FINRA_2111"]
    assert finra[0].missing_evidence_id == "EVID_RISK_OVERRIDE_ACK"
    assert finra[0].description.startswith("GRADED:")


def test_evaluate_graded_rule_without_fallback(evalsetup, monkeypatch):
    """IRS_WASH_SALE has a graded rule with no evidence_fallback."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["WASH_SALE"])
    delta, _ = _eval(_proposal(trade_size_usd=50_000.0),
                     _client_state(equity=100_000.0), prompt="tax loss")
    assert delta.gate_decision != "AUTO_APPROVE"
    wash = [d for d in delta.failed_details if d.rule_id == "IRS_WASH_SALE"]
    assert wash and wash[0].missing_evidence_id is None


# ── Evidence scoring (covers _score_evidence_coverage + _scrap_is_grounded) ─

def test_evaluate_evidence_ack_fast_path_cures(evalsetup, monkeypatch):
    """Affirmative scrap + is_ack=True → C_ev=1.0 → cures the rule."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    prompt = "buy TQQQ, yes I agree to the risk"
    prop = _proposal(
        trade_size_usd=50_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_RISK_OVERRIDE_ACK", value=True, scrap="yes I agree")],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0), prompt=prompt)
    # ACK cures FINRA_2111
    assert delta.gate_decision != "HUMAN_ESCALATION"


def test_evaluate_evidence_ack_without_keyword_falls_to_semantic(
    evalsetup, monkeypatch
):
    """ACK evidence whose scrap has no affirmative keyword → semantic path."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    monkeypatch.setattr(rule_engine, "get_embedding_similarity", lambda a, b: 0.42)
    prompt = "TQQQ trade with reasoning that is fine response from client"
    prop = _proposal(
        trade_size_usd=80_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_RISK_OVERRIDE_ACK", value=True,
            scrap="fine response")],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0), prompt=prompt)
    # C_ev = 0.42, partial cure but rule still fires somewhat
    assert "FINRA_2111" in delta.failed_rules or delta.gate_decision == "REFINEMENT"


def test_evaluate_evidence_empty_scrap_no_cure(evalsetup, monkeypatch):
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    prop = _proposal(
        trade_size_usd=80_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_RISK_OVERRIDE_ACK", value=True, scrap="")],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0), prompt="leveraged")
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_evidence_ungrounded_scrap_no_cure(evalsetup, monkeypatch):
    """Scrap not appearing in prompt → C_ev=0, no cure."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    prop = _proposal(
        trade_size_usd=80_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_RISK_OVERRIDE_ACK", value=True,
            scrap="totally fabricated quote")],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0),
                     prompt="leveraged speculative trade please")
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_evidence_substring_inside_word_not_grounded(evalsetup, monkeypatch):
    """The word-boundary fix in _scrap_is_grounded: 'user' must not match
    inside 'username'."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    prop = _proposal(
        trade_size_usd=80_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_RISK_OVERRIDE_ACK", value=True,
            # 'user' appears only inside 'username' in the prompt
            scrap="user")],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0),
                     prompt="my username is alice, also leveraged trade")
    # Grounding fails → no cure
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_evidence_with_punctuation_only_scrap_not_grounded(
    evalsetup, monkeypatch
):
    """Scrap with no word characters → _scrap_is_grounded short-circuits to False."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    prop = _proposal(
        trade_size_usd=80_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_RISK_OVERRIDE_ACK", value=True, scrap="!!!")],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0), prompt="leveraged !!!")
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_evidence_value_false_is_skipped(evalsetup, monkeypatch):
    """value=False means proposer didn't actually find this evidence."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    prop = _proposal(
        trade_size_usd=80_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_RISK_OVERRIDE_ACK", value=False, scrap="yes")],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0), prompt="leveraged yes")
    assert "EVID_RISK_OVERRIDE_ACK" in delta.missing_evidence_ids


def _flagged_state(equity=100_000.0):
    """Client with a prior violation — makes FINRA_2111 clause 2's
    NOT_CONTAINS-violation KYC fail when SPECULATIVE_PRODUCT fires."""
    return _client_state(equity=equity,
                         compliance_history="prior violation noted")


def test_evaluate_evidence_negative_semantic_score_clamps_to_zero(
    evalsetup, monkeypatch
):
    """Negative cosine sim is clamped to 0 in _score_evidence_coverage.

    Uses EVID_SPECULATIVE_WAIVER (non-ACK) so the test exercises the
    cite-your-source path: a non-ACK evidence with non-empty evidence_path
    is the only way to reach the semantic-similarity branch."""
    monkeypatch.setattr(rule_engine, "detect_signals",
                        lambda p: ["SPECULATIVE_PRODUCT"])
    monkeypatch.setattr(rule_engine, "get_embedding_similarity", lambda a, b: -0.5)
    prop = _proposal(
        trade_size_usd=10_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_SPECULATIVE_WAIVER", value=True,
            scrap="something unrelated",
            # Non-ACK semantic cures must carry a GraphRAG citation.
            evidence_path="GraphRAG ID: FINRA_2111")],
    )
    delta, _ = _eval(prop, _flagged_state(), prompt="leveraged something unrelated")
    # Clamped to 0 → no cure
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_evidence_non_ack_without_path_is_rejected(
    evalsetup, monkeypatch
):
    """Non-ACK evidence with empty evidence_path → C_ev=0 (cite-your-source rule)."""
    monkeypatch.setattr(rule_engine, "detect_signals",
                        lambda p: ["SPECULATIVE_PRODUCT"])
    monkeypatch.setattr(rule_engine, "get_embedding_similarity", lambda a, b: 1.0)
    prop = _proposal(
        trade_size_usd=10_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_SPECULATIVE_WAIVER", value=True,
            scrap="risk officer signed off on this speculative trade",
            evidence_path="")],  # empty path → reject
    )
    delta, _ = _eval(prop, _flagged_state(),
                     prompt="risk officer signed off on this speculative trade")
    # No cure despite perfect semantic similarity — path was missing
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_evidence_non_ack_with_path_cures_rule(evalsetup, monkeypatch):
    """Non-ACK evidence WITH a path + high semantic sim → rule is cured."""
    monkeypatch.setattr(rule_engine, "detect_signals",
                        lambda p: ["SPECULATIVE_PRODUCT"])
    monkeypatch.setattr(rule_engine, "get_embedding_similarity", lambda a, b: 0.95)
    prop = _proposal(
        trade_size_usd=1_000.0, asset_ticker="TQQQ",
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_SPECULATIVE_WAIVER", value=True,
            scrap="risk officer signed off on speculative trade",
            evidence_path="GraphRAG ID: FINRA_2111")],
    )
    delta, _ = _eval(prop, _flagged_state(),
                     prompt="risk officer signed off on speculative trade")
    assert delta.gate_decision != "HUMAN_ESCALATION"


def test_evaluate_evidence_no_description_in_ast_uses_static_fallback(
    evalsetup, monkeypatch
):
    """The conftest seed deliberately omits EVID_CONCENTRATION_ACK from the
    AST, so evaluate_proposal must fall back to the static failure's own
    description for C_ev matching."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    assets = [{"asset": "TQQQ", "value": 70_000.0}]
    prompt = "buy more TQQQ, yes I agree to concentration risk"
    prop = _proposal(
        action="BUY", asset_ticker="TQQQ", trade_size_usd=10_000.0,
        provided_evidence=[ProvidedEvidence(
            evidence_id="EVID_CONCENTRATION_ACK", value=True,
            scrap="yes I agree")],
    )
    delta, _ = _eval(prop, _client_state(equity=100_000.0, assets=assets),
                     prompt=prompt)
    # ACK keyword in scrap + is_ack=True from the static failure → C_ev=1.0
    assert delta.gate_decision != "HUMAN_ESCALATION"


# ── Ticker suppression branches ─────────────────────────────────────────────

def test_evaluate_spy_suppresses_high_risk_signal(evalsetup, monkeypatch):
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    delta, _ = _eval(
        _proposal(asset_ticker="SPY", trade_size_usd=1_000.0, instrument_type="EQUITY"),
        _client_state(equity=100_000.0), prompt="leveraged SPY"
    )
    assert "FINRA_2111" not in delta.failed_rules


def test_evaluate_spy_derivative_disables_suppression(evalsetup, monkeypatch):
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    delta, _ = _eval(
        _proposal(asset_ticker="SPY", trade_size_usd=10_000.0,
                  instrument_type="CALL_OPTION"),
        _client_state(equity=100_000.0), prompt="leveraged SPY"
    )
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_derivative_marker_in_prompt_disables_suppression(
    evalsetup, monkeypatch
):
    """Even with instrument_type=EQUITY, a derivative marker word disables suppression."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["HIGH_RISK_PRODUCT"])
    delta, _ = _eval(
        _proposal(asset_ticker="SPY", trade_size_usd=10_000.0, instrument_type="EQUITY"),
        _client_state(equity=100_000.0), prompt="leveraged SPY call option"
    )
    assert "FINRA_2111" in delta.failed_rules


def test_evaluate_signal_not_in_suppress_list_passes_through(evalsetup, monkeypatch):
    """A signal that isn't in SPY's suppress list doesn't get filtered."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: ["UNFAMILIAR_SIGNAL"])
    delta, _ = _eval(_proposal(asset_ticker="SPY", trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0), prompt="p")
    # Signal didn't match any AST trigger → no fire, but it wasn't filtered either
    assert delta.gate_decision == "AUTO_APPROVE"


# ── Trigger field=proposal.action with EQUALS on a non-static rule ─────────

def test_evaluate_proposal_action_equals_on_non_static_rule(evalsetup, monkeypatch):
    """OP.act_eq uses proposal.action==BUY; with age=40 the KYC passes so
    OP_COVERAGE doesn't fire, but the equality-match path executed."""
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    from app.auditor import signal_detector
    monkeypatch.setattr(signal_detector, "get_embedding_similarity", lambda a, b: 1.0)
    delta, _ = _eval(_proposal(action="BUY", trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0, age=40), prompt="p")
    # KYC age>0 passes → no OP.act_eq failure
    assert not any(d.clause_id == "OP.act_eq" for d in delta.failed_details)


# ── KYC portfolio_check on a non-static rule ───────────────────────────────

def test_evaluate_kyc_portfolio_check_returns_true(evalsetup, monkeypatch):
    """portfolio_check domain unconditionally passes (handled by static
    checks separately).  OP.portfolio_check is reachable only if SIG_PORTFOLIO
    fires."""
    _fire(monkeypatch, "SIG_PORTFOLIO")
    from app.auditor import signal_detector
    monkeypatch.setattr(signal_detector, "get_embedding_similarity", lambda a, b: 1.0)
    delta, _ = _eval(_proposal(asset_ticker="TQQQ", trade_size_usd=1_000.0),
                     _client_state(equity=100_000.0), prompt="p")
    assert not any(d.clause_id == "OP.portfolio_check" for d in delta.failed_details)


# ── Empty AST evidence descriptions are rejected at load time ──────────────

def test_seed_with_empty_evidence_description_raises_at_load(tmp_path, monkeypatch):
    """rule_db.load_all_regulations refuses an evidence_fallbacks row with
    empty description — silently auto-curing rules at runtime is worse than
    blowing up at load."""
    from app.database import rule_db, schema
    db_path = str(tmp_path / "bad.db")
    schema.init_schema(db_path)
    conn = schema.get_connection(db_path)
    conn.executescript("""
        INSERT INTO regulations VALUES ('R', 'r', 0, 'd');
        INSERT INTO rule_clauses VALUES ('C', 'R', 'd');
        INSERT INTO trigger_conditions VALUES ('T', 'C', 'prompt_signal', 'CONTAINS', 'X');
        INSERT INTO kyc_requirements VALUES ('K', 'T', 'f', '==', 't', 'profile');
        INSERT INTO evidence_fallbacks (evidence_id, kyc_id, description, is_ack)
            VALUES ('EV_EMPTY', 'K', '', 0);
    """)
    conn.commit()
    conn.close()
    with pytest.raises(ValueError, match="empty description"):
        rule_db.load_all_regulations(db_path)


# ── REVIEW action skips portfolio risk ──────────────────────────────────────

def test_evaluate_review_action_no_concentration_risk(evalsetup, monkeypatch):
    monkeypatch.setattr(rule_engine, "detect_signals", lambda p: [])
    delta, _ = _eval(_proposal(action="REVIEW", trade_size_usd=0.0),
                     _client_state(equity=100_000.0), prompt="what's my balance")
    assert "STATIC_PORTFOLIO" not in delta.failed_rules