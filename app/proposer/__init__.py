"""
Proposer Package - LLM trade proposal generation.

Contains:
- agent.py:   Core proposer logic (LLM calls, proposal parsing)
- prompts.py: All LLM prompt templates (editable without touching logic)
- rag.py:     Regulatory RAG retrieval for context enrichment
"""

from app.proposer.agent import (
    create_proposer_llm,
    generate_proposal,
    generate_proposal_freeform,
)
