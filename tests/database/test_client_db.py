"""Tests for app/database/client_db.py — public surface only.

Public API: get_client_data, get_client_state.

The private _load_vault (file read + cache) and _empty_state (empty-state
factory) are exercised through get_client_data / get_client_state.
"""

import json

import pytest

from app.database import client_db
from app.database.client_db import (
    get_client_data,
    get_client_state,
)


def test_get_client_data_loads_vault(vault_path):
    data = get_client_data("C1")
    assert data is not None
    assert data["client_id"] == "C1"


def test_get_client_data_unknown_returns_none(vault_path):
    assert get_client_data("ZZZ") is None


def test_get_client_data_caches_after_first_load(vault_path, monkeypatch):
    get_client_data("C1")
    # Replace the file with garbage — cache should hold
    vault_path.write_text("not valid json")
    assert get_client_data("C1") is not None


def test_get_client_state_returns_normalized_shape(vault_path):
    state = get_client_state("C1")
    assert set(state.keys()) == {"profile", "holdings", "account_state"}
    assert state["profile"]["age"] == 40
    assert state["account_state"]["kyc_verified"] is True
    assert len(state["holdings"]["assets"]) == 2


def test_get_client_state_unknown_client_returns_empty(vault_path):
    state = get_client_state("DOES_NOT_EXIST")
    assert state["holdings"]["assets"] == []
    assert state["account_state"]["total_equity_usd"] == 0.0


def test_get_client_state_explicit_client_data_bypasses_vault(vault_path):
    """Passing client_data directly skips the vault lookup."""
    explicit = {
        "age": 99,
        "risk_tolerance": "Aggressive",
        "compliance_history": "Has violations",
        "archetype": "WHALE",
        "holdings": [{"asset": "SPY", "value": 1.0}],
        "account_state": {
            "kyc_verified": False,
            "aml_ofac_cleared": False,
            "total_equity_usd": 1.0,
        },
    }
    state = get_client_state("anyid", client_data=explicit)
    assert state["profile"]["age"] == 99
    assert state["profile"]["risk_tolerance"] == "Aggressive"
    assert state["account_state"]["kyc_verified"] is False


def test_get_client_state_derives_total_equity_from_holdings_when_missing(vault_path):
    state = get_client_state("custom", client_data={
        "holdings": [{"asset": "X", "value": 100.0}, {"asset": "Y", "value": 50.0}],
        "account_state": {},
    })
    # account_state.total_equity_usd not provided → derived from holdings sum
    assert state["account_state"]["total_equity_usd"] == 150.0
    assert state["account_state"]["total_portfolio_value"] == 150.0


def test_get_client_state_uses_account_total_when_provided(vault_path):
    state = get_client_state("custom", client_data={
        "holdings": [{"asset": "X", "value": 10.0}],
        "account_state": {"total_equity_usd": 999.0},
    })
    assert state["account_state"]["total_equity_usd"] == 999.0


def test_get_client_state_empty_holdings_falls_back_to_account(vault_path):
    state = get_client_state("c", client_data={
        "holdings": [],
        "account_state": {"total_equity_usd": 1234.0},
    })
    # total_portfolio_value falls back to total_account_value when holdings sum is 0
    assert state["account_state"]["total_portfolio_value"] == 1234.0


