"""
Threshold tuning for the SBC signal detector.

Loads the (expanded) normal_corpus.json + labeled attack_prompts.json,
sweeps candidate values of ``Z_SCORE_THRESHOLD`` and
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
  3. For each candidate ``Z`` in {1.5, 1.75, 2.0, 2.25, 2.5}:
       a. Re-derives per-signal thresholds:
            θ_sig = max(mean_noise_sig + Z·std_noise_sig, 0.60)
       b. Predicts the highest-similarity signal per attack prompt
          (only counts a "hit" if sim ≥ θ).
       c. Confusion matrix vs. expected_violation labels →
          per-signal P / R / F1, plus macro-F1.
  4. For each candidate ``W`` in {0.45, 0.50, 0.55, 0.60, 0.65}:
       a. Computes max similarity of each attack prompt to the normal
          corpus.
       b. Reports false-positive rate on LEGAL_NORMAL attacks (should
          NOT fire NON_STANDARD_REQUEST) and recall on non-LEGAL
          attacks that have no detectable signal (should fire
          NON_STANDARD_REQUEST as a backstop).
  5. Prints a recommendation: the (Z, W) pair that maximises macro-F1
     subject to LEGAL_NORMAL false-positive rate ≤ 10 %.

The output is a decision aid, not a config change.  Update
Z_SCORE_THRESHOLD / WHITELIST_THRESHOLD in app/auditor/signal_detector.py
once you've picked a value.

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

# Hard floor used by the production detector — never let a derived
# threshold drop below this even if the noise floor is unusually low.
THETA_FLOOR = 0.60

# Candidate values to sweep
Z_CANDIDATES = (1.5, 1.75, 2.0, 2.25, 2.5)
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
    # Drop _meta and any non-list entries
    anchors = {k: v for k, v in anchors_raw.items() if k != "_meta" and isinstance(v, list)}
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


def _eval_z(sims: dict[str, Any], z: float) -> dict[str, Any]:
    """Sweep one Z value: derive per-signal thresholds, predict, score."""
    signals = sims["signals"]
    sim_n = sims["sim_normal_sig"]
    sim_a = sims["sim_attack_sig"]
    labels = sims["attack_labels"]

    # Derive thresholds (production parity)
    theta = {
        sig: max(float(sim_n[sig].mean() + z * sim_n[sig].std()), THETA_FLOOR)
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

    # ── Z sweep ──────────────────────────────────────────────────────
    print("\n=== Z_SCORE_THRESHOLD sweep ===")
    z_results = [_eval_z(sims, z) for z in Z_CANDIDATES]
    print(f"{'Z':>6} {'macro_F1':>10}   per-signal F1")
    for r in z_results:
        per_sig = "  ".join(
            f"{c[:6]}={r['per_class'][c]['F1']:.2f}"
            for c in sorted(r["per_class"])
            if c != "LEGAL_NORMAL"
        )
        print(f"  {r['z']:.2f}    {r['macro_f1']:.3f}    {per_sig}")

    best_z = max(z_results, key=lambda r: r["macro_f1"])
    print(f"\n  Best Z by macro-F1: {best_z['z']}  (F1={best_z['macro_f1']:.3f})")
    print(f"  Derived thresholds (≥ {THETA_FLOOR} floor):")
    for sig, theta in sorted(best_z["theta"].items()):
        print(f"    {sig:25}  θ = {theta:.3f}")

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
    print(f"  Set Z_SCORE_THRESHOLD = {best_z['z']}  in app/auditor/signal_detector.py")
    if acceptable:
        print(f"  Set WHITELIST_THRESHOLD = {best_w['w']}  in app/auditor/signal_detector.py")
    else:
        print("  Leave WHITELIST_THRESHOLD untouched until corpus is expanded.")
    print(
        "\nNote: the production detector chunks each prompt and takes the MAX "
        "anchor sim over (anchors × chunks).  This script uses a centroid + "
        "single-shot sim as a proxy — directionally correct, but absolute F1 "
        "may differ slightly from a full test_bench.py run.  Use the picks as "
        "a starting point and validate with test_bench.py afterwards."
    )


if __name__ == "__main__":
    main()
