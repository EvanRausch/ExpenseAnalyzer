"""
app.py — the HTTP API the iPhone app talks to.

Every request except signing in and Plaid's webhook carries "Authorization: Bearer <token>",
and only ever touches the data of the person that token belongs to.

Settings (environment or .env):
    APPLE_BUNDLE_ID     the app's bundle id, which Sign in with Apple tokens must be issued for
    PLAID_WEBHOOK_URL   this server's public /plaid/webhook address, once it has one
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time
from contextlib import asynccontextmanager
from typing import Callable, Literal

import jwt
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from server import pipeline
from server.book import Book
from server.db import (
    Bank,
    Job,
    User,
    add_login,
    add_user,
    banks_of,
    decrypt,
    delete_user,
    encrypt,
    make_engine,
    read_document,
    remove_login,
    user_for_token,
    write_document,
)
from src import ledger, plaid_sync

APPLE_ISSUER = "https://appleid.apple.com"
_apple_keys = jwt.PyJWKClient(f"{APPLE_ISSUER}/auth/keys")
_plaid_keys: dict[str, object] = {}
# Plaid's limit for how old a webhook may be
WEBHOOK_MAX_AGE = 300
# A bank linked moments ago may take minutes to load its history: ask again this many
# times, this far apart
NOT_READY_RETRIES = 10
NOT_READY_WAIT = 60


class AppleSignIn(BaseModel):
    identityToken: str = Field(max_length=10_000)


class Edit(BaseModel):
    type: Literal[
        "setCategory",
        "setNote",
        "trash",
        "restore",
        "markOfficial",
        "unmarkOfficial",
        "createCategory",
    ]
    keys: list[str] = Field(default_factory=list, max_length=5000)
    value: str = Field(default="", max_length=2000)
    # The group of a category made on the phone
    group: str = Field(default="", max_length=200)


class Edits(BaseModel):
    edits: list[Edit] = Field(max_length=500)


def apple_user_id(identity_token: str) -> str:
    """Apple's stable id for the person who signed in; raises jwt.PyJWTError if not genuine."""
    key = _apple_keys.get_signing_key_from_jwt(identity_token).key
    claims = jwt.decode(
        identity_token,
        key,
        algorithms=["RS256"],
        audience=os.environ.get("APPLE_BUNDLE_ID", "EvanRL.ExpenseAnalyzer"),
        issuer=APPLE_ISSUER,
        options={"require": ["exp", "sub"]},
    )
    return claims["sub"]


def verify_plaid_webhook(body: bytes, signature: str) -> None:
    """Raise unless Plaid signed exactly this body within the last few minutes."""
    header = jwt.get_unverified_header(signature)
    if header.get("alg") != "ES256":
        raise ValueError("unexpected signature type")
    key_id = header["kid"]
    if key_id not in _plaid_keys:
        key = plaid_sync._post("/webhook_verification_key/get", {"key_id": key_id})[
            "key"
        ]
        if key.get("expired_at"):
            raise ValueError("signed with a retired key")
        _plaid_keys[key_id] = jwt.PyJWK(key).key
    claims = jwt.decode(
        signature,
        _plaid_keys[key_id],
        algorithms=["ES256"],
        options={"require": ["iat", "request_body_sha256"]},
    )
    if time.time() - claims["iat"] > WEBHOOK_MAX_AGE:
        raise ValueError("too old")
    digest = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(digest, claims["request_body_sha256"]):
        raise ValueError("body does not match")


def _in_background(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def create_app(
    engine: Engine | None = None,
    spawn: Callable[[Callable[[], None]], None] = _in_background,
) -> FastAPI:
    """Build the API. `spawn` runs a bank sync without making the request wait."""
    engine = engine or make_engine()
    # People whose sync this process is running right now
    syncing: set[str] = set()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        # Cloud Run stops the server a while after the last request, sync or no sync
        pipeline.interrupt_jobs(engine, set(syncing))

    app = FastAPI(
        title="ExpenseAnalyzer",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    bearer = HTTPBearer(auto_error=False)

    def database():
        with Session(engine) as db:
            yield db

    def signed_in(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        db: Session = Depends(database),
    ) -> User:
        user = user_for_token(db, credentials.credentials) if credentials else None
        if user is None:
            raise HTTPException(401, "Sign in again")
        return user

    def start_sync(user_id: str) -> bool:
        if not pipeline.start_job(engine, user_id):
            return False

        def work() -> None:
            syncing.add(user_id)
            try:
                for _ in range(NOT_READY_RETRIES):
                    result = pipeline.run_job(engine, user_id)
                    if result is None or result.get("complete", True):
                        break
                    time.sleep(NOT_READY_WAIT)
                    if not pipeline.start_job(engine, user_id):
                        break
            finally:
                syncing.discard(user_id)

        spawn(work)
        return True

    def status_of(db: Session, user: User) -> dict:
        job = db.get(Job, user.id)
        finished = job.finished_at if job else None
        return {
            "banks": [bank.institution or "Bank" for bank in banks_of(db, user.id)],
            "syncing": bool(job and job.running),
            "lastSync": (
                f"{finished.isoformat(timespec='seconds')}Z" if finished else None
            ),
            "error": job.error if job else "",
        }

    @app.exception_handler(plaid_sync.PlaidError)
    def bank_service_failed(request: Request, exc: plaid_sync.PlaidError):
        return JSONResponse({"detail": f"The bank service reported: {exc}"}, 502)

    # Not /healthz: Google's front end answers that path itself, so it never reaches us
    @app.get("/health")
    def health():
        return {"ok": True}

    # ------------------------------------------------------------------
    # Accounts
    # ------------------------------------------------------------------

    @app.post("/v1/auth/apple")
    def sign_in(
        body: AppleSignIn,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
        db: Session = Depends(database),
    ):
        try:
            apple_id = apple_user_id(body.identityToken)
        except jwt.PyJWTError:
            raise HTTPException(401, "Apple did not confirm this sign-in") from None
        user = db.scalar(select(User).where(User.apple_id == apple_id))
        if user is None:
            # A device signed in with a token from `server.cli` (an account without an
            # Apple id) keeps its data: the Apple sign-in is attached to that account
            current = (
                user_for_token(db, credentials.credentials) if credentials else None
            )
            if current is not None and current.apple_id is None:
                user = current
                user.apple_id = apple_id
            else:
                user = add_user(db, apple_id)
                db.flush()
        token = add_login(db, user)
        db.commit()
        return {"token": token}

    @app.post("/v1/auth/logout")
    def sign_out(
        user: User = Depends(signed_in),
        credentials: HTTPAuthorizationCredentials = Depends(bearer),
        db: Session = Depends(database),
    ):
        remove_login(db, credentials.credentials)
        db.commit()
        return {}

    @app.delete("/v1/account")
    def delete_account(
        user: User = Depends(signed_in), db: Session = Depends(database)
    ):
        for bank in banks_of(db, user.id):
            try:
                # Ends Plaid's access to the bank, not just our copy of the key
                plaid_sync._post(
                    "/item/remove", {"access_token": decrypt(engine, bank.access_token)}
                )
            except plaid_sync.PlaidError as exc:
                print(f"[server] Could not disconnect a bank at Plaid: {exc.code}")
        delete_user(db, user)
        db.commit()
        return {}

    # ------------------------------------------------------------------
    # Transactions
    # ------------------------------------------------------------------

    @app.get("/v1/export")
    def export(user: User = Depends(signed_in), db: Session = Depends(database)):
        book = Book(db, user.id)
        return ledger.phone_export(book.rows, book)

    @app.post("/v1/edits")
    def edit(
        body: Edits, user: User = Depends(signed_in), db: Session = Depends(database)
    ):
        book = Book(db, user.id)
        for change in body.edits:
            ledger.apply_edit(book.rows, change.model_dump(), book)
        book.save()
        db.commit()
        return ledger.phone_export(book.rows, book)

    # ------------------------------------------------------------------
    # Banks
    # ------------------------------------------------------------------

    @app.get("/v1/status")
    def status(user: User = Depends(signed_in), db: Session = Depends(database)):
        return status_of(db, user)

    @app.post("/v1/sync")
    def sync(user: User = Depends(signed_in), db: Session = Depends(database)):
        return {"started": bool(banks_of(db, user.id)) and start_sync(user.id)}

    @app.post("/v1/bank/link")
    def link_bank(user: User = Depends(signed_in), db: Session = Depends(database)):
        token, url = plaid_sync.create_link_session(
            user.id, os.environ.get("PLAID_WEBHOOK_URL", "")
        )
        # Remembered so that only the person who started this login can finish it
        write_document(db, user.id, "link_token", token)
        db.commit()
        return {"url": url}

    @app.post("/v1/bank/link/complete")
    def finish_linking(
        user: User = Depends(signed_in), db: Session = Depends(database)
    ):
        token = read_document(db, user.id, "link_token", "")
        # One look at the login page's outcome, without waiting for it
        linked = token and plaid_sync.wait_for_link(token, lambda: True, timeout=1)
        if not linked:
            raise HTTPException(409, "The bank login is not finished")
        public_token, institution = linked
        item = plaid_sync._post(
            "/item/public_token/exchange", {"public_token": public_token}
        )
        db.add(
            Bank(
                user_id=user.id,
                item_id=item["item_id"],
                institution=institution,
                access_token=encrypt(engine, item["access_token"]),
                state=json.dumps(pipeline.NEW_BANK_STATE),
            )
        )
        write_document(db, user.id, "link_token", "")
        db.commit()
        start_sync(user.id)
        return status_of(db, user)

    def plaid_event(body: bytes, signature: str) -> None:
        verify_plaid_webhook(body, signature)
        event = json.loads(body)
        code = event.get("webhook_code")
        with Session(engine) as db:
            bank = db.scalar(select(Bank).where(Bank.item_id == event.get("item_id")))
        print(
            f"[server] Plaid webhook {event.get('webhook_type')}/{code} for "
            f"{'a known' if bank else 'an unknown'} bank"
        )
        if code == "SYNC_UPDATES_AVAILABLE" and bank:
            start_sync(bank.user_id)

    @app.post("/plaid/webhook")
    async def plaid_webhook(request: Request):
        body = await request.body()
        signature = request.headers.get("Plaid-Verification", "")
        try:
            await run_in_threadpool(plaid_event, body, signature)
        except (jwt.PyJWTError, plaid_sync.PlaidError, ValueError, KeyError):
            raise HTTPException(400, "Not a genuine Plaid notification") from None
        return {}

    return app
