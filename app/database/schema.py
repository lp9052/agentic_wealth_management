"""
SQLite database schema and initialization for the SBC framework.

Two logical databases stored in a single SQLite file:
1. Regulatory Rule DB - AST-style rule definitions
2. Client State DB - Enhanced client profiles for deterministic checks

This module handles schema creation only. Seeding is in scripts/seed_rule_db.py.
"""

import sqlite3
import os
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "rules.db")
DB_PATH = os.path.normpath(DB_PATH)


def get_connection(db_path: str = DB_PATH) -> sqlite3.Connection:
    """Get a SQLite connection with row factory enabled."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(db_path: str = DB_PATH) -> None:
    """Create all tables if they don't exist."""
    conn = get_connection(db_path)
    cursor = conn.cursor()

    # -----------------------------------------------------------------------
    # Regulatory Rule Tables (AST)
    # -----------------------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS regulations (
            rule_id TEXT PRIMARY KEY,
            rule_name TEXT NOT NULL,
            severity TEXT NOT NULL CHECK(severity IN ('CRITICAL', 'RECOVERABLE')),
            description TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS rule_clauses (
            clause_id TEXT PRIMARY KEY,
            rule_id TEXT NOT NULL REFERENCES regulations(rule_id),
            description TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS trigger_conditions (
            condition_id TEXT PRIMARY KEY,
            clause_id TEXT NOT NULL REFERENCES rule_clauses(clause_id),
            trigger_field TEXT NOT NULL,
            trigger_operator TEXT NOT NULL,
            trigger_value TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS kyc_requirements (
            kyc_id TEXT PRIMARY KEY,
            condition_id TEXT NOT NULL REFERENCES trigger_conditions(condition_id),
            client_field TEXT NOT NULL,
            operator TEXT NOT NULL,
            threshold TEXT NOT NULL,
            domain TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS evidence_fallbacks (
            evidence_id TEXT PRIMARY KEY,
            kyc_id TEXT NOT NULL REFERENCES kyc_requirements(kyc_id),
            description TEXT
        )
    """)

    # -----------------------------------------------------------------------
    # Client State Tables
    # -----------------------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            client_id TEXT PRIMARY KEY,
            archetype TEXT,
            age INTEGER,
            risk_tolerance TEXT,
            intent_memos TEXT,
            compliance_history TEXT,
            kyc_verified INTEGER DEFAULT 1,
            aml_ofac_cleared INTEGER DEFAULT 1,
            total_equity_usd REAL DEFAULT 0.0
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_holdings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL REFERENCES clients(client_id),
            asset TEXT NOT NULL,
            value REAL,
            tax_lot_status TEXT,
            acquisition_date TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_id TEXT NOT NULL REFERENCES clients(client_id),
            relation TEXT NOT NULL
        )
    """)

    # -----------------------------------------------------------------------
    # Audit Trail
    # -----------------------------------------------------------------------

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_trail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT DEFAULT (datetime('now')),
            client_id TEXT,
            proposal_json TEXT,
            delta_json TEXT,
            final_status TEXT,
            iterations INTEGER
        )
    """)

    conn.commit()
    conn.close()
    logger.info("Database schema initialized at %s", db_path)


if __name__ == "__main__":
    init_schema()
    print(f"Schema created at {DB_PATH}")
