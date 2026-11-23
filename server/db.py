"""
db.py — the server's database: every table is keyed by the person the data belongs to.

DATABASE_URL picks the database (a local SQLite file by default, Postgres when deployed).
EXPENSE_SECRET_KEY encrypts bank access tokens; a local SQLite database gets a key
file next to it instead.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import secrets
import uuid
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import ForeignKey, Text, create_engine, delete, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from src.enrich_transactions import OUTPUT_COLUMNS

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'data' / 'server.db'}"
# The columns of the Mac app's enriched_transactions.csv, all kept as text
ROW_FIELDS = tuple(OUTPUT_COLUMNS)


def now() -> datetime.datetime:
    """Current UTC time without a zone, which is what both databases store."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(primary_key=True)
    # The stable identifier Sign in with Apple gives this person; empty for local accounts
    apple_id: Mapped[str | None] = mapped_column(unique=True)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)


class Login(Base):
    """A signed-in device. Only the hash of its token is stored."""

    __tablename__ = "logins"

    token_hash: Mapped[str] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    created_at: Mapped[datetime.datetime] = mapped_column(default=now)


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    # Order within the person's transactions, or within their trash
    position: Mapped[int]
    trashed: Mapped[bool] = mapped_column(default=False)
    source_file: Mapped[str] = mapped_column(Text, default="")
    account_type: Mapped[str] = mapped_column(Text, default="")
    date: Mapped[str] = mapped_column(Text, default="")
    description: Mapped[str] = mapped_column(Text, default="")
    amount: Mapped[str] = mapped_column(Text, default="")
    cashback_pct: Mapped[str] = mapped_column(Text, default="")
    purchase_mode: Mapped[str] = mapped_column(Text, default="")
    gemini_merchant: Mapped[str] = mapped_column(Text, default="")
    gemini_category: Mapped[str] = mapped_column(Text, default="")
    place_name: Mapped[str] = mapped_column(Text, default="")
    place_address: Mapped[str] = mapped_column(Text, default="")
    place_phone: Mapped[str] = mapped_column(Text, default="")
    place_website: Mapped[str] = mapped_column(Text, default="")
    place_rating: Mapped[str] = mapped_column(Text, default="")
    place_google_maps_url: Mapped[str] = mapped_column(Text, default="")
    place_types: Mapped[str] = mapped_column(Text, default="")
    ai_description: Mapped[str] = mapped_column(Text, default="")
    user_notes: Mapped[str] = mapped_column(Text, default="")


class Document(Base):
    """One of a person's small JSON files from the Mac app (corrections, notes, caches…)."""

    __tablename__ = "documents"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    name: Mapped[str] = mapped_column(primary_key=True)
    body: Mapped[str] = mapped_column(Text)


class Bank(Base):
    """A bank connection made through Plaid."""

    __tablename__ = "banks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    item_id: Mapped[str] = mapped_column(unique=True)
    institution: Mapped[str] = mapped_column(default="")
    access_token: Mapped[str] = mapped_column(Text)  # encrypted
    # Plaid's sync cursor, accounts and raw transactions, as plaid_sync keeps them
    state: Mapped[str] = mapped_column(Text, default="{}")


class Job(Base):
    """The latest bank sync for a person."""

    __tablename__ = "jobs"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), primary_key=True)
    running: Mapped[bool] = mapped_column(default=False)
    started_at: Mapped[datetime.datetime | None]
    finished_at: Mapped[datetime.datetime | None]
    error: Mapped[str] = mapped_column(Text, default="")


PERSONAL_TABLES = (Login, Transaction, Document, Bank, Job)


def make_engine(url: str | None = None) -> Engine:
    """Open the database, creating the file and the tables on first use."""
    url = url or os.environ.get("DATABASE_URL") or DEFAULT_DATABASE_URL
    # Requests and bank syncs run on different threads, which SQLite refuses by default
    options = {"check_same_thread": False} if url.startswith("sqlite") else {}
    engine = create_engine(url, connect_args=options)
    Base.metadata.create_all(engine)
    return engine


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------


def _secret_key(engine: Engine) -> bytes:
    key = os.environ.get("EXPENSE_SECRET_KEY", "").strip()
    if key:
        return key.encode()
    if engine.url.get_backend_name() != "sqlite" or not engine.url.database:
        raise RuntimeError("EXPENSE_SECRET_KEY must be set for this database")
    path = Path(engine.url.database).with_suffix(".key")
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(Fernet.generate_key())
    return path.read_bytes().strip()


def encrypt(engine: Engine, text: str) -> str:
    return Fernet(_secret_key(engine)).encrypt(text.encode()).decode()


def decrypt(engine: Engine, text: str) -> str:
    return Fernet(_secret_key(engine)).decrypt(text.encode()).decode()


# ---------------------------------------------------------------------------
# People and their devices
# ---------------------------------------------------------------------------


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def add_user(db: Session, apple_id: str | None = None) -> User:
    user = User(id=uuid.uuid4().hex, apple_id=apple_id)
    db.add(user)
    return user


def add_login(db: Session, user: User) -> str:
    """Sign a device in; returns the token it must send with every request."""
    token = secrets.token_urlsafe(32)
    db.add(Login(token_hash=_hash(token), user_id=user.id))
    return token


def user_for_token(db: Session, token: str) -> User | None:
    login = db.get(Login, _hash(token))
    return db.get(User, login.user_id) if login else None


def remove_login(db: Session, token: str) -> None:
    db.execute(delete(Login).where(Login.token_hash == _hash(token)))


def delete_user(db: Session, user: User) -> None:
    """Erase the person and everything stored for them."""
    for table in PERSONAL_TABLES:
        db.execute(delete(table).where(table.user_id == user.id))
    db.delete(user)


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def read_document(db: Session, user_id: str, name: str, default):
    document = db.get(Document, (user_id, name))
    return json.loads(document.body) if document else default


def write_document(db: Session, user_id: str, name: str, body) -> None:
    text = json.dumps(body, ensure_ascii=False)
    document = db.get(Document, (user_id, name))
    if document:
        document.body = text
    else:
        db.add(Document(user_id=user_id, name=name, body=text))
        db.flush()


def banks_of(db: Session, user_id: str) -> list[Bank]:
    return list(
        db.scalars(select(Bank).where(Bank.user_id == user_id).order_by(Bank.id))
    )
