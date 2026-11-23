"""
plaid_sync.py — Link a bank through Plaid and pull its transactions into the
statements folder, where the enrichment pipeline picks them up like any other
statement CSV.

Settings come from .env: PLAID_CLIENT_ID, PLAID_SECRET (production),
PLAID_SANDBOX_SECRET and PLAID_ENV ("sandbox" or "production").

Usage:
    python -m src.plaid_sync link     # log in to the bank, then fetch transactions
    python -m src.plaid_sync sync     # fetch new transactions
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import webbrowser
from pathlib import Path
from typing import Callable, NamedTuple

import requests
from dotenv import load_dotenv

if os.environ.get("BUDGETTRACKER_HOME"):
    # The server runs this for each person in a private folder laid out like this project
    PROJECT_ROOT = Path(os.environ["BUDGETTRACKER_HOME"])
elif getattr(sys, "frozen", False):
    PROJECT_ROOT = Path.home() / "Library" / "Application Support" / "BudgetTracker"
else:
    PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = PROJECT_ROOT / "data"

load_dotenv(PROJECT_ROOT / ".env")

HISTORY_DAYS = 730  # the most Plaid offers
_ACCOUNT_KINDS = {"depository": "debit", "credit": "credit"}
# Same headers as the parsed statement CSVs, so the enrichment loader reads these unchanged
_CSV_HEADERS = {
    "debit": ["date", "description", "amount", "account_type"],
    "credit": ["date", "description", "amount", "cashback_pct"],
}
_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})")


class PlaidError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class SyncResult(NamedTuple):
    new_rows: int
    complete: bool  # False while Plaid is still loading older history


def environment() -> str:
    env = os.environ.get("PLAID_ENV", "sandbox").strip().lower()
    if env not in ("sandbox", "production"):
        raise PlaidError(
            "CONFIG", f"PLAID_ENV must be 'sandbox' or 'production', not '{env}'"
        )
    return env


def statements_dir() -> Path:
    """Where Plaid CSVs are written. Sandbox test data is kept out of the real statements."""
    return DATA_DIR / (
        "statements" if environment() == "production" else "plaid_sandbox"
    )


def _post(path: str, payload: dict) -> dict:
    env = environment()
    client_id = os.environ.get("PLAID_CLIENT_ID", "").strip()
    secret_name = "PLAID_SECRET" if env == "production" else "PLAID_SANDBOX_SECRET"
    secret = os.environ.get(secret_name, "").strip()
    if not client_id or not secret:
        raise PlaidError(
            "CONFIG", f"PLAID_CLIENT_ID or {secret_name} is missing from .env"
        )
    resp = requests.post(
        f"https://{env}.plaid.com{path}",
        json={"client_id": client_id, "secret": secret, **payload},
        timeout=60,
    )
    try:
        data = resp.json()
    except ValueError:
        raise PlaidError(
            str(resp.status_code), "unexpected response from Plaid"
        ) from None
    if not resp.ok:
        raise PlaidError(
            data.get("error_code", str(resp.status_code)), data.get("error_message", "")
        )
    return data


# ---------------------------------------------------------------------------
# Local state: one entry per linked bank (access token, sync cursor, transactions)
# ---------------------------------------------------------------------------


def _state_file() -> Path:
    return DATA_DIR / f"plaid_state_{environment()}.json"


def _load_state() -> dict:
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"items": {}}


def _save_state(state: dict) -> None:
    path = _state_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    # Holds bank access tokens, so readable by the owner only
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def linked_banks() -> list[str]:
    """Names of the banks linked in the current Plaid environment."""
    return [
        item.get("institution") or "Bank" for item in _load_state()["items"].values()
    ]


# ---------------------------------------------------------------------------
# Linking a bank
# ---------------------------------------------------------------------------


def create_link_session(
    user_id: str = "local-user", webhook: str = ""
) -> tuple[str, str]:
    """Return (link_token, url) of a Plaid-hosted page where the user logs in to their bank."""
    payload = {
        "client_name": "ExpenseAnalyzer",
        "language": "en",
        "country_codes": ["CA"],
        "user": {"client_user_id": user_id},
        "products": ["transactions"],
        "transactions": {"days_requested": HISTORY_DAYS},
        "hosted_link": {},
    }
    if webhook:
        # Where Plaid announces new transactions for banks linked through this page
        payload["webhook"] = webhook
    data = _post("/link/token/create", payload)
    return data["link_token"], data["hosted_link_url"]


def wait_for_link(
    link_token: str,
    should_stop: Callable[[], bool] = lambda: False,
    timeout: float = 1800,
) -> tuple[str, str] | None:
    """Wait for the user to finish the hosted login page.

    Returns (public_token, bank name), or None if they closed the page or ran out of time.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sessions = _post("/link/token/get", {"link_token": link_token}).get(
            "link_sessions", []
        )
        for session in sessions:
            for added in (session.get("results") or {}).get("item_add_results", []):
                bank = (added.get("institution") or {}).get("name", "")
                return added["public_token"], bank
            if session.get("finished_at") or session.get("exit"):
                return None
        for _ in range(6):
            if should_stop():
                return None
            time.sleep(0.5)
    return None


def add_item(public_token: str, institution: str = "") -> None:
    """Trade the one-time token from the login page for a lasting connection and save it."""
    data = _post("/item/public_token/exchange", {"public_token": public_token})
    state = _load_state()
    state["items"][data["item_id"]] = {
        "access_token": data["access_token"],
        "institution": institution,
        "cursor": "",
        "accounts": {},
        "transactions": {},
    }
    _save_state(state)


# ---------------------------------------------------------------------------
# Fetching transactions
# ---------------------------------------------------------------------------


def _sync_item(item: dict) -> str:
    """Apply Plaid's changes since the saved cursor to item; return Plaid's update status."""
    for _ in range(3):
        cursor = item["cursor"]
        changed: list[dict] = []
        removed: list[dict] = []
        accounts: list[dict] = []
        try:
            while True:
                payload = {
                    "access_token": item["access_token"],
                    "count": 500,
                    "options": {"include_original_description": True},
                }
                if cursor:
                    payload["cursor"] = cursor
                page = _post("/transactions/sync", payload)
                changed += page["added"] + page["modified"]
                removed += page["removed"]
                accounts += page["accounts"]
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
        except PlaidError as exc:
            # Plaid requires restarting from the first cursor when data changes mid-pagination
            if exc.code == "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION":
                continue
            raise
        for account in accounts:
            item["accounts"][account["account_id"]] = account["type"]
        for txn in changed:
            item["transactions"][txn["transaction_id"]] = {
                "account_id": txn["account_id"],
                # authorized_date is the day of the purchase, which is what statements show
                "date": txn.get("authorized_date") or txn["date"],
                "description": (
                    txn.get("original_description") or txn.get("name") or ""
                ).strip(),
                "amount": txn["amount"],
                "pending": txn["pending"],
            }
        for txn in removed:
            item["transactions"].pop(txn["transaction_id"], None)
        item["cursor"] = cursor
        return page.get("transactions_update_status", "")
    raise PlaidError(
        "TRANSACTIONS_SYNC_MUTATION_DURING_PAGINATION",
        "the bank data kept changing during the sync; try again",
    )


def _write_statement_csvs(state: dict) -> int:
    """Rewrite plaid_debit.csv / plaid_credit.csv; return how many rows are new."""
    new_rows = 0
    for kind, header in _CSV_HEADERS.items():
        folder = statements_dir() / kind
        # Days already covered by a downloaded statement stay with that statement
        covered = [
            m.groups()
            for p in folder.glob("*.csv")
            if not p.name.startswith("plaid_") and (m := _RANGE_RE.search(p.stem))
        ]
        rows = sorted(
            (
                txn["date"],
                txn["description"],
                f"{txn['amount']:.2f}",
                "debit" if kind == "debit" else "",
            )
            for item in state["items"].values()
            for txn in item["transactions"].values()
            if not txn["pending"]
            and txn["description"]
            and _ACCOUNT_KINDS.get(item["accounts"].get(txn["account_id"])) == kind
            and not any(start <= txn["date"] <= end for start, end in covered)
        )
        # Fixed file name: the app's trash identifies transactions by source file
        path = folder / f"plaid_{kind}.csv"
        if not rows and not path.exists():
            continue
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        text = buf.getvalue()
        old = path.read_text(encoding="utf-8") if path.exists() else ""
        if text == old:
            continue
        new_rows += len(set(text.splitlines()[1:]) - set(old.splitlines()[1:]))
        folder.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return new_rows


def sync(should_stop: Callable[[], bool] = lambda: False) -> SyncResult:
    """Fetch new transactions for every linked bank and rewrite the Plaid statement CSVs."""
    state = _load_state()
    deadline = time.monotonic() + 90
    new_rows = 0
    while True:
        statuses = []
        for item in state["items"].values():
            statuses.append(_sync_item(item))
            _save_state(state)
        new_rows += _write_statement_csvs(state)
        # A bank linked seconds ago has nothing to return yet; Plaid signals that
        # with an empty cursor, and only sometimes with a NOT_READY status
        waiting = "NOT_READY" in statuses or any(
            not item["cursor"] for item in state["items"].values()
        )
        if not waiting or should_stop() or time.monotonic() > deadline:
            break
        time.sleep(3)
    complete = not waiting and "INITIAL_UPDATE_COMPLETE" not in statuses
    return SyncResult(new_rows, complete)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Link a bank through Plaid or fetch its new transactions"
    )
    parser.add_argument("command", choices=["link", "sync"])
    args = parser.parse_args()

    if args.command == "link":
        token, url = create_link_session()
        print(f"Opening the bank login page: {url}")
        webbrowser.open(url)
        linked = wait_for_link(token)
        if linked is None:
            print("Bank login was not completed.")
            return
        add_item(*linked)
    result = sync()
    print(f"{result.new_rows} new transaction(s) saved to {statements_dir()}")
    if not result.complete:
        print("Older history is still loading — run 'sync' again in a few minutes.")


if __name__ == "__main__":
    main()
