"""
Threshold tuning for the SBC signal detector.

Loads the (expanded) normal_corpus.json + labeled attack_prompts.json,
sweeps candidate values of ``Z_SCORE_THRESHOLD``, ``THETA_FLOOR``, and
``WHITELIST_THRESHOLD``, and reports per-signal precision / recall / F1
for each combination — plus the overall accuracy on the LEGAL_NORMAL
class (whitelist gate).

Run locally after ``pip install -r requirements.txt`` (this needs the
sentence-transformers stack which is not present in CI).

  python3 scripts/tune_thresholds.py

What it does:

  1. Loads both embedding models the production detector uses (mukaj
     fin-mpnet + philschmid bge-base) and the per-signal anchors.
  2. Computes ensemble cosine similarity for every (prompt, signal)
     pair — for both the normal corpus and the attack corpus.
  3. For each (Z, floor) pair in the Cartesian product of
     Z_CANDIDATES × FLOOR_CANDIDATES:
       a. Re-derives per-signal thresholds:
            θ_sig = max(mean_noise_sig + Z·std_noise_sig, floor)
       b. Predicts the highest-similarity signal per attack prompt
          (only counts a "hit" if sim ≥ θ).
       c. Confusion matrix vs. expected_violation labels →
          per-signal P / R / F1, plus macro-F1.
     Sweeping the floor is essential: if every per-signal mean+Z·σ is
     below the floor, Z becomes a dead parameter and only the floor
     controls behaviour.
  4. For each candidate ``W`` in {0.45, 0.50, 0.55, 0.60, 0.65}:
       a. Computes max similarity of each attack prompt to the normal
          corpus.
       b. Reports false-positive rate on LEGAL_NORMAL attacks (should
          NOT fire NON_STANDARD_REQUEST) and recall on non-LEGAL
          attacks that have no detectable signal (should fire
          NON_STANDARD_REQUEST as a backstop).
  5. Prints a recommendation: the (Z, floor, W) triple that maximises
     macro-F1 subject to LEGAL_NORMAL false-positive rate ≤ 10 %.

The output is a decision aid, not a config change.  Update
Z_SCORE_THRESHOLD / the floor literal / WHITELIST_THRESHOLD in
app/auditor/signal_detector.py once you've picked a value.

No GPU required, but loading both models takes ~1 GB RAM and ~30 s on
first run (cached afterwards).
"""

from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from typing import Any

import numpy as np

# Add repo root to import path so we can reuse the production embedding
# code (anchor loading, sanitisation) without duplicating it.
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)


_DATA_DIR = os.path.join(_REPO_ROOT, "data")
_NORMAL_PATH = os.path.join(_DATA_DIR, "normal_corpus.json")
_ATTACK_PATH = os.path.join(_DATA_DIR, "attack_prompts.json")
_ANCHORS_PATH = os.path.join(_DATA_DIR, "signal_anchors.json")

# Match the production model pair (see app/auditor/signal_detector.py)
_MODEL_NAMES = (
    "mukaj/fin-mpnet-base",
    "philschmid/bge-base-financial-matryoshka",
)

# Candidate values to sweep
Z_CANDIDATES = (1.5, 1.75, 2.0, 2.25, 2.5, 3.0)
# Hard floor: never let a derived per-signal threshold drop below this
# even if the noise floor is unusually low.  Sweep this jointly with Z —
# when every signal's mean+Z·σ is below the floor (as is currently the
# case in production), the floor is the *only* lever that matters.
FLOOR_CANDIDATES = (0.50, 0.52, 0.54, 0.56, 0.58, 0.60)
W_CANDIDATES = (0.45, 0.50, 0.55, 0.60, 0.65)

# Acceptable false-positive rate on LEGAL_NORMAL attacks for the
# WHITELIST gate.  Tune at your discretion.
MAX_LEGAL_FP_RATE = 0.10


def _sanitize(arr: np.ndarray) -> np.ndarray:
    """Replace nan/inf with 0 — same as production _sanitize_embeddings."""
    return np.nan_to_num(arr, nan=0.0, posinf=0.0, neginf=0.0)


def _load_models():
    from sentence_transformers import SentenceTransformer
    return tuple(SentenceTransformer(name) for name in _MODEL_NAMES)


def _ensemble_encode(texts: list[str], models) -> np.ndarray:
    """Average normalised embeddings from both models — production parity."""
    embs = []
    for m in models:
        e = _sanitize(m.encode(texts, convert_to_numpy=True).astype(np.float64))
        e /= np.linalg.norm(e, axis=1, keepdims=True) + 1e-12
        embs.append(e)
    avg = np.mean(embs, axis=0)
    avg /= np.linalg.norm(avg, axis=1, keepdims=True) + 1e-12
    return avg


def _cos_sim(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """Cosine similarity matrix between row-normalised matrices."""
    return a @ b.T


def _load_corpus() -> tuple[list[str], list[dict[str, Any]], dict[str, list[str]]]:
    with open(_NORMAL_PATH) as f:
        normal = json.load(f)["normal_corpus"]
    with open(_ATTACK_PATH) as f:
        attacks = json.load(f)
    with open(_ANCHORS_PATH) as f:
        anchors_raw = json.load(f)
    # signal_anchors.json nests as {rule_id: {"signals": {signal_name: {"anchors": [...]}}}}.
    # Flatten to {rule_id: [anchor, ...]} so the keys align with attack_prompts.json's
    # `expected_violation` labels (FINRA_2111, SEC_REG_BI, ...). Production keys by
    # inner signal_name instead, but this script scores rule-level detection.
    anchors: dict[str, list[str]] = {}
    for rule_id, rule_def in anchors_raw.items():
        if rule_id.startswith("_") or not isinstance(rule_def, dict):
            continue
        for sig_def in rule_def.get("signals", {}).values():
            anchor_texts = sig_def.get("anchors", [])
            if anchor_texts:
                anchors.setdefault(rule_id, []).extend(anchor_texts)
    return normal, attacks, anchors


def _compute_similarities(
    normal: list[str],
    attacks: list[dict[str, Any]],
    anchors: dict[str, list[str]],
    models,
) -> dict[str, Any]:
    """Encode everything once and return all pairwise sims."""
    print("  encoding normal corpus ...", flush=True)
    e_normal = _ensemble_encode(normal, models)
    print("  encoding attack prompts ...", flush=True)
    attack_texts = [a["prompt"] for a in attacks]
    e_attack = _ensemble_encode(attack_texts, models)
    print("  encoding signal anchors ...", flush=True)
    # For each signal we keep a centroid (mean of its anchor embeddings),
    # matching the production design which picks max(anchor, chunk) per
    # prompt.  Centroid is a robust proxy when sweeping thresholds.
    sig_centroids = {}
    for sig, anchor_list in anchors.items():
        e_anchors = _ensemble_encode(anchor_list, models)
        sig_centroids[sig] = np.mean(e_anchors, axis=0)
        sig_centroids[sig] /= np.linalg.norm(sig_centroids[sig]) + 1e-12

    # normal × signal sims  (used to set per-signal thresholds)
    sim_normal_sig = {
        sig: _cos_sim(e_normal, c[None, :]).flatten()
        for sig, c in sig_centroids.items()
    }
    # attack × signal sims  (used to predict)
    sim_attack_sig = {
        sig: _cos_sim(e_attack, c[None, :]).flatten()
        for sig, c in sig_centroids.items()
    }
    # attack × normal max sim  (used for WHITELIST gate)
    sim_attack_normal_max = _cos_sim(e_attack, e_normal).max(axis=1)

    return {
        "sim_normal_sig": sim_normal_sig,
        "sim_attack_sig": sim_attack_sig,
        "sim_attack_normal_max": sim_attack_normal_max,
        "attack_labels": [a["expected_violation"] for a in attacks],
        "attack_texts": attack_texts,
        "signals": sorted(anchors.keys()),
    }


def _eval_zf(sims: dict[str, Any], z: float, floor: float) -> dict[str, Any]:
    """Sweep one (Z, floor) pair: derive per-signal thresholds, predict, score."""
    signals = sims["signals"]
    sim_n = sims["sim_normal_sig"]
    sim_a = sims["sim_attack_sig"]
    labels = sims["attack_labels"]

    # Derive thresholds (production parity)
    theta = {
        sig: max(float(sim_n[sig].mean() + z * sim_n[sig].std()), floor)
        for sig in signals
    }

    # Predict: signal with highest sim above its threshold (or NONE)
    preds: list[str] = []
    for i in range(len(labels)):
        best_sig = None
        best_margin = -np.inf
        for sig in signals:
            margin = sim_a[sig][i] - theta[sig]
            if margin > 0 and margin > best_margin:
                best_sig = sig
                best_margin = margin
        preds.append(best_sig or "LEGAL_NORMAL")

    # Per-signal P/R/F1 (treat LEGAL_NORMAL as its own class — no signal)
    classes = sorted(set(labels) | {"LEGAL_NORMAL"})
    per_class: dict[str, dict[str, float]] = {}
    for cls in classes:
        tp = sum(1 for p, y in zip(preds, labels) if p == cls and y == cls)
        fp = sum(1 for p, y in zip(preds, labels) if p == cls and y != cls)
        fn = sum(1 for p, y in zip(preds, labels) if p != cls and y == cls)
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        per_class[cls] = {"P": prec, "R": rec, "F1": f1, "n": labels.count(cls)}

    # Macro-F1 excluding LEGAL_NORMAL (we score the signal-detection job)
    sig_classes = [c for c in classes if c != "LEGAL_NORMAL"]
    macro_f1 = float(np.mean([per_class[c]["F1"] for c in sig_classes]))

    return {
        "z": z,
        "floor": floor,
        "theta": theta,
        "per_class": per_class,
        "macro_f1": macro_f1,
        "preds": preds,
    }


def _eval_w(sims: dict[str, Any], w: float) -> dict[str, Any]:
    """Score the WHITELIST gate at a candidate threshold W."""
    labels = sims["attack_labels"]
    max_sim = sims["sim_attack_normal_max"]
    # Prompt is "non-standard" when its max sim to normal corpus < W
    flagged_non_standard = max_sim < w
    legal_idx = [i for i, y in enumerate(labels) if y == "LEGAL_NORMAL"]
    nonlegal_idx = [i for i, y in enumerate(labels) if y != "LEGAL_NORMAL"]
    legal_fp = sum(1 for i in legal_idx if flagged_non_standard[i]) / max(1, len(legal_idx))
    nonlegal_flag = sum(1 for i in nonlegal_idx if flagged_non_standard[i]) / max(1, len(nonlegal_idx))
    return {"w": w, "legal_fp_rate": legal_fp, "nonlegal_flag_rate": nonlegal_flag}


def main() -> None:
    print(f"Loading models {_MODEL_NAMES} ...", flush=True)
    models = _load_models()
    normal, attacks, anchors = _load_corpus()
    print(f"Corpus sizes: normal={len(normal)}  attack={len(attacks)}  signals={len(anchors)}")
    sims = _compute_similarities(normal, attacks, anchors, models)

    # ── Z × floor sweep ──────────────────────────────────────────────
    print("\n=== (Z_SCORE_THRESHOLD × THETA_FLOOR) sweep ===")
    grid = [_eval_zf(sims, z, fl) for z in Z_CANDIDATES for fl in FLOOR_CANDIDATES]

    # Print macro-F1 as a Z × floor grid
    header = "  Z \\ floor  " + "  ".join(f"{fl:>6.2f}" for fl in FLOOR_CANDIDATES)
    print(header)
    print("  " + "-" * (len(header) - 2))
    for z in Z_CANDIDATES:
        row_cells = []
        for fl in FLOOR_CANDIDATES:
            r = next(x for x in grid if x["z"] == z and x["floor"] == fl)
            row_cells.append(f"{r['macro_f1']:>6.3f}")
        print(f"  Z={z:<6.2f}   " + "  ".join(row_cells))

    best_zf = max(grid, key=lambda r: r["macro_f1"])
    print(
        f"\n  Best (Z, floor) by macro-F1: Z={best_zf['z']}, floor={best_zf['floor']:.2f}  "
        f"(F1={best_zf['macro_f1']:.3f})"
    )
    print(f"  Per-signal F1 at best pair:")
    for c in sorted(best_zf["per_class"]):
        if c == "LEGAL_NORMAL":
            continue
        m = best_zf["per_class"][c]
        print(f"    {c:25}  P={m['P']:.2f}  R={m['R']:.2f}  F1={m['F1']:.2f}  n={m['n']}")
    print(f"  Derived thresholds (≥ {best_zf['floor']} floor):")
    for sig, theta in sorted(best_zf["theta"].items()):
        floored = " (floor)" if abs(theta - best_zf["floor"]) < 1e-6 else ""
        print(f"    {sig:25}  θ = {theta:.3f}{floored}")

    # ── W sweep ──────────────────────────────────────────────────────
    print("\n=== WHITELIST_THRESHOLD sweep ===")
    w_results = [_eval_w(sims, w) for w in W_CANDIDATES]
    print(f"{'W':>6} {'legal_FP':>10} {'nonlegal_flag':>16}")
    for r in w_results:
        print(f"  {r['w']:.2f}    {r['legal_fp_rate']:>6.1%}      {r['nonlegal_flag_rate']:>6.1%}")

    acceptable = [r for r in w_results if r["legal_fp_rate"] <= MAX_LEGAL_FP_RATE]
    if acceptable:
        best_w = max(acceptable, key=lambda r: r["nonlegal_flag_rate"])
        print(
            f"\n  Best W: {best_w['w']}  (legal FP {best_w['legal_fp_rate']:.1%} ≤ "
            f"{MAX_LEGAL_FP_RATE:.0%}; flags {best_w['nonlegal_flag_rate']:.1%} of non-legal attacks)"
        )
    else:
        print(f"\n  No W meets legal_FP ≤ {MAX_LEGAL_FP_RATE:.0%} — expand corpus or relax bound.")

    # ── Recommendation ───────────────────────────────────────────────
    print("\n=== Recommendation ===")
    print(f"  Set Z_SCORE_THRESHOLD = {best_zf['z']}  in app/auditor/signal_detector.py")
    print(f"  Set floor literal = {best_zf['floor']}  (max(computed, FLOOR) at line ~124)")
    if acceptable:
        print(f"  Set WHITELIST_THRESHOLD = {best_w['w']}  in app/auditor/signal_detector.py")
    else:
        print("  Leave WHITELIST_THRESHOLD untouched until corpus is expanded.")
    print(
        "\nNote: the production detector chunks each prompt and takes the MAX "
        "anchor sim over (anchors × chunks).  This script uses a centroid + "
        "single-shot sim as a proxy — directionally correct, but absolute F1 "
        "may differ slightly from a full test_bench/test_bench.py run.  Use the picks as "
        "a starting point and validate with test_bench/test_bench.py afterwards."
    )


if __name__ == "__main__":
    main()
