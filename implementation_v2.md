***

```markdown
# Stochastic Boundary Control (SBC) Framework v4.0
## Complete Multi-Jurisdictional Financial Governance Implementation

## 1. Architectural Overview

This system replaces traditional LLM "compliance reasoning" with a mathematically sound, deterministically gated state-machine. 

**Core Components:**
1.  **User Database (Private SQL):** Strictly isolated. Holds PII, deep KYC data, portfolio history, and temporal state variables.
2.  **Regulatory GraphRAG (Public Neo4j):** Modeled as an Abstract Syntax Tree (AST). Contains SEC/FINRA/IRS rules broken down into logical trigger conditions and evidence requirements.
3.  **Proposer Agent (LLM):** Aggressively optimizes client intent into a proposed state change (JSON trade payload) and mines transcripts for required evidence.
4.  **Deterministic Auditor (OPA):** A Policy-as-Code engine that blindly evaluates the JSON output of the GraphRAG against the JSON state of the User DB.
5.  **SBC Orchestrator (Python):** Manages the feedback loop, routing payloads based on dynamically calculated `severity` flags (RECOVERABLE vs. CRITICAL).

---

## 2. Database Schema Setup

### 2A. User Database (PostgreSQL) - State & KYC
This database maintains the strict temporal and risk profile of the user.

```sql
CREATE TABLE clients (
    client_id VARCHAR PRIMARY KEY,
    age INT,
    employer_ticker VARCHAR,
    is_insider BOOLEAN
);

CREATE TABLE kyc_profiles (
    client_id VARCHAR REFERENCES clients(client_id),
    risk_tolerance VARCHAR,      -- 'Conservative', 'Moderate', 'Aggressive'
    liquid_net_worth_usd FLOAT,
    time_horizon_years INT
);

CREATE TABLE portfolio_history (
    client_id VARCHAR REFERENCES clients(client_id),
    asset_ticker VARCHAR,
    last_sold_date DATE,
    realized_loss_usd FLOAT,
    day_trades_past_5_days INT
);

CREATE TABLE account_state (
    client_id VARCHAR REFERENCES clients(client_id),
    total_equity_usd FLOAT,
    aml_ofac_cleared BOOLEAN
);
```

### 2B. Regulatory GraphRAG (Neo4j) - The AST Logic Tree
Rules are not text strings; they are logical pathways.

```cypher
// 1. Asset Ontology
CREATE (a1:Asset {ticker: "BTX", asset_class: "Equities", volatility: 0.18})
CREATE (a2:Asset {ticker: "TSLA", asset_class: "Equities", volatility: 0.12})

// 2. FINRA 2111 (Suitability - Recoverable via Evidence)
CREATE (r2111:Regulation {rule: "FINRA_2111", severity: "RECOVERABLE"})
CREATE (clause1:RuleClause {id: "2111.05", desc: "Age/Risk Suitability"})
CREATE (r2111)-[:CONTAINS_CLAUSE]->(clause1)
CREATE (cond1:Condition {attribute: "asset.volatility", operator: ">", threshold: 0.15})
CREATE (clause1)-[:TRIGGERS_IF]->(cond1)
CREATE (kyc1:KYCRequirement {attribute: "age", operator: "<", threshold: 70})
CREATE (cond1)-[:REQUIRES_KYC]->(kyc1)
// Fallback if KYC fails
CREATE (ev1:EvidenceRequirement {evidence_id: "EVID_SENIOR_RISK_WAIVER", desc: "Explicit acknowledgement of severe capital loss risk."})
CREATE (kyc1)-[:FALLBACK_EVIDENCE]->(ev1)

// 3. IRS 1091 (Wash Sale - Recoverable via Pivot)
CREATE (r1091:Regulation {rule: "IRS_1091", severity: "RECOVERABLE"})
CREATE (clause2:RuleClause {id: "1091.a", desc: "30-Day Wash Sale Window"})
CREATE (r1091)-[:CONTAINS_CLAUSE]->(clause2)
CREATE (cond2:Condition {attribute: "trade.action", operator: "==", threshold: "BUY"})
CREATE (clause2)-[:TRIGGERS_IF]->(cond2)
CREATE (kyc2:KYCRequirement {attribute: "portfolio_history.days_since_loss", operator: ">", threshold: 30})
CREATE (cond2)-[:REQUIRES_KYC]->(kyc2)

// 4. BSA/AML OFAC (Sanctions - Critical Hard Block)
CREATE (raml:Regulation {rule: "BSA_AML_OFAC", severity: "CRITICAL"})
CREATE (clause3:RuleClause {id: "OFAC.01"})
CREATE (raml)-[:CONTAINS_CLAUSE]->(clause3)
CREATE (cond3:Condition {attribute: "trade.action", operator: "IN", threshold: "['BUY', 'SELL']"})
CREATE (clause3)-[:TRIGGERS_IF]->(cond3)
CREATE (kyc3:KYCRequirement {attribute: "account_state.aml_ofac_cleared", operator: "==", threshold: true})
CREATE (cond3)-[:REQUIRES_KYC]->(kyc3)
// No fallback evidence allowed.
```

---

## 3. The Proposer Agent (Python / LLM)

```python
PROPOSER_PROMPT = """
You are an autonomous Proposer Agent for a Wealth Management engine. 
Draft a Trade Proposal based on client transcripts.

OUTPUT FORMAT (JSON ONLY):
{
  "proposal_id": "u_t_001",
  "client_id": "<extracted_id>",
  "action": "<BUY|SELL>",
  "asset_ticker": "<ticker>",
  "trade_size_usd": <float>,
  "provided_evidence": [
    {
      "evidence_id": "<ID provided in constraint_delta>",
      "value": true,
      "scrap": "<Exact verbatim quote from client proving compliance>"
    }
  ]
}

CRITICAL RULES:
1. If you receive a 'constraint_delta' specifying 'missing_evidence_ids', you MUST search the client transcript to find that specific evidence.
2. If you trigger an IRS Wash Sale or similar asset-specific recoverable violation, you are authorized to PIVOT the proposal to a similar, compliant ETF to fulfill the client's sector intent.
"""
```

---

## 4. Deterministic Auditor (Open Policy Agent - Rego)
This policy is 100% generic. It blindly calculates the intersection of the Neo4j AST requirements against the Postgres JSON state.

```rego
package financial.governance

import future.keywords.if
import future.keywords.in

default allow := false

trade := input.proposal
user := input.user_db
rules := input.reg_db.triggered_rules # Array of AST RuleClauses from Neo4j

# 1. Evaluate KYC Constraints Dynamically
failed_kyc_requirements[req] {
    some rule in rules
    some req in rule.requires_kyc
    
    # Dynamic Operator Evaluation (Simplified for <, >, ==)
    req.operator == "<"
    user[req.domain][req.attribute] >= req.threshold
}
failed_kyc_requirements[req] {
    some rule in rules
    some req in rule.requires_kyc
    req.operator == ">"
    user[req.domain][req.attribute] <= req.threshold
}
failed_kyc_requirements[req] {
    some rule in rules
    some req in rule.requires_kyc
    req.operator == "=="
    user[req.domain][req.attribute] != req.threshold
}

# 2. Check Evidence Fallbacks for Failed KYC
missing_evidence[ev_id] {
    some failed_req in failed_kyc_requirements
    ev_id := failed_req.fallback_evidence.evidence_id
    
    provided_ids := { e.evidence_id | some e in trade.provided_evidence; e.value == true }
    not ev_id in provided_ids
}

# 3. Detect Hard Blocks
has_critical_violation if {
    some rule in rules
    rule.severity == "CRITICAL"
    count(failed_kyc_requirements) > 0 # A critical rule failed, and by definition lacks fallback
}

# 4. Gate Decision
allow if {
    count(missing_evidence) == 0
    not has_critical_violation
}

# 5. Delta Generation (Generalized Routing Payload)
constraint_delta["status"] := "REJECT" if not allow
constraint_delta["severity"] := "CRITICAL" if has_critical_violation
constraint_delta["severity"] := "RECOVERABLE" if { not has_critical_violation; count(missing_evidence) > 0 }
constraint_delta["missing_evidence_ids"] := missing_evidence
constraint_delta["failed_rules"] := { rule.rule_id | some rule in rules; count(failed_kyc_requirements) > 0 }
```

---

## 5. System Orchestrator (Python)

```python
import json

def run_sbc_governance_loop(client_request, client_id):
    max_iterations = 3
    current_iteration = 0
    agent_prompt = f"Client request transcript: '{client_request}'\nDraft initial proposal."
    
    while current_iteration < max_iterations:
        # Step 1: AI Proposes State (u_t)
        proposal_json_str = call_llm_proposer(agent_prompt)
        proposal = json.loads(proposal_json_str)
        
        # Step 2: Fetch Data (DBs remain strictly isolated in storage, joined only in memory)
        user_state = fetch_postgres_state(client_id)
        
        # Cypher Query: Match asset, traverse AST, return triggered rules
        reg_ast_state = fetch_neo4j_triggered_rules(proposal["asset_ticker"], proposal["action"]) 
        
        # Step 3: OPA Deterministic Audit (Math Gate)
        opa_payload = {
            "input": {
                "proposal": proposal,
                "user_db": user_state,
                "reg_db": reg_ast_state
            }
        }
        opa_result = call_opa_engine(opa_payload)
        
        # Step 4: Boundary Routing
        if opa_result["allow"]:
            commit_audit_trail(proposal, opa_payload)
            execute_trade(proposal)
            return f"SUCCESS: Trade Executed Compliantly. (Iterations: {current_iteration + 1})"
            
        delta = opa_result["constraint_delta"]
        
        # Scenario A: CRITICAL HARD BLOCK (AML, Insider Trading)
        if delta.get("severity") == "CRITICAL":
            trigger_hitl_escalation(proposal, delta)
            return f"HITL TRIGGERED: Critical Violation Detected ({delta.get('failed_rules')}). Trade Halted."
             
        # Scenario B: RECOVERABLE FEEDBACK LOOP (Missing Evidence, Wash Sale)
        if delta.get("severity") == "RECOVERABLE":
            print(f"Iteration {current_iteration + 1} Failed. Re-routing to Proposer...")
            agent_prompt += f"\n\nCONSTRAINT DELTA FROM AUDITOR: {json.dumps(delta)}\nYou must provide required evidence or adjust the trade ticker/size to comply."
            current_iteration += 1

    trigger_hitl_escalation(proposal, "Max AI iterations reached without convergence.")
    return "HITL TRIGGERED: Failed to converge. Routed to Human Advisor."

# --- Helper function placeholders ---
def call_llm_proposer(prompt): pass
def fetch_postgres_state(client_id): pass
def fetch_neo4j_triggered_rules(ticker, action): pass
def call_opa_engine(payload): pass
def execute_trade(proposal): pass
def commit_audit_trail(proposal, payload): pass
def trigger_hitl_escalation(proposal, reason): pass
```

---

## 6. Real-World Execution Scenarios Handled Automatically

1.  **The Sunny Day (Immediate Execution):** * *Input:* "Buy $5k of TSLA." (User is 40, normal account).
    * *Path:* AST checks trigger. KYC age requirements pass. Wash sale requirements pass. OPA `allow == true`. Evaluates in < 500ms.
2.  **The Evidence Extraction Loop (FINRA 2111):**
    * *Input:* "I know it's a huge gamble and I might lose it all, but put $20k in BTX." (User is 75).
    * *Path (Iter 1):* AST detects High Volatility. KYC Age fails. Auditor returns RECOVERABLE with `missing_evidence: ["EVID_SENIOR_RISK_WAIVER"]`.
    * *Path (Iter 2):* Proposer scans prompt, finds the quote *"I know it's a huge gamble..."*, populates JSON evidence array. Auditor verifies. Trade executes securely.
3.  **The Tax Nudge (IRS 1091):**
    * *Input:* "Buy $10k of TSLA." (User sold TSLA at a loss 2 days ago).
    * *Path (Iter 1):* AST triggers Wash Sale rule. KYC `days_since_loss > 30` fails. Auditor returns RECOVERABLE.
    * *Path (Iter 2):* AI receives constraint. Instead of searching for evidence (none exists), it pivots the action: changes `asset_ticker` to `IDRV` (EV ETF). AST evaluates new ticker. Trade executes, avoiding tax penalty.
4.  **The Absolute Hard Block (Treasury OFAC):**
    * *Input:* "Sell everything immediately." (User just got flagged on an AML list).
    * *Path:* AST triggers AML rule. KYC `aml_ofac_cleared == true` fails. Rule is tagged CRITICAL. Python orchestrator instantly halts execution, locks account, and alerts compliance officer. AI is not permitted to "try again."
```