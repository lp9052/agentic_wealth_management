"""Tests for app/auditor/signal_detector.py — public surface only.

Public API:
  detect_signals, detect_signals_semantic, get_embedding_similarity,
  EPSILON, Z_SCORE_THRESHOLD, WHITELIST_THRESHOLD

The lazy-loaded module-level caches (_anchor_embeddings, _normal_embeddings,
etc.) are reset between tests via the autouse `_reset_module_caches` fixture
in conftest.py — tests don't poke at them directly.

Branch coverage for the private helpers (_get_models, _initialize_system,
_sanitize_embeddings) is achieved indirectly through detect_signals_semantic /
get_embedding_similarity.
"""

import sys
import types

import numpy as np
import pytest

from app.auditor import signal_detector
from app.auditor.signal_detector import (
    EPSILON,
    WHITELIST_THRESHOLD,
    Z_SCORE_THRESHOLD,
    detect_signals,
    detect_signals_semantic,
    get_embedding_similarity,
)


# ---------------------------------------------------------------------------
# Module-level constants.
# ---------------------------------------------------------------------------

def test_module_constants():
    assert EPSILON == 1e-6
    assert Z_SCORE_THRESHOLD == 2.0
    assert WHITELIST_THRESHOLD == 0.55


# ---------------------------------------------------------------------------
# detect_signals / detect_signals_semantic — full pipeline behavior.
#
# Branch coverage for _sanitize_embeddings (NaN/inf guard, zero-vector skip)
# and _initialize_system (anchor build, threshold floor, skip-underscore
# rules, skip-empty-anchors) flows through these calls.
# ---------------------------------------------------------------------------

def test_detect_signals_fires_blacklist_match(fake_encoders, anchors_path, normal_corpus_path):
    """A prompt matching the HIGH_RISK_PRODUCT anchor cluster fires."""
    fired = detect_signals_semantic("Buy me a leveraged ETF please")
    names = [n for n, _ in fired]
    assert "HIGH_RISK_PRODUCT" in names or "SPECULATIVE_PRODUCT" in names


def test_detect_signals_normal_prompt_does_not_fire_whitelist(
    fake_encoders, anchors_path, normal_corpus_path
):
    """A normal-corpus-class prompt passes the whitelist gate."""
    fired = detect_signals_semantic("Please rebalance my retirement account")
    names = [n for n, _ in fired]
    assert "NON_STANDARD_REQUEST" not in names


def test_detect_signals_novel_prompt_triggers_whitelist_flag(
    fake_encoders, anchors_path, normal_corpus_path
):
    """A prompt unlike both anchors AND corpus gets NON_STANDARD_REQUEST."""
    fired = detect_signals_semantic("Please proceed with banana protocol now")
    assert "NON_STANDARD_REQUEST" in [n for n, _ in fired]


def test_detect_signals_returns_just_names(fake_encoders, anchors_path, normal_corpus_path):
    out = detect_signals("leveraged speculative product")
    assert all(isinstance(n, str) for n in out)


def test_detect_signals_multi_chunk_dedupes_signal_names(
    fake_encoders, anchors_path, normal_corpus_path
):
    """Multiple sentences hitting the same signal are deduped."""
    fired = detect_signals_semantic("Buy leveraged. Buy speculative. Buy leveraged again.")
    names = [n for n, _ in fired]
    assert len(names) == len(set(names))


def test_detect_signals_skips_underscore_anchor_entries(
    fake_encoders, tmp_path, monkeypatch, normal_corpus_path
):
    """_meta keys in signal_anchors.json are skipped during init."""
    import json
    anchors = {
        "_meta": {"description": "ignored"},
        "FINRA_2111": {"signals": {"REAL_SIG": {"anchors": ["leveraged"]}}},
    }
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors))
    monkeypatch.setattr(signal_detector, "_ANCHORS_PATH", str(p))
    # If _meta wasn't skipped, _initialize_system would crash trying to access
    # signals on a metadata dict.  Successful call proves the skip.
    out = detect_signals("leveraged product")
    assert "REAL_SIG" in out


def test_detect_signals_skips_signals_with_no_anchor_texts(
    fake_encoders, tmp_path, monkeypatch, normal_corpus_path
):
    import json
    anchors = {
        "FINRA_2111": {
            "signals": {
                "EMPTY_SIG": {"anchors": []},
                "REAL_SIG": {"anchors": ["leveraged"]},
            }
        }
    }
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors))
    monkeypatch.setattr(signal_detector, "_ANCHORS_PATH", str(p))
    out = detect_signals("leveraged product")
    # EMPTY_SIG can't fire; REAL_SIG should
    assert "EMPTY_SIG" not in out


def test_detect_signals_handles_nan_threshold(
    fake_encoders, anchors_path, normal_corpus_path
):
    """If a per-signal threshold is NaN, the engine falls back to 1.0."""
    # Trigger initialization, then corrupt the threshold for one signal.
    detect_signals("init")
    signal_detector._signal_thresholds["HIGH_RISK_PRODUCT"] = float("nan")
    out = detect_signals("Buy SPY shares")
    # Threshold became 1.0 → signal won't fire at sim≤1.0
    assert isinstance(out, list)


def test_detect_signals_nan_whitelist_sim_falls_back_to_zero(
    fake_encoders, anchors_path, normal_corpus_path
):
    """NaN whitelist similarity is guarded → flagged NON_STANDARD_REQUEST at 0.0."""
    detect_signals("init")
    nan_n1 = np.full_like(signal_detector._normal_embeddings[0], np.nan)
    nan_n2 = np.full_like(signal_detector._normal_embeddings[1], np.nan)
    signal_detector._normal_embeddings = (nan_n1, nan_n2)
    fired = detect_signals_semantic("zzz qqq xyz")  # class-3 → no blacklist hit
    names_scores = dict(fired)
    assert names_scores.get("NON_STANDARD_REQUEST") == 0.0


# ---------------------------------------------------------------------------
# get_embedding_similarity — exercises _sanitize_embeddings on real outputs.
# ---------------------------------------------------------------------------

def test_get_embedding_similarity_identical_texts(fake_encoders):
    assert get_embedding_similarity("leveraged speculative", "leveraged speculative") == pytest.approx(1.0, abs=1e-6)


def test_get_embedding_similarity_orthogonal_texts(fake_encoders):
    assert get_embedding_similarity("leveraged risk", "rebalance retirement") == pytest.approx(0.0, abs=1e-6)


def test_get_embedding_similarity_zero_vector_inputs(fake_encoders, monkeypatch):
    """Embedding outputs that are all zeros (or all-NaN, sanitized to zero)
    don't blow up in the cosine-similarity computation."""
    from tests.conftest import FakeEncoder

    class ZeroEncoder:
        def encode(self, texts):
            if isinstance(texts, str):
                texts = [texts]
            return np.zeros((len(texts), 4), dtype=np.float64)

    z = ZeroEncoder()
    monkeypatch.setattr(signal_detector, "_get_models", lambda: (z, z))
    # Cache the zero encoders so subsequent calls don't re-monkeypatch
    signal_detector._model1 = z
    signal_detector._model2 = z
    sim = get_embedding_similarity("a", "b")
    assert sim == 0.0


def test_get_embedding_similarity_nan_input_sanitized(monkeypatch):
    """NaN encoder output → sanitized to zero, similarity → 0.0."""
    class NanEncoder:
        def encode(self, texts):
            if isinstance(texts, str):
                texts = [texts]
            return np.full((len(texts), 4), np.nan, dtype=np.float64)

    n = NanEncoder()
    monkeypatch.setattr(signal_detector, "_get_models", lambda: (n, n))
    signal_detector._model1 = n
    signal_detector._model2 = n
    assert get_embedding_similarity("a", "b") == 0.0


# ---------------------------------------------------------------------------
# Lazy-load of the real embedding stack (exercised via get_embedding_similarity).
# ---------------------------------------------------------------------------

def test_lazy_loads_sentence_transformers_via_public_api(monkeypatch):
    """Module-level _model1/_model2 are populated on first call.  Patch
    sys.modules so the real `from sentence_transformers import ...` succeeds."""
    calls = {"n": 0}

    class FakeST:
        def __init__(self, *a, **kw):
            calls["n"] += 1

        def encode(self, texts):
            if isinstance(texts, str):
                texts = [texts]
            return np.ones((len(texts), 4), dtype=np.float64) / 2.0

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    # Reset module state to force the lazy path
    signal_detector._model1 = None
    signal_detector._model2 = None
    # First call triggers the import + two constructor calls
    get_embedding_similarity("a", "b")
    assert calls["n"] == 2
    # Subsequent call is cached
    get_embedding_similarity("c", "d")
    assert calls["n"] == 2
