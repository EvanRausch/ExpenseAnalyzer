"""Tests for the rules shared by the Mac app and anything else that reads the data."""

import datetime
import subprocess
import sys
from pathlib import Path

import pytest

from src import ledger


def test_the_rules_load_without_the_window_toolkit():
    code = "import sys; import src.ledger; print(any(m.startswith('PySide6') for m in sys.modules))"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "False"


@pytest.mark.parametrize(
    "text, expected",
    [
        ("2026-03-02", datetime.date(2026, 3, 2)),
        ("2026/03/02", datetime.date(2026, 3, 2)),
        ("  2024-02-29 ", datetime.date(2024, 2, 29)),
        ("2026-02-30", None),
        ("2026/03-02", None),
        ("2026-3-2", None),
        ("02/03/2026", None),
        ("", None),
    ],
)
def test_dates_with_a_year_are_read_strictly(text, expected):
    assert ledger.row_date({"date": text}) == expected


def test_statement_dates_take_their_year_from_the_file_name():
    row = {"date": "05 03", "source_file": "credit_2024-02-20_to_2024-03-18.csv"}

    assert ledger.row_date(row) == datetime.date(2024, 3, 5)


def test_a_statement_date_in_the_future_belongs_to_the_year_before():
    tomorrow = datetime.date.today() + datetime.timedelta(days=1)
    row = {"date": f"{tomorrow.day} {tomorrow.month}", "source_file": "credit.csv"}

    try:
        expected = tomorrow.replace(year=tomorrow.year - 1)
    except ValueError:  # 29 February has no counterpart the year before
        expected = None
    assert ledger.row_date(row) == expected


@pytest.mark.parametrize("text", ["31 02", "5 13", "x y", "05 03 2026"])
def test_unreadable_statement_dates_are_skipped(text):
    assert ledger.row_date({"date": text, "source_file": "credit_2024.csv"}) is None


def test_overall_statistics_group_spending_by_month():
    def row(day, amount, **extra):
        return {"date": day, "amount": amount, "account_type": "credit", **extra}

    stats = ledger.compute_stats_overall(
        [
            row("2026-01-05", "40.00", gemini_category="Groceries"),
            row("2026-01-20", "60.00", gemini_category="Rent"),
            row("2026/02/03", "25.50", gemini_category="Groceries"),
            row("", "999", gemini_category="Rent"),
            row("2026-02-10", "-500", account_type="debit"),
        ]
    )

    # A row without a readable date still counts as spending, just not in any month
    assert stats["monthly"] == {"2026-01": 100.0, "2026-02": 25.5}
    assert stats["total_expense"] == 1124.5
    assert stats["total_income"] == 500.0
    assert stats["top_category"] == "Rent"


@pytest.fixture(autouse=True)
def no_saved_transfer_descriptions(monkeypatch):
    """Keeps the descriptions saved in the real data folder out of these tests."""
    monkeypatch.setattr(ledger, "_load_transfer_descriptions", lambda: set())


class Memory:
    """A store with only what learning transfers needs."""

    def __init__(self, trash=()):
        self.descriptions = set()
        self._trash = list(trash)

    def trash(self):
        return self._trash

    def transfer_descriptions(self):
        return set(self.descriptions)

    def save_transfer_descriptions(self, descriptions):
        self.descriptions = set(descriptions)


def bank(day, amount, description="Purchase /SHOP", **extra):
    return {
        "account_type": "debit",
        "date": day,
        "amount": amount,
        "description": description,
        **extra,
    }


def card(day, amount, description="SHOP MONTREAL QC", **extra):
    return {**bank(day, amount, description, **extra), "account_type": "credit"}


def test_money_leaving_the_bank_and_reaching_the_card_is_a_transfer():
    paid = bank("2026-03-06", "500.00", "Withdrawal")
    received = card("2026-03-09", "-500.00", "CREDIT")  # the Monday after
    groceries = card("2026-03-06", "500.00")
    rows = [paid, groceries, received]

    assert ledger.transfer_rows(rows) == [paid, received]
    assert ledger.spending_rows(rows) == [groceries]
    assert ledger.visible_rows(rows) == [groceries]


def test_a_cash_advance_from_the_card_is_neither_income_nor_spending():
    rows = [
        card("2026-05-12", "200.00", "ADV"),
        bank("2026-05-12", "-200.00", "Deposit"),
    ]

    stats = ledger.compute_stats_overall(rows)

    assert ledger.income_rows(rows) == []
    assert (stats["total_income"], stats["total_expense"]) == (0, 0)


@pytest.mark.parametrize(
    "first, second",
    [
        (bank("2026-03-06", "500.00"), card("2026-03-10", "-500.00", "CREDIT")),
        (bank("2026-03-06", "500.00"), card("2026-03-06", "-500.01", "CREDIT")),
        (bank("2026-03-06", "500.00"), card("2026-03-06", "500.00")),
        (bank("2026-03-06", "500.00"), bank("2026-03-06", "-500.00", "Deposit")),
    ],
    ids=["four days apart", "another amount", "both spent", "same account"],
)
def test_lines_that_only_resemble_each_other_are_not_a_transfer(first, second):
    assert ledger.transfer_rows([first, second]) == []


def test_each_line_is_one_side_of_a_single_transfer():
    earlier = bank("2026-03-02", "50.00")
    same_day = bank("2026-03-05", "50.00")
    received = card("2026-03-05", "-50.00", "CREDIT")
    rows = [earlier, same_day, received]

    assert ledger.transfer_rows(rows) == [same_day, received]
    assert ledger.spending_rows(rows) == [earlier]


@pytest.mark.parametrize(
    "row, expected",
    [
        (card("2026-03-06", "-800.00", "PAIEMENT CAISSE"), True),
        (card("2026-03-06", "-800.00", "PAYMENT - THANK YOU"), True),
        (
            bank("2026-03-06", "-300.00", "Credit card cash advance-Internet /**8044"),
            True,
        ),
        (
            bank(
                "2026-03-06",
                "800.00",
                "Paiement facture - AccesD Internet /Remises",
                gemini_category="Bill Payment",
                gemini_merchant="Remises",
            ),
            True,
        ),
        (card("2026-03-06", "-30.00", "AMZN Mktp CA"), False),
        (card("2026-03-06", "45.00", "PAYMENT PLAN STORE"), False),
        (
            bank(
                "2026-03-06",
                "90.00",
                "Bill payment - AccesD - Internet /BELL CANADA",
                gemini_category="Telecom & Internet",
                gemini_merchant="Bell Canada",
            ),
            False,
        ),
    ],
    ids=[
        "card payment",
        "card payment in English",
        "cash advance received",
        "payment to the card under its French name",
        "refund on the card",
        "purchase on the card",
        "bill paid to a company",
    ],
)
def test_a_transfer_missing_its_other_side_is_known_by_its_wording(row, expected):
    assert (ledger.transfer_rows([row]) == [row]) is expected


def test_a_description_matched_as_a_transfer_is_one_without_its_other_side():
    store = Memory()
    history = [
        bank("2026-01-05", "500.00", "Virement  /MA CARTE"),
        card("2026-01-05", "-500.00", "VERSEMENT REÇU"),
        bank("2026-02-05", "320.00", "Virement /MA CARTE"),
        card("2026-02-06", "-320.00", "VERSEMENT REÇU"),
    ]
    # Written as a downloaded statement does: no accent, other capitals
    alone = card("2026-03-05", "-410.00", "Versement recu")
    purchase = card("2026-03-05", "410.00")

    ledger.learn_transfers(history, store)

    assert store.descriptions == {"virement /ma carte", "versement recu"}
    assert ledger.transfer_rows([alone, purchase], store) == [alone]
    assert ledger.spending_rows([alone, purchase], store) == [purchase]
    # Someone who never had those transfers
    assert ledger.transfer_rows([alone, purchase], Memory()) == []


def test_chance_matches_do_not_make_a_description_a_transfer():
    store = Memory()
    cash_back = [card(f"2026-0{m}-10", "-25.00", "CRÉDIT REMISES") for m in range(1, 6)]
    # Sent to two people on days a cash-back credit of the same amount arrived
    sent = [
        bank("2026-01-10", "25.00", "Interac e-Transfer to /Raul"),
        bank("2026-02-11", "25.00", "Interac e-Transfer to /Jose"),
    ]

    ledger.learn_transfers(cash_back + sent, store)

    assert store.descriptions == set()


def test_transfers_in_the_trash_are_learned_from_too():
    trash = [
        card("2026-04-23", "500.00", "AVANCE"),
        bank("2026-04-23", "-500.00", "Advance received"),
        card("2026-05-06", "120.00", "AVANCE"),
        bank("2026-05-06", "-120.00", "Advance received"),
    ]
    store = Memory(trash)
    store.descriptions = {"learned earlier"}

    ledger.learn_transfers([], store)

    assert store.descriptions == {"learned earlier", "avance", "advance received"}
