"""Tests for app/database/schema.py — DDL + migration."""

import sqlite3

import pytest

from app.database import schema
from app.database.schema import get_connection, init_schema


def test_get_connection_returns_row_factory(tmp_path):
    conn = get_connection(str(tmp_path / "x.db"))
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_init_schema_creates_all_tables(tmp_path):
    db = str(tmp_path / "x.db")
    init_schema(db)
    conn = get_connection(db)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    expected = {
        "regulations", "rule_clauses", "trigger_conditions",
        "kyc_requirements", "evidence_fallbacks",
        "clients", "client_holdings", "client_relations",
        "audit_trail",
    }
    assert expected <= tables


def test_init_schema_is_idempotent(tmp_path):
    db = str(tmp_path / "x.db")
    init_schema(db)
    init_schema(db)  # second call must not error
    conn = get_connection(db)
    # All expected tables still present
    count = conn.execute(
        "SELECT count(*) FROM sqlite_master WHERE type='table'"
    ).fetchone()[0]
    assert count >= 9


def test_init_schema_migrates_legacy_severity_column(tmp_path):
    """Old DBs with a `severity` column on regulations must be dropped + rebuilt."""
    db = str(tmp_path / "x.db")
    conn = sqlite3.connect(db)
    # Create the legacy shape
    conn.execute("CREATE TABLE regulations (rule_id TEXT PRIMARY KEY, severity TEXT)")
    conn.commit()
    conn.close()

    init_schema(db)

    # New shape: bypass_tsf column present, severity gone
    conn = get_connection(db)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(regulations)").fetchall()}
    assert "bypass_tsf" in cols
    assert "severity" not in cols


def test_init_schema_adds_is_ack_column_to_existing_evidence_table(tmp_path):
    """Pre-existing evidence_fallbacks without is_ack get the column injected."""
    db = str(tmp_path / "x.db")
    conn = sqlite3.connect(db)
    # Build a "post-severity" but "pre-is_ack" state by creating just the
    # evidence table.
    conn.execute("""
        CREATE TABLE evidence_fallbacks (
            evidence_id TEXT PRIMARY KEY,
            kyc_id TEXT,
            description TEXT
        )
    """)
    conn.commit()
    conn.close()

    init_schema(db)

    conn = get_connection(db)
    cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(evidence_fallbacks)"
    ).fetchall()}
    assert "is_ack" in cols


def test_init_schema_backfills_ack_evidence_ids(tmp_path):
    db = str(tmp_path / "x.db")
    init_schema(db)
    conn = get_connection(db)

    # Insert a regulation + KYC so we have a FK target for evidence_fallbacks
    conn.execute("INSERT INTO regulations (rule_id, rule_name, bypass_tsf, description) "
                 "VALUES ('R', 'r', 0, 'd')")
    conn.execute("INSERT INTO rule_clauses (clause_id, rule_id, description) "
                 "VALUES ('C', 'R', 'd')")
    conn.execute("INSERT INTO trigger_conditions VALUES "
                 "('T', 'C', 'prompt_signal', 'CONTAINS', 'X')")
    conn.execute("INSERT INTO kyc_requirements VALUES "
                 "('K', 'T', 'f', '==', 't', 'profile')")
    # Insert an ACK evidence with is_ack=0 — the migration should bump it to 1.
    conn.execute("INSERT INTO evidence_fallbacks (evidence_id, kyc_id, description, is_ack) "
                 "VALUES ('EVID_RISK_OVERRIDE_ACK', 'K', 'd', 0)")
    conn.commit()
    conn.close()

    init_schema(db)  # run again — UPDATE backfill should set is_ack=1

    conn = get_connection(db)
    row = conn.execute(
        "SELECT is_ack FROM evidence_fallbacks WHERE evidence_id='EVID_RISK_OVERRIDE_ACK'"
    ).fetchone()
    assert row["is_ack"] == 1


def test_schema_module_default_db_path():
    """The default DB_PATH points into the project data directory."""
    assert schema.DB_PATH.endswith("rules.db")
