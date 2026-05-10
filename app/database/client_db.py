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
    
    Returns:
        {
            "profile": {
                "age": int,
                "risk_tolerance": str,
                "compliance_history": str,
                "archetype": str,
            },
            "holdings": {
                "assets": [...],
                "has_restricted_holdings": bool,
                "has_recent_loss_sale": bool,
                "restricted_tickers": [...],
                "loss_sale_tickers": [...],
            },
            "account_state": {
                "kyc_verified": bool,
                "aml_ofac_cleared": bool,
                "total_equity_usd": float,
            },
            "relational": {
                "connections": [...],
                "has_insider_connection": bool,
                "connected_tickers": [...],
            }
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

    # Holdings analysis
    holdings = client_data.get("holdings", [])
    has_restricted = any(
        "Restricted" in h.get("asset", "") or h.get("tax_lot_status") == "Restricted Lock-up"
        for h in holdings
    )
    has_recent_loss = any(
        h.get("tax_lot_status") == "Recently Sold at Loss" for h in holdings
    )
    restricted_tickers = [
        h["asset"].replace("Restricted Ticker: ", "").replace("Ticker: ", "")
        for h in holdings
        if "Restricted" in h.get("asset", "") or h.get("tax_lot_status") == "Restricted Lock-up"
    ]
    loss_sale_tickers = [
        h["asset"].replace("Ticker: ", "")
        for h in holdings
        if h.get("tax_lot_status") == "Recently Sold at Loss"
    ]

    holdings_state = {
        "assets": holdings,
        "has_restricted_holdings": has_restricted,
        "has_recent_loss_sale": has_recent_loss,
        "restricted_tickers": restricted_tickers,
        "loss_sale_tickers": loss_sale_tickers,
    }

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

    # Relational map
    relations = client_data.get("relational_map", [])
    connected_tickers = []
    has_insider = False
    for rel in relations:
        if "Ticker:" in rel:
            ticker = rel.split("Ticker: ")[-1].strip()
            connected_tickers.append(ticker)
            has_insider = True

    relational = {
        "connections": relations,
        "has_insider_connection": has_insider,
        "connected_tickers": connected_tickers,
    }

    return {
        "profile": profile,
        "holdings": holdings_state,
        "account_state": account_state,
        "relational": relational,
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
        "holdings": {
            "assets": [],
            "has_restricted_holdings": False,
            "has_recent_loss_sale": False,
            "restricted_tickers": [],
            "loss_sale_tickers": [],
        },
        "account_state": {
            "kyc_verified": True,
            "aml_ofac_cleared": True,
            "total_equity_usd": 0.0,
            "total_portfolio_value": 0.0,
        },
        "relational": {
            "connections": [],
            "has_insider_connection": False,
            "connected_tickers": [],
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
