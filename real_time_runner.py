import os
import sys
import logging
from dotenv import load_dotenv

# Silence logging for a clean UI
logging.basicConfig(level=logging.ERROR)
logger = logging.getLogger(__name__)

# Add the root directory to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

load_dotenv()

from app.engine import graph
from app.database.client_db import get_client_state, get_client_data

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def print_header(text):
    print(f"\n\033[1;34m{'='*80}\033[0m")
    print(f"\033[1;34m  {text}\033[0m")
    print(f"\033[1;34m{'='*80}\033[0m\n")

def main():
    clear_screen()
    print_header("AGENTIC WEALTH AUDITOR — REAL-TIME SESSION")
    
    # Select Client
    client_id = "CLEAN_RETIREE"
    raw_client_data = get_client_data(client_id)
    client_state = get_client_state(client_id, raw_client_data)
    
    print(f"Active Client Profile: {client_state['profile']['archetype']}")
    print(f"Client ID: {client_id}")
    print(f"Risk Tolerance: {client_state['profile']['risk_tolerance']}")
    print(f"Compliance Status: {client_state['profile']['compliance_history']}")
    print(f"Available Cash: ${client_state['account_state']['total_equity_usd']:,.2f}")
    print("-" * 40)

    # Initial Prompt
    initial_prompt = input("\n\033[1;32mEnter your trade request:\033[0m ")
    
    state = {
        "client_id": client_id,
        "client_data": raw_client_data,
        "prompt": initial_prompt,
        "proposal": "",
        "proposal_json": {},
        "critique": "",
        "constraint_delta": {},
        "status": "PENDING",
        "revision_count": 0,
        "supervisor_enabled": True,
        "is_real_time": True,
        "additional_info": "",
        "info_injected": False,
        "fired_rules": [],
        "history_log": "",
        "risk_scores": [],
    }

    print("\n⏳ Auditor is evaluating your request...")
    
    # Execute Graph
    # The graph now handles its own internal looping and terminal input
    final_state = graph.invoke(state)
    
    # Final Summary
    print_header("SESSION CONCLUDED")
    print(f"Final Status: {final_state['status']}")
    print("-" * 40)
    print("\nFull Audit Trace:")
    print(final_state['history_log'])
    
    if final_state['status'] == "CERTIFIED_COMPLIANT":
        print("\n\033[1;32m✅ TRADE CERTIFIED COMPLIANT. EXECUTION AUTHORIZED.\033[0m")
    else:
        print("\n\033[1;31m❌ TRADE BLOCKED. SESSION ENDED.\033[0m")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSession terminated by user.")
        sys.exit(0)
