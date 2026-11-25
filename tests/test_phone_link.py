"""Tests for the iPhone link: the file the Mac app exports and the changes it takes back."""

import json

import pytest

from src import ledger


def _row(**overrides):
    row = {
        "source_file": "plaid_debit.csv",
        "account_type": "debit",
        "date": "2026-03-02",
        "description": "ACHAT METRO #123",
        "amount": "42.50",
        "purchase_mode": "in_person",
        "gemini_merchant": "Metro",
        "gemini_category": "Groceries",
        "ai_description": "Groceries at Metro",
        "place_name": "Metro Plus",
        "place_phone": "514-555-0100",
        "place_website": "https://metro.example",
        "user_notes": "",
    }
    row.update(overrides)
    return row


@pytest.fixture
def mac(tmp_path, monkeypatch):
    """Stand-ins for the Mac app's own files, so nothing touches the real data folder."""
    files = {"trash": [], "official": set(), "corrections": {}, "notes": {}}
    monkeypatch.setattr(ledger, "load_trash", lambda: list(files["trash"]))
    monkeypatch.setattr(
        ledger, "save_trash", lambda rows: files.update(trash=list(rows))
    )
    monkeypatch.setattr(
        ledger, "_load_official_sources", lambda: set(files["official"])
    )
    monkeypatch.setattr(
        ledger, "_save_official_sources", lambda s: files.update(official=set(s))
    )
    monkeypatch.setattr(ledger, "_load_transfer_descriptions", lambda: set())
    monkeypatch.setattr(
        ledger,
        "save_correction",
        lambda desc, cat: files["corrections"].update({desc: cat}),
    )
    monkeypatch.setattr(
        ledger,
        "save_user_note",
        lambda desc, note: files["notes"].update({desc: note}),
    )
    monkeypatch.setattr(ledger, "_get_known_categories", lambda: ["Groceries", "Rent"])
    monkeypatch.setattr(ledger.FileStore, "custom_categories", lambda self: {})
    monkeypatch.setattr(
        ledger,
        "_load_summary_descs",
        lambda: {"categories": {"Groceries": "Food shops"}, "merchants": {}},
    )
    monkeypatch.setattr(ledger, "_load_ai_cache", lambda: ("statements-v1", "## Trend"))
    monkeypatch.setattr(ledger, "_statements_fingerprint", lambda: "statements-v1")
    files["folder"] = tmp_path / "ExpenseAnalyzer"
    files["applied"] = tmp_path / "phone_edits_applied.json"
    return files


def _export(mac, rows):
    ledger.export_for_phone(rows, mac["folder"], mac["applied"])
    return json.loads((mac["folder"] / "transactions.json").read_text(encoding="utf-8"))


def _phone_sends(mac, *edits):
    mac["folder"].mkdir(exist_ok=True)
    (mac["folder"] / "phone_edits.json").write_text(json.dumps({"edits": list(edits)}))


def _apply(mac, rows):
    return ledger.apply_phone_edits(rows, mac["folder"], mac["applied"])


def test_export_holds_what_the_mac_app_shows(mac):
    mac["trash"] = [_row(description="TRASHED", amount="9.99")]
    mac["official"] = {"Employer"}
    rows = [
        _row(),
        _row(description="PAIE", amount="-1500", gemini_merchant="Employer"),
        # Statement credit rows carry only "day month"; the year comes from the file name
        _row(
            account_type="credit",
            date="05 03",
            source_file="credit_2026-02-20_to_2026-03-18.csv",
            gemini_merchant="",
            gemini_category="",
        ),
        # Paying off the credit card is hidden everywhere in the Mac app
        _row(gemini_merchant="Visa Desjardins", gemini_category="Bill Payment"),
    ]

    export = _export(mac, rows)

    groceries, pay, card_purchase = export["transactions"]
    assert groceries == {
        "key": "2026-03-02\x1f42.50\x1fACHAT METRO #123\x1fplaid_debit.csv",
        "date": "2026-03-02",
        "merchant": "Metro",
        "summary": "Groceries at Metro",
        "bankDescription": "ACHAT METRO #123",
        "amount": 42.5,
        "category": "Groceries",
        "account": "debit",
        "isIncome": False,
        "mode": "in_person",
        "place": "Metro Plus",
        "phone": "514-555-0100",
        "website": "https://metro.example",
        "notes": "",
    }
    assert (pay["amount"], pay["isIncome"]) == (-1500.0, True)
    assert (card_purchase["date"], card_purchase["merchant"]) == (
        "2026-03-05",
        "ACHAT METRO #123",
    )
    assert card_purchase["category"] == "Other"
    assert [t["bankDescription"] for t in export["trash"]] == ["TRASHED"]
    assert export["categories"] == ["Bill Payment", "Groceries", "Other", "Rent"]
    # Every category sits in a group of the two-level list; old names go under "Other"
    placed = {
        group["name"]: sorted(set(group["categories"]) & set(export["categories"]))
        for group in export["groups"]
        if set(group["categories"]) & set(export["categories"])
    }
    assert placed == {
        "Housing & home": ["Rent"],
        "Other": ["Bill Payment", "Groceries", "Other"],
    }
    assert [g["name"] for g in export["groups"]][:2] == [
        "Housing & home",
        "Utilities & telecom",
    ]
    assert all(g["emoji"] for g in export["groups"])
    assert set(export["emojis"]) == set(export["categories"]) | {
        g["name"] for g in export["groups"]
    }
    assert export["categoryDescriptions"] == {"Groceries": "Food shops"}
    assert export["officialIncomeSources"] == ["Employer"]
    assert export["aiAnalysis"] == "## Trend"
    assert export["exportedAt"].endswith("Z")
    assert not list(mac["folder"].glob(".*"))


def test_nothing_is_written_when_icloud_drive_is_off(mac, tmp_path):
    ledger.export_for_phone([_row()], tmp_path / "no-icloud" / "ExpenseAnalyzer")

    assert not (tmp_path / "no-icloud").exists()


def test_category_change_from_the_phone_follows_the_mac_rules(mac):
    edited = _row()
    same_merchant = _row(date="2026-03-09", description="ACHAT METRO #456")
    already_other = _row(
        date="2026-03-10", description="METRO PHARMA", gemini_category="Pharmacy"
    )
    rows = [edited, same_merchant, already_other]
    _phone_sends(
        mac,
        {
            "id": "e1",
            "type": "setCategory",
            "keys": [ledger._phone_key(edited)],
            "value": "Rent",
        },
    )

    assert _apply(mac, rows) == 1

    # The merchant's other rows in the old category follow; rows already recategorised do not
    assert [r["gemini_category"] for r in rows] == ["Rent", "Rent", "Pharmacy"]
    assert mac["corrections"] == {
        "ACHAT METRO #123": "Rent",
        "ACHAT METRO #456": "Rent",
    }


def test_notes_trash_restore_and_official_sources_from_the_phone(mac):
    kept = _row()
    same_description = _row(date="2026-03-09")
    trashed = _row(description="OOPS", amount="5.00")
    in_trash = _row(description="BRING BACK", amount="7.00")
    mac["trash"] = [in_trash]
    rows = [kept, same_description, trashed]
    _phone_sends(
        mac,
        {
            "id": "e1",
            "type": "setNote",
            "keys": [ledger._phone_key(kept)],
            "value": "weekly shop",
        },
        {"id": "e2", "type": "trash", "keys": [ledger._phone_key(trashed)]},
        {"id": "e3", "type": "restore", "keys": [ledger._phone_key(in_trash)]},
        {"id": "e4", "type": "markOfficial", "value": "Employer"},
    )

    assert _apply(mac, rows) == 4

    assert [r["description"] for r in rows] == [
        "ACHAT METRO #123",
        "ACHAT METRO #123",
        "BRING BACK",
    ]
    assert [r["user_notes"] for r in rows] == ["weekly shop", "weekly shop", ""]
    assert mac["notes"] == {"ACHAT METRO #123": "weekly shop"}
    assert [r["description"] for r in mac["trash"]] == ["OOPS"]
    assert mac["official"] == {"Employer"}


def test_each_phone_change_is_applied_once_and_acknowledged(mac):
    rows = [_row(), _row(description="OOPS", amount="5.00")]
    _phone_sends(
        mac, {"id": "e1", "type": "trash", "keys": [ledger._phone_key(rows[1])]}
    )

    assert _apply(mac, rows) == 1
    assert _apply(mac, rows) == 0
    assert len(mac["trash"]) == 1
    # The export tells the phone which changes it can stop sending
    assert _export(mac, rows)["appliedEdits"] == ["e1"]


def test_junk_entries_in_the_phone_file_are_ignored(mac):
    mac["folder"].mkdir()
    (mac["folder"] / "phone_edits.json").write_text(
        '{"edits": ["junk", {"type": "trash"}]}'
    )
    rows = [_row()]

    assert _apply(mac, rows) == 0
    assert len(rows) == 1
