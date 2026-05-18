"""
LLM Proposer Agent.

Generates structured trade proposals from client requests.
Accepts constraint_delta feedback for iterative revision.
Prompts are defined in app/proposer/prompts.py for easy modification.

On revision cycles the agent enriches its system prompt with:
  1. Full regulation text (ChromaDB semantic retrieval per failed rule)
  2. Related / sibling rules from the knowledge graph
  3. Specific audit failure descriptions from the constraint_delta
This ensures the LLM knows exactly what to change without guessing.
"""

import json
import logging
import os
from typing import Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage, SystemMessage

from app.auditor.models import TradeProposal, ProvidedEvidence, INSTRUMENT_TYPES
from app.proposer.prompts import (
    PROPOSER_SYSTEM_PROMPT,
    PROPOSER_FREEFORM_SYSTEM_PROMPT,
    build_proposer_user_prompt,
    build_freeform_user_prompt,
    build_constraint_delta_addendum,
    build_critique_addendum,
)

logger = logging.getLogger(__name__)

# Path to the flat regulation knowledge graph (used for related-rule lookup)
_REG_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "data", "regulations.json")
)
_regulations_cache: Optional[list] = None


def _load_regulations() -> list:
    """Load and cache regulations.json."""
    global _regulations_cache
    if _regulations_cache is None:
        try:
            with open(_REG_PATH, "r") as f:
                _regulations_cache = json.load(f)
        except Exception as e:
            logger.error("Failed to load regulations.json: %s", e)
            _regulations_cache = []
    return _regulations_cache


def _build_rag_context(constraint_delta: dict) -> str:
    """
    Build a rich knowledge context block for the proposer on revision cycles.

    Sources (in order of injection):
      1. ChromaDB semantic retrieval — full regulation text for each failed rule
      2. Related-rule graph expansion — sibling rules the LLM should be aware of
      3. Audit failure details — the specific descriptions from the auditor
    """
    failed_rules: list[str] = constraint_delta.get("failed_rules", [])
    failed_details: list[dict] = constraint_delta.get("failed_details", [])
    missing_evidence: list[str] = constraint_delta.get("missing_evidence_ids", [])

    if not failed_rules:
        return ""

    regulations = _load_regulations()
    reg_by_id = {r["id"]: r for r in regulations}

    sections: list[str] = []

    # ── 1. Full regulation text via ChromaDB semantic retrieval ───────────────
    try:
        from app.proposer.rag import retrieve_regulations
        seen_ids: set[str] = set()
        for rule_id in failed_rules:
            reg_entry = reg_by_id.get(rule_id)
            query = (
                reg_entry["text"][:200] if reg_entry else rule_id
            )  # Use the rule text as the semantic query for best recall
            chunks = retrieve_regulations(query, k=2)
            for chunk in chunks:
                if chunk not in seen_ids:   # deduplicate by content
                    seen_ids.add(chunk)
                    sections.append(f"[Source Link/Path: GraphRAG ID: {rule_id}]\n{chunk}")
    except Exception as e:
        logger.warning("ChromaDB RAG retrieval failed, falling back to flat lookup: %s", e)
        # Fallback: inject static text directly from knowledge graph
        for rule_id in failed_rules:
            entry = reg_by_id.get(rule_id)
            if entry:
                sections.append(
                    f"[Source Link/Path: GraphRAG ID: {rule_id}]\n"
                    f"Rule: {entry['metadata']['rule']}\n"
                    f"Text: {entry['text']}"
                )

    # ── 2. Related-rule graph expansion ──────────────────────────────────────
    related_ids: set[str] = set()
    for rule_id in failed_rules:
        entry = reg_by_id.get(rule_id)
        if entry:
            for rel in entry["metadata"].get("related", []):
                if rel not in failed_rules:   # only expand to rules not already flagged
                    related_ids.add(rel)

    if related_ids:
        related_block = "[Related Rules — contextual awareness]\n"
        for rel_id in sorted(related_ids):
            entry = reg_by_id.get(rel_id)
            if entry:
                related_block += (
                    f"  [Source Link/Path: GraphRAG ID: {rel_id}] ({entry['metadata']['rule']}): "
                    f"{entry['text'][:300]}...\n"
                )
        sections.append(related_block)

    # ── 3. Specific audit failure descriptions ────────────────────────────────
    if failed_details:
        detail_block = "[Auditor Failure Details — exact reasons for rejection]\n"
        for d in failed_details:
            tag = "ABSOLUTE" if d.get("bypass_tsf") else "GRADED"
            detail_block += (
                f"  Rule {d.get('rule_id', '?')} [{tag}]: "
                f"{d.get('description', '')}\n"
            )
        if missing_evidence:
            detail_block += (
                f"  Missing evidence required: {missing_evidence}\n"
                f"  → You MUST provide these evidence IDs in provided_evidence, "
                f"or pivot to a compliant alternative.\n"
            )
        sections.append(detail_block)

    return "\n\n".join(sections)


def create_proposer_llm() -> ChatGoogleGenerativeAI:
    """Create the proposer LLM instance."""
    return ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.2, max_retries=2)


from pydantic import BaseModel, Field, field_validator
import re as _re

class ProvidedEvidenceSchema(BaseModel):
    evidence_id: str
    value: bool
    scrap: str = ""
    evidence_path: str = Field(
        default="",
        description="The specific GraphRAG or document path/link from the retrieved context justifying this evidence. e.g. [GraphRAG ID: FINRA_2111]"
    )

class TradeProposalSchema(BaseModel):
    action: str = Field(
        description="Trade action. MUST be exactly one of: BUY, SELL, HOLD, REVIEW"
    )
    asset_ticker: str = Field(
        description=(
            "The underlying exchange ticker symbol ONLY — 1 to 6 uppercase letters or dots. "
            "NEVER include instrument type, expiry, strike, or descriptive words here. "
            "Examples: 'SPY', 'AAPL', 'BRK.B'. "
            "For options, futures, or derivatives write ONLY the underlying ticker "
            "(e.g. 'SPY', NOT 'SPY call options' or 'SPY 500C')."
        )
    )
    instrument_type: str = Field(
        default="EQUITY",
        description=(
            "The instrument type being traded. Use EXACTLY one of: "
            f"{', '.join(INSTRUMENT_TYPES)}. "
            "For a plain stock purchase use EQUITY; for an ETF use ETF. "
            "For SPY call options use CALL_OPTION, for SPY puts use PUT_OPTION, "
            "for index futures use FUTURES.  Anything else: OTHER."
        )
    )
    trade_size_usd: float = Field(
        description="Notional trade size in USD. Must be a positive number."
    )
    rationale: str = Field(
        description=(
            "Internal explanation of the trade. For derivatives, include instrument "
            "details (call/put, strike, expiry) here — NOT in asset_ticker."
        )
    )
    user_question: str = Field(
        description="Direct question to the user asking for missing information or acknowledgment."
    )
    provided_evidence: list[ProvidedEvidenceSchema] = Field(default_factory=list)

    @field_validator("asset_ticker", mode="before")
    @classmethod
    def _extract_underlying_ticker(cls, v: str) -> str:
        """
        Safety net: if the LLM sends a multi-word string like 'SPY call options',
        extract the first valid ticker token (uppercase letters + dots, 1–6 chars).
        This prevents STATIC_PORTFOLIO CRITICAL blocks from bad LLM output.
        """
        raw = str(v).strip()
        # Already a clean ticker?
        if _re.fullmatch(r"[A-Z][A-Z0-9\.]{0,5}", raw):
            return raw
        # Extract first token that looks like a ticker (uppercase letters/dots, max 6)
        tokens = raw.upper().split()
        for token in tokens:
            # Strip trailing punctuation
            token = _re.sub(r"[^A-Z0-9\.]", "", token)
            if token and _re.fullmatch(r"[A-Z][A-Z0-9\.]{0,5}", token):
                return token
        # Last resort: take first 6 chars of first word, uppercase
        return raw.split()[0].upper()[:6] if raw else "UNKNOWN"

    @field_validator("action", mode="before")
    @classmethod
    def _normalize_action(cls, v: str) -> str:
        """Accept minor LLM casing variations like 'buy' or 'Buy'."""
        return str(v).strip().upper()

    @field_validator("instrument_type", mode="before")
    @classmethod
    def _normalize_instrument_type(cls, v: str) -> str:
        """
        Normalize loose LLM variants ('call option', 'Call_Option', 'option',
        'stock', 'future') to a canonical member of INSTRUMENT_TYPES.  Falling
        through means the value passes as-is to the auditor, which then trips
        the unknown-instrument path.
        """
        if v is None:
            return "EQUITY"
        # Step 1: uppercase, collapse whitespace/hyphens into underscores
        s = _re.sub(r"[\s\-]+", "_", str(v).strip().upper())
        if s in INSTRUMENT_TYPES:
            return s
        # Step 2: light alias mapping for the most common LLM variants
        # (kept narrow — anything we don't recognize stays unmapped so we
        # don't silently coerce a typo into the wrong derivative class).
        aliases = {
            "STOCK": "EQUITY", "EQUITIES": "EQUITY", "SHARE": "EQUITY", "SHARES": "EQUITY",
            "CALL": "CALL_OPTION", "CALLS": "CALL_OPTION", "CALL_OPTIONS": "CALL_OPTION",
            "PUT": "PUT_OPTION", "PUTS": "PUT_OPTION", "PUT_OPTIONS": "PUT_OPTION",
            "OPTION": "OPTION", "OPTIONS": "OPTION",   # generic option, kept distinct
            "FUTURE": "FUTURES", "FUTURE_CONTRACT": "FUTURES", "FUTURES_CONTRACT": "FUTURES",
            "ETFS": "ETF", "FUND": "ETF",
            "BONDS": "BOND", "FIXED_INCOME": "BOND",
        }
        return aliases.get(s, s)


def generate_proposal(
    client_id: str, client_data: dict, prompt: str,
    constraint_delta: Optional[dict] = None, iteration: int = 1,
    llm: Optional[ChatGoogleGenerativeAI] = None,
    previous_proposal_context: str = "",
) -> TradeProposal:
    """Generate a structured JSON trade proposal using the LLM."""
    if llm is None:
        llm = create_proposer_llm()

    client_json = json.dumps(client_data, indent=2)
    system_msg = PROPOSER_SYSTEM_PROMPT
    if constraint_delta:
        rag_context = _build_rag_context(constraint_delta)
        system_msg += build_constraint_delta_addendum(constraint_delta, rag_context)

    user_msg = build_proposer_user_prompt(
        client_json, prompt, iteration,
        previous_proposal_context=previous_proposal_context,
    )
    
    # Use native structured output (Tool Calling under the hood)
    structured_llm = llm.with_structured_output(TradeProposalSchema)
    response: TradeProposalSchema = structured_llm.invoke([
        SystemMessage(content=system_msg), 
        HumanMessage(content=user_msg)
    ])
    
    # Map back to our internal dataclass
    evidence = [
        ProvidedEvidence(
            evidence_id=e.evidence_id, 
            value=e.value, 
            scrap=e.scrap,
            evidence_path=e.evidence_path
        )
        for e in response.provided_evidence
    ]
    
    return TradeProposal(
        proposal_id=f"{client_id}_t_{iteration}",
        client_id=client_id,
        action=response.action,
        asset_ticker=response.asset_ticker,
        instrument_type=getattr(response, "instrument_type", "EQUITY") or "EQUITY",
        trade_size_usd=float(response.trade_size_usd),
        rationale=response.rationale,
        user_question=response.user_question,
        provided_evidence=evidence,
    )

def generate_proposal_freeform(
    client_id: str, client_data: dict, prompt: str,
    critique: Optional[str] = None, llm: Optional[ChatGoogleGenerativeAI] = None,
) -> str:
    """Generate a free-form proposal for unsupervised (no audit) mode."""
    if llm is None:
        llm = create_proposer_llm()

    client_json = json.dumps(client_data, indent=2)
    sys_prompt = PROPOSER_FREEFORM_SYSTEM_PROMPT
    if critique:
        sys_prompt += build_critique_addendum(critique)

    user_msg = build_freeform_user_prompt(client_json, prompt)
    response = llm.invoke([SystemMessage(content=sys_prompt), HumanMessage(content=user_msg)])
    return response.content
