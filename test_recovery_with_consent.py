"""
test_recovery_with_consent.py — Simulate a user providing consent in the initial prompt
to see if the Proposer correctly attaches it as evidence and the Auditor clears it.
"""

import json
import time
import sys
import os
from dotenv import load_dotenv
load_dotenv()

from app.engine import graph

LOG_FILE = os.path.join(os.path.dirname(__file__), "recovery_success_trail.log")

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

# We append the consent directly to the prompt to simulate the user "responding"
# in a way the LLM Proposer can see in its current 'prompt' context.
CASE = {
    "label": "RECOVERY TEST — FINRA_2111 (With Proactive Consent)",
    "expected": "CERTIFIED_COMPLIANT",
    "client_id": "7f1034da-1a46-4818-a07a-5d4ceb4f8cc5",
    "prompt": (
        "Buy $50,000 of TQQQ. "
        "I explicitly acknowledge the high risk and potential for severe capital loss in writing (EVID_RISK_OVERRIDE_ACK). "
        "I also have the required risk officer sign-off for my speculative trading history (EVID_SPECULATIVE_WAIVER)."
    ),
}

def load_client(client_id: str) -> dict:
    with open("data/vault.json") as f:
        clients = {c["client_id"]: c for c in json.load(f)}
    return clients.get(client_id)

def build_state(client_id: str, client_data: dict, prompt: str) -> dict:
    return {
        "client_id": client_id,
        "client_data": client_data,
        "prompt": prompt,
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
    }

def print_trail(label: str, final_state: dict, elapsed: float):
    print(f"\n  Outcome  : {final_state.get('status', 'UNKNOWN')}")
    print(f"  Revisions: {final_state.get('revision_count', 0)}")
    print(f"  Elapsed  : {elapsed:.1f}s")
    print(f"\n  COMPLETE AUDIT TRAIL\n{'-'*40}")
    print(final_state.get("history_log") or "  (no trail)")
    print(f"\n  FINAL AUDITOR CRITIQUE\n{'-'*40}")
    print(final_state.get("critique") or "(none)")

def main():
    print(f"⏳ Running {CASE['label']} ...")
    client_data = load_client(CASE["client_id"])
    state = build_state(CASE["client_id"], client_data, CASE["prompt"])
    t0 = time.time()
    final = graph.invoke(state)
    elapsed = time.time() - t0
    print_trail(CASE["label"], final, elapsed)
    _tee.close()

if __name__ == "__main__":
    main()
