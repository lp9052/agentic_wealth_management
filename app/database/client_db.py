"""
Client State Database access layer.

Handles loading client data from vault.json and/or SQLite,
and provides normalized client state for the auditor.
"""

import json
import os
import logging
from typing import Optional

from app.database.schema import get_connection, DB_PATH

logger = logging.getLogger(__name__)

VAULT_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "vault.json")
VAULT_PATH = os.path.normpath(VAULT_PATH)

# In-memory cache of client data (loaded from vault.json)
_client_cache: dict[str, dict] = {}


def _load_vault() -> None:
    """Load all clients from vault.json into memory."""
    global _client_cache
    if _client_cache:
        return
    with open(VAULT_PATH, "r") as f:
        clients = json.load(f)
    _client_cache = {c["client_id"]: c for c in clients}
    logger.info("Loaded %d clients from vault.json", len(_client_cache))


def get_client_data(client_id: str) -> Optional[dict]:
    """Get raw client data from the vault."""
    _load_vault()
    return _client_cache.get(client_id)


def get_client_state(client_id: str, client_data: Optional[dict] = None) -> dict:
    """
    Build a normalized client state dictionary for the auditor.

    This transforms raw vault data into the structured format the
    rule engine needs for KYC checks.

    Only the keys actually consumed by run_static_checks() and _kyc_passes()
    are produced — every other client_field is sourced from rules.db
    KYC requirements (currently: profile.*, account_state.*, portfolio_check.*,
    proposal_check.*).

    Returns:
        {
            "profile": {
                "age": int,
                "risk_tolerance": str,
                "compliance_history": str,
                "archetype": str,
            },
            "holdings": {
                "assets": [...],  # raw vault holdings list
            },
            "account_state": {
                "kyc_verified": bool,
                "aml_ofac_cleared": bool,
                "total_equity_usd": float,
                "total_portfolio_value": float,
            },
        }
    """
    if client_data is None:
        client_data = get_client_data(client_id)
    if client_data is None:
        logger.warning("Client %s not found, returning empty state", client_id)
        return _empty_state()

    # Profile
    profile = {
        "age": client_data.get("age", 40),
        "risk_tolerance": client_data.get("risk_tolerance", "Moderate"),
        "compliance_history": client_data.get("compliance_history", "Clean record."),
        "archetype": client_data.get("archetype", "NORMAL"),
    }

    # Holdings — only the raw asset list is consumed downstream
    # (rule_engine.run_static_checks iterates assets[] for concentration and
    # holdings checks).  No KYC requirement keys on derived holding flags, so
    # we don't compute them.
    holdings = client_data.get("holdings", [])
    holdings_state = {"assets": holdings}

    # Account state
    acct = client_data.get("account_state", {})

    # Sum ALL holdings regardless of asset type (bonds, cash, equities, alternatives).
    # This is the correct denominator for concentration risk — the whole portfolio,
    # not just the equity sleeve.
    total_portfolio_value = sum(h.get("value", 0.0) for h in holdings)

    # Use the explicit total_equity_usd from the vault if provided; otherwise
    # derive it from holdings so we never understate the account value.
    total_account_value = float(
        acct.get("total_equity_usd") or total_portfolio_value
    )

    account_state = {
        "kyc_verified": acct.get("kyc_verified", True),
        "aml_ofac_cleared": acct.get("aml_ofac_cleared", True),
        # total_equity_usd kept for backward compat with risk_scoring TSF formula
        "total_equity_usd": total_account_value,
        # total_portfolio_value is explicit: sum of every holding line
        "total_portfolio_value": total_portfolio_value or total_account_value,
    }

    return {
        "profile": profile,
        "holdings": holdings_state,
        "account_state": account_state,
    }


def _empty_state() -> dict:
    """Return a safe empty client state."""
    return {
        "profile": {
            "age": 40,
            "risk_tolerance": "Moderate",
            "compliance_history": "Clean record.",
            "archetype": "NORMAL",
        },
        "holdings": {"assets": []},
        "account_state": {
            "kyc_verified": True,
            "aml_ofac_cleared": True,
            "total_equity_usd": 0.0,
            "total_portfolio_value": 0.0,
        },
    }


def sync_vault_to_db(db_path: str = DB_PATH) -> None:
    """
    Sync vault.json clients into the SQLite client tables.
    Used for audit trail queries and reporting.
    """
    _load_vault()
    conn = get_connection(db_path)
    c = conn.cursor()

    c.execute("DELETE FROM client_relations")
    c.execute("DELETE FROM client_holdings")
    c.execute("DELETE FROM clients")

    for cid, data in _client_cache.items():
        acct = data.get("account_state", {})
        c.execute(
            "INSERT INTO clients VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                data.get("archetype"),
                data.get("age", 40),
                data.get("risk_tolerance", "Moderate"),
                data.get("intent_memos"),
                data.get("compliance_history"),
                1 if acct.get("kyc_verified", True) else 0,
                1 if acct.get("aml_ofac_cleared", True) else 0,
                acct.get("total_equity_usd", 0.0),
            ),
        )
        for h in data.get("holdings", []):
            c.execute(
                "INSERT INTO client_holdings (client_id, asset, value, tax_lot_status, acquisition_date) "
                "VALUES (?, ?, ?, ?, ?)",
                (cid, h["asset"], h.get("value"), h.get("tax_lot_status"), h.get("acquisition_date")),
            )
        for rel in data.get("relational_map", []):
            c.execute(
                "INSERT INTO client_relations (client_id, relation) VALUES (?, ?)",
                (cid, rel),
            )

    conn.commit()
    conn.close()
    logger.info("Synced %d clients to SQLite", len(_client_cache))
