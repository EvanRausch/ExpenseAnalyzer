"""
cli.py — run and administer the server from the terminal.

    python -m server.cli serve            start the API on http://127.0.0.1:8000
    python -m server.cli add-user         create an empty account and print its token
    python -m server.cli import-mac       create an account holding the Mac app's data
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
import os
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
    add_login,
    add_user,
    banks_of,
    make_engine,
)


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
    hook = commands.add_parser(
        "set-webhook", help="point every connected bank at the server's webhook"
    )
    hook.add_argument("url", help="the address ending in /plaid/webhook")
    args = parser.parse_args()

    engine = make_engine()
    if args.command == "serve":
        uvicorn.run(create_app(engine), host=args.host, port=args.port)
        return
    if args.command == "set-webhook":
        with Session(engine) as db:
            banks = db.scalars(select(Bank)).all()
            pipeline.set_webhook(engine, banks, args.url)
        print(f"Plaid will announce new transactions of {len(banks)} bank(s) there.")
        return
    with Session(engine) as db:
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
