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
