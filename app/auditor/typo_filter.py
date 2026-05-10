"""
Typo Pre-Filter — Step 1 of the compliance signal pipeline.

Uses SymSpell (O(1) approximate string matching) to correct likely typos
in the user prompt BEFORE it reaches the embedding layer.

Why this matters:
  Anchor embeddings are trained on clean financial text.  A misspelled word
  like "derivatice" or "annuty" embeds in a different region of the vector
  space than the anchor, suppressing a signal that should have fired.
  Correcting first ensures the embedding operates on the intended meaning.

Design decisions:
  - ALL_CAPS tokens are skipped → protects ticker symbols (SPY, AAPL, etc.)
  - Numeric tokens are skipped → "$100k", "3x", "144" are untouched
  - Very short tokens (≤ 2 chars) are skipped → "I", "my", "to" are safe
  - Financial domain terms are seeded at high frequency so SymSpell never
    "corrects" them away (e.g., "fiduciary" → "fiduciary", not "judiciary")
  - Only high-confidence corrections are applied (exactly 1 suggestion).
    Ambiguous corrections are left to the embedding to handle.
  - Max edit distance = 2 (covers most real typos; 1 transposition or 2 dels)
"""

import logging
import re

logger = logging.getLogger(__name__)

_symspell = None  # Lazy-loaded

# Financial and compliance domain terms.
# Added at frequency 10_000_000 so SymSpell never corrects them away.
FINANCIAL_DOMAIN_TERMS: list[str] = [
    # Asset classes & instruments
    "annuity", "annuities", "etf", "reit", "ira", "401k", "roth",
    "derivative", "derivatives", "futures", "options", "collateral",
    "arbitrage", "rebalance", "rebalancing", "ticker", "equities",
    "maturity", "volatility", "vix", "otc", "adr",
    # Regulatory / compliance terms
    "finra", "ofac", "kyc", "aml", "fiduciary", "suitability",
    "brokerage", "custodian", "esq", "prospectus", "sec", "irs",
    "disclosure", "diligence", "hedging", "lockup",
    # Actions
    "liquidate", "allocate", "rebalance", "rollover", "reinvest",
    # Common financial abbreviations
    "spx", "spy", "qqq", "voo", "vti", "iwm", "dia", "xlf", "xle",
]

# Regex: purely numeric tokens including dollar amounts like $100k, 3x, 2.5%
_NUMERIC_RE = re.compile(r"^[\$£€¥]?[\d,\.]+[kKmMbBxX%]?$")


def _get_symspell():
    """Lazy-load SymSpell with the standard English frequency dictionary."""
    global _symspell
    if _symspell is not None:
        return _symspell

    try:
        from symspellpy import SymSpell, Verbosity
    except ImportError:
        logger.warning(
            "symspellpy not installed — typo filter is DISABLED. "
            "Run: pip install symspellpy>=6.7.7"
        )
        return None

    ss = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)

    # Load the bundled English frequency dictionary
    import pkg_resources
    dict_path = pkg_resources.resource_filename(
        "symspellpy", "frequency_dictionary_en_82_765.txt"
    )
    loaded = ss.load_dictionary(dict_path, term_index=0, count_index=1)
    if not loaded:
        logger.error("SymSpell: failed to load frequency dictionary — typo filter disabled.")
        return None

    # Inject financial terms with very high frequency so they're never over-corrected
    for term in FINANCIAL_DOMAIN_TERMS:
        ss.create_dictionary_entry(term, 10_000_000)

    logger.info(
        "SymSpell typo filter ready — %d financial domain terms seeded.", len(FINANCIAL_DOMAIN_TERMS)
    )
    _symspell = ss
    return _symspell


def correct_typos(text: str, max_edit_distance: int = 2) -> str:
    """
    Correct likely typos in *text* before it reaches the embedding layer.

    Rules:
      - ALL_CAPS tokens are passed through unchanged  (ticker symbols)
      - Numeric / currency / percentage tokens are passed through unchanged
      - Tokens ≤ 2 chars are passed through unchanged
      - A correction is applied ONLY when SymSpell returns exactly one
        top suggestion with edit_distance > 0  (unambiguous fix)
      - Leading/trailing punctuation is preserved around corrected tokens

    Returns the corrected string, or the original if SymSpell is unavailable.
    """
    # Contextual overrides for common homophones that SymSpell won't catch (since 'by' is a valid word)
    text = re.sub(r"\b(want\s+to|wanna|like\s+to|going\s+to|need\s+to)\s+by\b", r"\1 buy", text, flags=re.IGNORECASE)

    sym = _get_symspell()
    if sym is None:
        return text  # Graceful degradation

    from symspellpy import Verbosity

    tokens = text.split()
    corrected_tokens = []

    for token in tokens:
        # Strip surrounding punctuation to examine the bare word
        stripped_prefix = ""
        stripped_suffix = ""
        clean = token

        # Peel leading punctuation
        while clean and not clean[0].isalnum():
            stripped_prefix += clean[0]
            clean = clean[1:]
        # Peel trailing punctuation
        while clean and not clean[-1].isalnum():
            stripped_suffix = clean[-1] + stripped_suffix
            clean = clean[:-1]

        # Skip: empty after stripping
        if not clean:
            corrected_tokens.append(token)
            continue

        # Skip: ALL_CAPS → ticker symbol or acronym
        if clean.isupper():
            corrected_tokens.append(token)
            continue

        # Skip: numeric / currency / percentage
        if _NUMERIC_RE.match(clean):
            corrected_tokens.append(token)
            continue

        # Skip: very short tokens
        if len(clean) <= 2:
            corrected_tokens.append(token)
            continue

        # Query SymSpell (lower-case lookup)
        suggestions = sym.lookup(clean.lower(), Verbosity.TOP, max_edit_distance=max_edit_distance)

        if suggestions and len(suggestions) == 1 and suggestions[0].distance > 0:
            correction = suggestions[0].term
            # Re-apply original capitalization
            if clean[0].isupper():
                correction = correction.capitalize()
            if clean != correction:
                logger.debug("Typo: '%s' → '%s'", clean, correction)
            corrected_tokens.append(stripped_prefix + correction + stripped_suffix)
        else:
            corrected_tokens.append(token)

    return " ".join(corrected_tokens)
