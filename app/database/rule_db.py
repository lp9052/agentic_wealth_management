"""
Regulatory Rule Database access layer.

Loads the AST rule tree from SQLite and constructs in-memory
Regulation → RuleClause → TriggerCondition → KYCRequirement objects.
"""

from app.database.schema import get_connection, DB_PATH
from app.auditor.models import (
    Regulation,
    RuleClause,
    TriggerCondition,
    KYCRequirement,
    EvidenceFallback,
    TriggerOperator,
    KYCOperator,
)


def load_all_regulations(db_path: str = DB_PATH) -> list[Regulation]:
    """
    Load the full AST from SQLite into a list of Regulation objects.
    
    This is called once at startup and cached in memory by the rule registry.
    """
    conn = get_connection(db_path)
    c = conn.cursor()

    # 1. Load all evidence fallbacks keyed by kyc_id.  The kyc_id itself is
    # only used to attach the fallback to its parent KYC; we don't carry it
    # into the in-memory model.  Empty descriptions are rejected loudly —
    # they'd silently auto-cure rules in _score_evidence_coverage.
    fallback_map: dict[str, EvidenceFallback] = {}
    for row in c.execute("SELECT * FROM evidence_fallbacks"):
        description = row["description"] or ""
        if not description.strip():
            raise ValueError(
                f"evidence_fallbacks row {row['evidence_id']!r} has empty "
                "description; non-empty descriptions are required so that "
                "_score_evidence_coverage's semantic similarity has a "
                "target.  Fix the seed."
            )
        fallback_map[row["kyc_id"]] = EvidenceFallback(
            evidence_id=row["evidence_id"],
            description=description,
            is_ack=bool(row["is_ack"]),
        )

    # 2. Load all KYC requirements keyed by condition_id
    kyc_map: dict[str, list[KYCRequirement]] = {}
    for row in c.execute("SELECT * FROM kyc_requirements"):
        kyc = KYCRequirement(
            kyc_id=row["kyc_id"],
            condition_id=row["condition_id"],
            client_field=row["client_field"],
            operator=KYCOperator(row["operator"]),
            threshold=row["threshold"],
            domain=row["domain"],
            fallback=fallback_map.get(row["kyc_id"]),
        )
        kyc_map.setdefault(row["condition_id"], []).append(kyc)

    # 3. Load all trigger conditions keyed by clause_id
    cond_map: dict[str, list[TriggerCondition]] = {}
    for row in c.execute("SELECT * FROM trigger_conditions"):
        cond = TriggerCondition(
            condition_id=row["condition_id"],
            clause_id=row["clause_id"],
            trigger_field=row["trigger_field"],
            trigger_operator=TriggerOperator(row["trigger_operator"]),
            trigger_value=row["trigger_value"],
            kyc_requirements=kyc_map.get(row["condition_id"], []),
        )
        cond_map.setdefault(row["clause_id"], []).append(cond)

    # 4. Load all clauses keyed by rule_id
    clause_map: dict[str, list[RuleClause]] = {}
    for row in c.execute("SELECT * FROM rule_clauses"):
        clause = RuleClause(
            clause_id=row["clause_id"],
            rule_id=row["rule_id"],
            description=row["description"] or "",
            conditions=cond_map.get(row["clause_id"], []),
        )
        clause_map.setdefault(row["rule_id"], []).append(clause)

    # 5. Load all regulations
    regulations: list[Regulation] = []
    for row in c.execute("SELECT * FROM regulations"):
        reg = Regulation(
            rule_id=row["rule_id"],
            rule_name=row["rule_name"],
            bypass_tsf=bool(row["bypass_tsf"]),
            description=row["description"] or "",
            clauses=clause_map.get(row["rule_id"], []),
        )
        regulations.append(reg)

    conn.close()
    return regulations
