"""
cli.py — run and administer the server from the terminal.

    python -m server.cli serve            start the API on http://127.0.0.1:8000
    python -m server.cli add-user         create an empty account and print its token
    python -m server.cli import-mac       create an account holding the Mac app's data
    python -m server.cli new-token        print a fresh token for the account (one account)
    python -m server.cli set-webhook URL  tell Plaid where to announce new transactions

The printed token is what the app sends as "Authorization: Bearer <token>"; the iPhone app
accepts it on its sign-in screen. Signing in with Apple later from that phone attaches the
Apple account to the same data.

DATABASE_URL and EXPENSE_SECRET_KEY choose the database (see db.py); PLAID_ENV must match
the Mac app's bank connection (production) for import-mac to bring it along. When
PLAID_WEBHOOK_URL is set, import-mac also points the imported banks at it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session

from server import pipeline
from server.app import create_app
from server.db import (
    PROJECT_ROOT,
    Bank,
    Transaction,
    User,
    add_login,
    add_user,
    banks_of,
    make_engine,
)


def _already_imported(db: Session, home: Path) -> bool:
    """True if a bank from this Mac's Plaid state is already in the database."""
    path = pipeline._plaid_state_file(home / "data")
    try:
        items = json.loads(path.read_text(encoding="utf-8")).get("items", {})
    except (OSError, json.JSONDecodeError):
        return False
    return any(db.scalar(select(Bank).where(Bank.item_id == i)) for i in items)


def main() -> None:
    parser = argparse.ArgumentParser(description="ExpenseAnalyzer server")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("add-user", help="create an empty account")
    mac = commands.add_parser(
        "import-mac", help="create an account from the Mac app's data"
    )
    mac.add_argument(
        "--home", type=Path, default=PROJECT_ROOT, help="folder containing data/"
    )
    commands.add_parser("new-token", help="print a fresh token for the account")
    hook = commands.add_parser(
        "set-webhook", help="point every connected bank at the server's webhook"
    )
    hook.add_argument("url", help="the address ending in /plaid/webhook")
    args = parser.parse_args()

    engine = make_engine()
    if args.command == "serve":
        uvicorn.run(create_app(engine), host=args.host, port=args.port)
        return
    if args.command == "new-token":
        with Session(engine) as db:
            users = db.scalars(select(User)).all()
            if len(users) != 1:
                sys.exit(f"Expected one account, found {len(users)}.")
            token = add_login(db, users[0])
            db.commit()
        print(f"Token: {token}")
        return
    if args.command == "set-webhook":
        with Session(engine) as db:
            banks = db.scalars(select(Bank)).all()
            pipeline.set_webhook(engine, banks, args.url)
        print(f"Plaid will announce new transactions of {len(banks)} bank(s) there.")
        return
    with Session(engine) as db:
        if args.command == "import-mac" and _already_imported(db, args.home):
            sys.exit(
                "This Mac's bank connection is already in the database: the import "
                "was done. For a new sign-in token run: python -m server.cli new-token"
            )
        user = add_user(db)
        db.flush()
        if args.command == "import-mac":
            pipeline.import_folder(db, engine, user.id, args.home)
            rows = db.scalars(select(Transaction).where(Transaction.user_id == user.id))
            banks = banks_of(db, user.id)
            counts = [row.trashed for row in rows]
            print(
                f"Imported {counts.count(False)} transactions, {counts.count(True)} in "
                f"the trash, {len(banks)} bank connection(s)."
            )
            webhook = os.environ.get("PLAID_WEBHOOK_URL", "")
            if webhook and banks:
                pipeline.set_webhook(engine, banks, webhook)
                print("Plaid will announce new transactions to the server.")
        token = add_login(db, user)
        db.commit()
    print(f"Token: {token}")


if __name__ == "__main__":
    main()
