"""Tests for the Plaid sync module (Plaid itself is faked; nothing touches the network)."""

from types import SimpleNamespace

import pytest

from src import enrich_transactions, plaid_sync

CHEQUING, VISA, RRSP = "acc-chequing", "acc-visa", "acc-rrsp"
ACCOUNTS = [
    {"account_id": CHEQUING, "type": "depository"},
    {"account_id": VISA, "type": "credit"},
    {"account_id": RRSP, "type": "investment"},
]


def _txn(txn_id, account, day, amount, description, pending=False, authorized=None):
    return {
        "transaction_id": txn_id,
        "account_id": account,
        "date": day,
        "authorized_date": authorized,
        "original_description": description,
        "name": "cleaned-up name",
        "amount": amount,
        "pending": pending,
    }


def _page(added=(), modified=(), removed=(), cursor="c1"):
    return {
        "added": list(added),
        "modified": list(modified),
        "removed": [{"transaction_id": t} for t in removed],
        "accounts": ACCOUNTS,
        "next_cursor": cursor,
        "has_more": False,
        "transactions_update_status": "HISTORICAL_UPDATE_COMPLETE",
    }


@pytest.fixture
def plaid(tmp_path, monkeypatch):
    """A temp data folder with one linked bank, and a fake Plaid to queue responses on."""
    monkeypatch.setattr(plaid_sync, "DATA_DIR", tmp_path)
    monkeypatch.setenv("PLAID_ENV", "production")
    plaid_sync._save_state(
        {
            "items": {
                "item-1": {
                    "access_token": "access-test",
                    "institution": "Test Bank",
                    "cursor": "",
                    "accounts": {},
                    "transactions": {},
                }
            }
        }
    )
    fake = SimpleNamespace(requests=[], responses=[])

    def fake_post(path, payload):
        fake.requests.append((path, payload))
        return fake.responses.pop(0)

    monkeypatch.setattr(plaid_sync, "_post", fake_post)
    return fake


def test_sync_writes_csvs_the_enrichment_loader_can_read(plaid, tmp_path):
    plaid.responses.append(
        _page(
            added=[
                _txn("t1", CHEQUING, "2026-03-02", 42.5, "ACHAT METRO #123"),
                _txn("t2", CHEQUING, "2026-03-03", -1500, "PAIE EMPLOYEUR"),
                _txn(
                    "t3",
                    VISA,
                    "2026-03-05",
                    19.99,
                    "NETFLIX.COM",
                    authorized="2026-03-04",
                ),
                _txn("t4", VISA, "2026-03-06", 5, "STILL PENDING", pending=True),
                _txn("t5", RRSP, "2026-03-07", 100, "INVESTMENT BUY"),
            ]
        )
    )

    result = plaid_sync.sync()

    assert result == plaid_sync.SyncResult(new_rows=4, complete=True)
    debit_csv = tmp_path / "statements" / "debit" / "plaid_debit.csv"
    credit_csv = tmp_path / "statements" / "credit" / "plaid_credit.csv"

    assert enrich_transactions._is_parsed_debit_csv(debit_csv)
    debit = enrich_transactions._read_parsed_debit_csv(debit_csv)
    assert [
        (r["account_type"], r["date"], r["description"], r["amount"], r["pending"])
        for r in debit
    ] == [
        ("debit", "2026-03-02", "ACHAT METRO #123", "42.50", ""),
        ("debit", "2026-03-03", "PAIE EMPLOYEUR", "-1500.00", ""),
    ]

    assert not enrich_transactions._is_parsed_debit_csv(credit_csv)
    assert enrich_transactions._is_credit_csv(credit_csv)
    credit = enrich_transactions._read_credit_csv(credit_csv)
    # Investment transactions are left out; pending ones are marked; the purchase date
    # wins over the posting date
    assert [
        (r["account_type"], r["date"], r["description"], r["amount"], r["pending"])
        for r in credit
    ] == [
        ("credit", "2026-03-04", "NETFLIX.COM", "19.99", ""),
        ("credit", "2026-03-06", "STILL PENDING", "5.00", "1"),
    ]


def test_days_covered_by_a_downloaded_statement_are_skipped(plaid, tmp_path):
    debit_dir = tmp_path / "statements" / "debit"
    debit_dir.mkdir(parents=True)
    (debit_dir / "debit_2026-01-01_to_2026-01-31.csv").write_text("")
    plaid.responses.append(
        _page(
            added=[
                _txn("t1", CHEQUING, "2026-01-31", 10, "INSIDE STATEMENT"),
                _txn("t2", CHEQUING, "2026-02-01", 20, "AFTER STATEMENT"),
            ]
        )
    )

    plaid_sync.sync()

    rows = enrich_transactions._read_parsed_debit_csv(debit_dir / "plaid_debit.csv")
    assert [r["description"] for r in rows] == ["AFTER STATEMENT"]


def test_later_sync_resumes_from_cursor_and_applies_changes(plaid, tmp_path):
    plaid.responses.append(
        _page(
            added=[
                _txn("t1", CHEQUING, "2026-03-02", 10, "KEPT"),
                _txn("t2", CHEQUING, "2026-03-03", 20, "REMOVED LATER"),
                _txn("t3", CHEQUING, "2026-03-04", 30, "AMOUNT CHANGES"),
            ],
            cursor="c1",
        )
    )
    plaid_sync.sync()
    plaid.responses.append(
        _page(
            modified=[_txn("t3", CHEQUING, "2026-03-04", 35, "AMOUNT CHANGES")],
            removed=["t2"],
            cursor="c2",
        )
    )

    result = plaid_sync.sync()

    assert plaid.requests[-1][1]["cursor"] == "c1"
    assert result.new_rows == 1
    rows = enrich_transactions._read_parsed_debit_csv(
        tmp_path / "statements" / "debit" / "plaid_debit.csv"
    )
    assert [(r["description"], r["amount"]) for r in rows] == [
        ("KEPT", "10.00"),
        ("AMOUNT CHANGES", "35.00"),
    ]


def test_a_pending_line_is_replaced_by_the_posted_one(plaid, tmp_path):
    """Plaid removes the pending transaction and adds the posted one in its place."""
    plaid.responses.append(
        _page(
            added=[_txn("p1", VISA, "2026-03-06", 36.11, "IGA 8093", pending=True)],
            cursor="c1",
        )
    )
    plaid_sync.sync()
    credit_csv = tmp_path / "statements" / "credit" / "plaid_credit.csv"
    rows = enrich_transactions._read_credit_csv(credit_csv)
    assert [(r["description"], r["amount"], r["pending"]) for r in rows] == [
        ("IGA 8093", "36.11", "1")
    ]

    plaid.responses.append(
        _page(
            added=[
                _txn(
                    "t9", VISA, "2026-03-08", 36.11, "IGA 8093", authorized="2026-03-06"
                )
            ],
            removed=["p1"],
            cursor="c2",
        )
    )
    plaid_sync.sync()

    rows = enrich_transactions._read_credit_csv(credit_csv)
    assert [(r["date"], r["description"], r["amount"], r["pending"]) for r in rows] == [
        ("2026-03-06", "IGA 8093", "36.11", "")
    ]


def test_sync_waits_for_a_newly_linked_bank(plaid, tmp_path, monkeypatch):
    monkeypatch.setattr(plaid_sync.time, "sleep", lambda _seconds: None)
    nothing_yet = _page(cursor="")
    nothing_yet["transactions_update_status"] = "TRANSACTIONS_UPDATE_STATUS_UNKNOWN"
    plaid.responses += [
        nothing_yet,
        _page(added=[_txn("t1", CHEQUING, "2026-03-02", 10, "FIRST DATA")]),
    ]

    result = plaid_sync.sync()

    assert result == plaid_sync.SyncResult(new_rows=1, complete=True)


def test_sandbox_data_stays_out_of_the_real_statements(plaid, tmp_path, monkeypatch):
    monkeypatch.setenv("PLAID_ENV", "sandbox")

    assert plaid_sync.statements_dir() == tmp_path / "plaid_sandbox"
    # Sandbox and production keep separate lists of linked banks
    assert plaid_sync.linked_banks() == []
