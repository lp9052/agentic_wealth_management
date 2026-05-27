"""
Rule Registry - loads and caches the regulatory AST in memory.

Provides fast access to all regulations and their clause trees
without repeated database queries.
"""

import logging
from typing import Optional

from app.auditor.models import Regulation
from app.database.rule_db import load_all_regulations

logger = logging.getLogger(__name__)

# Module-level singleton cache
_regulations: Optional[list[Regulation]] = None
_rule_index: Optional[dict[str, Regulation]] = None


def get_regulations() -> list[Regulation]:
    """Get all regulations (cached after first load)."""
    global _regulations
    if _regulations is None:
        _regulations = load_all_regulations()
        logger.info(
            "Rule registry loaded: %d regulations, %d total clauses",
            len(_regulations),
            sum(len(r.clauses) for r in _regulations),
        )
    return _regulations


def get_rule_index() -> dict[str, Regulation]:
    """Get regulations indexed by rule_id (cached)."""
    global _rule_index
    if _rule_index is None:
        _rule_index = {r.rule_id: r for r in get_regulations()}
    return _rule_index
