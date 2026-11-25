"""
make_demo_data.py — a folder of made-up transactions for the iPhone app's UI test, the
simulator and screenshots. Never real data.

    python ios/make_demo_data.py /tmp/demo
    xcodebuild test ... -only-testing:ExpenseAnalyzerUITests TEST_RUNNER_DATA_FOLDER=/tmp/demo
    # or in the simulator: launch argument  -dataFolder /tmp/demo

The merchants and amounts are the ones ExpenseAnalyzerUITests.swift looks for.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import categories  # noqa: E402

# (category, merchant, account, how many, typical amount)
SPENDING = [
    ("Supermarkets & grocery stores", "Maple Market", "credit", 9, 64),
    ("Supermarkets & grocery stores", "Metro", "debit", 5, 38),
    ("Restaurants", "Luna Pizzeria", "credit", 6, 42),
    ("Restaurants", "Chez Lu", "credit", 3, 78),
    ("Coffee shops & desserts", "Café Olimpico", "credit", 8, 5),
    ("Public transit", "STM", "debit", 6, 97),
    ("Fuel & gas stations", "Petro-Canada", "credit", 4, 55),
    ("Video & music streaming", "Netflix", "credit", 6, 17),
    ("Electronics & computers", "Gadget Depot", "credit", 2, 240),
    ("Rent", "Landlord", "debit", 6, 1450),
    ("Phone, internet & TV", "Fizz", "credit", 6, 49),
    ("Pharmacies & drugstores", "Jean Coutu", "credit", 4, 23),
    ("Gyms & fitness studios", "Econofitness", "credit", 6, 29),
    ("Clothing, shoes & accessories", "Simons", "credit", 2, 120),
]
INCOME = [
    ("Salary & wages", "Acme Corp", 2400, 6),
    ("Government benefits & tax refunds", "Canada Revenue Agency", 310, 3),
]


def _row(category, merchant, account, date, amount, income=False, index=0):
    description = f"{merchant.upper()} {index}"
    source = f"plaid_{account}.csv"
    return {
        "key": "\x1f".join([date, f"{amount:.2f}", description, source]),
        "date": date,
        "merchant": merchant,
        "summary": f"{category} at {merchant}" if not income else "",
        "bankDescription": description,
        "amount": amount,
        "category": category,
        "account": account,
        "isIncome": income,
        "mode": "transfer" if income else "in_person",
        "place": f"{merchant}, Montréal" if account == "credit" else "",
        "phone": "514-555-0100" if merchant == "Luna Pizzeria" else "",
        "website": "https://luna.example" if merchant == "Luna Pizzeria" else "",
        "notes": "",
    }


def build() -> dict:
    rng = random.Random(7)
    transactions, trash = [], []
    for category, merchant, account, count, typical in SPENDING:
        for i in range(count):
            month = 12 - (i * 11 // max(count - 1, 1))  # spread over the year
            date = f"2026-{month:02d}-{rng.randint(1, 28):02d}"
            amount = round(typical * rng.uniform(0.7, 1.3), 2)
            row = _row(category, merchant, account, date, amount, index=i)
            # One Gadget Depot purchase starts in the trash, so the test can restore it
            (trash if merchant == "Gadget Depot" and i == 1 else transactions).append(
                row
            )
    for category, merchant, amount, count in INCOME:
        for i in range(count):
            date = f"2026-{(i * 2 + 1):02d}-15"
            transactions.append(
                _row(category, merchant, "debit", date, -amount, True, i)
            )
    # One older year so the date filter has something to choose
    transactions.append(
        _row("Restaurants", "Luna Pizzeria", "credit", "2025-12-20", 51.0, index=99)
    )
    names = sorted({t["category"] for t in transactions + trash})
    groups = [
        {"name": g, "emoji": categories.GROUP_EMOJI.get(g, "🏷️"), "categories": m}
        for g, m in categories.grouped({}, names)
    ]
    emojis = {
        n: categories.emoji_of(n) or "🏷️"
        for n in set(names) | set(categories.ALL_CATEGORIES)
    }
    emojis.update({g["name"]: g["emoji"] for g in groups})
    return {
        "exportedAt": "2026-11-21T20:22:56Z",
        "appliedEdits": [],
        "transactions": transactions,
        "trash": trash,
        "categories": names,
        "groups": groups,
        "emojis": emojis,
        "categoryDescriptions": {"Restaurants": "Sit-down meals, tips included"},
        "merchantDescriptions": {
            "Supermarkets & grocery stores": {"Maple Market": "The weekly shop"}
        },
        "officialIncomeSources": ["Acme Corp"],
        "aiAnalysis": "## Spending\n- Rent is most of it\n- Eating out is **growing**",
    }


def main() -> None:
    folder = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/expenseanalyzer-demo")
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transactions.json").write_text(
        json.dumps(build(), ensure_ascii=False), encoding="utf-8"
    )
    (folder / "phone_edits.json").unlink(missing_ok=True)
    print(f"Wrote {folder / 'transactions.json'}")


if __name__ == "__main__":
    main()
