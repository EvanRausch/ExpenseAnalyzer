"""
book.py — one person's data, loaded from the database in the shape the shared rules expect.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from server.db import ROW_FIELDS, Transaction, read_document, write_document
from src import ledger


class Book:
    """A person's transactions as the rows ledger works on, and the rest of their data
    through the same methods as ledger.FileStore. Nothing is stored until save()."""

    def __init__(self, db: Session, user_id: str):
        self.db = db
        self.user_id = user_id
        records = db.scalars(
            select(Transaction)
            .where(Transaction.user_id == user_id)
            .order_by(Transaction.position, Transaction.id)
        ).all()
        self._records = {record.id: record for record in records}
        self.rows = [self._row(r) for r in records if not r.trashed]
        self._trash = [self._row(r) for r in records if r.trashed]
        self._documents: dict[str, object] = {}

    @staticmethod
    def _row(record: Transaction) -> dict:
        row = {field: getattr(record, field) for field in ROW_FIELDS}
        # Underscore keys are ignored by the rules; this one finds the record again in save()
        row["_id"] = record.id
        return row

    def document(self, name: str, default):
        if name not in self._documents:
            self._documents[name] = read_document(self.db, self.user_id, name, None)
        body = self._documents[name]
        return default if body is None else body

    def set_document(self, name: str, body) -> None:
        self._documents[name] = body
        write_document(self.db, self.user_id, name, body)

    # ------------------------------------------------------------------
    # What ledger asks of a store
    # ------------------------------------------------------------------

    def trash(self) -> list[dict]:
        return list(self._trash)

    def save_trash(self, rows: list[dict]) -> None:
        self._trash = list(rows)

    def official_sources(self) -> set:
        return set(self.document("official_sources", []))

    def save_official_sources(self, sources: set) -> None:
        self.set_document("official_sources", sorted(sources))

    def save_correction(self, description: str, category: str) -> None:
        self.set_document(
            "corrections", {**self.document("corrections", {}), description: category}
        )

    def save_note(self, description: str, note: str) -> None:
        notes = dict(self.document("notes", {}))
        if note:
            notes[description] = note
        else:
            notes.pop(description, None)
        self.set_document("notes", notes)

    def known_categories(self) -> list[str]:
        return self.document("custom_categories", None) or list(ledger.KNOWN_CATEGORIES)

    def emoji(self, category: str) -> str:
        if category in ledger.BUILT_IN_EMOJI:
            return ledger.BUILT_IN_EMOJI[category]
        saved = self.document("emojis", {}).get(category)
        return saved if isinstance(saved, str) and ledger._is_emoji(saved) else "🏷️"

    def summary_descriptions(self) -> dict:
        return self.document("summary_descriptions", {})

    def ai_analysis(self) -> str:
        return self.document("ai_analysis", "")

    # ------------------------------------------------------------------

    def save(self) -> None:
        """Store the rows and the trash as they are now, writing only what changed."""
        kept: set[int] = set()
        for trashed, rows in ((False, self.rows), (True, self._trash)):
            for position, row in enumerate(rows):
                record = self._records.get(row.get("_id"))
                if record is None or record.id in kept:
                    record = Transaction(user_id=self.user_id)
                    self.db.add(record)
                else:
                    kept.add(record.id)
                for field in ROW_FIELDS:
                    value = row.get(field)
                    value = "" if value is None else str(value)
                    if getattr(record, field) != value:
                        setattr(record, field, value)
                record.position, record.trashed = position, trashed
        for record_id, record in self._records.items():
            if record_id not in kept:
                self.db.delete(record)
        self.db.flush()
