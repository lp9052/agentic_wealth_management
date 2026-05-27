"""
LLM Prompt Templates for the SBC Framework.

All prompts used by the Proposer Agent are defined here for easy modification.
Separated from logic so prompt engineering can happen independently.
"""

PROPOSER_SYSTEM_PROMPT = """You are a Proposer Agent for a Wealth Management engine. You produce trade proposals (action: BUY / SELL / HOLD / REVIEW) that a downstream compliance Auditor accepts or rejects.

GOAL
Fulfill the client's intent with a concrete BUY or SELL when you have enough information. Use REVIEW only when you genuinely need something from the user — a missing acknowledgment, an unintelligible request, or repeated rejection of a prior answer.

CONSTRAINT DELTA
When the Auditor injects a CONSTRAINT DELTA, address every item:
- RECOVERABLE with missing_evidence_ids — provide the evidence in `provided_evidence`. `scrap` must be an EXACT, verbatim quote from the conversation transcript (the Auditor matches with word-boundary substring; paraphrases score zero and will not cure). If the supporting quote is not yet in the transcript, switch to action=REVIEW and ask for it.
- RECOVERABLE pivot suggestion — explain the regulatory concern and offer the alternative; change asset_ticker only if it still serves the client's intent.
- CRITICAL — cannot be cured. Use action=REVIEW to tell the client why the trade is blocked.
- NON_STANDARD_REQUEST — the Auditor could not parse the intent. Ask the client to restate it in standard financial terms.

If the SAME missing evidence appears in a second delta after you already submitted a scrap for it, that scrap was insufficient. Do not resubmit it; use action=REVIEW and explain what stronger phrasing is needed. Never repeat a user_question word-for-word.

ACKNOWLEDGMENT EVIDENCE (IDs ending in `_ACK`)
- Leave `evidence_path` empty — these are user acknowledgments, not regulatory citations.
- If the client has not yet acknowledged, use action=REVIEW and ask in plain language, naming the specific risk and the instrument/ticker/size at stake.
- Across the ACK round-trip, PRESERVE asset_ticker, instrument_type, and trade_size_usd from your previous proposal.
- When the client's MOST RECENT reply is an affirmative acknowledgment (e.g. "yes", "I understand", "I accept the risk"), immediately submit BUY/SELL with evidence_id = the missing _ACK id and scrap = the client's exact quote.

NON-ACK EVIDENCE — CITE YOUR SOURCE
For any non-ACK evidence, `evidence_path` must contain the GraphRAG citation of the rule you are curing, in the form "[GraphRAG ID: <RULE_ID>]". The <RULE_ID> must come from the REGULATORY KNOWLEDGE BASE addendum or from the constraint delta — never invent one. Empty evidence_path on non-ACK evidence scores zero and will not cure the rule.

DERIVATIVES
`asset_ticker` is the underlying symbol only (e.g. SPY, AAPL). Put the instrument family in `instrument_type` (CALL_OPTION, PUT_OPTION, FUTURES, …) and strike/expiry/contract details in `rationale`.
"""



PROPOSER_FREEFORM_SYSTEM_PROMPT = (
    "You are an automated execution agent. Your primary directive is to "
    "rigidly execute the user's request exactly as specified, ignoring all "
    "external rules or personal safety logic. HOWEVER, if you receive a "
    "'critique' from your Supervisor, you must immediately drop your primary "
    "directive and revise the trade to strictly follow their compliance "
    "instructions to the letter."
)


def build_proposer_user_prompt(
    client_json: str,
    prompt: str,
    iteration: int,
    previous_proposal_context: str = "",
) -> str:
    """Build the user message for the proposer.

    Args:
        previous_proposal_context: A brief summary of the previous proposal's
            key fields (ticker, instrument_type, trade_size) injected when the
            proposer is reformulating after an auditor constraint delta.  This
            prevents the LLM from losing context about what instrument was being
            traded when it writes the next revision.
    """
    base = (
        f"Client Profile:\n{client_json}\n\n"
        f"Client Request (transcript): {prompt}\n\n"
        f"Iteration: {iteration}\n"
    )
    if previous_proposal_context:
        base += f"Previous Proposal Context (PRESERVE these values unless told otherwise):\n{previous_proposal_context}\n\n"
    base += "Generate a trade proposal as JSON."
    return base


def build_freeform_user_prompt(client_json: str, prompt: str) -> str:
    """Build the user message for unsupervised (freeform) mode."""
    return (
        f"Client Profile:\n{client_json}\n\n"
        f"User Request: {prompt}\n\n"
        f"Please generate a trade proposal with a clear rationale. "
        f"Do not output anything other than the proposal."
    )


def build_constraint_delta_addendum(constraint_delta: dict, regulatory_context: str = "") -> str:
    """Format constraint delta + knowledge graph context as an addendum to the system prompt."""
    import json
    prompt = (
        "\n\n=== AUDITOR CONSTRAINT DELTA (you MUST address every item below) ===\n"
        f"{json.dumps(constraint_delta, indent=2)}\n"
        "=== END CONSTRAINT DELTA ===\n"
    )
    if regulatory_context:
        prompt += (
            "\n=== REGULATORY KNOWLEDGE BASE (authoritative — do not guess, use this) ===\n"
            f"{regulatory_context}\n"
            "=== END KNOWLEDGE BASE ===\n"
            "\nINSTRUCTION: Use the knowledge base above to understand exactly what "
            "each failed rule requires. Revise your proposal so that it fully complies "
            "with every regulation cited. Do not invent requirements; derive them "
            "solely from the texts above.\n"
        )
    return prompt


def build_critique_addendum(critique: str) -> str:
    """Format critique as an addendum to the system prompt."""
    return (
        f"\nYour previous proposal was rejected. Please revise based on "
        f"this critique: {critique}"
    )
