"""
Seed script for the regulatory rule metadatabase.

Populates the SQLite AST with all 8 original regulations.
Key insight: Many violations are INTENT-BASED — the prompt itself reveals
the violation regardless of client state. These use domain='proposal_check'
which always fails when the trigger fires.

Run: python3 scripts/seed_rule_db.py
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.database.schema import init_schema, get_connection, DB_PATH


def seed_regulations(db_path: str = DB_PATH) -> None:
    init_schema(db_path)
    conn = get_connection(db_path)
    c = conn.cursor()

    for table in ["evidence_fallbacks", "kyc_requirements", "trigger_conditions", "rule_clauses", "regulations"]:
        c.execute(f"DELETE FROM {table}")

    def insert_evidence(evidence_id: str, kyc_id: str, description: str, is_ack: bool = False) -> None:
        c.execute(
            "INSERT INTO evidence_fallbacks (evidence_id, kyc_id, description, is_ack) "
            "VALUES (?, ?, ?, ?)",
            (evidence_id, kyc_id, description, 1 if is_ack else 0),
        )

    # === FINRA 2111 - Suitability (RECOVERABLE) ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("FINRA_2111", "FINRA Rule 2111", "RECOVERABLE",
        "Suitability - requires reasonable basis to believe a recommended transaction is suitable for the customer."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("2111.01", "FINRA_2111", "High-risk/leveraged/speculative product suitability check"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("2111.01.T1", "2111.01", "prompt_signal", "CONTAINS", "HIGH_RISK_PRODUCT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("2111.01.K1", "2111.01.T1", "risk_tolerance", "==", "Aggressive", "profile"))
    insert_evidence("EVID_RISK_OVERRIDE_ACK", "2111.01.K1",
        "Client explicitly acknowledges the high risk and potential for severe capital loss in writing.",
        is_ack=True)

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("2111.02", "FINRA_2111", "Speculative trading for client with prior compliance flags"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("2111.02.T1", "2111.02", "prompt_signal", "CONTAINS", "SPECULATIVE_PRODUCT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("2111.02.K1", "2111.02.T1", "compliance_history", "SEMANTIC_SIMILAR", "Clean record with no violations.", "profile"))
    insert_evidence("EVID_SPECULATIVE_WAIVER", "2111.02.K1",
        "Risk officer sign-off acknowledging speculative trading history.")

    # === FINRA 2090 - KYC (CRITICAL) - INTENT-BASED ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("FINRA_2090", "FINRA Rule 2090", "CRITICAL",
        "Know Your Customer - requires reasonable diligence to know essential facts concerning every customer."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("2090.01", "FINRA_2090", "Attempt to bypass KYC verification or act for unverified party"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("2090.01.T1", "2090.01", "prompt_signal", "CONTAINS", "KYC_BYPASS"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("2090.01.K1", "2090.01.T1", "intent_violation", "==", "1", "proposal_check"))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("2090.02", "FINRA_2090", "Suspicious fund transfer to unverified entity"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("2090.02.T1", "2090.02", "prompt_signal", "CONTAINS", "SUSPICIOUS_TRANSFER"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("2090.02.K1", "2090.02.T1", "intent_violation", "==", "1", "proposal_check"))

    # === SEC Reg BI (RECOVERABLE) - INTENT-BASED ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("SEC_REG_BI", "SEC Regulation Best Interest", "RECOVERABLE",
        "Broker-dealers must act in retail customer's best interest without placing firm interests ahead."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("REG_BI.01", "SEC_REG_BI", "Trade routing prioritizing firm benefit over client best execution"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("REG_BI.01.T1", "REG_BI.01", "prompt_signal", "CONTAINS", "SOFT_DOLLAR_ROUTING"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("REG_BI.01.K1", "REG_BI.01.T1", "intent_violation", "==", "1", "proposal_check"))
    insert_evidence("EVID_BEST_EXEC_DISCLOSURE", "REG_BI.01.K1",
        "Disclosure that trade will be routed for best execution, overriding client's soft-dollar request.")

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("REG_BI.02", "SEC_REG_BI", "Recommending highest-fee product without documenting alternatives"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("REG_BI.02.T1", "REG_BI.02", "prompt_signal", "CONTAINS", "HIGH_FEE_PRODUCT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("REG_BI.02.K1", "REG_BI.02.T1", "intent_violation", "==", "1", "proposal_check"))
    insert_evidence("EVID_FEE_ALTERNATIVES_DOCUMENTED", "REG_BI.02.K1",
        "Documentation of alternative lower-fee products presented to client.")

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("REG_BI.03", "SEC_REG_BI", "Recommending product based on advisor commission over client interest"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("REG_BI.03.T1", "REG_BI.03", "prompt_signal", "CONTAINS", "HIGH_COMMISSION_PRODUCT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("REG_BI.03.K1", "REG_BI.03.T1", "intent_violation", "==", "1", "proposal_check"))
    insert_evidence("EVID_COMMISSION_DISCLOSURE", "REG_BI.03.K1",
        "Full commission disclosure and suitability analysis provided.")

    # === SEC 144 - Restricted Stock (CRITICAL) - INTENT-BASED ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("SEC_144", "SEC Rule 144", "CRITICAL",
        "Governs resale of restricted or control securities. Requires holding period, volume limits, Form 144."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("SEC144.01", "SEC_144", "Sale of restricted/control securities before holding period"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("SEC144.01.T1", "SEC144.01", "prompt_signal", "CONTAINS", "RESTRICTED_STOCK_SALE"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("SEC144.01.K1", "SEC144.01.T1", "intent_violation", "==", "1", "proposal_check"))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("SEC144.02", "SEC_144", "Using derivatives to circumvent restricted stock lockup"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("SEC144.02.T1", "SEC144.02", "prompt_signal", "CONTAINS", "DERIVATIVE_LOCKUP_CIRCUMVENT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("SEC144.02.K1", "SEC144.02.T1", "intent_violation", "==", "1", "proposal_check"))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("SEC144.03", "SEC_144", "Attempting to sell affiliate/control shares without Form 144"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("SEC144.03.T1", "SEC144.03", "prompt_signal", "CONTAINS", "SKIP_FORM_144"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("SEC144.03.K1", "SEC144.03.T1", "intent_violation", "==", "1", "proposal_check"))

    # === SEC 10b-5 - Insider Trading (CRITICAL) - INTENT-BASED ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("SEC_10b5", "SEC Rule 10b-5", "CRITICAL",
        "Prohibits insider trading and fraud. Trading on MNPI is strictly forbidden."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("10b5.01", "SEC_10b5", "Trading on material non-public information from insider source"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("10b5.01.T1", "10b5.01", "prompt_signal", "CONTAINS", "INSIDER_TIP"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("10b5.01.K1", "10b5.01.T1", "intent_violation", "==", "1", "proposal_check"))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("10b5.02", "SEC_10b5", "Trading based on non-public board-level information"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("10b5.02.T1", "10b5.02", "prompt_signal", "CONTAINS", "BOARD_TIP"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("10b5.02.K1", "10b5.02.T1", "intent_violation", "==", "1", "proposal_check"))

    # === IRS Wash Sale (RECOVERABLE) - INTENT-BASED ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("IRS_WASH_SALE", "IRS Wash-Sale Rule (Section 1091)", "RECOVERABLE",
        "Prohibits claiming tax deductions on securities sold at loss if identical securities repurchased within 30 days."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("WS.01", "IRS_WASH_SALE", "Selling at loss and rebuying same/identical security within 30 days"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("WS.01.T1", "WS.01", "prompt_signal", "CONTAINS", "WASH_SALE_PATTERN"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("WS.01.K1", "WS.01.T1", "intent_violation", "==", "1", "proposal_check"))
    insert_evidence("EVID_WASH_SALE_PIVOT", "WS.01.K1",
        "Proposer pivots trade to a non-substantially-identical ETF in the same sector.")

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("WS.02", "IRS_WASH_SALE", "Cross-account wash sale via spouse account or IRA"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("WS.02.T1", "WS.02", "prompt_signal", "CONTAINS", "CROSS_ACCOUNT_WASH"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("WS.02.K1", "WS.02.T1", "intent_violation", "==", "1", "proposal_check"))
    insert_evidence("EVID_CROSS_ACCOUNT_PIVOT", "WS.02.K1",
        "Proposer pivots to non-identical security or defers purchase beyond 30-day window.")

    # === FINRA 3280 - Selling Away (CRITICAL) - INTENT-BASED ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("FINRA_3280", "FINRA Rule 3280", "CRITICAL",
        "Prohibits associated persons from participating in private securities transactions outside firm supervision."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("3280.01", "FINRA_3280", "Facilitating private securities transaction outside firm oversight"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("3280.01.T1", "3280.01", "prompt_signal", "CONTAINS", "OFF_PLATFORM_INVESTMENT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("3280.01.K1", "3280.01.T1", "intent_violation", "==", "1", "proposal_check"))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("3280.02", "FINRA_3280", "Investment in advisor's personal network"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("3280.02.T1", "3280.02", "prompt_signal", "CONTAINS", "ADVISOR_NETWORK_INVESTMENT"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("3280.02.K1", "3280.02.T1", "intent_violation", "==", "1", "proposal_check"))

    # === FINRA 3240 - Borrowing/Lending (CRITICAL) - INTENT-BASED ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("FINRA_3240", "FINRA Rule 3240", "CRITICAL",
        "Prohibits borrowing from or lending to customers unless specific exemptions apply."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("3240.01", "FINRA_3240", "Personal lending/borrowing arrangement between advisor and client"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("3240.01.T1", "3240.01", "prompt_signal", "CONTAINS", "PERSONAL_LENDING"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("3240.01.K1", "3240.01.T1", "intent_violation", "==", "1", "proposal_check"))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("3240.02", "FINRA_3240", "Using client securities as collateral for personal loan"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("3240.02.T1", "3240.02", "prompt_signal", "CONTAINS", "COLLATERAL_LENDING"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("3240.02.K1", "3240.02.T1", "intent_violation", "==", "1", "proposal_check"))

    # === STATIC CHECKS (CRITICAL) - Portfolio validation ===
    c.execute("INSERT INTO regulations VALUES (?,?,?,?)", ("STATIC_PORTFOLIO", "Portfolio Validation", "RECOVERABLE",
        "Deterministic checks for account status, funds, holdings, and concentration risk."))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("STATIC.01", "STATIC_PORTFOLIO", "Insufficient funds for purchase"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("STATIC.01.T1", "STATIC.01", "proposal.action", "==", "BUY"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("STATIC.01.K1", "STATIC.01.T1", "sufficient_funds", "==", "1", "portfolio_check"))

    c.execute("INSERT INTO rule_clauses VALUES (?,?,?)", ("STATIC.02", "STATIC_PORTFOLIO", "Insufficient holdings for sale"))
    c.execute("INSERT INTO trigger_conditions VALUES (?,?,?,?,?)", ("STATIC.02.T1", "STATIC.02", "proposal.action", "==", "SELL"))
    c.execute("INSERT INTO kyc_requirements VALUES (?,?,?,?,?,?)", ("STATIC.02.K1", "STATIC.02.T1", "sufficient_holdings", "==", "1", "portfolio_check"))

    conn.commit()
    conn.close()

    # Print summary
    conn = get_connection(db_path)
    c = conn.cursor()
    for table in ["regulations", "rule_clauses", "trigger_conditions", "kyc_requirements", "evidence_fallbacks"]:
        count = c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table}: {count}")
    conn.close()


if __name__ == "__main__":
    seed_regulations()
