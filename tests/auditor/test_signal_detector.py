"""Tests for app/auditor/signal_detector.py — pipeline w/ mocked embedders."""

import sys
import types

import numpy as np
import pytest

from app.auditor import signal_detector
from app.auditor.signal_detector import (
    EPSILON,
    WHITELIST_THRESHOLD,
    Z_SCORE_THRESHOLD,
    _initialize_system,
    _sanitize_embeddings,
    detect_signals,
    detect_signals_semantic,
    get_embedding_similarity,
)


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

def test_module_constants_have_documented_values():
    assert EPSILON == 1e-6
    assert Z_SCORE_THRESHOLD == 2.0
    assert WHITELIST_THRESHOLD == 0.55


# ---------------------------------------------------------------------------
# _sanitize_embeddings — numerical guard
# ---------------------------------------------------------------------------

def test_sanitize_replaces_nan_and_inf_with_zero():
    embs = np.array([[np.nan, np.inf, -np.inf]], dtype=np.float64)
    out = _sanitize_embeddings(embs)
    # All sources of non-finite → zero, so norm=0 → safe_mask=False → returned zero vector
    assert np.allclose(out, 0.0)


def test_sanitize_unit_normalizes_finite_vectors():
    embs = np.array([[3.0, 4.0]], dtype=np.float64)
    out = _sanitize_embeddings(embs)
    assert np.allclose(np.linalg.norm(out, axis=-1), 1.0)
    assert out[0, 0] == pytest.approx(0.6)
    assert out[0, 1] == pytest.approx(0.8)


def test_sanitize_handles_zero_vector_without_dividing():
    embs = np.zeros((1, 4), dtype=np.float64)
    out = _sanitize_embeddings(embs)
    assert np.allclose(out, 0.0)


# ---------------------------------------------------------------------------
# _initialize_system — anchor & threshold construction
# ---------------------------------------------------------------------------

def test_initialize_system_idempotent_after_first_call(fake_encoders, anchors_path, normal_corpus_path):
    _initialize_system()
    first_thresholds = signal_detector._signal_thresholds
    first_anchors = signal_detector._anchor_embeddings
    _initialize_system()  # second call must be a no-op
    assert signal_detector._signal_thresholds is first_thresholds
    assert signal_detector._anchor_embeddings is first_anchors


def test_initialize_system_skips_underscore_metadata_rules(fake_encoders, anchors_path, normal_corpus_path):
    _initialize_system()
    assert "_meta" not in signal_detector._anchor_embeddings


def test_initialize_system_skips_signals_with_no_anchor_texts(fake_encoders, tmp_path, monkeypatch, normal_corpus_path):
    import json
    anchors = {
        "FINRA_2111": {
            "signals": {
                "EMPTY_SIG": {"anchors": []},
                "REAL_SIG": {"anchors": ["leveraged speculative"]},
            }
        }
    }
    p = tmp_path / "anchors.json"
    p.write_text(json.dumps(anchors))
    monkeypatch.setattr(signal_detector, "_ANCHORS_PATH", str(p))
    _initialize_system()
    assert "EMPTY_SIG" not in signal_detector._anchor_embeddings
    assert "REAL_SIG" in signal_detector._anchor_embeddings


def test_initialize_system_threshold_floor_at_060(fake_encoders, anchors_path, normal_corpus_path):
    _initialize_system()
    for sig, thr in signal_detector._signal_thresholds.items():
        assert thr >= 0.60


# ---------------------------------------------------------------------------
# detect_signals_semantic — full pipeline behavior
# ---------------------------------------------------------------------------

def test_detect_signals_semantic_fires_on_blacklist_match(fake_encoders, anchors_path, normal_corpus_path):
    # Prompt contains "leveraged" → FakeEncoder maps it to the "high risk" class
    fired = detect_signals_semantic("Buy me a leveraged ETF please")
    names = [n for n, _ in fired]
    assert "HIGH_RISK_PRODUCT" in names or "SPECULATIVE_PRODUCT" in names


def test_detect_signals_semantic_normal_prompt_uses_whitelist(fake_encoders, anchors_path, normal_corpus_path):
    # "Please rebalance my retirement account" is in the normal corpus → no NON_STANDARD_REQUEST
    fired = detect_signals_semantic("Please rebalance my retirement account")
    assert not fired or all(n != "NON_STANDARD_REQUEST" for n, _ in fired)


def test_detect_signals_semantic_falls_to_whitelist_flag(fake_encoders, anchors_path, normal_corpus_path):
    # Class-3 (no overlap with anchors OR corpus) → triggers NON_STANDARD_REQUEST
    fired = detect_signals_semantic("Please proceed with banana protocol now")
    names = [n for n, _ in fired]
    assert "NON_STANDARD_REQUEST" in names


def test_detect_signals_returns_just_names(fake_encoders, anchors_path, normal_corpus_path):
    out = detect_signals("leveraged speculative product purchase")
    assert isinstance(out, list)
    assert all(isinstance(n, str) for n in out)


def test_detect_signals_semantic_handles_nan_threshold(fake_encoders, anchors_path, normal_corpus_path, monkeypatch):
    # Force a NaN threshold to exercise the np.isnan guard path
    _initialize_system()
    signal_detector._signal_thresholds["HIGH_RISK_PRODUCT"] = float("nan")
    out = detect_signals_semantic("Buy SPY shares")
    # Threshold became 1.0 due to NaN → signal can't fire above sim=1.0 trivially
    assert isinstance(out, list)


def test_detect_signals_semantic_nan_whitelist_sim_falls_back_to_zero(
    fake_encoders, anchors_path, normal_corpus_path
):
    """If the whitelist similarity is NaN, the NON_STANDARD_REQUEST gate uses 0.0."""
    # Initialize and then poison the normal embeddings so the dot product → NaN.
    _initialize_system()
    nan_n1 = np.full_like(signal_detector._normal_embeddings[0], np.nan)
    nan_n2 = np.full_like(signal_detector._normal_embeddings[1], np.nan)
    signal_detector._normal_embeddings = (nan_n1, nan_n2)
    fired = detect_signals_semantic("zzz qqq xyz")  # class 3 → no blacklist match
    names = [n for n, _ in fired]
    assert "NON_STANDARD_REQUEST" in names
    score = dict(fired)["NON_STANDARD_REQUEST"]
    assert score == 0.0


def test_detect_signals_dedupes_same_signal_max_score(fake_encoders, anchors_path, normal_corpus_path):
    """Multi-chunk match keeps the highest score for the same signal name."""
    fired = detect_signals_semantic("Buy leveraged. Buy speculative. Buy more leveraged.")
    names = [n for n, _ in fired]
    # Each signal at most once
    assert len(names) == len(set(names))


# ---------------------------------------------------------------------------
# get_embedding_similarity
# ---------------------------------------------------------------------------

def test_get_embedding_similarity_identical_texts(fake_encoders):
    sim = get_embedding_similarity("leveraged speculative", "leveraged speculative")
    assert sim == pytest.approx(1.0, abs=1e-6)


def test_get_embedding_similarity_different_classes(fake_encoders):
    sim = get_embedding_similarity("leveraged risk", "rebalance retirement account")
    # FakeEncoder routes these to different one-hot classes → sim = 0
    assert sim == pytest.approx(0.0, abs=1e-6)


# ---------------------------------------------------------------------------
# _get_models — exercise the real lazy-load branch with a stub module.
# ---------------------------------------------------------------------------

def test_get_models_lazy_loads_and_caches(monkeypatch):
    """Patch sentence_transformers in sys.modules so the real branch runs."""
    sentinel1 = object()
    sentinel2 = object()
    calls = {"n": 0}

    class FakeST:
        instances = [sentinel1, sentinel2]

        def __new__(cls, *args, **kwargs):
            calls["n"] += 1
            return cls.instances[calls["n"] - 1]

    fake_mod = types.ModuleType("sentence_transformers")
    fake_mod.SentenceTransformer = FakeST
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_mod)

    signal_detector._model1 = None
    signal_detector._model2 = None
    m1, m2 = signal_detector._get_models()
    assert m1 is sentinel1
    assert m2 is sentinel2
    # Calling again is a no-op — cached values returned
    m1b, m2b = signal_detector._get_models()
    assert m1b is sentinel1 and m2b is sentinel2
    assert calls["n"] == 2  # two constructors total, not four
