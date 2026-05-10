"""
run_recoverable_case.py — Run a recoverable test case through the full SBC LangGraph loop.
Case: FINRA_2111 (Suitability) for a client with Moderate risk tolerance.
The auditor should flag it as RECOVERABLE, and the proposer should attempt to fix it.
"""

import json
import time
import textwrap
import sys
import os
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

from app.engine import graph

LOG_FILE = os.path.join(os.path.dirname(__file__), "recoverable_audit_trail.log")

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
    "label": "RECOVERABLE CASE — FINRA_2111 (High Risk / Moderate Client)",
    "expected": "CERTIFIED_COMPLIANT", # We hope it recovers!
    "client_id": "7f1034da-1a46-4818-a07a-5d4ceb4f8cc5",
    "prompt": "Max out margin on my conservative retirement account to aggressively acquire 0DTE options.",
}

def load_client(client_id: str) -> dict:
    with open("data/vault.json") as f:
        clients = {c["client_id"]: c for c in json.load(f)}
    client = clients.get(client_id)
    if not client:
        raise ValueError(f"Client {client_id} not found in vault.json")
    return client

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

def print_trail(label: str, expected: str, final_state: dict, elapsed: float):
    width = 72
    print("\n" + "═" * width)
    print(f"  {label}")
    print("═" * width)
    print(f"\n  Prompt   : {textwrap.shorten(final_state.get('prompt',''), 80)}")
    print(f"  Outcome  : {final_state.get('status', 'UNKNOWN')}")
    print(f"  Revisions: {final_state.get('revision_count', 0)}")
    print(f"  Elapsed  : {elapsed:.1f}s")
    print(f"  Fired    : {final_state.get('fired_rules') or 'none'}")
    print(f"\n{'─'*width}")
    print("  COMPLETE AUDIT TRAIL")
    print(f"{'─'*width}")
    trail = final_state.get("history_log") or "  (no trail recorded)"
    for line in trail.splitlines():
        print("  " + line)
    print(f"\n{'─'*width}")
    print("  FINAL AUDITOR CRITIQUE")
    print(f"{'─'*width}")
    for line in (final_state.get("critique") or "(none)").splitlines():
        print("  " + line)
    print("═" * width + "\n")

def main():
    print(f"{'═'*72}")
    print(f"  RECOVERABLE CASE RUN — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Log: {LOG_FILE}")
    print(f"{'═'*72}")

    print(f"\n⏳ Running {CASE['label']} ...")
    client_data = load_client(CASE["client_id"])
    state = build_state(CASE["client_id"], client_data, CASE["prompt"])
    t0 = time.time()
    final = graph.invoke(state)
    elapsed = time.time() - t0
    print_trail(CASE["label"], CASE["expected"], final, elapsed)

    _tee.close()
    sys.stdout = _tee._stdout
    print(f"\n📄 Audit trail saved → {LOG_FILE}")

if __name__ == "__main__":
    main()
