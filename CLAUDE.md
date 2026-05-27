# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

The **Stochastic Boundary Control (SBC) Framework** — a wealth-management trade engine that pairs an LLM Proposer (Gemini 2.5 Flash) with a fully deterministic compliance Auditor. The Proposer drafts trades from a client transcript; the Auditor evaluates them against a regulatory AST + semantic signal detector and either auto-approves, asks for a revision, or escalates to a human. The math gate — not the LLM — is the authority.

## Setup & commands

```bash
# 1. Install deps (Python 3.11+ recommended; numpy 2.0, torch 2.8, langgraph 0.6)
pip install -r requirements.txt

# 2. Provide a Google API key for the Proposer LLM and ChromaDB embeddings
echo "GOOGLE_API_KEY=..." > .env

# 3. Seed the regulatory AST into SQLite (data/rules.db).
#    REQUIRED before any auditor run. Re-run after editing scripts/seed_rule_db.py.
python3 scripts/seed_rule_db.py

# 4. (Optional) Ingest regulations.json into ChromaDB for proposer-side RAG.
#    Only needed if you want the Proposer to receive full regulation text on
#    revision cycles. Without it, the proposer falls back to flat lookup.
python3 -m app.proposer.rag

# Run the FastAPI server
uvicorn app.main:app --reload

# Run the interactive terminal session (one client, multi-turn)
python3 real_time_runner.py

# Run the full benchmark (signal detection accuracy + supervised vs unsupervised
# latency + per-rule catch rate). Writes to reports/performance_report.md.
python3 test_bench/test_bench.py

# Run the canonical case studies → reports/case_study_audit_log.md
python3 test_bench/test_case_studies.py

# Run the unit-test suite. pyproject.toml configures pytest with
# --cov-fail-under=100, so partial runs "fail" on coverage — drop cov flags
# while iterating on a single test.
pytest
pytest tests/test_specific.py --no-cov   # single-file, no coverage gate
```

`pyproject.toml` configures **pytest with 100 %-branch-coverage gating** on `app/`; there is no linter and no CI workflow (`.github/` is absent). `tests/` holds the unit suite. `test_bench/` holds end-to-end benchmarks (`test_bench.py`, `test_case_studies.py`), not unit-test targets — they hit the real Gemini API and take minutes per run. Shrink `test_bench.py` by tweaking `num_samples` in `run_tests()`. Generated reports land in `reports/`.

Regenerating fixture data (rarely needed): `scripts/generate_vault.py` produces `data/vault.json`; `scripts/generate_prompts.py` produces `data/attack_prompts.json`. `scripts/tune_thresholds.py` sweeps `Z_SCORE_THRESHOLD` / `WHITELIST_THRESHOLD` over the normal corpus + attack prompts and emits a decision matrix for re-calibrating the signal detector.

## Architecture

### The SBC loop (`app/engine.py`)

A LangGraph state machine with three nodes — `proposer_node → auditor_node → user_simulator_node` — looping until the gate decides. State is a `TypedDict` (`AgentState`) with 16 fields covering inputs (`client_id`, `client_data`, `prompt`), proposer outputs (`proposal`, `proposal_json`), auditor outputs (`critique`, `constraint_delta`, `status`, `fired_rules`, `risk_scores`), loop control (`revision_count`, `supervisor_enabled`, `is_real_time`), the running `history_log`, and the user-simulator handshake (`additional_info`, `info_injected`). Any new field has to land in **all** initial-state dicts (`app/main.py`, `test_bench/test_bench.py`, `real_time_runner.py`).

Routing is driven by gate constants `SBC_GATE_AUTO = 0.20` and `SBC_GATE_ESCALATE = 0.80` in `risk_scoring.py`:
- `< 0.20` → `AUTO_APPROVE` (trade executes, graph ends)
- `[0.20, 0.80)` → `REFINEMENT` (loop back to proposer with constraint delta)
- `≥ 0.80` → `HUMAN_ESCALATION` (hard block, graph ends)

`MAX_ITERATIONS = 6` in `engine.py` is the convergence safety valve. **Every round increments `revision_count`** — REVIEW (proposer asking a clarifying question), REFINEMENT, and the initial proposal all count equally. The code comment at the top of `engine.py` claims REVIEW rounds don't count, but the implementation increments `revision_count` unconditionally in `proposer_node`; treat the implementation as the source of truth and budget your conversations accordingly.

The single LLM instance is cached in `_llm` (module global, lazy-initialized) — don't create new `ChatGoogleGenerativeAI` instances per request.

### The Proposer (`app/proposer/`)

- `agent.py` — `generate_proposal()` calls Gemini with `with_structured_output(TradeProposalSchema)` (Pydantic, tool-calling under the hood). `generate_proposal_freeform()` is the unsupervised baseline used by `test_bench/test_bench.py`.
- `prompts.py` — All system prompts. Heavy with behavioral rules around evidence handling, the difference between `REVIEW` and `BUY`/`SELL`, and how to preserve `asset_ticker` + `instrument_type` across revisions. Modify here, not in `agent.py`.
- `rag.py` — ChromaDB-backed RAG that injects full regulation text into the system prompt on revision cycles. Falls back to flat `regulations.json` lookup if Chroma is uninitialized.

The `TradeProposalSchema.asset_ticker` field has a `_extract_underlying_ticker` validator that strips garbage like "SPY call options" → "SPY". If the LLM sends a multi-word value, the validator finds the first valid ticker token. Don't bypass this — bad tickers cause `STATIC_PORTFOLIO` CRITICAL blocks downstream.

### The Auditor (`app/auditor/`)

Pure-logic pipeline, **no LLM calls**. `evaluate_proposal()` in `rule_engine.py` is the entry point.

Typo correction happens upstream — `proposer_node` calls `typo_filter.correct_typos` (SymSpell with seeded financial-domain vocabulary) on `state["prompt"]` *before* the prompt ever reaches `evaluate_proposal`, so everything below operates on the corrected text.

`evaluate_proposal` pipeline (in order):

1. **Static checks** (`run_static_checks`) — Account/KYC/AML status, invalid action, invalid ticker, negative trade size, trade size > $10M ceiling, insufficient funds (BUY), insufficient holdings (SELL), >50% concentration (BUY). All emitted as `STATIC_PORTFOLIO` failures.
2. **Semantic signal detection** (`signal_detector.detect_signals`) — Dual-model ensemble (`mukaj/fin-mpnet-base` + `philschmid/bge-base-financial-matryoshka`) over sentence-chunked input. Per-signal Z-score thresholds are computed at init from the normal-corpus noise floor (`Z_SCORE_THRESHOLD = 2.0`, minimum 0.60). If no blacklist signal fires, a whitelist gate compares against `normal_corpus.json`; below `WHITELIST_THRESHOLD = 0.55` the prompt gets a `NON_STANDARD_REQUEST` flag.
3. **Per-ticker signal suppression** (`ticker_config.json`) — Whitelisted tickers (SPY, VOO, AAPL, etc.) suppress noisy signals like `HIGH_RISK_PRODUCT` / `SPECULATIVE_PRODUCT`. Suppression is **skipped** when the proposal's `instrument_type` is a derivative type OR the prompt contains derivative markers (`option`, `call`, `put`, `strike`, `expir`, `0dte`, `leveraged`, `inverse`, `3x`/`2x`/`1x`, `contract`, `collar`, `hedge`, `short`) — buying SPY calls is still risky.
4. **Cascading adjacency** — When a signal fires, related rules from `regulations.json.metadata.related` are force-evaluated even if their own trigger condition didn't match. Second-level cascades fire when those forced rules themselves fail.
5. **Rule evaluation** — Walks `Regulation → RuleClause → TriggerCondition → KYCRequirement` (loaded once from SQLite via `rule_registry.get_regulations()`). KYC domain `proposal_check` means "intent-based, always fails when triggered"; `portfolio_check` is handled entirely by static checks; everything else looks up fields in the normalized client state.
6. **Continuous evidence scoring** — For each `missing_evidence_id`, semantic similarity between the LLM-provided `scrap` and the canonical evidence description gives `C_ev ∈ [0, 1]`. The scrap must be grounded in the original prompt (substring match) or `C_ev = 0`. Evidence IDs ending in `_ACK` get a fast-path: any affirmative keyword ("yes", "agree", "acknowledge", ...) scores 1.0.
7. **Composite risk score** (`risk_scoring.compute_audit_risk`) — Per-rule `R_i = TSF(S_norm) · (1 − C_ev_i) · ω_i`; combined via complement-product `R = 1 − ∏(1 − R_i)`. CRITICAL rules bypass TSF (always 1.0) so trade size doesn't soften them. RECOVERABLE rules use `TSF = 1 − e^(−λ·S_norm)` with `λ = 5` (responsive across the 0.5 %–50 % portfolio band; see `risk_scoring.LAMBDA_TRADE_SIZE_SCALING` for the operating-band rationale). Per-rule weights live in `DEFAULT_RULE_WEIGHTS` — CRITICAL weights are ≥ 0.85 so they always escalate.

### Data layer (`app/database/`, `data/`)

- `data/rules.db` (SQLite) — The regulatory AST. Schema in `schema.py`, loaded into in-memory dataclasses by `rule_db.load_all_regulations()` and cached in `rule_registry`. **Regenerate via `scripts/seed_rule_db.py` whenever the rule tree changes.** The schema in `schema.py` and the seed data in `seed_rule_db.py` are the source of truth; the SQLite file is a build artifact.
- `data/vault.json` — Client profiles (KYC, holdings, relational map). `client_db.get_client_state()` normalizes raw vault data into `{profile, holdings, account_state, relational}` — the auditor only ever sees the normalized form.
- `data/regulations.json` — Plain regulation text + adjacency graph (`metadata.related`). Used by the Proposer's RAG and by the auditor's cascading-audit setup. Adjacency is loaded **exclusively** from this file (no hardcoded fallback) — if the file is missing or malformed, the auditor crashes loudly.
- `data/signal_anchors.json` — Anchor texts per signal (used to build embedding centroids at init).
- `data/normal_corpus.json` — "Innocent" prompts that define the noise floor for Z-score thresholds and serve as the whitelist gate.
- `data/ticker_config.json` — Per-ticker suppress lists + global derivative markers. Both `app/auditor/rule_engine.py` and the signal pipeline read from here.
- `data/attack_prompts.json` — Labeled adversarial prompts used by `test_bench/test_bench.py`.

### State key conventions

`AgentState` (in `engine.py`) is the only contract between nodes. New fields must be added to `AgentState`, the initial state dicts in `app/main.py` / `test_bench/test_bench.py` / `real_time_runner.py`, and any places that `state.get()` them. The `history_log` field accumulates a human-readable trace across all rounds; the `risk_scores` field accumulates `SBCRiskScore.to_dict()` per iteration for the audit trail.

## Conventions worth knowing

- **No silent fallbacks for missing config.** Missing `ticker_config.json` or `regulations.json` crashes loudly — these are deployment errors, not runtime conditions. Don't add try/except wrappers around their loads.
- **Pure dataclasses for auditor models, Pydantic for LLM I/O.** `app/auditor/models.py` is plain `@dataclass` (mutable, fast, no validation overhead). The Proposer's input/output uses Pydantic (`TradeProposalSchema`) so it can drive Gemini's structured-output API.
- **Ticker validator is load-bearing.** The regex-based extractor in `TradeProposalSchema._extract_underlying_ticker` prevents the LLM's "SPY call options" outputs from getting blocked as invalid tickers. If you change it, also update `instrument_type` handling in `auditor_node` and the derivative-marker check in `evaluate_proposal`.
- **`REVIEW` ≠ violation, but it still gets audited.** When the proposer can't proceed without more info from the user, it emits `action="REVIEW"` with a question in `user_question`. The auditor *always* runs (the router never skips it — `router_node` always returns `auditor_node`), records a score for the audit trail, and `auditor_node` then **rewrites `CERTIFIED_COMPLIANT` → `NEEDS_REVISION` on REVIEW rounds** so a clean question doesn't terminate the session before the user has answered. `HUMAN_ESCALATION` on a REVIEW round (e.g., the user's question contains an `INSIDER_TIP` signal) still hard-blocks. In real-time mode (`is_real_time=True`) the auditor additionally prints the critique to the terminal.
- **CRITICAL rule weights ≥ `SBC_GATE_ESCALATE`.** Don't lower weights for CRITICAL rules in `DEFAULT_RULE_WEIGHTS` below 0.80 — they must always trip the human-escalation gate. Same for `STATIC_PORTFOLIO` when it carries CRITICAL failures.
- **One LLM, one shared instance.** `engine._get_llm()` caches the proposer LLM at module level. Don't instantiate `ChatGoogleGenerativeAI` per node call.
- **`fired_rules` filtering in test_bench.** `test_bench/test_bench.py` strips `STATIC_PORTFOLIO` from `fired_rules` when computing rule-category catch rates — it's noise for benchmarking regulatory detection, but a real failure for end users. Keep them in the audit trail; only filter in metrics aggregation.
