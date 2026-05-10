"""
run_conversational_test.py — Test the new 'User Simulator' feature.
The user provides a high-risk request initially.
The Auditor blocks it (NEEDS_REVISION).
The User Simulator injects 'additional_info' (consent).
The Proposer (now more conversational) receives the consent and successfully recovers.
"""

import json
import time
import sys
import os
from dotenv import load_dotenv
load_dotenv()

from app.engine import graph

LOG_FILE = os.path.join(os.path.dirname(__file__), "conversational_audit_trail.log")

class _Tee:
    def __init__(self, filepath: str):
        self._file = open(filepath, "w", encoding="utf-8")
        self._stdout = sys.stdout
    def write(self, data: str):
        self._stdout.write(data)
        self._file.write(data)
    def flush(self):
        self._stdout.flush()
        self._file.flush()
    def close(self):
        self._file.close()

_tee = _Tee(LOG_FILE)
sys.stdout = _tee

CASE = {
    "label": "CONVERSATIONAL RECOVERY — TQQQ Buy with Mid-Loop Consent",
    "expected": "CERTIFIED_COMPLIANT",
    "client_id": "7f1034da-1a46-4818-a07a-5d4ceb4f8cc5",
    "prompt": "I want to buy $50,000 of TQQQ in my retirement account.",
    "additional_info": (
        "Yes, I understand TQQQ is a triple-leveraged ETF with high volatility. "
        "I explicitly acknowledge the high risk and potential for severe capital loss in writing and agree to proceed. "
        "I also have the risk officer sign-off for my speculative history (EVID_SPECULATIVE_WAIVER)."
    )
}

def load_client(client_id: str) -> dict:
    with open("data/vault.json") as f:
        clients = {c["client_id"]: c for c in json.load(f)}
    return clients.get(client_id)

def build_state(client_id: str, client_data: dict, case: dict) -> dict:
    return {
        "client_id": client_id,
        "client_data": client_data,
        "prompt": case["prompt"],
        "proposal": "",
        "proposal_json": {},
        "critique": "",
        "constraint_delta": {},
        "status": "PENDING",
        "revision_count": 0,
        "supervisor_enabled": True,
        "fired_rules": [],
        "history_log": "",
        "risk_scores": [],
        # New fields for simulation
        "additional_info": case.get("additional_info", ""),
        "info_injected": False
    }

def print_trail(label: str, final_state: dict, elapsed: float):
    width = 72
    print("\n" + "═" * width)
    print(f"  {label}")
    print("═" * width)
    print(f"\n  Final Outcome  : {final_state.get('status', 'UNKNOWN')}")
    print(f"  Total Rounds   : {final_state.get('revision_count', 0)}")
    print(f"  Time Taken     : {elapsed:.1f}s")
    
    print(f"\n{'─'*width}")
    print("  COMPLETE CONVERSATIONAL TRAIL")
    print(f"{'─'*width}")
    print(final_state.get("history_log") or "  (no trail recorded)")
    
    print(f"\n{'─'*width}")
    print("  FINAL STATUS")
    print(f"{'─'*width}")
    print(f"  Status: {final_state.get('status')}")
    print(f"  Rules : {final_state.get('fired_rules')}")
    print("═" * width + "\n")

def main():
    print("⏳ Running Conversational Test ...")
    client_data = load_client(CASE["client_id"])
    state = build_state(CASE["client_id"], client_data, CASE)
    
    t0 = time.time()
    final = graph.invoke(state)
    elapsed = time.time() - t0
    
    print_trail(CASE["label"], final, elapsed)
    _tee.close()

if __name__ == "__main__":
    main()
