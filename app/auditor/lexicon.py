"""
Compliance Lexicon Ontology for Deterministic Information Extraction.

This module defines the semantic slots required to trigger specific
compliance violations. By using structured slot-filling rather than
generic sentence embeddings, we achieve 100% deterministic, auditable
compliance checks without arbitrary cosine similarity thresholds.

Each rule defines multiple 'slots'. A sentence must contain at least
one lemma/keyword from EACH required slot to trigger the signal.
"""

COMPLIANCE_ONTOLOGY = {
    "HIGH_RISK_PRODUCT": {
        "action": ["buy", "purchase", "invest", "put", "shift", "allocate"],
        "modifier": ["triple", "leveraged", "inverse", "3x", "2x", "complex"],
        "asset": ["etf", "note", "derivative", "option", "vix"]
    },
    "SPECULATIVE_PRODUCT": {
        "action": ["buy", "invest", "put", "allocate"],
        "asset": ["penny", "otc", "pink sheet", "unproven", "speculative", "promissory"],
        "source": ["retirement", "savings", "conservative", "nest egg"]
    },
    "KYC_BYPASS": {
        "action": ["open", "create", "setup", "fund"],
        "target": ["nephew", "friend", "cousin", "brother", "sister", "someone else", "third party"],
        "bypass": ["without", "skip", "ignore", "don't need", "bypass"],
        "document": ["verification", "identity", "kyc", "documents", "paperwork", "id"]
    },
    "SUSPICIOUS_TRANSFER": {
        "action": ["wire", "transfer", "send", "move"],
        "target": ["shell", "offshore", "cayman", "panama", "unregistered", "entity"],
        "bypass": ["without", "skip", "ignore", "bypass", "no questions"],
        "document": ["aml", "source", "compliance", "check", "verification"]
    },
    "SOFT_DOLLAR_ROUTING": {
        "action": ["route", "send", "direct", "execute"],
        "motivation": ["perk", "soft dollar", "soft-dollar", "kickback", "rebate", "benefit"],
        "actor": ["broker", "dealer", "firm", "you"]
    },
    "HIGH_FEE_PRODUCT": {
        "action": ["buy", "invest", "put", "purchase"],
        "asset": ["proprietary", "internal", "own", "in-house"],
        "modifier": ["highest", "high", "max"],
        "cost": ["fee", "expense", "cost"],
        "bypass": ["don't care", "regardless", "ignore", "refuse"]
    },
    "HIGH_COMMISSION_PRODUCT": {
        "action": ["sell", "recommend", "buy", "put"],
        "motivation": ["highest", "max", "most", "best"],
        "cost": ["commission", "payout", "comp", "compensation", "fee"],
        "bypass": ["regardless", "ignore", "don't care"]
    },
    "RESTRICTED_STOCK_SALE": {
        "action": ["sell", "liquidate", "dump", "dispose"],
        "asset": ["restricted", "control", "unregistered", "legend", "144"],
        "modifier": ["before", "immediately", "now", "hasn't expired", "prior"],
        "timing": ["holding", "period", "lockup", "lock-up", "expiry"]
    },
    "DERIVATIVE_LOCKUP_CIRCUMVENT": {
        "action": ["use", "buy", "hedge", "short"],
        "instrument": ["put", "option", "derivative", "collar", "short"],
        "asset": ["restricted", "control", "lockup", "lock-up"],
        "intent": ["hedge", "protect", "circumvent", "dispose", "lock"]
    },
    "SKIP_FORM_144": {
        "action": ["sell", "liquidate", "dump"],
        "asset": ["affiliate", "control", "insider", "restricted"],
        "bypass": ["without", "skip", "ignore", "don't file", "bypass"],
        "document": ["form 144", "144", "sec form", "filing"]
    },
    "INSIDER_TIP": {
        "source": ["cousin", "friend", "insider", "contact", "colleague", "neighbor"],
        "action": ["told", "leak", "tip", "mention", "said", "share", "heard", "got"],
        "event": ["merger", "earnings", "deal", "trial", "results", "announce", "acquisition"]
    },
    "BOARD_TIP": {
        "source": ["board", "director", "chairman", "c-suite", "exec", "ceo", "cfo"],
        "action": ["told", "leak", "tip", "mention", "said", "share", "heard", "got"],
        "event": ["merger", "earnings", "deal", "guidance", "dividend", "strategy", "results"]
    },
    "WASH_SALE_PATTERN": {
        "action1": ["sell", "liquidate", "dump"],
        "motivation": ["loss", "tax", "harvest", "deduction"],
        "action2": ["buy", "repurchase", "rebuy", "get"],
        "timing": ["back", "tomorrow", "today", "immediately", "soon", "morning"]
    },
    "CROSS_ACCOUNT_WASH": {
        "action1": ["sell", "liquidate", "dump"],
        "context1": ["loss", "tax", "harvest"],
        "action2": ["buy", "repurchase", "rebuy"],
        "target": ["ira", "spouse", "sub-account", "wife", "husband", "other account"]
    },
    "OFF_PLATFORM_INVESTMENT": {
        "action": ["execute", "invest", "buy", "do", "participate"],
        "modifier": ["off", "outside", "private", "unregistered"],
        "context": ["book", "record", "platform", "system", "radar", "firm"]
    },
    "ADVISOR_NETWORK_INVESTMENT": {
        "action": ["invest", "put", "use", "direct"],
        "asset": ["funds", "money", "capital", "brokerage"],
        "target": ["cousin", "friend", "brother", "sister", "family", "your"],
        "entity": ["startup", "venture", "fund", "company", "business"]
    },
    "PERSONAL_LENDING": {
        "actor1": ["you", "advisor", "broker", "rep"],
        "action": ["lend", "loan", "borrow"],
        "actor2": ["me", "my", "I", "customer"],
        "context": ["personal", "private", "own account", "directly", "fifty", "thousand", "cash", "funds"]
    },
    "COLLATERAL_LENDING": {
        "action": ["use", "pledge", "put up"],
        "asset": ["portfolio", "equity", "securities", "stock", "account"],
        "context": ["collateral", "security", "guarantee"],
        "target": ["loan", "lending", "arrangement", "borrowing"]
    }
}
