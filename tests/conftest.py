"""
Shared pytest fixtures for the SBC test suite.

The fixtures here exist to break the project's hard runtime dependencies on
deployed config files, the seeded SQLite DB, the LLM, and the embedding models
so individual unit tests can run in isolation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Cache reset — many modules cache state at module level.  Clear them at the
# start of each test so the previous test's writes don't bleed in.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_module_caches() -> Iterator[None]:
    """Clear module-level caches at the start of each test.

    Imports are intentionally local so test modules that don't transitively
    depend on the proposer (e.g. tests/proposer/test_prompts.py, which only
    imports a pure-template module) don't have to pay for pulling in
    langchain_google_genai's heavy C-extension chain.
    """
    import sys

    if "app.auditor.rule_engine" in sys.modules:
        sys.modules["app.auditor.rule_engine"]._ticker_config_cache = None
        sys.modules["app.auditor.rule_engine"]._adjacent_risks_cache = None
    if "app.auditor.rule_registry" in sys.modules:
        sys.modules["app.auditor.rule_registry"]._regulations = None
        sys.modules["app.auditor.rule_registry"]._rule_index = None
    if "app.auditor.signal_detector" in sys.modules:
        sd = sys.modules["app.auditor.signal_detector"]
        sd._model1 = None
        sd._model2 = None
        sd._anchor_embeddings = None
        sd._signal_thresholds = None
        sd._normal_embeddings = None
    if "app.auditor.typo_filter" in sys.modules:
        sys.modules["app.auditor.typo_filter"]._symspell = None
    if "app.database.client_db" in sys.modules:
        sys.modules["app.database.client_db"]._client_cache = {}
    if "app.proposer.agent" in sys.modules:
        sys.modules["app.proposer.agent"]._regulations_cache = None
    yield


# ---------------------------------------------------------------------------
# Minimal fixture data used by the rule-engine / risk-scoring / rule-DB tests.
# Each test gets a fresh tmp_path so test isolation is automatic.
# ---------------------------------------------------------------------------

MINIMAL_REGULATIONS = [
    {
        "id": "FINRA_2111",
        # FINRA_2090 in the adjacency is intentional: FINRA_2090's KYC is
        # proposal_check (always fails when triggered), so when 2111 fires
        # and cascades, the forced FINRA_2090 walk exercises the
        # `is_forced and kyc.domain == "proposal_check"` skip branch.
        "metadata": {"rule": "Suitability", "related": ["SEC_REG_BI", "FINRA_2090"], "tags": ["suitability"]},
        "text": "Suitability rule — requires reasonable basis.",
    },
    {
        "id": "SEC_REG_BI",
        "metadata": {"rule": "Reg BI", "related": [], "tags": ["disclosure"]},
        "text": "Regulation Best Interest — disclosure duty.",
    },
    {
        "id": "FINRA_2090",
        "metadata": {"rule": "KYC", "related": [], "tags": ["kyc"]},
        "text": "Know Your Customer — verification duty.",
    },
]

MINIMAL_TICKER_CONFIG = {
    "tickers": {
        "SPY": {"suppress_signals": ["HIGH_RISK_PRODUCT", "SPECULATIVE_PRODUCT"]},
        "TQQQ": {"suppress_signals": []},
    },
    "derivative_markers": ["option", "call", "put", "strike", "0dte"],
}

MINIMAL_ANCHORS = {
    "_meta": {"description": "tiny test anchors"},
    "FINRA_2111": {
        "signals": {
            "HIGH_RISK_PRODUCT": {"anchors": ["high risk leveraged product"]},
            "SPECULATIVE_PRODUCT": {"anchors": ["speculative bet"]},
        },
    },
    "FINRA_2090": {
        "signals": {
            "KYC_BYPASS": {"anchors": ["skip kyc verification"]},
        },
    },
}

MINIMAL_NORMAL_CORPUS = {
    "_meta": {"description": "tiny test corpus"},
    "normal_corpus": [
        "I want to buy 100 shares of SPY",
        "Please rebalance my retirement account",
        "How is my portfolio performing this quarter",
    ],
}

MINIMAL_VAULT = [
    {
        "client_id": "C1",
        "archetype": "NORMAL",
        "age": 40,
        "risk_tolerance": "Moderate",
        "intent_memos": "memo",
        "relational_map": ["spouse"],
        "compliance_history": "Clean record.",
        "holdings": [
            {"asset": "SPY", "value": 50000.0, "tax_lot_status": "long", "acquisition_date": "2020-01-01"},
            {"asset": "BND", "value": 50000.0, "tax_lot_status": "long", "acquisition_date": "2020-01-01"},
        ],
        "account_state": {
            "kyc_verified": True,
            "aml_ofac_cleared": True,
            "total_equity_usd": 100000.0,
        },
    },
]


@pytest.fixture
def regulations_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a minimal regulations.json and point rule_engine + proposer at it."""
    from app.auditor import rule_engine
    from app.proposer import agent as proposer_agent

    p = tmp_path / "regulations.json"
    p.write_text(json.dumps(MINIMAL_REGULATIONS))
    monkeypatch.setattr(rule_engine, "_REGULATIONS_PATH", str(p))
    monkeypatch.setattr(proposer_agent, "_REG_PATH", str(p))
    return p


@pytest.fixture
def ticker_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a minimal ticker_config.json and point rule_engine at it."""
    from app.auditor import rule_engine

    p = tmp_path / "ticker_config.json"
    p.write_text(json.dumps(MINIMAL_TICKER_CONFIG))
    monkeypatch.setattr(rule_engine, "_TICKER_CONFIG_PATH", str(p))
    return p


@pytest.fixture
def anchors_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.auditor import signal_detector

    p = tmp_path / "signal_anchors.json"
    p.write_text(json.dumps(MINIMAL_ANCHORS))
    monkeypatch.setattr(signal_detector, "_ANCHORS_PATH", str(p))
    return p


@pytest.fixture
def normal_corpus_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from app.auditor import signal_detector

    p = tmp_path / "normal_corpus.json"
    p.write_text(json.dumps(MINIMAL_NORMAL_CORPUS))
    monkeypatch.setattr(signal_detector, "_NORMAL_PATH", str(p))
    return p


@pytest.fixture
def vault_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point client_db at a minimal vault.json."""
    from app.database import client_db

    p = tmp_path / "vault.json"
    p.write_text(json.dumps(MINIMAL_VAULT))
    monkeypatch.setattr(client_db, "VAULT_PATH", str(p))
    return p


# ---------------------------------------------------------------------------
# SQLite DB fixtures.  Each test gets a fresh tmp DB so concurrent runs and
# repeat-runs of the same test don't see stale state.
# ---------------------------------------------------------------------------

def _seed_minimal_db(db_path: str) -> None:
    """Insert just enough rule tree to exercise rule_engine in isolation."""
    from app.database.schema import init_schema, get_connection

    init_schema(db_path)
    conn = get_connection(db_path)
    c = conn.cursor()
    for t in ("evidence_fallbacks", "kyc_requirements", "trigger_conditions",
              "rule_clauses", "regulations"):
        c.execute(f"DELETE FROM {t}")

    # FINRA_2111 — graded, suitability check, ACK evidence
    c.execute("INSERT INTO regulations VALUES (?, ?, ?, ?)",
              ("FINRA_2111", "Suitability", 0, "Suitability check"))
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("2111.01", "FINRA_2111", "High-risk product"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("2111.01.T1", "2111.01", "prompt_signal", "CONTAINS", "HIGH_RISK_PRODUCT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("2111.01.K1", "2111.01.T1", "risk_tolerance", "==", "Aggressive", "profile"))
    c.execute("INSERT INTO evidence_fallbacks (evidence_id, kyc_id, description, is_ack) "
              "VALUES (?, ?, ?, ?)",
              ("EVID_RISK_OVERRIDE_ACK", "2111.01.K1",
               "Client acknowledges high risk in writing.", 1))

    # FINRA_2111 clause 2 — non-ACK evidence (risk officer sign-off).  The
    # semantic-similarity path in _score_evidence_coverage is reachable only
    # through a non-ACK evidence + non-empty evidence_path; this clause is
    # where tests exercise that.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("2111.02", "FINRA_2111", "Speculative product trading"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("2111.02.T1", "2111.02", "prompt_signal", "CONTAINS", "SPECULATIVE_PRODUCT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("2111.02.K1", "2111.02.T1", "compliance_history",
               "NOT_CONTAINS", "violation", "profile"))
    c.execute("INSERT INTO evidence_fallbacks (evidence_id, kyc_id, description, is_ack) "
              "VALUES (?, ?, ?, ?)",
              ("EVID_SPECULATIVE_WAIVER", "2111.02.K1",
               "Risk officer sign-off documenting speculative trading approval.", 0))

    # FINRA_2090 — absolute, KYC bypass, proposal_check (always fails when triggered)
    c.execute("INSERT INTO regulations VALUES (?, ?, ?, ?)",
              ("FINRA_2090", "KYC", 1, "KYC duty"))
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("2090.01", "FINRA_2090", "KYC bypass attempt"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("2090.01.T1", "2090.01", "prompt_signal", "CONTAINS", "KYC_BYPASS"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("2090.01.K1", "2090.01.T1", "intent_violation", "==", "1", "proposal_check"))

    # SEC_REG_BI — graded, action=IN list, with a fallback evidence item
    c.execute("INSERT INTO regulations VALUES (?, ?, ?, ?)",
              ("SEC_REG_BI", "Reg BI", 0, "Disclosure duty"))
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("REGBI.01", "SEC_REG_BI", "Disclosure required on buy/sell"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("REGBI.01.T1", "REGBI.01", "proposal.action", "IN", "[BUY,SELL]"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("REGBI.01.K1", "REGBI.01.T1", "age", ">", "0", "profile"))
    c.execute("INSERT INTO evidence_fallbacks (evidence_id, kyc_id, description, is_ack) "
              "VALUES (?, ?, ?, ?)",
              ("EVID_DISCLOSURE", "REGBI.01.K1",
               "Client received Form CRS disclosure", 0))

    # IRS_WASH_SALE — graded, no evidence fallback on the KYC (covers the
    # "GRADED without fallback" branch in evaluate_proposal).  Also has an
    # empty adjacency list so the "no cascade candidates" branch fires.
    c.execute("INSERT INTO regulations VALUES (?, ?, ?, ?)",
              ("IRS_WASH_SALE", "Wash Sale", 0, "Wash sale rule"))
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("WASH.01", "IRS_WASH_SALE", "Wash sale trigger"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("WASH.01.T1", "WASH.01", "prompt_signal", "CONTAINS", "WASH_SALE"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("WASH.01.K1", "WASH.01.T1", "missing_field", "==", "True", "profile"))

    # OP_COVERAGE — synthetic rule used to exercise every KYCOperator branch
    # and every _trigger_matches branch through evaluate_proposal.  Each
    # clause isolates one operator path; tests fire individual signals to
    # activate the clause they care about without touching the others.
    c.execute("INSERT INTO regulations VALUES (?, ?, ?, ?)",
              ("OP_COVERAGE", "Operator Coverage", 0, "Branch coverage rule"))

    # K-bool-EQ:  threshold=True, op===   profile.kyc_verified
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.bool_eq", "OP_COVERAGE", "bool EQUALS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.bool_eq.T", "OP.bool_eq", "prompt_signal", "CONTAINS", "SIG_BOOL_EQ"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.bool_eq.K", "OP.bool_eq.T", "kyc_verified", "==", "True", "profile"))

    # K-bool-NEQ:  threshold=False, op=!=
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.bool_neq", "OP_COVERAGE", "bool NOT_EQUALS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.bool_neq.T", "OP.bool_neq", "prompt_signal", "CONTAINS", "SIG_BOOL_NEQ"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.bool_neq.K", "OP.bool_neq.T", "kyc_verified", "!=", "False", "profile"))

    # K-num-LT
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.num_lt", "OP_COVERAGE", "num LESS_THAN"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.num_lt.T", "OP.num_lt", "prompt_signal", "CONTAINS", "SIG_NUM_LT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.num_lt.K", "OP.num_lt.T", "age", "<", "30", "profile"))

    # K-num-EQ
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.num_eq", "OP_COVERAGE", "num EQUALS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.num_eq.T", "OP.num_eq", "prompt_signal", "CONTAINS", "SIG_NUM_EQ"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.num_eq.K", "OP.num_eq.T", "age", "==", "40", "profile"))

    # K-num-NEQ
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.num_neq", "OP_COVERAGE", "num NOT_EQUALS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.num_neq.T", "OP.num_neq", "prompt_signal", "CONTAINS", "SIG_NUM_NEQ"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.num_neq.K", "OP.num_neq.T", "age", "!=", "40", "profile"))

    # K-str-EQ  (already covered by FINRA_2111 risk_tolerance==Aggressive,
    # but kept here for symmetry — different signal name keeps it isolated)
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.str_eq", "OP_COVERAGE", "str EQUALS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.str_eq.T", "OP.str_eq", "prompt_signal", "CONTAINS", "SIG_STR_EQ"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.str_eq.K", "OP.str_eq.T", "archetype", "==", "WHALE", "profile"))

    # K-str-NEQ
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.str_neq", "OP_COVERAGE", "str NOT_EQUALS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.str_neq.T", "OP.str_neq", "prompt_signal", "CONTAINS", "SIG_STR_NEQ"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.str_neq.K", "OP.str_neq.T", "archetype", "!=", "NORMAL", "profile"))

    # K-str-NOT_CONTAINS
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.str_nc", "OP_COVERAGE", "str NOT_CONTAINS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.str_nc.T", "OP.str_nc", "prompt_signal", "CONTAINS", "SIG_STR_NC"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.str_nc.K", "OP.str_nc.T", "compliance_history", "NOT_CONTAINS",
               "violation", "profile"))

    # K-SEMANTIC_SIMILAR  (signal_detector.get_embedding_similarity is the
    # similarity function; tests monkeypatch its score to a known value).
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.sem", "OP_COVERAGE", "SEMANTIC_SIMILAR"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.sem.T", "OP.sem", "prompt_signal", "CONTAINS", "SIG_SEM"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.sem.K", "OP.sem.T", "compliance_history",
               "SEMANTIC_SIMILAR", "Clean record.", "profile"))

    # K-numeric-with-NOT_CONTAINS  (falls through bool/numeric to the string
    # comparator at the bottom of _kyc_passes).  archetype is a string field
    # but threshold "0" parses as numeric — exercises the bool→numeric→string
    # fall-through.  Actually: threshold "0" matches the bool guard first.
    # Use NOT_CONTAINS with a numeric value + non-numeric threshold to force
    # the string fall-through.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.num_str_nc", "OP_COVERAGE", "numeric value NOT_CONTAINS string"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.num_str_nc.T", "OP.num_str_nc", "prompt_signal", "CONTAINS",
               "SIG_NUM_STR_NC"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.num_str_nc.K", "OP.num_str_nc.T", "age", "NOT_CONTAINS",
               "42", "profile"))

    # Trigger field outside the known three (prompt_signal / proposal.action /
    # proposal.asset_ticker) — exercises the bottom `return False` in
    # _trigger_matches via the "no outer branch matched" path.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.unknown_field", "OP_COVERAGE", "unknown trigger_field"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.unknown_field.T", "OP.unknown_field", "proposal.future_field",
               "==", "ANY"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.unknown_field.K", "OP.unknown_field.T", "age", ">", "0", "profile"))

    # prompt_signal × EXISTS — now a supported semantics ("any signal fired").
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.sig_exists", "OP_COVERAGE", "prompt_signal EXISTS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.sig_exists.T", "OP.sig_exists", "prompt_signal", "EXISTS", ""))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.sig_exists.K", "OP.sig_exists.T", "age", ">", "0", "profile"))

    # prompt_signal × IN — unsupported on prompt_signal; exercises the
    # "neither CONTAINS/EQUALS nor EXISTS" fallthrough → return False.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.sig_in", "OP_COVERAGE", "prompt_signal IN (unsupported)"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.sig_in.T", "OP.sig_in", "prompt_signal", "IN", "[A,B]"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.sig_in.K", "OP.sig_in.T", "age", ">", "0", "profile"))

    # proposal.action × CONTAINS — unsupported on action; exercises
    # action-side fallthrough.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.act_contains", "OP_COVERAGE", "proposal.action CONTAINS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.act_contains.T", "OP.act_contains", "proposal.action",
               "CONTAINS", "BUY"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.act_contains.K", "OP.act_contains.T", "age", ">", "0", "profile"))

    # proposal.action × EXISTS — supported.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.act_exists", "OP_COVERAGE", "proposal.action EXISTS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.act_exists.T", "OP.act_exists", "proposal.action", "EXISTS", ""))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.act_exists.K", "OP.act_exists.T", "age", ">", "0", "profile"))

    # proposal.asset_ticker × EQUALS — unsupported (only EXISTS handled there).
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.ticker_eq", "OP_COVERAGE", "proposal.asset_ticker EQUALS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.ticker_eq.T", "OP.ticker_eq", "proposal.asset_ticker",
               "==", "ANY"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.ticker_eq.K", "OP.ticker_eq.T", "age", ">", "0", "profile"))

    # proposal.asset_ticker × EXISTS — supported.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.ticker_exists", "OP_COVERAGE", "proposal.asset_ticker EXISTS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.ticker_exists.T", "OP.ticker_exists", "proposal.asset_ticker",
               "EXISTS", ""))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.ticker_exists.K", "OP.ticker_exists.T", "age", ">", "0", "profile"))

    # Misconfigured KYC: LESS_THAN on a string field.  Exercises the
    # bottom-of-function `return True` (conservative pass) in _kyc_passes
    # when no op+value-type handler matches.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.misconfig_op", "OP_COVERAGE", "LESS_THAN on string field"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.misconfig_op.T", "OP.misconfig_op", "prompt_signal",
               "CONTAINS", "SIG_MISCONFIG"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.misconfig_op.K", "OP.misconfig_op.T", "risk_tolerance",
               "<", "Aggressive", "profile"))

    # Missing-field KYC, signal-triggered.  Used by tests to exercise both
    # `is_forced=True → return True` (when cascaded) and the default `return
    # False` (when triggered directly).
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.missing_field", "OP_COVERAGE", "missing client field"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.missing_field.T", "OP.missing_field", "prompt_signal",
               "CONTAINS", "SIG_MISSING"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.missing_field.K", "OP.missing_field.T", "nonexistent_field",
               "==", "anything", "profile"))

    # Trigger field=proposal.action with EQUALS operator on a non-static rule.
    # STATIC.05 uses this combination but the static block is skipped in the
    # AST walk, so we need a non-static rule to reach line 365.
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.act_eq", "OP_COVERAGE", "proposal.action EQUALS"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.act_eq.T", "OP.act_eq", "proposal.action", "==", "BUY"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.act_eq.K", "OP.act_eq.T", "age", ">", "0", "profile"))

    # KYC with portfolio_check domain on a non-static rule (the static rule
    # has one too but is skipped in the AST walk via `if is_static: continue`).
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("OP.portfolio_check", "OP_COVERAGE", "portfolio_check KYC"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("OP.portfolio_check.T", "OP.portfolio_check", "prompt_signal",
               "CONTAINS", "SIG_PORTFOLIO"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("OP.portfolio_check.K", "OP.portfolio_check.T", "any", "==",
               "any", "portfolio_check"))


    # STATIC_PORTFOLIO — required so the engine can harvest the
    # EVID_CONCENTRATION_ACK description from the AST.
    c.execute("INSERT INTO regulations VALUES (?, ?, ?, ?)",
              ("STATIC_PORTFOLIO", "Static portfolio", 1, "Static rules"))
    c.execute("INSERT INTO rule_clauses VALUES (?, ?, ?)",
              ("STATIC.05", "STATIC_PORTFOLIO", "Concentration"))
    c.execute("INSERT INTO trigger_conditions VALUES (?, ?, ?, ?, ?)",
              ("STATIC.05.T1", "STATIC.05", "proposal.action", "==", "BUY"))
    c.execute("INSERT INTO kyc_requirements VALUES (?, ?, ?, ?, ?, ?)",
              ("STATIC.05.K1", "STATIC.05.T1", "concentration", ">", "0.5", "portfolio_check"))
    # Note: we intentionally don't seed EVID_CONCENTRATION_ACK into the AST.
    # Run_static_checks references that evidence_id on the concentration
    # failure, so the absence forces evaluate_proposal to use the
    # static-failure-description fallback (lines 608-611).  The dynamic-weight
    # ACK fast-path test still passes because the static failure carries
    # is_ack=True on its own FailedRuleDetail.

    conn.commit()
    conn.close()


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """A fully-seeded minimal rules DB, wired into every consumer.

    ``load_all_regulations``'s ``db_path`` default is captured at import time,
    so monkeypatching ``rule_db.DB_PATH`` alone leaves the function pointed at
    the production DB.  We wrap the loader to force-use the test DB.
    """
    from app.auditor import rule_registry
    from app.database import rule_db, schema

    db_path = str(tmp_path / "rules.db")
    _seed_minimal_db(db_path)
    monkeypatch.setattr(schema, "DB_PATH", db_path)
    monkeypatch.setattr(rule_db, "DB_PATH", db_path)

    real_loader = rule_db.load_all_regulations
    monkeypatch.setattr(
        rule_registry, "load_all_regulations",
        lambda *_a, **_kw: real_loader(db_path),
    )

    # Force the rule registry to load the new DB the next time it's asked.
    rule_registry._regulations = None
    rule_registry._rule_index = None
    return db_path


# ---------------------------------------------------------------------------
# Embedding model mocks.  Deterministic, dependency-free.
# ---------------------------------------------------------------------------

class FakeEncoder:
    """
    Deterministic encoder that returns a one-hot embedding per text.

    Routes each text into one of four orthogonal classes so cosine similarity
    is either 1.0 (same class) or 0.0 (different class).  The classes are:

      class 0 (HIGH-RISK / LEVERAGED / SPECULATIVE) — fires the FINRA_2111 anchors
      class 1 (KYC / VERIFICATION)                 — fires FINRA_2090 anchors
      class 2 (NORMAL_CORPUS_TOKENS)               — matches the seeded normal corpus
      class 3 (NOVEL)                              — neither anchors nor corpus
    """

    _HIGH_RISK = ("high risk", "leveraged", "speculative")
    _KYC = ("kyc", "verification")
    _NORMAL = ("buy", "rebalance", "retirement", "portfolio", "spy", "shares")

    def encode(self, texts):
        if isinstance(texts, str):
            texts = [texts]
        vecs = []
        for t in texts:
            v = np.zeros(4, dtype=np.float64)
            lo = t.lower()
            if any(k in lo for k in self._HIGH_RISK):
                v[0] = 1.0
            elif any(k in lo for k in self._KYC):
                v[1] = 1.0
            elif any(k in lo for k in self._NORMAL):
                v[2] = 1.0
            else:
                v[3] = 1.0
            vecs.append(v)
        return np.array(vecs, dtype=np.float64)


@pytest.fixture
def fake_encoders(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeEncoder, FakeEncoder]:
    """Replace `_get_models` with two deterministic FakeEncoders."""
    from app.auditor import signal_detector

    m1 = FakeEncoder()
    m2 = FakeEncoder()

    def _fake_get_models():
        signal_detector._model1 = m1
        signal_detector._model2 = m2
        return m1, m2

    monkeypatch.setattr(signal_detector, "_get_models", _fake_get_models)
    return m1, m2


# ---------------------------------------------------------------------------
# Quiet a noisy import path.  `sys` is imported above so callers can opt to
# clear cached app.* modules manually for cross-test isolation if they need
# to override module-level constants captured at import time.
# ---------------------------------------------------------------------------

@pytest.fixture
def reload_app_modules(monkeypatch: pytest.MonkeyPatch):
    """Yield a helper that clears a named app.* module from sys.modules."""

    def _clear(name: str) -> None:
        sys.modules.pop(name, None)

    return _clear
