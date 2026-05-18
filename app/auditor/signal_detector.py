"""
Semantic Signal Detector — invoked by rule_engine.evaluate_proposal()
after static checks and before per-ticker suppression.

Internal flow:
  * Text chunker           — sentence-split the globally corrected prompt
  * Ensemble embedding     — fin-mpnet-base + bge-base-financial-matryoshka
  * Embedding sanitization — NaN/inf guard + L2 normalization
  * Blacklist check        — cosine sim vs. signal anchors w/ Z-score threshold
  * Whitelist check        — normal corpus gate: low similarity → NON_STANDARD_REQUEST
  * Return (signal_name, score) pairs sorted by score desc

The downstream SBC risk score (Trade Size Factor) attenuates the risk of
NON_STANDARD_REQUEST for small trades, so we don't need any "soft cascade"
limiting here.  A single whitelist threshold (0.55) is used.
"""

import json
import logging
import os
import re
import numpy as np
from typing import Optional

logger = logging.getLogger(__name__)

# Increased epsilon for numerical stability on M1/M2 Apple Silicon MPS
EPSILON = 1e-6

_model1 = None
_model2 = None
_anchor_embeddings: Optional[dict[str, tuple[np.ndarray, np.ndarray]]] = None
_signal_thresholds: Optional[dict[str, float]] = None
_normal_embeddings: Optional[tuple[np.ndarray, np.ndarray]] = None

_ANCHORS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "signal_anchors.json"
)
_NORMAL_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "data", "normal_corpus.json"
)

# Statistical boundary (Z-Score) used to set per-signal detection thresholds
Z_SCORE_THRESHOLD: float = 2.0

# ── Whitelist gate threshold ────────────────────────────────────────────────
WHITELIST_THRESHOLD: float = 0.55


def _get_models():
    """Lazy-load the Ensemble Models (loaded once, reused for all calls)."""
    global _model1, _model2
    if _model1 is None or _model2 is None:
        logger.info("Loading Ensemble Models (fin-mpnet-base & bge-base-financial-matryoshka)...")
        from sentence_transformers import SentenceTransformer
        _model1 = SentenceTransformer("mukaj/fin-mpnet-base", device="cpu")
        _model2 = SentenceTransformer("philschmid/bge-base-financial-matryoshka", device="cpu")
    return _model1, _model2


def _initialize_system():
    """
    Build anchor embeddings and compute per-signal Z-score thresholds.
    Called once; results are cached in module-level globals.
    """
    global _anchor_embeddings, _signal_thresholds, _normal_embeddings
    if (
        _anchor_embeddings is not None
        and _signal_thresholds is not None
        and _normal_embeddings is not None
    ):
        return

    model1, model2 = _get_models()
    anchors_path = os.path.normpath(_ANCHORS_PATH)

    with open(anchors_path, "r") as f:
        raw = json.load(f)

    # Build per-signal anchor matrices (one per signal name)
    _anchor_embeddings = {}
    for rule_id, rule_def in raw.items():
        if rule_id.startswith("_"):
            continue
        for signal_name, signal_def in rule_def.get("signals", {}).items():
            anchor_texts = signal_def.get("anchors", [])
            if anchor_texts:
                _anchor_embeddings[signal_name] = (
                    _sanitize_embeddings(model1.encode(anchor_texts).astype(np.float64)),
                    _sanitize_embeddings(model2.encode(anchor_texts).astype(np.float64)),
                )

    # Build normal corpus embeddings (used for whitelist gate)
    with open(os.path.normpath(_NORMAL_PATH), "r") as f:
        normal_data = json.load(f)
    normal_corpus = normal_data.get("normal_corpus", [])
    _normal_embeddings = (
        _sanitize_embeddings(model1.encode(normal_corpus).astype(np.float64)),
        _sanitize_embeddings(model2.encode(normal_corpus).astype(np.float64)),
    )

    # Compute per-signal statistical thresholds from normal corpus noise floor
    _signal_thresholds = {}
    n1 = _normal_embeddings[0].astype(np.float64)
    n2 = _normal_embeddings[1].astype(np.float64)

    for signal_name, (embs1, embs2) in _anchor_embeddings.items():
        sims1 = n1 @ embs1.astype(np.float64).T
        sims2 = n2 @ embs2.astype(np.float64).T
        avg_noise = (sims1 + sims2) / 2.0
        max_noise = np.max(avg_noise, axis=1)
        mean_noise = float(np.mean(max_noise))
        std_noise = float(np.std(max_noise))
        computed = mean_noise + (Z_SCORE_THRESHOLD * std_noise)
        _signal_thresholds[signal_name] = max(computed, 0.60)

    logger.info(
        "Signal thresholds computed (Z=%.1f) for %d signals using Ensemble.",
        Z_SCORE_THRESHOLD,
        len(_signal_thresholds),
    )


def _sanitize_embeddings(embs: np.ndarray) -> np.ndarray:
    """
    Ensure embeddings are finite and unit-normalized.

    Handles NaN/inf produced by Apple Silicon MPS acceleration.
    Zero-vectors (all-NaN inputs) are kept as zero rather than dividing by near-zero.
    """
    embs = np.nan_to_num(embs.astype(np.float64), nan=0.0, posinf=0.0, neginf=0.0)
    norms = np.linalg.norm(embs, axis=-1, keepdims=True)
    safe_mask = (norms > 1e-12).astype(np.float64)
    return (embs * safe_mask) / (norms + (1.0 - safe_mask))


def detect_signals_semantic(prompt: str) -> list[tuple[str, float]]:
    """
    Full signal detection pipeline.  Returns (signal_name, score) pairs.

    Steps performed here:
      1. Typo pre-filter  (correct_typos — imported from typo_filter)
      2. Text chunker     (sentence split → embed whole prompt + each sentence)
      3. Ensemble embed   (model1 + model2, averaged)
      4. Sanitize         (_sanitize_embeddings)
      5. Blacklist check  (per-signal Z-score threshold)
      6. Whitelist check  (vs single WHITELIST_THRESHOLD)
    """
    # ── Initialize models ────────────────────────────────────────────────────
    _initialize_system()
    model1, model2 = _get_models()

    # ── Text chunker ─────────────────────────────────────────────────────────
    # Split on sentence boundaries; embed the full prompt AND each sentence.
    # This catches violations hidden in subordinate clauses.
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", prompt) if s.strip()]
    chunks_to_embed = [prompt] + sentences

    # ── Steps 3 + 4: Ensemble embedding + sanitization ──────────────────────
    prompt_embs1 = _sanitize_embeddings(model1.encode(chunks_to_embed).astype(np.float64))
    prompt_embs2 = _sanitize_embeddings(model2.encode(chunks_to_embed).astype(np.float64))

    fired_dict: dict[str, float] = {}

    # ── Blacklist check ──────────────────────────────────────────────────────
    # For each signal, compute ensemble cosine similarity vs. all anchor texts.
    # Fire if max similarity across (anchors × chunks) exceeds the Z-score threshold.
    for signal_name, (anchor_m1, anchor_m2) in _anchor_embeddings.items():
        m1 = _sanitize_embeddings(anchor_m1.astype(np.float64))
        m2 = _sanitize_embeddings(anchor_m2.astype(np.float64))

        with np.errstate(all="ignore"):
            sims1 = np.clip(m1 @ prompt_embs1.T, -1.0, 1.0)
            sims2 = np.clip(m2 @ prompt_embs2.T, -1.0, 1.0)
            avg_similarities = (sims1 + sims2) / 2.0

        max_sim = float(np.max(avg_similarities))
        threshold = _signal_thresholds.get(signal_name, 1.0)
        if np.isnan(threshold):
            threshold = 1.0

        if max_sim >= threshold:
            if signal_name not in fired_dict or max_sim > fired_dict[signal_name]:
                fired_dict[signal_name] = max_sim

    # ── Whitelist check ──────────────────────────────────────────────────────
    # Only run if the blacklist found nothing — "innocent until proven suspicious."
    if not fired_dict:
        n1 = _normal_embeddings[0]
        n2 = _normal_embeddings[1]

        with np.errstate(all="ignore"):
            sims1 = np.clip(n1 @ prompt_embs1[0], -1.0, 1.0)
            sims2 = np.clip(n2 @ prompt_embs2[0], -1.0, 1.0)
            avg_normal_sims = (sims1 + sims2) / 2.0
            max_normal_sim = float(np.max(avg_normal_sims))

        if np.isnan(max_normal_sim):
            max_normal_sim = 0.0

        if max_normal_sim < WHITELIST_THRESHOLD:
            logger.info(
                "Prompt failed whitelist (sim=%.3f < %.2f). "
                "Flagging NON_STANDARD_REQUEST.",
                max_normal_sim, WHITELIST_THRESHOLD,
            )
            fired_dict["NON_STANDARD_REQUEST"] = max_normal_sim

    fired = sorted(fired_dict.items(), key=lambda x: x[1], reverse=True)
    return fired


def detect_signals(prompt: str) -> list[str]:
    """Return only the signal names (no scores) for callers that need a simple list."""
    return [name for name, _ in detect_signals_semantic(prompt)]


def get_embedding_similarity(text1: str, text2: str) -> float:
    """Compute ensemble cosine similarity between two arbitrary strings."""
    model1, model2 = _get_models()

    emb1_m1 = _sanitize_embeddings(model1.encode([text1]).astype(np.float64))[0]
    emb2_m1 = _sanitize_embeddings(model1.encode([text2]).astype(np.float64))[0]
    emb1_m2 = _sanitize_embeddings(model2.encode([text1]).astype(np.float64))[0]
    emb2_m2 = _sanitize_embeddings(model2.encode([text2]).astype(np.float64))[0]

    with np.errstate(all="ignore"):
        sim1 = float(np.clip(np.dot(emb1_m1, emb2_m1), -1.0, 1.0))
        sim2 = float(np.clip(np.dot(emb1_m2, emb2_m2), -1.0, 1.0))

    return (sim1 + sim2) / 2.0
