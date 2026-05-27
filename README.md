# Stochastic Boundary Control (SBC) Framework

An agentic wealth-management trade engine that pairs an **LLM Proposer** (Gemini
2.5 Flash) with a **fully deterministic compliance Auditor**. The Proposer
drafts trades from a client transcript; the Auditor evaluates them against a
regulatory AST and a semantic signal detector, then either auto-approves, asks
for a revision, or escalates to a human.

The math gate — not the LLM — is the authority.

## How it works

The system runs as a LangGraph state machine with three nodes:

```
proposer_node → auditor_node → user_simulator_node ──┐
       ▲                                              │
       └────────────── refinement loop ───────────────┘
```

Each audit produces a composite risk score in `[0, 1]`, which drives the gate:

| Composite score | Decision            | Behavior                                     |
| --------------- | ------------------- | -------------------------------------------- |
| `< 0.20`        | `AUTO_APPROVE`      | Trade executes; graph terminates.            |
| `[0.20, 0.80)`  | `REFINEMENT`        | Loop back to the proposer with a delta.      |
| `≥ 0.80`        | `HUMAN_ESCALATION`  | Hard block; graph terminates.                |

A safety valve (`MAX_ITERATIONS = 6`) caps revision cycles. Clarifying
`REVIEW` rounds (the proposer asking the user a question) do **not** consume
iterations — only `BUY`/`SELL`/`HOLD` proposals do.

## Components

### Proposer (`app/proposer/`)
- `agent.py` — Calls Gemini with `with_structured_output(TradeProposalSchema)`.
- `prompts.py` — All system prompts (evidence handling, `REVIEW` vs `BUY/SELL`,
  ticker preservation across revisions).
- `rag.py` — ChromaDB-backed RAG that injects full regulation text into the
  system prompt on revision cycles; falls back to flat lookup if Chroma is
  uninitialized.

### Auditor (`app/auditor/`)
Pure-logic, **no LLM calls**. `rule_engine.evaluate_proposal()` runs:

1. **Static checks** — Account/KYC/AML status, ticker validity, trade size
   sanity, insufficient funds (BUY), insufficient holdings (SELL), >50%
   concentration.
2. **Typo filter** — SymSpell with a financial-domain vocabulary.
3. **Semantic signal detection** — Dual-model ensemble
   (`mukaj/fin-mpnet-base` + `philschmid/bge-base-financial-matryoshka`) over
   sentence-chunked input, with per-signal Z-score thresholds derived from a
   normal-corpus noise floor. A whitelist gate flags `NON_STANDARD_REQUEST`
   when no blacklist signal fires.
4. **Per-ticker signal suppression** — Whitelisted tickers (SPY, VOO, AAPL,
   …) suppress noisy signals, except when derivative markers (`option`,
   `call`, `put`, `0dte`, …) are present.
5. **Cascading adjacency** — Fired signals force-evaluate related rules via
   `regulations.json.metadata.related`.
6. **Rule evaluation** — Walks `Regulation → RuleClause → TriggerCondition →
   KYCRequirement` loaded once from SQLite.
7. **Continuous evidence scoring** — Semantic similarity between the LLM's
   evidence scrap and the canonical evidence description, gated by grounding
   in the original prompt.
8. **Composite risk score** — Per-rule `R_i = TSF(S_norm) · (1 − C_ev_i) · ω_i`,
   combined via complement-product `R = 1 − ∏(1 − R_i)`. CRITICAL rules bypass
   TSF; RECOVERABLE rules use `TSF = 1 − e^(−λ·S_norm)` with `λ = 5`.

### Data layer (`app/database/`, `data/`)
- `data/rules.db` — Regulatory AST (SQLite). Build artifact; regenerate via
  `scripts/seed_rule_db.py` whenever the rule tree changes.
- `data/vault.json` — Client profiles (KYC, holdings, relational map),
  normalized by `client_db.get_client_state()`.
- `data/regulations.json` — Plain regulation text and the adjacency graph
  used for cascading audits and proposer RAG.
- `data/signal_anchors.json` — Anchor texts per signal (build embedding
  centroids at init).
- `data/normal_corpus.json` — "Innocent" prompts defining the noise floor
  and whitelist gate.
- `data/ticker_config.json` — Per-ticker suppress lists and global derivative
  markers.
- `data/attack_prompts.json` — Labeled adversarial prompts for `test_bench.py`.

## Setup

Requires Python 3.11+ (numpy 2.0, torch 2.8, langgraph 0.6).

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure the Proposer LLM and ChromaDB embeddings
echo "GOOGLE_API_KEY=..." > .env

# 3. Seed the regulatory AST into SQLite (REQUIRED before any auditor run).
#    Re-run after editing scripts/seed_rule_db.py.
python3 scripts/seed_rule_db.py

# 4. (Optional) Ingest regulations.json into ChromaDB so the Proposer gets
#    full regulation text on revision cycles. Without it the proposer falls
#    back to flat lookup.
python3 -m app.proposer.rag
```

## Running

```bash
# FastAPI server
uvicorn app.main:app --reload

# Interactive terminal session (one client, multi-turn)
python3 real_time_runner.py

# End-to-end benchmark: signal accuracy + supervised vs unsupervised latency
# + per-rule catch rate. Hits the real Gemini API; writes performance_report.md.
# Shrink runs by tweaking num_samples in run_tests().
python3 test_bench.py
```

There is no test runner, linter, or CI configured. `test_bench.py` is an
end-to-end benchmark, not a unit-test suite.

To regenerate fixture data (rarely needed):

```bash
python3 scripts/generate_vault.py     # → data/vault.json
python3 scripts/generate_prompts.py   # → data/attack_prompts.json
```

## State contract

`AgentState` in `app/engine.py` is the only contract between nodes. It carries:
the prompt, structured proposal, constraint delta, fired rules, per-iteration
risk scores, and a human-readable `history_log`. New state fields must be
added to `AgentState` and to the initial state dicts in `app/main.py`,
`test_bench.py`, and `real_time_runner.py`.

## Conventions

- **No silent fallbacks for missing config.** Missing `ticker_config.json` or
  `regulations.json` crashes loudly — these are deployment errors, not
  runtime conditions.
- **Pure dataclasses for auditor models, Pydantic for LLM I/O.** The Proposer
  uses Pydantic (`TradeProposalSchema`) to drive Gemini's structured-output
  API; the auditor uses plain `@dataclass` for speed and mutability.
- **Ticker validator is load-bearing.** The regex extractor in
  `TradeProposalSchema._extract_underlying_ticker` strips "SPY call options"
  to "SPY" before the auditor sees it.
- **`REVIEW` ≠ violation.** The auditor still records a score for the audit
  trail, but `REVIEW` rounds don't consume iterations. Real-time mode skips
  the auditor for `REVIEW` rounds to avoid double-printing.
- **CRITICAL rule weights ≥ `SBC_GATE_ESCALATE`.** CRITICAL rules in
  `DEFAULT_RULE_WEIGHTS` must always trip human escalation; do not lower
  their weights below 0.80.
- **One LLM, one shared instance.** `engine._get_llm()` caches the Proposer
  LLM at module level — don't instantiate `ChatGoogleGenerativeAI` per node.

## Design doc

`implementation_v2.md` is the original design document. The runtime has
diverged (SQLite instead of Postgres/Neo4j, a Python rule engine instead of
OPA/Rego, score-driven gates instead of severity tags) — trust the code.
