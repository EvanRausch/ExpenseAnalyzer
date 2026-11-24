"""
pipeline.py — bank syncs, run with the Mac app's own pipeline.

The pipeline (plaid_sync + enrich_transactions) works on a data folder. For each sync the
person's data is written to a private folder laid out like the Mac app's, the pipeline
runs there in its own process, and the results are read back into the database.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Callable

from sqlalchemy import or_, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from server.book import Book
from server.db import ROW_FIELDS, Bank, Job, User, banks_of, decrypt, encrypt, now
from src import ledger, plaid_sync

PROJECT_ROOT = Path(__file__).parent.parent
# Document name -> the file the Mac app keeps it in
DOCUMENT_FILES = {
    "corrections": "category_corrections.json",
    "notes": "user_notes.json",
    "official_sources": "official_income_sources.json",
    "custom_categories": "custom_categories.json",
    "emojis": "category_emojis.json",
    "summary_descriptions": "summary_descriptions.json",
    "places_cache": "places_cache.json",
}
PIPELINE_OUTPUTS = ("emojis", "summary_descriptions", "places_cache")
NEW_BANK_STATE = {"cursor": "", "accounts": {}, "transactions": {}}
# A sync still marked as running after this long is taken to have died
STALE_AFTER = datetime.timedelta(hours=1)


def _read(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _write(path: Path, body) -> None:
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")


def _plaid_state_file(data: Path) -> Path:
    return data / f"plaid_state_{plaid_sync.environment()}.json"


def _is_from_bank(row: dict) -> bool:
    return (row.get("source_file") or "").startswith("plaid_")


def _bank_state(item: dict) -> dict:
    """The part of a plaid_sync item that is kept in Bank.state."""
    return {key: item[key] for key in NEW_BANK_STATE if key in item}


# ---------------------------------------------------------------------------
# Database <-> folder
# ---------------------------------------------------------------------------


def import_folder(db: Session, engine: Engine, user_id: str, home: Path) -> None:
    """Copy the Mac app's data (home/data) into a new person's account."""
    data = home / "data"
    book = Book(db, user_id)
    enriched = data / "enriched_transactions.csv"
    if enriched.exists():
        with enriched.open(encoding="utf-8", errors="replace") as f:
            book.rows[:] = list(csv.DictReader(f))
    book.save_trash([r for r in _read(data / "trash.json", []) if isinstance(r, dict)])
    for name, file in DOCUMENT_FILES.items():
        body = _read(data / file, None)
        if body is not None:
            book.set_document(name, body)
    # The Mac app only shows its saved analysis while the statement files are unchanged
    cache = _read(data / "ai_analysis_cache.json", {})
    statements = sorted(p.name for p in (data / "statements").rglob("*.csv"))
    if cache.get("analysis") and cache.get("fingerprint") == "|".join(statements):
        book.set_document("ai_analysis", cache["analysis"])
    ledger.learn_transfers(book.rows, book)
    for item_id, item in _read(_plaid_state_file(data), {}).get("items", {}).items():
        db.add(
            Bank(
                user_id=user_id,
                item_id=item_id,
                institution=item.get("institution", ""),
                access_token=encrypt(engine, item["access_token"]),
                state=json.dumps({**NEW_BANK_STATE, **_bank_state(item)}),
            )
        )
    book.save()


def materialise(book: Book, banks: list[Bank], engine: Engine, home: Path) -> dict:
    """Write the person's data to home/data; returns what is needed to merge the results."""
    data = home / "data"
    data.mkdir(parents=True)
    if book.rows:
        with (data / "enriched_transactions.csv").open(
            "w", newline="", encoding="utf-8"
        ) as f:
            writer = csv.DictWriter(f, fieldnames=ROW_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(book.rows)
    _write(
        data / "trash.json",
        [{field: row.get(field, "") for field in ROW_FIELDS} for row in book.trash()],
    )
    for name, file in DOCUMENT_FILES.items():
        body = book.document(name, None)
        if body is not None:
            _write(data / file, body)
    items = {
        bank.item_id: {
            "access_token": decrypt(engine, bank.access_token),
            "institution": bank.institution,
            **json.loads(bank.state),
        }
        for bank in banks
    }
    _write(_plaid_state_file(data), {"items": items})
    return {
        "keys": {ledger._tx_key(row) for row in book.rows},
        "corrections": dict(book.document("corrections", {})),
        "notes": dict(book.document("notes", {})),
    }


def ingest(
    book: Book, banks: list[Bank], home: Path, before: dict, succeeded: bool
) -> None:
    """Read the pipeline's results from home/data into the person's account.

    `before` is what materialise returned; anything the person changed since then is kept.
    """
    data = home / "data"
    items = _read(_plaid_state_file(data), {}).get("items", {})
    for bank in banks:
        if bank.item_id in items:
            # Saved even after a failure, so a later sync continues from Plaid's cursor
            bank.state = json.dumps(_bank_state(items[bank.item_id]))
    enriched = data / "enriched_transactions.csv"
    if not succeeded or not enriched.exists():
        return
    for name in PIPELINE_OUTPUTS:
        body = _read(data / DOCUMENT_FILES[name], None)
        if body is not None:
            book.set_document(name, body)

    with enriched.open(encoding="utf-8", errors="replace") as f:
        fresh = list(csv.DictReader(f))
    key = ledger._tx_key
    trashed = {key(row) for row in book.trash()}
    fresh = [row for row in fresh if key(row) not in trashed]
    fresh_keys = {key(row) for row in fresh}
    # The pipeline only produces bank rows that existed when it started: keep the rest,
    # such as a transaction restored from the trash in the meantime
    kept = [
        row
        for row in book.rows
        if key(row) not in fresh_keys
        and not (_is_from_bank(row) and key(row) in before["keys"])
    ]
    records = defaultdict(list)
    for row in book.rows:
        records[key(row)].append(row["_id"])
    corrections = book.document("corrections", {})
    notes = book.document("notes", {})
    for row in fresh:
        if records[key(row)]:
            row["_id"] = records[key(row)].pop(0)
        description = row["description"]
        changed = corrections.get(description)
        if changed and changed != before["corrections"].get(description):
            row["gemini_category"] = changed
        if notes.get(description) != before["notes"].get(description):
            row["user_notes"] = notes.get(description, "")
    book.rows[:] = fresh + kept
    ledger.learn_transfers(book.rows, book)


# ---------------------------------------------------------------------------
# Running a sync
# ---------------------------------------------------------------------------


def run_pipeline(home: Path) -> dict:
    """Run worker.py on the folder; returns {"new_rows", "complete"}."""
    # The pipeline needs the Plaid and Google keys, not the keys to the database
    private = ("DATABASE_URL", "EXPENSE_SECRET_KEY")
    environment = {k: v for k, v in os.environ.items() if k not in private}
    result = subprocess.run(
        [sys.executable, "-m", "server.worker"],
        cwd=PROJECT_ROOT,
        env={**environment, "BUDGETTRACKER_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=STALE_AFTER.total_seconds(),
    )
    if result.returncode:
        lines = result.stderr.strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "the sync stopped unexpectedly")
    return json.loads(result.stdout.strip().splitlines()[-1])


def analyse(rows: list[dict], store) -> str:
    """Gemini's write-up of the spending, or "" when it is not available."""
    if not ledger._gemini_client:
        return ""
    text = ledger._gemini_spending_analysis(rows, store)
    return "" if text.startswith("Analysis failed:") else text


def start_job(engine: Engine, user_id: str) -> bool:
    """Mark a sync as running for the person; False if one already is."""
    with Session(engine) as db:
        if db.get(Job, user_id) is None:
            db.add(Job(user_id=user_id))
            try:
                db.commit()
            except IntegrityError:  # another request created it at the same moment
                db.rollback()
        started = db.execute(
            update(Job)
            .where(Job.user_id == user_id)
            .where(or_(Job.running.is_(False), Job.started_at < now() - STALE_AFTER))
            .values(running=True, started_at=now(), error="")
        )
        db.commit()
        return started.rowcount == 1


def run_job(
    engine: Engine, user_id: str, runner: Callable[[Path], dict] | None = None
) -> None:
    """Fetch the person's new bank transactions and categorise them, after start_job."""
    error = ""
    try:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            with Session(engine) as db:
                before = materialise(
                    Book(db, user_id), banks_of(db, user_id), engine, home
                )
            result = None
            try:
                result = (runner or run_pipeline)(home)
            except Exception as exc:  # noqa: BLE001 — reported to the person below
                error = str(exc)
            with Session(engine) as db:
                if db.get(User, user_id) is None:  # the account was deleted meanwhile
                    return
                book = Book(db, user_id)
                ingest(book, banks_of(db, user_id), home, before, result is not None)
                if (
                    result
                    and book.rows
                    and (result["new_rows"] or not book.ai_analysis())
                ):
                    text = analyse(book.rows, book)
                    if text:
                        book.set_document("ai_analysis", text)
                book.save()
                db.commit()
    except Exception as exc:  # noqa: BLE001
        error = error or str(exc)
    finally:
        with Session(engine) as db:
            db.execute(
                update(Job)
                .where(Job.user_id == user_id)
                .values(running=False, finished_at=now(), error=error[:300])
            )
            db.commit()


INTERRUPTED = "The server stopped during the sync; it will finish on the next one"


def interrupt_jobs(engine: Engine, user_ids: set[str]) -> None:
    """Record that these people's running syncs died with the server.

    Without this a sync killed mid-way would stay marked as running for STALE_AFTER.
    """
    if not user_ids:
        return
    with Session(engine) as db:
        db.execute(
            update(Job)
            .where(Job.user_id.in_(list(user_ids)))
            .where(Job.running.is_(True))
            .values(running=False, finished_at=now(), error=INTERRUPTED)
        )
        db.commit()


def set_webhook(engine: Engine, banks: list[Bank], url: str) -> None:
    """Tell Plaid to announce new transactions of these banks at `url`."""
    for bank in banks:
        plaid_sync._post(
            "/item/webhook/update",
            {"access_token": decrypt(engine, bank.access_token), "webhook": url},
        )
