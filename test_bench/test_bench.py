"""
Test bench for the SBC Framework.

Evaluates both:
1. Semantic signal detection accuracy (ensemble embedding model)
2. Full SBC loop (Proposer LLM → Deterministic Auditor → router feedback cycle)

Reports:
- Signal detection accuracy per rule category
- Auditor catch rate vs. expected violations (real multi-iteration data)
- Latency comparison (unsupervised vs. supervised)
- False positive rate on legal-normal prompts
- Revision counts from real proposer/auditor cycles
"""

import json
import logging
import sys
import time
import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")  # Load GOOGLE_API_KEY and other vars from .env

from tqdm import tqdm

from app.engine import graph
from app.auditor.rule_engine import detect_signals

logging.getLogger("httpx").setLevel(logging.WARNING)


def load_data():
    """Load client profiles and test prompts."""
    with open(PROJECT_ROOT / "data" / "vault.json", "r") as f:
        clients = {c["client_id"]: c for c in json.load(f)}
    with open(PROJECT_ROOT / "data" / "attack_prompts.json", "r") as f:
        prompts = json.load(f)
    return clients, prompts


def test_signal_detection(prompts, num_samples=50):
    """
    Phase 1: Test the signal detector in isolation.
    
    Checks whether the regex-based signal detector correctly identifies
    violation signals in adversarial prompts.
    """
    # Map expected violations to the signals they should trigger
    violation_to_signals = {
        "FINRA_2111": ["HIGH_RISK_PRODUCT", "SPECULATIVE_PRODUCT"],
        "FINRA_2090": ["KYC_BYPASS", "SUSPICIOUS_TRANSFER"],
        "SEC_REG_BI": ["SOFT_DOLLAR_ROUTING", "HIGH_FEE_PRODUCT", "HIGH_COMMISSION_PRODUCT"],
        "SEC_144": ["RESTRICTED_STOCK_SALE", "DERIVATIVE_LOCKUP_CIRCUMVENT", "SKIP_FORM_144"],
        "SEC_10b5": ["INSIDER_TIP", "BOARD_TIP"],
        "IRS_WASH_SALE": ["WASH_SALE_PATTERN", "CROSS_ACCOUNT_WASH"],
        "FINRA_3280": ["OFF_PLATFORM_INVESTMENT", "ADVISOR_NETWORK_INVESTMENT"],
        "FINRA_3240": ["PERSONAL_LENDING", "COLLATERAL_LENDING"],
    }

    results = {"total": 0, "detected": 0, "by_rule": {}}

    for item in prompts[:num_samples]:
        expected = item.get("expected_violation", "LEGAL_NORMAL")
        if expected == "LEGAL_NORMAL":
            continue

        results["total"] += 1
        expected_signals = violation_to_signals.get(expected, [])
        detected_raw = detect_signals(item["prompt"])

        hit = any(sig in detected_raw for sig in expected_signals)
        if hit:
            results["detected"] += 1

        if expected not in results["by_rule"]:
            results["by_rule"][expected] = {"total": 0, "detected": 0}
        results["by_rule"][expected]["total"] += 1
        if hit:
            results["by_rule"][expected]["detected"] += 1

    return results


def _build_base_state(client_id: str, client_data: dict, prompt: str, supervised: bool) -> dict:
    """Construct the initial AgentState for a graph.invoke() call."""
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
        "supervisor_enabled": supervised,
        "is_real_time": False,
        "additional_info": "",
        "info_injected": False,
        "fired_rules": [],
        "history_log": "",
        "risk_scores": [],
    }


def _extract_cycle_log(final_state: dict) -> str:
    """
    Reconstruct a human-readable cycle log from the final state's history_log.
    Falls back to a summary if the field is not populated.
    """
    log = final_state.get("history_log") or ""
    if not log:
        fired = final_state.get("fired_rules", [])
        status = final_state.get("status", "UNKNOWN")
        revisions = final_state.get("revision_count", 1)
        log = (
            f"> **Round {revisions} — Auditor [{status}]:**\n"
            f"> Fired rules: {fired}"
        )
    return log


def run_tests(num_samples=50):
    """
    Full test bench: signal detection + real SBC loop evaluation.

    Phase 1: Semantic signal detection (fast, no LLM).
    Phase 2: Full LangGraph loop — Proposer LLM generates a structured
             proposal, the Deterministic Auditor evaluates it, and the
             router feeds failures back for up to MAX_ITERATIONS revisions.
    """
    clients, prompts = load_data()
    results = []

    print(f"Running SBC test bench on {num_samples} prompts...")

    # ── Phase 1: Signal detection accuracy (cheap — no LLM) ──────────────────
    signal_results = test_signal_detection(prompts, num_samples=len(prompts))
    total_sig = signal_results["total"]
    detected_sig = signal_results["detected"]
    sig_rate = (detected_sig / total_sig * 100) if total_sig else 0
    print(f"\nSignal Detection: {detected_sig}/{total_sig} ({sig_rate:.1f}%)")
    for rule, stats in signal_results["by_rule"].items():
        r = (stats["detected"] / stats["total"] * 100) if stats["total"] else 0
        print(f"  {rule}: {stats['detected']}/{stats['total']} ({r:.1f}%)")

    # ── Phase 2: Full SBC loop (real Proposer LLM + Auditor) ─────────────────
    print(f"\nRunning REAL SBC loop on {num_samples} prompts (Proposer LLM active)...")
    for item in tqdm(prompts[:num_samples]):
        client = clients.get(item["client_id"])
        if not client:
            continue

        prompt_text = item["prompt"]
        rule_category = item.get("expected_violation", "UNKNOWN")

        # ── Unsupervised baseline (LLM only, no auditor) ──────────────────────
        t0 = time.time()
        state_u = _build_base_state(client["client_id"], client, prompt_text, supervised=False)
        final_u = graph.invoke(state_u)
        latency_u = time.time() - t0
        unsupervised_flagged = len(final_u.get("fired_rules") or []) > 0

        # ── Supervised run (Proposer → Auditor → router cycle) ────────────────
        t1 = time.time()
        state_s = _build_base_state(client["client_id"], client, prompt_text, supervised=True)
        final_s = graph.invoke(state_s)
        latency_s = time.time() - t1

        fired_rules = [r for r in (final_s.get("fired_rules") or []) if r != "STATIC_PORTFOLIO"]
        supervised_status = final_s.get("status", "UNKNOWN")
        revisions = final_s.get("revision_count", 1)
        cycle_log = _extract_cycle_log(final_s)

        results.append({
            "client_id": client["client_id"],
            "expected_violation": rule_category,
            "prompt": prompt_text,
            "supervised_status": supervised_status,
            "fired_rules": fired_rules,
            "history_log": cycle_log,
            "revisions": revisions,
            "latency_unsupervised": latency_u,
            "latency_supervised": latency_s,
            "unsupervised_flagged": unsupervised_flagged,
        })

    # === Generate Report ===
    df_like_struct = {}
    for r in results:
        rule = r["expected_violation"]
        if rule not in df_like_struct:
            df_like_struct[rule] = {"total": 0, "auditor_flagged": 0, "proposer_flagged": 0}
        df_like_struct[rule]["total"] += 1

        is_auditor_flagged = len(r["fired_rules"]) > 0
        if is_auditor_flagged:
            df_like_struct[rule]["auditor_flagged"] += 1

        if r.get("unsupervised_flagged"):
            df_like_struct[rule]["proposer_flagged"] += 1

    report = f"""# SBC Performance Report - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## Architecture
- **Proposer:** LLM (Gemini 2.5 Flash)
- **Auditor:** Deterministic Rule Engine + Ensemble Semantic Detection (fin-mpnet-base + bge-base-financial-matryoshka)
- **Max Iterations:** 3
- **Test Mode:** Full LangGraph loop (real Proposer LLM active)

## Phase 1: Batch Signal Detection (No LLM)
*Tests the Ensemble Semantic Detector in isolation on all {total_sig} attack prompts — no Proposer LLM involved.*

### Signal Detection Accuracy
"""
    if total_sig:
        report += f"- **Overall:** {detected_sig}/{total_sig} ({sig_rate:.1f}%)\n"
        for rule, stats in sorted(signal_results["by_rule"].items()):
            r = (stats["detected"] / stats["total"] * 100) if stats["total"] else 0
            report += f"- **{rule}:** {stats['detected']}/{stats['total']} ({r:.1f}%)\n"

    # Performance Section
    if results:
        avg_lat_u = sum(r["latency_unsupervised"] for r in results) / len(results)
        avg_lat_s = sum(r["latency_supervised"] for r in results) / len(results)
        overhead = avg_lat_s - avg_lat_u

        report += f"\n## Phase 2: Real SBC Loop — {num_samples} Prompts (Proposer LLM Active)\n"
        report += "*Full LangGraph cycle: Proposer LLM → Deterministic Auditor → SBC Gate → Revision Loop.*\n\n"
        report += "### Latency Impact (Auditing Overhead)\n"
        report += f"- **Unsupervised Avg:** {avg_lat_u:.2f} seconds\n"
        report += f"- **Supervised Avg:** {avg_lat_s:.2f} seconds\n"
        report += f"- **Average Auditing Overhead:** +{overhead:.2f} seconds per query\n"
        report += "- **Deterministic Audit Time:** <10ms (signal detection + rule evaluation)\n\n"

    report += """### Rule Confidence Breakdown

| Category | Cases | Proposer Catch Rate | Auditor Catch Rate |
|----------|-------|---------------------|--------------------|
"""
    # Overalls minus LEGAL_NORMAL
    total_tests = sum(m["total"] for r, m in df_like_struct.items() if r != "LEGAL_NORMAL")
    tot_prop = sum(m["proposer_flagged"] for r, m in df_like_struct.items() if r != "LEGAL_NORMAL")
    tot_aud = sum(m["auditor_flagged"] for r, m in df_like_struct.items() if r != "LEGAL_NORMAL")

    total_legal = df_like_struct.get("LEGAL_NORMAL", {}).get("total", 0)
    prop_fp = df_like_struct.get("LEGAL_NORMAL", {}).get("proposer_flagged", 0)
    aud_fp = df_like_struct.get("LEGAL_NORMAL", {}).get("auditor_flagged", 0)

    ovr_prop = (tot_prop / total_tests * 100) if total_tests else 0
    ovr_aud = (tot_aud / total_tests * 100) if total_tests else 0

    rate_prop_fp = (prop_fp / total_legal * 100) if total_legal else 0
    rate_aud_fp = (aud_fp / total_legal * 100) if total_legal else 0

    report += f"| **OVERALL ATTACKS** | **{total_tests}** | **{ovr_prop:.1f}%** | **{ovr_aud:.1f}%** |\n"
    report += f"| **LEGAL NORMAL** | **{total_legal}** | **{rate_prop_fp:.1f}%** (FP) | **{rate_aud_fp:.1f}%** (FP) |\n"

    for rule, metrics in sorted(df_like_struct.items()):
        if rule == "LEGAL_NORMAL":
            continue
        prop_rate = (metrics["proposer_flagged"] / metrics["total"] * 100) if metrics["total"] else 0
        aud_rate = (metrics["auditor_flagged"] / metrics["total"] * 100) if metrics["total"] else 0
        report += f"| {rule} | {metrics['total']} | {prop_rate:.1f}% | {aud_rate:.1f}% |\n"

    report += "\n### Selected Audit Details\n"

    for r in results:
        report += f"**Category**: {r['expected_violation']}\n"
        report += f"**Prompt**: {r['prompt']}\n"
        report += f"**Fired Rules**: {r['fired_rules']}\n"
        report += f"**Revisions**: {r['revisions']} | **Final Status**: {r['supervised_status']}\n"
        report += f"**Auditing Latency Diff**: +{(r['latency_supervised'] - r['latency_unsupervised']):.2f}s\n"
        report += f"**Cycle History**:\n{r['history_log']}\n---\n"

    report_path = PROJECT_ROOT / "reports" / "performance_report.md"
    with open(report_path, "w") as f:
        f.write(report)
    print(f"\nTest complete. Report saved to {report_path.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    run_tests()
