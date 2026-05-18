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

    # Idempotent migration: pre-existing rules.db files have a `severity`
    # column with a CHECK constraint.  Detect the old shape and drop the
    # whole rule tree in FK order — the seed re-inserts everything anyway,
    # and CHECK constraints can't be altered in place under SQLite.
    existing_tables = {row[0] for row in cursor.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    if "regulations" in existing_tables:
        reg_cols = {row[1] for row in cursor.execute("PRAGMA table_info(regulations)").fetchall()}
        if "severity" in reg_cols and "bypass_tsf" not in reg_cols:
            logger.info("regulations: migrating severity → bypass_tsf (drop + reseed required).")
            for t in ("evidence_fallbacks", "kyc_requirements", "trigger_conditions",
                     "rule_clauses", "regulations"):
                cursor.execute(f"DROP TABLE IF EXISTS {t}")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS regulations (
            rule_id TEXT PRIMARY KEY,
            rule_name TEXT NOT NULL,
            bypass_tsf INTEGER NOT NULL DEFAULT 0,
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
            description TEXT,
            is_ack INTEGER NOT NULL DEFAULT 0
        )
    """)

    # Idempotent migration: pre-existing DBs created before is_ack was added
    # get the column injected with the default 0.  The seed (or the
    # _ACK_EVIDENCE_IDS UPDATE below) then sets it to 1 where appropriate.
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(evidence_fallbacks)").fetchall()}
    if "is_ack" not in existing_cols:
        cursor.execute("ALTER TABLE evidence_fallbacks ADD COLUMN is_ack INTEGER NOT NULL DEFAULT 0")
        logger.info("evidence_fallbacks: added is_ack column (migration).")

    # Backfill is_ack=1 for the canonical client-acknowledgment evidence so
    # any DB — freshly seeded or evolved — agrees with the EvidenceFallback
    # contract.  Idempotent: re-running is a no-op once values are set.
    _ACK_EVIDENCE_IDS = ("EVID_RISK_OVERRIDE_ACK", "EVID_SUITABILITY_ACK", "EVID_CONCENTRATION_ACK")
    cursor.execute(
        "UPDATE evidence_fallbacks SET is_ack=1 WHERE evidence_id IN (?, ?, ?) AND is_ack=0",
        _ACK_EVIDENCE_IDS,
    )

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
