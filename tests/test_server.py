"""Tests for the server: accounts, each person's data, bank linking and bank syncs."""

import csv
import hashlib
import json
import threading
import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from server import app as api
from server import pipeline
from server.book import Book
from server.db import (
    ROW_FIELDS,
    Bank,
    Document,
    Job,
    Login,
    Transaction,
    User,
    add_login,
    add_user,
    decrypt,
    encrypt,
    make_engine,
)
from src import plaid_sync


def _row(**overrides):
    row = {field: "" for field in ROW_FIELDS}
    row.update(
        source_file="plaid_debit.csv",
        account_type="debit",
        date="2026-03-02",
        description="ACHAT METRO #123",
        amount="42.50",
        purchase_mode="in_person",
        gemini_merchant="Metro",
        gemini_category="Groceries",
        ai_description="Groceries at Metro",
    )
    row.update(overrides)
    return row


def _key(row):
    return "\x1f".join(
        [row["date"], row["amount"], row["description"], row["source_file"]]
    )


class FakePlaid:
    """Stands in for plaid_sync._post: answers from `replies` and remembers the calls."""

    def __init__(self):
        self.calls = []
        self.replies = {}

    def __call__(self, path, payload):
        self.calls.append((path, payload))
        if path not in self.replies:
            raise plaid_sync.PlaidError("UNEXPECTED", path)
        return self.replies[path]


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.delenv("EXPENSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("PLAID_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    plaid = FakePlaid()
    monkeypatch.setattr(plaid_sync, "_post", plaid)
    monkeypatch.setattr(api, "_plaid_keys", {})
    monkeypatch.setattr(pipeline, "analyse", lambda rows, store: "")
    engine = make_engine(f"sqlite:///{tmp_path / 'server.db'}")
    queued = []  # bank syncs the API asked for, not yet run
    client = TestClient(api.create_app(engine, spawn=queued.append))
    return SimpleNamespace(engine=engine, client=client, plaid=plaid, queued=queued)


def _person(server, rows=(), trash=(), documents=None, bank=None):
    """Create an account with some data; returns (user id, request headers)."""
    with Session(server.engine) as db:
        user = add_user(db)
        db.flush()
        book = Book(db, user.id)
        book.rows[:] = [dict(row) for row in rows]
        book.save_trash([dict(row) for row in trash])
        for name, body in (documents or {}).items():
            book.set_document(name, body)
        book.save()
        if bank:
            db.add(
                Bank(
                    user_id=user.id,
                    item_id=bank,
                    institution="Desjardins",
                    access_token=encrypt(server.engine, f"access-{bank}"),
                    state=json.dumps({**pipeline.NEW_BANK_STATE, "cursor": "c1"}),
                )
            )
        token = add_login(db, user)
        db.commit()
        return user.id, {"Authorization": f"Bearer {token}"}


def _export(server, headers):
    response = server.client.get("/v1/export", headers=headers)
    assert response.status_code == 200
    return response.json()


def _edit(server, headers, **edit):
    response = server.client.post("/v1/edits", headers=headers, json={"edits": [edit]})
    assert response.status_code == 200
    return response.json()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def test_requests_without_a_valid_token_are_refused(server):
    _person(server, rows=[_row()])
    wrong = {"Authorization": "Bearer not-a-real-token"}

    assert server.client.get("/v1/export").status_code == 401
    assert server.client.get("/v1/export", headers=wrong).status_code == 401
    assert (
        server.client.post("/v1/edits", headers=wrong, json={"edits": []}).status_code
        == 401
    )
    assert server.client.get("/v1/status", headers=wrong).status_code == 401
    assert server.client.delete("/v1/account", headers=wrong).status_code == 401
    assert server.client.get("/health").json() == {"ok": True}


@pytest.fixture
def apple(monkeypatch):
    """Sign in with Apple, with a key of our own in place of Apple's."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    keys = SimpleNamespace(
        get_signing_key_from_jwt=lambda token: SimpleNamespace(key=key.public_key())
    )
    monkeypatch.setattr(api, "_apple_keys", keys)
    monkeypatch.setenv("APPLE_BUNDLE_ID", "EvanRL.ExpenseAnalyzer")

    def identity_token(person, signer=key, **claims):
        claims = {
            "iss": "https://appleid.apple.com",
            "aud": "EvanRL.ExpenseAnalyzer",
            "sub": person,
            "exp": int(time.time()) + 600,
            **claims,
        }
        return jwt.encode(claims, signer, algorithm="RS256")

    return identity_token


def test_sign_in_with_apple_gives_each_person_one_account(server, apple):
    def sign_in(person):
        response = server.client.post(
            "/v1/auth/apple", json={"identityToken": apple(person)}
        )
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['token']}"}

    phone, tablet, someone_else = (
        sign_in("apple-1"),
        sign_in("apple-1"),
        sign_in("apple-2"),
    )

    with Session(server.engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 2
        assert db.scalar(select(func.count()).select_from(Login)) == 3
    assert phone != tablet
    # Signing out one device leaves the person's other device signed in
    assert server.client.post("/v1/auth/logout", headers=phone).status_code == 200
    assert server.client.get("/v1/export", headers=phone).status_code == 401
    assert server.client.get("/v1/export", headers=tablet).status_code == 200
    assert server.client.get("/v1/export", headers=someone_else).status_code == 200


def test_sign_ins_apple_did_not_issue_for_this_app_are_refused(server, apple):
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forgeries = [
        apple("apple-1", aud="com.other.app"),
        apple("apple-1", exp=int(time.time()) - 5),
        apple("apple-1", iss="https://example.com"),
        apple("apple-1", signer=stranger),
        "not-a-token",
    ]

    for token in forgeries:
        response = server.client.post("/v1/auth/apple", json={"identityToken": token})
        assert response.status_code == 401
    with Session(server.engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 0


def test_signing_in_with_apple_keeps_the_data_of_a_token_account(server, apple):
    """The phone first signs in with the token import-mac printed, Apple comes later."""
    _, by_token = _person(server, rows=[_row()])

    def sign_in(person, headers=None):
        response = server.client.post(
            "/v1/auth/apple", json={"identityToken": apple(person)}, headers=headers
        )
        assert response.status_code == 200
        return {"Authorization": f"Bearer {response.json()['token']}"}

    by_apple = sign_in("apple-1", headers=by_token)

    assert len(_export(server, by_apple)["transactions"]) == 1
    assert len(_export(server, by_token)["transactions"]) == 1
    # From now on that Apple account finds the same data from any device
    assert len(_export(server, sign_in("apple-1"))["transactions"]) == 1
    with Session(server.engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 1
    # An account that already has an Apple id is not handed to a different Apple user
    assert _export(server, sign_in("apple-2", headers=by_apple))["transactions"] == []
    with Session(server.engine) as db:
        assert db.scalar(select(func.count()).select_from(User)) == 2


def test_deleting_an_account_erases_it_and_disconnects_the_bank(server):
    user_id, headers = _person(
        server,
        rows=[_row()],
        trash=[_row(description="OLD")],
        bank="item-1",
        documents={"notes": {"ACHAT METRO #123": "weekly"}},
    )
    other_id, other = _person(server, rows=[_row()], bank="item-2")
    pipeline.start_job(server.engine, user_id)
    server.plaid.replies["/item/remove"] = {}

    assert server.client.delete("/v1/account", headers=headers).status_code == 200

    assert server.plaid.calls == [("/item/remove", {"access_token": "access-item-1"})]
    with Session(server.engine) as db:
        assert db.get(User, user_id) is None
        for table in (Transaction, Document, Bank, Login, Job):
            owners = set(db.scalars(select(table.user_id)))
            assert user_id not in owners
        assert db.get(User, other_id) is not None
    assert server.client.get("/v1/export", headers=headers).status_code == 401
    assert len(_export(server, other)["transactions"]) == 1


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def test_export_is_what_the_mac_app_sends_the_phone(server):
    rows = [
        _row(),
        _row(
            description="PAIE",
            amount="-1500",
            gemini_merchant="Employer",
            gemini_category="Payroll / Income",
        ),
        _row(
            description="CAT FOOD",
            amount="20.00",
            gemini_merchant="Mondou",
            gemini_category="Chat",
        ),
        # Paying off the credit card is hidden everywhere in the Mac app
        _row(gemini_merchant="Visa Desjardins", gemini_category="Bill Payment"),
    ]
    _, headers = _person(
        server,
        rows=rows,
        trash=[_row(description="TRASHED", amount="9.99")],
        documents={
            "custom_categories": {"Chat": "", "Voyages": "Travel"},
            "emojis": {"Chat": "🐈", "Groceries": "🥦", "Voyages": "not an emoji"},
            "summary_descriptions": {
                "categories": {"Groceries": "Food shops"},
                "merchants": {"Groceries": {"Metro": "Weekly shop"}},
            },
            "official_sources": ["Employer"],
            "ai_analysis": "## Trend",
        },
    )

    export = _export(server, headers)

    groceries, pay, cat = export["transactions"]
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
        "place": "",
        "phone": "",
        "website": "",
        "notes": "",
        "isPending": False,
    }
    assert (pay["amount"], pay["isIncome"]) == (-1500.0, True)
    assert [t["bankDescription"] for t in export["trash"]] == ["TRASHED"]
    # The two-level list's categories plus what the data and the person's own list hold
    assert set(export["categories"]) > {
        "Bill Payment",
        "Chat",
        "Groceries",
        "Payroll / Income",
        "Voyages",
        "Restaurants",
    }
    assert export["categories"] == sorted(export["categories"])
    placed = {g["name"]: g["categories"] for g in export["groups"]}
    assert "Restaurants" in placed["Food & drink"]
    assert "Voyages" in placed["Travel"]  # the person's own category, in its group
    assert "Chat" in placed["Other"]  # a name from an older list, without a group
    # Built-in emojis win, then the person's own, then a plain tag
    assert export["emojis"]["Groceries"] == "🛒"
    assert export["emojis"]["Chat"] == "🐈"
    assert export["emojis"]["Voyages"] == "🏷️"
    assert export["emojis"]["Food & drink"] == "🍽️"
    assert export["categoryDescriptions"] == {"Groceries": "Food shops"}
    assert export["merchantDescriptions"] == {"Groceries": {"Metro": "Weekly shop"}}
    assert export["officialIncomeSources"] == ["Employer"]
    assert export["aiAnalysis"] == "## Trend"


def test_a_new_account_starts_empty(server):
    _, headers = _person(server)

    export = _export(server, headers)

    assert export["transactions"] == export["trash"] == []
    assert "Restaurants" in export["categories"]
    assert export["aiAnalysis"] == ""
    status = server.client.get("/v1/status", headers=headers).json()
    assert status == {"banks": [], "syncing": False, "lastSync": None, "error": ""}


def test_changes_from_the_phone_follow_the_mac_rules_and_are_kept(server):
    first = _row()
    same_merchant = _row(date="2026-03-09", description="ACHAT METRO #456")
    elsewhere = _row(
        date="2026-03-10", description="METRO PHARMA", gemini_category="Pharmacy"
    )
    pay = _row(description="PAIE", amount="-1500", gemini_merchant="Employer")
    user_id, headers = _person(
        server,
        rows=[first, same_merchant, elsewhere, pay],
        trash=[_row(description="BRING BACK")],
    )

    _edit(server, headers, type="setCategory", keys=[_key(first)], value="Rent")
    _edit(server, headers, type="setNote", keys=[_key(first)], value="weekly shop")
    _edit(server, headers, type="trash", keys=[_key(elsewhere)])
    _edit(server, headers, type="restore", keys=[_key(_row(description="BRING BACK"))])
    answer = _edit(server, headers, type="markOfficial", value="Employer")

    export = _export(server, headers)
    assert {k: v for k, v in answer.items() if k != "exportedAt"} == {
        k: v for k, v in export.items() if k != "exportedAt"
    }
    shown = [
        (t["bankDescription"], t["category"], t["notes"])
        for t in export["transactions"]
    ]
    # The merchant's other rows in the old category follow; the note only covers its own description
    assert shown == [
        ("ACHAT METRO #123", "Rent", "weekly shop"),
        ("ACHAT METRO #456", "Rent", ""),
        ("PAIE", "Groceries", ""),
        ("BRING BACK", "Groceries", ""),
    ]
    assert [t["bankDescription"] for t in export["trash"]] == ["METRO PHARMA"]
    assert export["officialIncomeSources"] == ["Employer"]
    with Session(server.engine) as db:
        book = Book(db, user_id)
        assert book.document("corrections", {}) == {
            "ACHAT METRO #123": "Rent",
            "ACHAT METRO #456": "Rent",
        }
        assert book.document("notes", {}) == {"ACHAT METRO #123": "weekly shop"}

    _edit(server, headers, type="setNote", keys=[_key(first)], value="")
    _edit(server, headers, type="unmarkOfficial", value="Employer")
    export = _export(server, headers)
    assert export["transactions"][0]["notes"] == ""
    assert export["officialIncomeSources"] == []


def test_people_only_see_and_change_their_own_data(server):
    _, anna = _person(server, rows=[_row(), _row(description="ANNA ONLY")])
    ben_id, ben = _person(
        server, rows=[_row()], documents={"official_sources": ["Ben Inc"]}
    )

    # The same transaction exists in both accounts, so its key is the same too
    _edit(server, anna, type="setCategory", keys=[_key(_row())], value="Rent")
    _edit(server, anna, type="trash", keys=[_key(_row())])
    _edit(server, anna, type="markOfficial", value="Anna Ltd")

    assert [t["bankDescription"] for t in _export(server, anna)["transactions"]] == [
        "ANNA ONLY"
    ]
    bens = _export(server, ben)
    assert [(t["bankDescription"], t["category"]) for t in bens["transactions"]] == [
        ("ACHAT METRO #123", "Groceries")
    ]
    assert bens["trash"] == []
    assert bens["officialIncomeSources"] == ["Ben Inc"]
    with Session(server.engine) as db:
        assert Book(db, ben_id).document("corrections", {}) == {}


def test_a_category_made_on_the_phone_joins_its_group(server):
    _, headers = _person(server, rows=[_row()])

    export = server.client.post(
        "/v1/edits",
        headers=headers,
        json={
            "edits": [
                {"type": "createCategory", "value": "Snacks", "group": "Food & drink"},
                {"type": "setCategory", "keys": [_key(_row())], "value": "Snacks"},
                {"type": "createCategory", "value": "Odd", "group": "No such group"},
            ]
        },
    ).json()

    placed = {g["name"]: g["categories"] for g in export["groups"]}
    assert "Snacks" in placed["Food & drink"]
    assert "Odd" in placed["Other"]
    assert export["transactions"][0]["category"] == "Snacks"
    with Session(server.engine) as db:
        saved = db.scalar(select(Document).where(Document.name == "custom_categories"))
        assert json.loads(saved.body) == {"Snacks": "Food & drink", "Odd": "Other"}


def test_malformed_changes_are_rejected(server):
    _, headers = _person(server, rows=[_row()])

    for body in (
        {"edits": [{"type": "deleteEverything", "keys": []}]},
        {"edits": [{"type": "setNote", "keys": ["k"], "value": "x" * 2001}]},
        {"edits": "trash"},
        {},
    ):
        assert (
            server.client.post("/v1/edits", headers=headers, json=body).status_code
            == 422
        )
    assert len(_export(server, headers)["transactions"]) == 1


# ---------------------------------------------------------------------------
# Banks
# ---------------------------------------------------------------------------


def test_linking_a_bank_keeps_its_key_encrypted_and_starts_a_sync(server, monkeypatch):
    monkeypatch.setenv("PLAID_WEBHOOK_URL", "https://api.example/plaid/webhook")
    user_id, headers = _person(server)
    _, bystander = _person(server)
    server.plaid.replies["/link/token/create"] = {
        "link_token": "link-1",
        "hosted_link_url": "https://plaid.example/login",
    }
    server.plaid.replies["/link/token/get"] = {"link_sessions": []}

    started = server.client.post("/v1/bank/link", headers=headers)
    assert started.json() == {"url": "https://plaid.example/login"}
    request = server.plaid.calls[0][1]
    assert request["user"] == {"client_user_id": user_id}
    assert request["webhook"] == "https://api.example/plaid/webhook"

    # Nothing to finish yet, and nobody else can finish this person's login
    assert (
        server.client.post("/v1/bank/link/complete", headers=headers).status_code == 409
    )
    assert (
        server.client.post("/v1/bank/link/complete", headers=bystander).status_code
        == 409
    )

    server.plaid.replies["/link/token/get"] = {
        "link_sessions": [
            {
                "results": {
                    "item_add_results": [
                        {
                            "public_token": "public-1",
                            "institution": {"name": "Desjardins"},
                        }
                    ]
                }
            }
        ]
    }
    server.plaid.replies["/item/public_token/exchange"] = {
        "item_id": "item-1",
        "access_token": "access-secret",
    }
    finished = server.client.post("/v1/bank/link/complete", headers=headers)

    assert finished.status_code == 200
    assert finished.json()["banks"] == ["Desjardins"]
    assert finished.json()["syncing"] is True
    assert len(server.queued) == 1
    assert ("/link/token/get", {"link_token": "link-1"}) in server.plaid.calls
    with Session(server.engine) as db:
        bank = db.scalar(select(Bank))
        assert (bank.user_id, bank.item_id) == (user_id, "item-1")
        assert "access-secret" not in bank.access_token
        assert decrypt(server.engine, bank.access_token) == "access-secret"
    # The login cannot be used a second time
    assert (
        server.client.post("/v1/bank/link/complete", headers=headers).status_code == 409
    )
    assert server.client.get("/v1/status", headers=bystander).json()["banks"] == []


def test_a_bank_service_failure_is_reported_not_crashed(server):
    _, headers = _person(server)

    response = server.client.post("/v1/bank/link", headers=headers)

    assert response.status_code == 502
    assert "UNEXPECTED" in response.json()["detail"]


@pytest.fixture
def plaid_signs(server):
    """Sign webhook bodies the way Plaid does, with a key the fake Plaid vouches for."""
    key = ec.generate_private_key(ec.SECP256R1())
    public = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    server.plaid.replies["/webhook_verification_key/get"] = {
        "key": {
            **public,
            "alg": "ES256",
            "kid": "key-1",
            "use": "sig",
            "expired_at": None,
        }
    }

    def sign(body, signer=key, age=0, digest=None):
        claims = {
            "iat": int(time.time()) - age,
            "request_body_sha256": digest or hashlib.sha256(body).hexdigest(),
        }
        token = jwt.encode(claims, signer, algorithm="ES256", headers={"kid": "key-1"})
        return {"Plaid-Verification": token, "Content-Type": "application/json"}

    return sign


def _news(item_id, code="SYNC_UPDATES_AVAILABLE"):
    event = {"webhook_type": "TRANSACTIONS", "webhook_code": code, "item_id": item_id}
    return json.dumps(event).encode()


def test_plaid_announcing_new_transactions_starts_a_sync_for_the_banks_owner(
    server, plaid_signs
):
    owner_id, owner = _person(server, bank="item-1")
    _, other = _person(server, bank="item-2")

    for body in (
        _news("item-1"),
        _news("item-unknown"),
        _news("item-2", "DEFAULT_UPDATE"),
    ):
        response = server.client.post(
            "/plaid/webhook", content=body, headers=plaid_signs(body)
        )
        assert response.status_code == 200

    assert len(server.queued) == 1
    assert server.client.get("/v1/status", headers=owner).json()["syncing"] is True
    assert server.client.get("/v1/status", headers=other).json()["syncing"] is False
    # Plaid's key is fetched once and then remembered
    assert [path for path, _ in server.plaid.calls] == ["/webhook_verification_key/get"]


def test_notifications_plaid_did_not_sign_are_refused(server, plaid_signs):
    _person(server, bank="item-1")
    body = _news("item-1")
    stranger = ec.generate_private_key(ec.SECP256R1())
    forgeries = [
        {"Content-Type": "application/json"},
        {"Plaid-Verification": "not-a-token"},
        plaid_signs(body, signer=stranger),
        plaid_signs(body, age=600),
        plaid_signs(body, digest=hashlib.sha256(_news("item-9")).hexdigest()),
    ]

    for headers in forgeries:
        response = server.client.post("/plaid/webhook", content=body, headers=headers)
        assert response.status_code == 400
    assert server.queued == []


def test_only_one_sync_runs_per_person_at_a_time(server, monkeypatch):
    monkeypatch.setattr(
        pipeline, "run_pipeline", lambda home: {"new_rows": 0, "complete": True}
    )
    _, headers = _person(server, rows=[_row()], bank="item-1")
    _, other = _person(server, bank="item-2")
    _, no_bank = _person(server)

    assert server.client.post("/v1/sync", headers=headers).json() == {"started": True}
    assert server.client.post("/v1/sync", headers=headers).json() == {"started": False}
    assert server.client.post("/v1/sync", headers=other).json() == {"started": True}
    assert server.client.post("/v1/sync", headers=no_bank).json() == {"started": False}
    assert len(server.queued) == 2

    server.queued.pop(0)()

    status = server.client.get("/v1/status", headers=headers).json()
    assert (status["syncing"], status["error"]) == (False, "")
    assert status["lastSync"].endswith("Z")
    assert len(_export(server, headers)["transactions"]) == 1
    assert server.client.post("/v1/sync", headers=headers).json() == {"started": True}


def test_a_sync_running_when_the_server_stops_is_reported_as_interrupted(
    server, monkeypatch
):
    """Cloud Run stops the server after a quiet spell, even in the middle of a sync."""
    started, released = threading.Event(), threading.Event()

    def slow_pipeline(home):
        started.set()
        released.wait(5)
        return {"new_rows": 0, "complete": True}

    monkeypatch.setattr(pipeline, "run_pipeline", slow_pipeline)
    threads = []

    def in_thread(work):
        threads.append(threading.Thread(target=work))
        threads[-1].start()

    _, headers = _person(server, rows=[_row()], bank="item-1")
    with TestClient(api.create_app(server.engine, spawn=in_thread)) as client:
        assert client.post("/v1/sync", headers=headers).json() == {"started": True}
        assert started.wait(5)
        assert client.get("/v1/status", headers=headers).json()["syncing"] is True
    # Leaving the block is the server shutting down

    status = client.get("/v1/status", headers=headers).json()
    assert (status["syncing"], status["error"]) == (False, pipeline.INTERRUPTED)
    assert client.post("/v1/sync", headers=headers).json() == {"started": True}
    released.set()
    for thread in threads:
        thread.join(5)


# ---------------------------------------------------------------------------
# Bank syncs
# ---------------------------------------------------------------------------


def _write_csv(path, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ROW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def test_a_sync_runs_the_pipeline_on_the_persons_data_and_merges_the_results(
    server, monkeypatch
):
    monkeypatch.setattr(
        pipeline, "analyse", lambda rows, store: f"{len(rows)} rows analysed"
    )
    stays, removed_by_bank = _row(), _row(description="PENDING HOLD", amount="1.00")
    from_statement = _row(description="OLD STATEMENT", source_file="credit_2024.csv")
    in_trash = _row(description="TRASHED", amount="9.99")
    user_id, headers = _person(
        server,
        rows=[stays, removed_by_bank, from_statement],
        trash=[in_trash],
        documents={"corrections": {"X": "Rent"}, "places_cache": {"old": {}}},
        bank="item-1",
    )
    with Session(server.engine) as db:
        original_id = Book(db, user_id).rows[0]["_id"]
    seen = {}

    def pipeline_run(home):
        data = home / "data"
        with (data / "enriched_transactions.csv").open(encoding="utf-8") as f:
            seen["rows"] = [row["description"] for row in csv.DictReader(f)]
        seen["trash"] = [
            r["description"] for r in json.loads((data / "trash.json").read_text())
        ]
        seen["corrections"] = json.loads(
            (data / "category_corrections.json").read_text()
        )
        state = json.loads((data / "plaid_state_sandbox.json").read_text())
        seen["bank"] = dict(state["items"]["item-1"])
        # The person keeps using the app while the pipeline works
        _edit(
            server, headers, type="setNote", keys=[_key(stays)], value="noted meanwhile"
        )
        _edit(server, headers, type="restore", keys=[_key(in_trash)])
        # What the real pipeline leaves behind
        state["items"]["item-1"]["cursor"] = "c2"
        (data / "plaid_state_sandbox.json").write_text(json.dumps(state))
        (data / "places_cache.json").write_text(
            json.dumps({"new": {"place_name": "Metro"}})
        )
        (data / "category_emojis.json").write_text(json.dumps({"Chat": "🐈"}))
        _write_csv(
            data / "enriched_transactions.csv",
            [
                _row(ai_description="Described again"),
                _row(description="NEW PURCHASE", amount="7.00", gemini_category="Chat"),
            ],
        )
        return {"new_rows": 1, "complete": True}

    assert pipeline.start_job(server.engine, user_id)
    pipeline.run_job(server.engine, user_id, pipeline_run)

    assert seen["rows"] == ["ACHAT METRO #123", "PENDING HOLD", "OLD STATEMENT"]
    assert seen["trash"] == ["TRASHED"]
    assert seen["corrections"] == {"X": "Rent"}
    assert (seen["bank"]["access_token"], seen["bank"]["cursor"]) == (
        "access-item-1",
        "c1",
    )

    export = _export(server, headers)
    shown = [
        (t["bankDescription"], t["summary"], t["notes"]) for t in export["transactions"]
    ]
    assert shown == [
        ("ACHAT METRO #123", "Described again", "noted meanwhile"),
        ("NEW PURCHASE", "Groceries at Metro", ""),
        # Not the pipeline's to remove: a statement row, and one restored while it ran
        ("OLD STATEMENT", "Groceries at Metro", ""),
        ("TRASHED", "Groceries at Metro", ""),
    ]
    assert export["trash"] == []
    assert export["emojis"]["Chat"] == "🐈"
    assert export["aiAnalysis"] == "4 rows analysed"
    with Session(server.engine) as db:
        book = Book(db, user_id)
        assert book.rows[0]["_id"] == original_id
        assert book.document("places_cache", {}) == {"new": {"place_name": "Metro"}}
        bank = db.scalar(select(Bank))
        assert json.loads(bank.state)["cursor"] == "c2"
        assert "access" not in bank.state
    status = server.client.get("/v1/status", headers=headers).json()
    assert (status["syncing"], status["error"]) == (False, "")


def test_a_sync_remembers_the_descriptions_of_the_persons_transfers(server):
    def paid(day, amount):
        return _row(description="VIREMENT CARTE", date=day, amount=amount)

    def received(day, amount):
        return _row(
            description="VERSEMENT",
            date=day,
            amount=f"-{amount}",
            account_type="credit",
            source_file="plaid_credit.csv",
        )

    matched = [
        paid("2026-01-05", "500.00"),
        received("2026-01-05", "500.00"),
        paid("2026-02-05", "320.00"),
        received("2026-02-06", "320.00"),
    ]
    alone = received("2026-03-05", "410.00")  # its other side is not in the data
    user_id, headers = _person(server, rows=matched + [alone], bank="item-1")
    _, someone_else = _person(server, rows=[alone])

    def shown(person):
        return [t["bankDescription"] for t in _export(server, person)["transactions"]]

    assert shown(headers) == ["VERSEMENT"]
    assert pipeline.start_job(server.engine, user_id)
    pipeline.run_job(
        server.engine, user_id, lambda home: {"new_rows": 0, "complete": True}
    )

    assert shown(headers) == []
    assert shown(someone_else) == ["VERSEMENT"]


def test_a_failed_sync_changes_nothing_and_says_why(server):
    user_id, headers = _person(server, rows=[_row()], bank="item-1")

    def pipeline_run(home):
        data = home / "data"
        state = json.loads((data / "plaid_state_sandbox.json").read_text())
        state["items"]["item-1"]["cursor"] = "c2"
        (data / "plaid_state_sandbox.json").write_text(json.dumps(state))
        _write_csv(data / "enriched_transactions.csv", [])
        raise RuntimeError("ITEM_LOGIN_REQUIRED: the bank wants you to log in again")

    assert pipeline.start_job(server.engine, user_id)
    pipeline.run_job(server.engine, user_id, pipeline_run)

    assert len(_export(server, headers)["transactions"]) == 1
    status = server.client.get("/v1/status", headers=headers).json()
    assert status["syncing"] is False
    assert status["error"].startswith("ITEM_LOGIN_REQUIRED")
    with Session(server.engine) as db:
        # What was fetched before the failure is not asked for again
        assert json.loads(db.scalar(select(Bank)).state)["cursor"] == "c2"
    assert pipeline.start_job(server.engine, user_id)


def test_moving_to_the_two_level_list_keeps_clear_corrections_and_redoes_the_rest(
    server,
):
    rows = [
        _row(description="METRO", gemini_category="Groceries"),
        _row(description="RESTO", gemini_category="Food & Dining"),
        _row(description="BAR", gemini_category="Food & Dining"),
        _row(description="NEW", gemini_category="Restaurants"),
        _row(description="DONE", gemini_category="Coffee shops & desserts"),
    ]
    corrections = {"METRO": "Groceries", "RESTO": "Food & Dining", "NEW": "Restaurants"}
    user_id, headers = _person(
        server, rows=rows, documents={"corrections": corrections}
    )

    with Session(server.engine) as db:
        assert pipeline.forget_categories(db, user_id) == 2
        db.commit()

    with Session(server.engine) as db:
        book = Book(db, user_id)
        assert {r["description"]: r["gemini_category"] for r in book.rows} == {
            "METRO": "Supermarkets & grocery stores",  # renamed: the match is clear
            "RESTO": "",  # "Food & Dining" could be several things: Gemini decides
            "BAR": "",
            "NEW": "Restaurants",  # already a name of the list
            "DONE": "Coffee shops & desserts",  # likewise, from an earlier run
        }
        assert book.document("corrections", {}) == {
            "METRO": "Supermarkets & grocery stores",
            "NEW": "Restaurants",
        }


def test_an_older_database_gets_the_new_columns(tmp_path):
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = make_engine(url)
    with engine.begin() as connection:  # a table from before the pending column existed
        connection.execute(text("ALTER TABLE transactions DROP COLUMN pending"))
    engine.dispose()

    engine = make_engine(url)

    with Session(engine) as db:
        user = add_user(db)
        db.flush()
        book = Book(db, user.id)
        book.rows[:] = [_row(pending="1")]
        book.save()
        db.commit()
    with Session(engine) as db:
        assert db.scalar(select(Transaction.pending)) == "1"


def test_the_mac_apps_data_folder_can_be_imported(server, tmp_path):
    data = tmp_path / "mac" / "data"
    (data / "statements" / "debit").mkdir(parents=True)
    (data / "statements" / "debit" / "plaid_debit.csv").write_text("date\n")
    _write_csv(data / "enriched_transactions.csv", [_row(), _row(description="SECOND")])
    files = {
        "trash.json": [_row(description="TRASHED")],
        "category_corrections.json": {"SECOND": "Rent"},
        "official_income_sources.json": ["Employer"],
        "custom_categories.json": ["Groceries", "Voyages"],
        "ai_analysis_cache.json": {
            "fingerprint": "plaid_debit.csv",
            "analysis": "## Mac",
        },
        "plaid_state_sandbox.json": {
            "items": {
                "item-1": {
                    "access_token": "access-secret",
                    "institution": "Desjardins",
                    "cursor": "c9",
                    "accounts": {"a": "depository"},
                    "transactions": {"t": {"amount": 1}},
                }
            }
        },
    }
    for name, body in files.items():
        (data / name).write_text(json.dumps(body))
    with Session(server.engine) as db:
        user = add_user(db)
        db.flush()
        pipeline.import_folder(db, server.engine, user.id, tmp_path / "mac")
        headers = {"Authorization": f"Bearer {add_login(db, user)}"}
        db.commit()

    export = _export(server, headers)

    assert [t["bankDescription"] for t in export["transactions"]] == [
        "ACHAT METRO #123",
        "SECOND",
    ]
    assert [t["bankDescription"] for t in export["trash"]] == ["TRASHED"]
    assert {"Groceries", "Voyages", "Restaurants"} < set(export["categories"])
    assert export["officialIncomeSources"] == ["Employer"]
    assert export["aiAnalysis"] == "## Mac"
    assert server.client.get("/v1/status", headers=headers).json()["banks"] == [
        "Desjardins"
    ]
    with Session(server.engine) as db:
        bank = db.scalar(select(Bank))
        assert decrypt(server.engine, bank.access_token) == "access-secret"
        assert json.loads(bank.state) == {
            "cursor": "c9",
            "accounts": {"a": "depository"},
            "transactions": {"t": {"amount": 1}},
        }
