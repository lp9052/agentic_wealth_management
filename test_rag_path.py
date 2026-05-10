from dotenv import load_dotenv
load_dotenv()
from app.proposer.agent import create_proposer_llm, generate_proposal

client_data = {
    "profile": {"risk_tolerance": "moderate", "investment_objective": "growth", "liquidity_needs": "low", "time_horizon_years": 10},
    "holdings": {"cash": 100000},
    "account_state": {"status": "active"},
    "relational": {}
}

prompt = "I want to put everything I have into some highly speculative 0DTE options for SPY."

constraint_delta = {
    "failed_rules": ["FINRA_2111"],
    "missing_evidence_ids": ["EVID_SUITABILITY_ACK"],
    "failed_details": [
        {
            "rule_id": "FINRA_2111",
            "clause_id": "c1",
            "severity": "RECOVERABLE",
            "description": "Options trading is not suitable for a moderate risk profile without an explicit waiver.",
            "missing_evidence_id": "EVID_SUITABILITY_ACK"
        }
    ]
}

# Provide the answer in the conversation context directly as if it was in round 2.
new_prompt = prompt + "\n\n[USER UPDATE]: I understand the risk and want to proceed anyway."

print("Running Proposer LLM...")
proposal = generate_proposal(
    client_id="test",
    client_data=client_data,
    prompt=new_prompt,
    constraint_delta=constraint_delta,
    iteration=2,
    llm=create_proposer_llm(),
    previous_proposal_context="asset_ticker: SPY\ninstrument_type: CALL_OPTION\ntrade_size_usd: 100000\n"
)

print("\n--- RESULTS ---")
for ev in proposal.provided_evidence:
    print(f"Evidence ID: {ev.evidence_id}")
    print(f"Scrap: {ev.scrap}")
    print(f"Evidence Path: {ev.evidence_path}")
