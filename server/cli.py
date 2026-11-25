"""
cli.py — run and administer the server from the terminal.

    python -m server.cli serve            start the API on http://127.0.0.1:8000
    python -m server.cli add-user         create an empty account and print its token
    python -m server.cli import-mac       create an account holding the Mac app's data
    python -m server.cli new-token [--account PREFIX]
                                          print a fresh token for an account (lists them if
                                          there are several and none is named)
    python -m server.cli set-webhook URL  tell Plaid where to announce new transactions
    python -m server.cli recategorise     have Gemini categorise everyone's data again with
                                          the two-level list (runs the pipeline from here)

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
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server import pipeline
from server.app import create_app
from server.db import (
    PROJECT_ROOT,
    Bank,
    Job,
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
    fresh = commands.add_parser("new-token", help="print a fresh token for an account")
    fresh.add_argument(
        "--account", default="", help="first characters of the account id, when several"
    )
    hook = commands.add_parser(
        "set-webhook", help="point every connected bank at the server's webhook"
    )
    hook.add_argument("url", help="the address ending in /plaid/webhook")
    commands.add_parser(
        "recategorise",
        help="categorise every transaction again with the two-level list",
    )
    args = parser.parse_args()

    engine = make_engine()
    if args.command == "serve":
        uvicorn.run(create_app(engine), host=args.host, port=args.port)
        return
    if args.command == "new-token":
        with Session(engine) as db:
            users = db.scalars(select(User).order_by(User.created_at)).all()
            chosen = [u for u in users if u.id.startswith(args.account)]
            if len(chosen) != 1:
                print(
                    "Accounts (oldest first); say which with --account <first characters>:"
                )
                for user in users:
                    rows = db.scalar(
                        select(func.count())
                        .select_from(Transaction)
                        .where(
                            Transaction.user_id == user.id,
                            Transaction.trashed.is_(False),
                        )
                    )
                    banks = ", ".join(
                        b.institution or "bank" for b in banks_of(db, user.id)
                    )
                    print(
                        f"  {user.id[:8]}  created {user.created_at:%Y-%m-%d %H:%M} UTC, "
                        f"{rows} transactions, {banks or 'no bank'}"
                    )
                sys.exit(1 if users else "No accounts yet.")
            token = add_login(db, chosen[0])
            db.commit()
        print(f"Token: {token}")
        return
    if args.command == "recategorise":
        with Session(engine) as db:
            user_ids = list(db.scalars(select(User.id)))
            for user_id in user_ids:
                blanked = pipeline.forget_categories(db, user_id)
                print(
                    f"Account {user_id[:8]}…: {blanked} transactions to categorise again."
                )
            db.commit()
        for user_id in user_ids:
            if not pipeline.start_job(engine, user_id):
                sys.exit(
                    f"A sync is already running for {user_id[:8]}…; try again later."
                )
            print(
                f"Running the pipeline for {user_id[:8]}… (a few minutes)…", flush=True
            )
            pipeline.run_job(engine, user_id)
            with Session(engine) as db:
                job = db.get(Job, user_id)
                print(f"  {'failed: ' + job.error if job.error else 'done'}")
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
