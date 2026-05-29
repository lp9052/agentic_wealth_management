"""
FastAPI entry point for the Agentic Wealth Governance API.

Provides endpoints for:
- /propose-trade: Submit a trade request through the SBC governance loop
- /audit-signal: Test signal detection on raw text (debugging)
- /health: Health check
"""

import logging

from fastapi import FastAPI
from pydantic import BaseModel

from app.engine import graph
from app.auditor.rule_engine import detect_signals

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="Agentic Wealth Governance API",
    description="SBC Framework - Deterministic Compliance Engine",
    version="2.0.0",
)


class TradeRequest(BaseModel):
    client_id: str
    client_data: dict
    prompt: str
    supervisor_enabled: bool = True


class SignalTestRequest(BaseModel):
    text: str


@app.post("/propose-trade")
def propose_trade(req: TradeRequest):
    """
    Submit a trade request through the SBC governance loop.
    
    With supervisor_enabled=True (default):
      Proposer → Deterministic Auditor → Routing Loop
    
    With supervisor_enabled=False:
      Proposer only (no compliance checks)
    """
    initial_state = {
        "client_id": req.client_id,
        "client_data": req.client_data,
        "prompt": req.prompt,
        "proposal": "",
        "proposal_json": {},
        "critique": "",
        "constraint_delta": {},
        "status": "PENDING",
        "revision_count": 0,
        "supervisor_enabled": req.supervisor_enabled,
        "is_real_time": False,
        "additional_info": "",
        "info_injected": False,
        "fired_rules": [],
        "history_log": "",
        "risk_scores": [],
    }

    final_state = graph.invoke(initial_state)
    return {
        "final_proposal": final_state.get("proposal"),
        # status is populated by the graph in normal runs: the auditor sets it
        # in supervised mode, and the proposer sets CERTIFIED_COMPLIANT in
        # unsupervised mode.  "UNKNOWN" is a defensive floor for the degenerate
        # case where the graph returns no status at all.
        "status": final_state.get("status", "UNKNOWN"),
        "critique": final_state.get("critique"),
        "revision_count": final_state.get("revision_count"),
        "fired_rules": final_state.get("fired_rules", []),
    }


@app.post("/audit-signal")
def audit_signal(req: SignalTestRequest):
    """Debug endpoint: detect regulatory signals in raw text."""
    signals = detect_signals(req.text)
    return {"text": req.text, "detected_signals": signals}


@app.get("/health")
def health():
    """Health check endpoint."""
    return {"status": "ok", "version": "2.0.0", "engine": "SBC Deterministic Auditor"}
