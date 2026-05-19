"""
Proposer Package - LLM trade proposal generation.

Contains:
- agent.py:   Core proposer logic (LLM calls, proposal parsing)
- prompts.py: All LLM prompt templates (editable without touching logic)
- rag.py:     Regulatory RAG retrieval for context enrichment

The top-level re-exports (``create_proposer_llm``, ``generate_proposal``,
``generate_proposal_freeform``) are exposed via ``__getattr__`` so that
``from app.proposer import prompts`` doesn't pull in the heavyweight
langchain/google-genai/cryptography import chain that ``agent`` depends on.
"""

from typing import Any

__all__ = (
    "create_proposer_llm",
    "generate_proposal",
    "generate_proposal_freeform",
)


def __getattr__(name: str) -> Any:
    """Lazy import: forward attribute access on the package to app.proposer.agent."""
    if name in __all__:
        from app.proposer import agent
        value = getattr(agent, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
