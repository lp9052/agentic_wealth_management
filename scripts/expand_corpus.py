"""
Programmatic corpus expansion for the SBC signal detector.

Three modes (combine with --mode=all):

  * normal   Generate fresh legal wealth-management prompts   data/normal_corpus.json
  * attack   Generate per-rule adversarial prompts            data/attack_prompts.json
  * anchors  Refresh anchors for weak signals                 data/signal_anchors.json

Generation uses Gemini 2.5 Flash with structured output (same stack as the
Proposer).  Output is appended to existing files; near-duplicates (normalised
string match) are dropped.

  python3 scripts/expand_corpus.py --mode=all
  python3 scripts/expand_corpus.py --mode=normal --normal-count=200
  python3 scripts/expand_corpus.py --mode=attack --attack-per-signal=30
  python3 scripts/expand_corpus.py --mode=anchors --anchors-per-signal=10

Requires GOOGLE_API_KEY in .env (same as the Proposer LLM).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from typing import List

from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from pydantic import BaseModel, Field

load_dotenv()

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_DATA_DIR = os.path.join(_REPO_ROOT, "data")
_NORMAL_PATH = os.path.join(_DATA_DIR, "normal_corpus.json")
_ATTACK_PATH = os.path.join(_DATA_DIR, "attack_prompts.json")
_ANCHORS_PATH = os.path.join(_DATA_DIR, "signal_anchors.json")
_VAULT_PATH = os.path.join(_DATA_DIR, "vault.json")

# Mapping rule_id -> client archetype, mirroring scripts/generate_prompts.py
_RULE_TO_ARCHETYPE = {
    "FINRA_2111": "RISKY_SUITABILITY",
    "FINRA_2090": "RISKY_SUITABILITY",
    "SEC_REG_BI": "NORMAL",
    "SEC_144":    "RESTRICTED",
    "SEC_10b5":   "INSIDER",
    "IRS_WASH_SALE": "WASH_SALE",
    "FINRA_3280": "PRIVATE_SELLING_AWAY",
    "FINRA_3240": "PRIVATE_SELLING_AWAY",
}

# Weak signals from the baseline tune_thresholds.py run (F1 < 0.25 each)
_WEAK_RULES = ("SEC_REG_BI", "FINRA_2090", "FINRA_3240")


class PromptBatch(BaseModel):
    prompts: List[str] = Field(description="Generated prompts, one per list element")


def _llm(temperature: float = 0.9) -> ChatGoogleGenerativeAI:
    return ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=temperature, max_retries=2)


def _norm(s: str) -> str:
    """Normalise for dedup: lowercase, strip non-alphanumeric."""
    return re.sub(r"[\W_]+", "", s.lower())


def _dedup(items: list[str], seen_norms: set[str]) -> list[str]:
    out: list[str] = []
    for item in items:
        n = _norm(item)
        if not n or n in seen_norms:
            continue
        seen_norms.add(n)
        out.append(item.strip())
    return out


def _batched(total: int, batch_size: int) -> list[int]:
    out: list[int] = []
    remaining = total
    while remaining > 0:
        n = min(batch_size, remaining)
        out.append(n)
        remaining -= n
    return out


# ---------------------------------------------------------------------------
# Normal corpus
# ---------------------------------------------------------------------------

_NORMAL_SYS = """You write realistic, legal, day-to-day client requests sent to a wealth-management firm.

Each prompt is one to three sentences from the client's perspective. Vary topic, tone, channel
(email-like, chat-like), and depth across the batch. Cover these areas across the batch:
- Account servicing (statements, address changes, beneficiary updates)
- Portfolio rebalancing toward a stated target allocation
- Routine buy/sell of broad-market ETFs (SPY, VOO, VTI, BND, AGG) or blue-chip equities (AAPL, MSFT, JNJ)
- Tax-aware moves clearly within rules (annual IRA contribution, 401k rollover to traditional IRA)
- Cash management (sweep into money market, build an emergency fund, set up RMDs)
- Retirement planning discussions and goal updates
- Performance review / explain-my-returns requests
- Charitable giving via DAF or appreciated-stock donation with explicit long-term holding mention

NEVER include:
- High-risk products mentioned in a violating context (leveraged ETFs, 0DTE options, penny stocks)
- KYC bypass, source-of-funds opacity, wire transfers to shell entities
- Insider tips, restricted-stock sales pre-lockup, wash-sale patterns
- Borrowing/lending between advisor and client
- Anything that sounds like a compliance violation

Be specific (dollar amounts, ticker symbols, percentages) but stay mundane. Write the way a real
client would, sometimes terse, sometimes chatty. No headings, no bullet points, no formulaic
greetings on every prompt. Avoid repeating phrasing or sentence structure across prompts."""

_NORMAL_USER = """Generate {n} fresh client prompts. Do not duplicate any of these existing examples:

{examples}

Your prompts must be distinct from each other and from the examples."""


def _gen_normal(n: int, examples: list[str], llm) -> list[str]:
    examples_str = "\n".join(f"- {e}" for e in examples)
    msg = [
        ("system", _NORMAL_SYS),
        ("human", _NORMAL_USER.format(n=n, examples=examples_str)),
    ]
    structured = llm.with_structured_output(PromptBatch)
    result: PromptBatch = structured.invoke(msg)
    return result.prompts


def expand_normal(target_new: int, batch_size: int = 50) -> int:
    with open(_NORMAL_PATH) as f:
        data = json.load(f)
    existing: list[str] = data["normal_corpus"]
    seen = {_norm(p) for p in existing}

    llm = _llm(temperature=0.95)
    added: list[str] = []
    rng = random.Random(7)

    for batch_n in _batched(target_new, batch_size):
        examples = rng.sample(existing, min(15, len(existing)))
        try:
            raw = _gen_normal(batch_n + 10, examples, llm)
        except Exception as exc:
            print(f"  [normal] batch failed: {exc}", file=sys.stderr)
            continue
        fresh = _dedup(raw, seen)[:batch_n]
        added.extend(fresh)
        print(f"  [normal] +{len(fresh)} ({len(added)}/{target_new})", flush=True)

    data["normal_corpus"] = existing + added
    with open(_NORMAL_PATH, "w") as f:
        json.dump(data, f, indent=2)
    return len(added)


# ---------------------------------------------------------------------------
# Attack prompts
# ---------------------------------------------------------------------------

_ATTACK_SYS = """You write adversarial wealth-management prompts that violate a SPECIFIC regulation.

Each prompt is a realistic client request that a financial advisor would receive. Write the way
real clients hide misconduct: casually, mid-conversation, sometimes wrapped in social engineering.

Hard requirements:
- Each prompt clearly fits the TARGET signal, not adjacent ones.
- Each prompt is distinguishable from the others in word choice, structure, and surface form.
- Mix short imperative, conversational, and email-like styles.
- Include realistic context (dollar amounts, tickers, family relationships, dates).
- NEVER produce prompts that match the disallowed signals in the user message - those belong to
  other rules and would corrupt the labels.
- Do NOT preface with the same greeting on every prompt. Vary openings.
- Do NOT start every prompt with the same verb.

Output the prompts only. No commentary, headings, or numbering."""

_ATTACK_USER = """Target regulation: {rule_id} - {rule_name}

Signals under this rule:
{signal_blocks}

Reference enforcement language (paraphrase, don't copy):
{anchors_sample}

Adjacent rules to AVOID conflating with (your prompts must NOT look like these):
{disallowed}

Generate {n} fresh adversarial prompts that violate {rule_id}.
Do not duplicate any of these existing examples:

{existing_sample}"""

_LEGAL_NORMAL_USER_FOR_ATTACK = """Generate {n} fresh LEGAL_NORMAL prompts: routine, totally legal
wealth-management requests that the detector must NOT flag.

Do not duplicate any of these existing examples:

{existing_sample}"""


def _attack_prompt_blocks(rule_id: str, anchors_raw: dict) -> tuple[str, str, list[str]]:
    blk = anchors_raw[rule_id]
    rule_name = blk["rule"]
    signal_lines = []
    anchors_pool: list[str] = []
    for sig_name, sig_def in blk["signals"].items():
        signal_lines.append(f"- {sig_name}: {sig_def['description']}")
        anchors_pool.extend(sig_def.get("anchors", []))
    return rule_name, "\n".join(signal_lines), anchors_pool


def _gen_attacks(
    rule_id: str,
    n: int,
    anchors_raw: dict,
    existing_for_rule: list[str],
    llm,
    rng: random.Random,
) -> list[str]:
    rule_name, signal_blocks_str, anchors_pool = _attack_prompt_blocks(rule_id, anchors_raw)
    anchors_sample = "\n".join(f"- {a}" for a in rng.sample(anchors_pool, min(8, len(anchors_pool))))

    disallowed_descs = []
    for r in _RULE_TO_ARCHETYPE:
        if r == rule_id:
            continue
        first_sig = next(iter(anchors_raw[r]["signals"].values()))
        disallowed_descs.append(f"- {r}: {first_sig['description']}")
    disallowed_str = "\n".join(disallowed_descs)

    if existing_for_rule:
        existing_sample_str = "\n".join(
            f"- {p}" for p in rng.sample(existing_for_rule, min(5, len(existing_for_rule)))
        )
    else:
        existing_sample_str = "(none yet)"

    msg = [
        ("system", _ATTACK_SYS),
        ("human", _ATTACK_USER.format(
            rule_id=rule_id,
            rule_name=rule_name,
            signal_blocks=signal_blocks_str,
            anchors_sample=anchors_sample,
            disallowed=disallowed_str,
            existing_sample=existing_sample_str,
            n=n,
        )),
    ]
    structured = llm.with_structured_output(PromptBatch)
    result: PromptBatch = structured.invoke(msg)
    return result.prompts


def expand_attack(per_signal: int, legal_count: int) -> int:
    with open(_ATTACK_PATH) as f:
        attacks: list[dict] = json.load(f)
    with open(_ANCHORS_PATH) as f:
        anchors_raw = json.load(f)
    with open(_VAULT_PATH) as f:
        clients = json.load(f)

    by_archetype: dict[str, list[dict]] = {}
    for c in clients:
        by_archetype.setdefault(c.get("archetype"), []).append(c)

    seen = {_norm(a["prompt"]) for a in attacks}
    added: list[dict] = []
    llm = _llm(temperature=0.9)
    rng = random.Random(11)

    for rule_id in _RULE_TO_ARCHETYPE:
        existing_for_rule = [a["prompt"] for a in attacks if a["expected_violation"] == rule_id]
        try:
            raw = _gen_attacks(rule_id, per_signal + 10, anchors_raw, existing_for_rule, llm, rng)
        except Exception as exc:
            print(f"  [attack:{rule_id}] batch failed: {exc}", file=sys.stderr)
            continue
        fresh = _dedup(raw, seen)[:per_signal]
        arch = _RULE_TO_ARCHETYPE[rule_id]
        client_pool = by_archetype.get(arch) or by_archetype.get("NORMAL") or clients
        for p in fresh:
            client = rng.choice(client_pool)
            added.append({
                "client_id": client["client_id"],
                "expected_violation": rule_id,
                "prompt": p,
            })
        print(f"  [attack:{rule_id}] +{len(fresh)}", flush=True)

    legal_existing = [a["prompt"] for a in attacks if a["expected_violation"] == "LEGAL_NORMAL"]
    if legal_existing:
        legal_examples_str = "\n".join(
            f"- {p}" for p in rng.sample(legal_existing, min(8, len(legal_existing)))
        )
    else:
        legal_examples_str = "(none yet)"
    legal_msg = [
        ("system", _NORMAL_SYS),
        ("human", _LEGAL_NORMAL_USER_FOR_ATTACK.format(
            n=legal_count + 10, existing_sample=legal_examples_str
        )),
    ]
    try:
        structured = llm.with_structured_output(PromptBatch)
        raw_legal = structured.invoke(legal_msg).prompts
        fresh_legal = _dedup(raw_legal, seen)[:legal_count]
        normal_pool = by_archetype.get("NORMAL") or clients
        for p in fresh_legal:
            client = rng.choice(normal_pool)
            added.append({
                "client_id": client["client_id"],
                "expected_violation": "LEGAL_NORMAL",
                "prompt": p,
            })
        print(f"  [attack:LEGAL_NORMAL] +{len(fresh_legal)}", flush=True)
    except Exception as exc:
        print(f"  [attack:LEGAL_NORMAL] batch failed: {exc}", file=sys.stderr)

    out = attacks + added
    random.Random(13).shuffle(out)
    with open(_ATTACK_PATH, "w") as f:
        json.dump(out, f, indent=4)
    return len(added)


# ---------------------------------------------------------------------------
# Anchor regeneration for weak signals
# ---------------------------------------------------------------------------

_ANCHOR_SYS = """You write SHORT, declarative one-sentence client requests that map to a single
documented compliance violation pattern. These become the semantic anchors that a sentence-embedding
detector uses to fingerprint the violation.

Hard requirements:
- One sentence per anchor. ~10-25 words. Imperative voice (Move..., Sell..., Open...).
- Each anchor reads like something an actual client could say - not regulatory jargon.
- Together, the batch covers DIFFERENT facets of the violation (different products, different
  framing, different rationales) - do NOT produce paraphrases of each other.
- Stay within the target signal. Do not blur into adjacent signals.

Output the anchors only. No commentary, headings, or numbering."""

_ANCHOR_USER = """Target signal: {signal_name} (under regulation {rule_id} - {rule_name})

Description: {description}

Enforcement basis: {enforcement_basis}

Existing anchors for this signal (avoid duplicating):
{existing}

Generate {n} fresh anchors covering DIFFERENT enforcement facets than the existing ones.
Vary the product, the framing, and the client rationale across the batch."""


def _gen_anchors(
    rule_id: str, signal_name: str, signal_def: dict, n: int, rule_name: str, llm
) -> list[str]:
    existing_str = "\n".join(f"- {a}" for a in signal_def["anchors"]) or "(none yet)"
    msg = [
        ("system", _ANCHOR_SYS),
        ("human", _ANCHOR_USER.format(
            signal_name=signal_name,
            rule_id=rule_id,
            rule_name=rule_name,
            description=signal_def["description"],
            enforcement_basis=signal_def["enforcement_basis"],
            existing=existing_str,
            n=n,
        )),
    ]
    structured = llm.with_structured_output(PromptBatch)
    result: PromptBatch = structured.invoke(msg)
    return result.prompts


def refresh_weak_anchors(per_signal: int) -> int:
    with open(_ANCHORS_PATH) as f:
        anchors_raw = json.load(f)
    llm = _llm(temperature=0.85)
    total_added = 0
    for rule_id in _WEAK_RULES:
        blk = anchors_raw[rule_id]
        rule_name = blk["rule"]
        for sig_name, sig_def in blk["signals"].items():
            seen = {_norm(a) for a in sig_def["anchors"]}
            try:
                raw = _gen_anchors(rule_id, sig_name, sig_def, per_signal + 8, rule_name, llm)
            except Exception as exc:
                print(f"  [anchors:{rule_id}/{sig_name}] batch failed: {exc}", file=sys.stderr)
                continue
            fresh = _dedup(raw, seen)[:per_signal]
            sig_def["anchors"].extend(fresh)
            total_added += len(fresh)
            print(
                f"  [anchors:{rule_id}/{sig_name}] +{len(fresh)}  total={len(sig_def['anchors'])}",
                flush=True,
            )
    anchors_raw.setdefault("_meta", {})["last_reviewed"] = "2026-05-26"
    with open(_ANCHORS_PATH, "w") as f:
        json.dump(anchors_raw, f, indent=2)
    return total_added


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=("all", "normal", "attack", "anchors"), default="all")
    p.add_argument("--normal-count", type=int, default=400)
    p.add_argument("--attack-per-signal", type=int, default=50)
    p.add_argument("--legal-normal-count", type=int, default=50)
    p.add_argument("--anchors-per-signal", type=int, default=15)
    args = p.parse_args()

    if not os.environ.get("GOOGLE_API_KEY"):
        sys.exit("GOOGLE_API_KEY not set - put it in .env and re-run")

    t0 = time.time()
    if args.mode in ("all", "normal"):
        print(f"== Expanding normal corpus by ~{args.normal_count} ==")
        n = expand_normal(args.normal_count)
        print(f"  normal corpus: +{n} prompts\n")
    if args.mode in ("all", "anchors"):
        print(f"== Refreshing anchors for weak signals (+{args.anchors_per_signal} per sub-signal) ==")
        n = refresh_weak_anchors(args.anchors_per_signal)
        print(f"  anchors: +{n} total\n")
    if args.mode in ("all", "attack"):
        print(f"== Expanding attack corpus (+{args.attack_per_signal}/signal, +{args.legal_normal_count} LEGAL_NORMAL) ==")
        n = expand_attack(args.attack_per_signal, args.legal_normal_count)
        print(f"  attack prompts: +{n}\n")
    print(f"Done in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
