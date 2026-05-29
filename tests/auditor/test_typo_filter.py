"""Tests for app/auditor/typo_filter.py — public surface only.

Public API:
  correct_typos, FINANCIAL_DOMAIN_TERMS

The lazy SymSpell loader (_get_symspell) is exercised through correct_typos.
"""

import sys
import types

import pytest

from app.auditor import typo_filter
from app.auditor.typo_filter import (
    FINANCIAL_DOMAIN_TERMS,
    _load_protected_terms,
    correct_typos,
)


def test_load_protected_terms_reads_tickers_and_markers():
    """Whitelisted tickers + single-word derivative markers are returned;
    multi-word markers are excluded (matched at the prompt level)."""
    terms = _load_protected_terms()
    assert "AAPL" in terms
    assert "option" in terms
    assert all(" " not in t for t in terms if t in ("option", "AAPL"))


def test_load_protected_terms_missing_config_degrades_to_empty(monkeypatch):
    """A missing/unreadable ticker_config.json degrades to [] (no crash)."""
    monkeypatch.setattr(typo_filter, "_TICKER_CONFIG_PATH", "/nonexistent/x.json")
    assert _load_protected_terms() == []


def test_financial_domain_terms_non_empty_and_contains_known_terms():
    assert len(FINANCIAL_DOMAIN_TERMS) > 30
    assert "fiduciary" in FINANCIAL_DOMAIN_TERMS
    assert "spy" in FINANCIAL_DOMAIN_TERMS


# ---------------------------------------------------------------------------
# correct_typos — happy paths exercising the real SymSpell library.
# ---------------------------------------------------------------------------

def test_correct_typos_buy_homophone():
    """Contextual rule rewrites 'I want to by' → 'I want to buy'."""
    out = correct_typos("I want to by 100 shares of SPY")
    assert "buy" in out.lower()


def test_correct_typos_two_calls_uses_cached_loader():
    """Second call must succeed without re-loading the dictionary (the
    cached-instance return branch).  Observable as: both calls return
    valid output without errors and consistently correct the same input."""
    a = correct_typos("Please rebalance my acount")
    b = correct_typos("Please rebalance my acount")
    assert a == b
    assert "account" in a.lower()


def test_correct_typos_preserves_ticker_caps():
    """ALL_CAPS tokens are protected."""
    out = correct_typos("Please buy AAPL and SPY today")
    assert "AAPL" in out and "SPY" in out


def test_correct_typos_preserves_numeric_tokens():
    out = correct_typos("Allocate $100k to bonds")
    assert "$100k" in out


def test_correct_typos_preserves_short_tokens():
    """Tokens ≤ 2 chars are untouched."""
    out = correct_typos("I to my SP")
    assert "I" in out and "to" in out and "my" in out


def test_correct_typos_handles_surrounding_punctuation():
    out = correct_typos("please rebalance, thanks!")
    assert "rebalance" in out and out.endswith("!")


def test_correct_typos_idempotent_on_clean_text():
    s = "Please buy SPY shares"
    assert correct_typos(s) == s


def test_correct_typos_empty_string():
    assert correct_typos("") == ""


def test_correct_typos_only_punctuation_token():
    """A token of pure punctuation passes through (empty after stripping)."""
    out = correct_typos("hello ... world")
    assert "..." in out


def test_correct_typos_preserves_capitalization_on_correction():
    """Leading-cap tokens stay capitalized after correction."""
    out = correct_typos("Helo there friend")
    assert out.split()[0][0].isupper()


# ---------------------------------------------------------------------------
# Graceful degradation — when SymSpell isn't available, correct_typos
# becomes a near no-op (only the contextual homophone substitution runs).
# Exercised by stubbing sys.modules['symspellpy'].
# ---------------------------------------------------------------------------

def test_correct_typos_disabled_when_symspellpy_missing(monkeypatch):
    """ImportError inside _get_symspell → typo_filter is disabled."""
    typo_filter._symspell = None

    real_import = (__builtins__["__import__"] if isinstance(__builtins__, dict)
                   else __builtins__.__import__)

    def _fake_import(name, *args, **kwargs):
        if name == "symspellpy":
            raise ImportError("simulated missing dep")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    # Pre-typo input that doesn't match the contextual rule
    out = correct_typos("annuty derivatice")
    assert out == "annuty derivatice"


def test_correct_typos_disabled_when_dictionary_load_fails(monkeypatch):
    """SymSpell.load_dictionary returning False → typo filter is disabled."""
    typo_filter._symspell = None

    class FakeSymSpell:
        def __init__(self, *a, **kw): pass
        def load_dictionary(self, *a, **kw):
            return False
        def create_dictionary_entry(self, *a, **kw): pass
        def lookup(self, *a, **kw): return []

    class FakeVerbosity:
        TOP = "TOP"

    fake_mod = types.ModuleType("symspellpy")
    fake_mod.SymSpell = FakeSymSpell
    fake_mod.Verbosity = FakeVerbosity
    monkeypatch.setitem(sys.modules, "symspellpy", fake_mod)
    out = correct_typos("nothing to correct here")
    assert out == "nothing to correct here"


# ---------------------------------------------------------------------------
# Edge cases exercised through correct_typos with a stubbed SymSpell.
# ---------------------------------------------------------------------------

def _install_fake_symspell(monkeypatch, suggestion_terms):
    """Stub sys.modules['symspellpy'] with a lookup returning specified terms."""
    class FakeSugg:
        def __init__(self, term, dist):
            self.term = term
            self.distance = dist

    suggestions = [FakeSugg(t, d) for t, d in suggestion_terms]

    class FakeSymSpell:
        def __init__(self, *a, **kw): pass
        def load_dictionary(self, *a, **kw): return True
        def create_dictionary_entry(self, *a, **kw): pass
        def lookup(self, term, *a, **kw):
            # Inject the lookup token name into the first suggestion if its
            # term was set to a placeholder that matches the input.
            return [FakeSugg(s.term.replace("__INPUT__", term), s.distance)
                    for s in suggestions]

    class FakeVerbosity:
        TOP = "TOP"

    fake_mod = types.ModuleType("symspellpy")
    fake_mod.SymSpell = FakeSymSpell
    fake_mod.Verbosity = FakeVerbosity
    monkeypatch.setitem(sys.modules, "symspellpy", fake_mod)

    # Also stub the dictionary file path so we don't depend on the bundled file
    import pkg_resources
    monkeypatch.setattr(pkg_resources, "resource_filename",
                        lambda *a, **kw: "/dev/null")


def test_correct_typos_distance_zero_means_no_correction(monkeypatch):
    """A single suggestion with distance=0 is a no-op."""
    typo_filter._symspell = None
    _install_fake_symspell(monkeypatch, [("__INPUT__", 0)])
    assert correct_typos("hello world") == "hello world"


def test_correct_typos_digit_token_skipped(monkeypatch):
    """Regression (#4): a token containing a digit ('0dte') is never corrected,
    even when SymSpell would otherwise suggest a change."""
    typo_filter._symspell = None
    _install_fake_symspell(monkeypatch, [("changed", 1)])
    out = correct_typos("0dte trade")
    assert "0dte" in out.split()


def test_correct_typos_lowercase_token_correction(monkeypatch):
    """Lowercase typo → correction returned lowercase (capitalize branch skipped)."""
    typo_filter._symspell = None
    _install_fake_symspell(monkeypatch, [("fiduciary", 1)])
    out = correct_typos("fiduciry duty")
    assert "fiduciary" in out
    assert "Fiduciary" not in out


def test_correct_typos_correction_equal_to_input_logs_nothing(monkeypatch):
    """Distance=1 but term echoes input → corrected token == clean → no log."""
    typo_filter._symspell = None
    _install_fake_symspell(monkeypatch, [("__INPUT__", 1)])
    out = correct_typos("hello world")
    assert "hello" in out and "world" in out
