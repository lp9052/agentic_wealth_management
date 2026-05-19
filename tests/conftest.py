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
    from app.database import client_db, rule_db, schema

    db_path = str(tmp_path / "rules.db")
    _seed_minimal_db(db_path)
    monkeypatch.setattr(schema, "DB_PATH", db_path)
    monkeypatch.setattr(client_db, "DB_PATH", db_path)
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
