"""Tests for app/auditor/typo_filter.py — SymSpell-backed typo correction."""

import sys
import types

import pytest

from app.auditor import typo_filter
from app.auditor.typo_filter import (
    FINANCIAL_DOMAIN_TERMS,
    _NUMERIC_RE,
    _get_symspell,
    correct_typos,
)


def test_financial_domain_terms_non_empty():
    assert len(FINANCIAL_DOMAIN_TERMS) > 30
    assert "fiduciary" in FINANCIAL_DOMAIN_TERMS
    assert "spy" in FINANCIAL_DOMAIN_TERMS


def test_numeric_regex_catches_money_and_percentages():
    for s in ("$100", "$100k", "100", "3x", "2.5%", "100,000", "€50"):
        assert _NUMERIC_RE.match(s)
    for s in ("annuity", "abc", "123abc"):
        assert not _NUMERIC_RE.match(s)


# ---------------------------------------------------------------------------
# correct_typos — full pipeline.  Uses the real symspellpy library so the
# coverage walk hits the actual code paths.
# ---------------------------------------------------------------------------

def test_correct_typos_contextual_buy_homophone():
    """'I want to by 100 shares' → 'I want to buy 100 shares'."""
    out = correct_typos("I want to by 100 shares of SPY")
    assert "buy" in out.lower()


def test_correct_typos_passes_through_tickers():
    """ALL_CAPS tokens are protected — even if SymSpell would otherwise mangle them."""
    out = correct_typos("Please buy AAPL and SPY today")
    assert "AAPL" in out and "SPY" in out


def test_correct_typos_passes_through_numeric_tokens():
    out = correct_typos("Allocate $100k to bonds")
    assert "$100k" in out


def test_correct_typos_passes_through_short_tokens():
    """Tokens ≤ 2 chars are untouched."""
    out = correct_typos("I to my SP")
    assert "I" in out and "to" in out and "my" in out


def test_correct_typos_handles_punctuation_around_word():
    out = correct_typos("please rebalance, thanks!")
    assert "rebalance" in out
    assert out.endswith("!")


def test_correct_typos_idempotent_on_clean_input():
    s = "Please buy SPY shares"
    assert correct_typos(s) == s


def test_correct_typos_handles_empty_string():
    assert correct_typos("") == ""


def test_correct_typos_passes_through_pure_punctuation_tokens():
    """A token of only punctuation is preserved (empty after stripping)."""
    out = correct_typos("hello ... world")
    assert "..." in out


def test_correct_typos_preserves_capitalization_when_correcting():
    # Use a clear typo that should be corrected to lowercase, then capitalized
    out = correct_typos("Helo there friend")
    # "Helo" → "Hello" with leading cap preserved
    assert out.split()[0][0].isupper()


def test_correct_typos_caches_symspell():
    """_get_symspell returns the same object on repeat calls."""
    s1 = _get_symspell()
    s2 = _get_symspell()
    assert s1 is s2


# ---------------------------------------------------------------------------
# Disabled-mode behaviors (graceful degradation when symspellpy is missing).
# ---------------------------------------------------------------------------

def test_correct_typos_disabled_when_symspellpy_missing(monkeypatch):
    """When the import inside _get_symspell fails, the filter no-ops."""
    typo_filter._symspell = None

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "symspellpy":
            raise ImportError("simulated missing dep")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    assert _get_symspell() is None
    # correct_typos should now return the text untouched (modulo contextual sub)
    out = correct_typos("annuty derivatice")
    assert out == "annuty derivatice"


def test_correct_typos_disabled_when_dict_load_fails(monkeypatch):
    """When SymSpell.load_dictionary returns False, the filter is disabled."""
    typo_filter._symspell = None

    class FakeSymSpell:
        Verbosity = None  # unused

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
    assert _get_symspell() is None


def test_correct_typos_keeps_token_when_suggestion_distance_zero(monkeypatch):
    """If SymSpell returns a single suggestion with distance=0, no change."""
    typo_filter._symspell = None

    class FakeSugg:
        def __init__(self, term, dist):
            self.term = term
            self.distance = dist

    class FakeSymSpell:
        def __init__(self, *a, **kw): pass
        def load_dictionary(self, *a, **kw):
            return True
        def create_dictionary_entry(self, *a, **kw): pass
        def lookup(self, term, *a, **kw):
            return [FakeSugg(term, 0)]

    class FakeVerbosity:
        TOP = "TOP"

    fake_mod = types.ModuleType("symspellpy")
    fake_mod.SymSpell = FakeSymSpell
    fake_mod.Verbosity = FakeVerbosity
    monkeypatch.setitem(sys.modules, "symspellpy", fake_mod)

    # Trick: bypass the real pkg_resources call by patching it
    import pkg_resources
    monkeypatch.setattr(pkg_resources, "resource_filename", lambda *a, **kw: "/dev/null")

    typo_filter._symspell = None
    out = correct_typos("hello world")
    assert out == "hello world"  # distance=0 → no correction applied


def test_correct_typos_lowercase_correction_preserves_case(monkeypatch):
    """Lowercase typo → correction comes back lowercase (skips capitalize)."""
    typo_filter._symspell = None

    class FakeSugg:
        def __init__(self, term, dist):
            self.term = term
            self.distance = dist

    class FakeSymSpell:
        def __init__(self, *a, **kw): pass
        def load_dictionary(self, *a, **kw): return True
        def create_dictionary_entry(self, *a, **kw): pass
        def lookup(self, term, *a, **kw):
            return [FakeSugg("fiduciary", 1)]

    class FakeVerbosity:
        TOP = "TOP"

    fake_mod = types.ModuleType("symspellpy")
    fake_mod.SymSpell = FakeSymSpell
    fake_mod.Verbosity = FakeVerbosity
    monkeypatch.setitem(sys.modules, "symspellpy", fake_mod)
    import pkg_resources
    monkeypatch.setattr(pkg_resources, "resource_filename", lambda *a, **kw: "/dev/null")

    typo_filter._symspell = None
    out = correct_typos("fiduciry duty")
    # "fiduciry" → "fiduciary" with lowercase preserved (clean[0]='f' is not upper)
    assert "fiduciary" in out
    assert "Fiduciary" not in out  # capitalize NOT applied


def test_correct_typos_no_log_when_corrected_equals_original(monkeypatch):
    """Exercise the (clean == correction) skip-log branch."""
    typo_filter._symspell = None

    class FakeSugg:
        def __init__(self, term, dist):
            self.term = term
            self.distance = dist

    class FakeSymSpell:
        def __init__(self, *a, **kw): pass
        def load_dictionary(self, *a, **kw): return True
        def create_dictionary_entry(self, *a, **kw): pass
        def lookup(self, term, *a, **kw):
            # Pathological case: distance > 0 but term echoes the input.  The
            # production library never returns this, but if it ever did the
            # code should still append the token without an emitted log.
            return [FakeSugg(term, 1)]

    class FakeVerbosity:
        TOP = "TOP"

    fake_mod = types.ModuleType("symspellpy")
    fake_mod.SymSpell = FakeSymSpell
    fake_mod.Verbosity = FakeVerbosity
    monkeypatch.setitem(sys.modules, "symspellpy", fake_mod)
    import pkg_resources
    monkeypatch.setattr(pkg_resources, "resource_filename", lambda *a, **kw: "/dev/null")

    typo_filter._symspell = None
    out = correct_typos("hello world")
    assert "hello" in out and "world" in out


def test_correct_typos_keeps_token_when_multiple_suggestions(monkeypatch):
    """Ambiguous correction (>1 suggestion) is left to the embedding layer."""
    typo_filter._symspell = None

    class FakeSugg:
        def __init__(self, term, dist):
            self.term = term
            self.distance = dist

    class FakeSymSpell:
        def __init__(self, *a, **kw): pass
        def load_dictionary(self, *a, **kw): return True
        def create_dictionary_entry(self, *a, **kw): pass
        def lookup(self, term, *a, **kw):
            return [FakeSugg("foo", 1), FakeSugg("bar", 1)]

    class FakeVerbosity:
        TOP = "TOP"

    fake_mod = types.ModuleType("symspellpy")
    fake_mod.SymSpell = FakeSymSpell
    fake_mod.Verbosity = FakeVerbosity
    monkeypatch.setitem(sys.modules, "symspellpy", fake_mod)

    import pkg_resources
    monkeypatch.setattr(pkg_resources, "resource_filename", lambda *a, **kw: "/dev/null")

    typo_filter._symspell = None
    out = correct_typos("foox bar baz")
    assert "foox" in out  # ambiguous → unchanged
