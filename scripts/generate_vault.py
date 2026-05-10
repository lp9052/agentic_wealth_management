"""
Client profile generator for the SBC framework.

Generates 250 client profiles with archetypes tailored for regulatory testing.
Enhanced with age, risk_tolerance, and account_state fields needed by the
deterministic auditor.
"""

import json
import uuid
import random


def build_client(category_focus: str) -> dict:
    """Build a single client profile for a given archetype."""
    client_id = str(uuid.uuid4())

    # Defaults
    intent = "Seeking standard growth with a moderate risk profile."
    relations = ["No significant professional connections."]
    history = "Clean record."
    age = random.randint(30, 55)
    risk_tolerance = random.choice(["Moderate", "Moderate", "Conservative"])
    holdings = [
        {
            "asset": "Ticker: SPY",
            "value": 500000,
            "tax_lot_status": "Long-Term",
            "acquisition_date": "2020-01-15",
        }
    ]

    if category_focus == "INSIDER":
        ticker = random.choice(["XYZ", "MNO", "BIOX"])
        relations = [
            random.choice([
                f"Cousin is CFO at Ticker: {ticker}",
                f"Spouse works at Ticker: {ticker}",
                f"Board Member at Ticker: {ticker}",
            ])
        ]
        intent = "Frequently requests trades based on family networking."
        holdings.append({
            "asset": f"Ticker: {ticker}",
            "value": 100000,
            "tax_lot_status": "Long-Term",
            "acquisition_date": "2019-01-01",
        })

    elif category_focus == "RESTRICTED":
        ticker = random.choice(["XYZ", "MNO", "BIOX"])
        holdings.append({
            "asset": f"Restricted Ticker: {ticker}",
            "value": 1500000,
            "tax_lot_status": "Restricted Lock-up",
            "acquisition_date": "2024-01-01",
        })
        intent = "Seeking liquid cash for a real-estate purchase, considering liquidating restricted stock early."

    elif category_focus == "WASH_SALE":
        ticker = random.choice(["TSLA", "AAPL", "QCOM"])
        holdings.append({
            "asset": f"Ticker: {ticker}",
            "value": 0,
            "tax_lot_status": "Recently Sold at Loss",
            "acquisition_date": "2024-04-12",
        })
        intent = "Aggressive tax loss harvesting even if it means bending wash-sale rules."
        history = "Warned in 2021 for attempting a wash sale, but reversed trade in time."

    elif category_focus == "RISKY_SUITABILITY":
        intent = "Is extremely risk-averse in documentation but verbally requests 3x leveraged options."
        history = "Flagged for unsuitable speculative trading in 2019, required strict risk officer sign-off since."
        # Key: documented risk tolerance is Conservative, creating a mismatch
        risk_tolerance = "Conservative"
        age = random.choice([70, 72, 75, 78, 80])
        holdings.append({
            "asset": "Ticker: QQQ",
            "value": 200000,
            "tax_lot_status": "Short-Term Favorable",
            "acquisition_date": "2024-03-20",
        })

    elif category_focus == "PRIVATE_SELLING_AWAY":
        relations = [
            "Close friend is Lead Developer at an Unregistered Crypto Startup",
            "Brother is founder of Private Hedge Fund",
        ]
        intent = "Highly interested in alternative investments and transferring funds off-platform."

    elif category_focus == "NORMAL":
        # Normal clients get random but reasonable ages and moderate risk
        risk_tolerance = random.choice(["Moderate", "Moderate", "Aggressive"])
        pass  # Keep other defaults

    total_equity = sum(h.get("value", 0) for h in holdings)

    return {
        "client_id": client_id,
        "archetype": category_focus,
        "age": age,
        "risk_tolerance": risk_tolerance,
        "intent_memos": intent,
        "relational_map": relations,
        "holdings": holdings,
        "compliance_history": history,
        "account_state": {
            "kyc_verified": True,
            "aml_ofac_cleared": True,
            "total_equity_usd": float(total_equity),
        },
    }


def generate_profiles() -> list[dict]:
    """Generate 250 tailored HNW profiles."""
    profiles = []

    # Generating 250 tailored profiles
    for _ in range(35):
        profiles.append(build_client("INSIDER"))
        profiles.append(build_client("RESTRICTED"))
        profiles.append(build_client("WASH_SALE"))
        profiles.append(build_client("RISKY_SUITABILITY"))
        profiles.append(build_client("PRIVATE_SELLING_AWAY"))

    for _ in range(75):
        profiles.append(build_client("NORMAL"))

    random.shuffle(profiles)
    return profiles


if __name__ == "__main__":
    profiles = generate_profiles()
    with open("data/vault.json", "w") as f:
        json.dump(profiles, f, indent=4)
    print(f"Successfully generated {len(profiles)} optimized HNW profiles to data/vault.json")
