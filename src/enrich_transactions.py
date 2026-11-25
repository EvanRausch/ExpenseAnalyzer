"""
enrich_transactions.py — Enrich all bank statement transactions with
merchant info from Google Gemini and Google Places API (New).

Pipeline:
  1. Load every CSV from data/statements/ recursively.
  2. Batch all unique descriptions through Gemini (gemini-2.0-flash) which
     classifies each as online / in_person / transfer / income / fee / other
     and guesses a merchant name + spending category.
  3. For "in_person" transactions, query the Google Places Text Search API
     to find the real business (name, address, phone, website, rating, map URL).
  4. Write data/enriched_transactions.csv (overwritten on each run).

Usage:
    python -m src.enrich_transactions
    python -m src.enrich_transactions --city "Quebec City, QC"
    python -m src.enrich_transactions --statements-dir data/statements
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import time
from collections import defaultdict
from pathlib import Path

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types as genai_types

from src import categories

# ---------------------------------------------------------------------------
# Paths & runtime config
# ---------------------------------------------------------------------------

# The server runs this for each person in a private folder laid out like this project
PROJECT_ROOT = Path(
    os.environ.get("BUDGETTRACKER_HOME") or Path(__file__).parent.parent
)
STATEMENTS_DIR = PROJECT_ROOT / "data" / "statements"
OUTPUT_FILE = PROJECT_ROOT / "data" / "enriched_transactions.csv"
CORRECTIONS_FILE = PROJECT_ROOT / "data" / "category_corrections.json"
PLACES_CACHE_FILE = PROJECT_ROOT / "data" / "places_cache.json"
USER_NOTES_FILE = PROJECT_ROOT / "data" / "user_notes.json"
TRASH_FILE = PROJECT_ROOT / "data" / "trash.json"
CATEGORY_EMOJI_FILE = PROJECT_ROOT / "data" / "category_emojis.json"
SUMMARY_DESC_FILE = PROJECT_ROOT / "data" / "summary_descriptions.json"
CUSTOM_CATEGORIES_FILE = PROJECT_ROOT / "data" / "custom_categories.json"

load_dotenv(PROJECT_ROOT / ".env")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_FIELDS = (
    "places.displayName,"
    "places.formattedAddress,"
    "places.nationalPhoneNumber,"
    "places.websiteUri,"
    "places.rating,"
    "places.googleMapsUri,"
    "places.types,"
    "places.primaryTypeDisplayName"
)
PLACES_RATE_LIMIT_SEC = 0.5  # seconds between Places requests
GEMINI_BATCH_SIZE = 80  # descriptions per Gemini prompt
GEMINI_MODEL = "gemini-2.5-flash"  # override with --model

# Geographic bias: rectangle covering Montréal → Québec City corridor
# (Places API circle max radius is 50 km — use rectangle instead)
# SW: ~45.3°N 74.3°W (south of Montréal)  NE: ~47.1°N 70.8°W (north of QC City)
_QC_LOCATION_BIAS = {
    "rectangle": {
        "low": {"latitude": 45.3, "longitude": -74.3},
        "high": {"latitude": 47.1, "longitude": -70.8},
    }
}
GEMINI_MAX_RETRIES = 6  # retry budget for 429 responses
GEMINI_RETRY_BASE = 10.0  # base wait seconds (doubles each retry)

OUTPUT_COLUMNS = [
    "source_file",
    "account_type",
    "date",
    "description",
    "amount",
    "cashback_pct",
    # Gemini fields
    "purchase_mode",
    "gemini_merchant",
    "gemini_category",
    # Places fields
    "place_name",
    "place_address",
    "place_phone",
    "place_website",
    "place_rating",
    "place_google_maps_url",
    "place_types",
    # AI-generated description (from Gemini + Places context)
    "ai_description",
    # User annotation
    "user_notes",
]

# ---------------------------------------------------------------------------
# Statement loaders
# ---------------------------------------------------------------------------


def _is_credit_csv(path: Path) -> bool:
    """Credit CSVs written by pdf_parser.py start with 'date,description,amount,cashback_pct'."""
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        return first_line.startswith("date,description,amount,cashback_pct")
    except Exception:
        return False


def _is_parsed_debit_csv(path: Path) -> bool:
    """Parsed debit PDF CSVs (from debit_pdf_to_csv) start with 'date,description,amount,account_type'."""
    try:
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[0]
        return first_line.startswith("date,description,amount,account_type")
    except Exception:
        return False


def _read_parsed_debit_csv(path: Path) -> list[dict]:
    """Read a CSV produced by debit_pdf_to_csv()."""
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            desc = (row.get("description") or "").strip()
            if not desc:
                continue
            rows.append(
                {
                    "source_file": path.name,
                    "account_type": "debit",
                    "date": (row.get("date") or "").strip(),
                    "description": desc,
                    "amount": (row.get("amount") or "").strip(),
                    "cashback_pct": "",
                }
            )
    return rows


def _read_credit_csv(path: Path) -> list[dict]:
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for row in csv.DictReader(f):
            desc = (row.get("description") or "").strip()
            if not desc:
                continue
            rows.append(
                {
                    "source_file": path.name,
                    "account_type": "credit",
                    "date": (row.get("date") or "").strip(),
                    "description": desc,
                    "amount": (row.get("amount") or "").strip(),
                    "cashback_pct": (row.get("cashback_pct") or "").strip(),
                }
            )
    return rows


def _read_debit_csv(path: Path) -> list[dict]:
    """Desjardins debit export layout (no header row):
    col[0]=institution  col[1]=transit  col[2]=account  col[3]=date(YYYY/MM/DD)
    col[4]=seq          col[5]=description               col[6]=cheque_no
    col[7]=withdrawal   col[8]=deposit                   col[13]=balance
    """
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for raw in csv.reader(f):
            if len(raw) < 6:
                continue
            date_str = raw[3].strip() if len(raw) > 3 else ""
            desc = raw[5].strip() if len(raw) > 5 else ""
            withdrawal = raw[7].strip() if len(raw) > 7 else ""
            deposit = raw[8].strip() if len(raw) > 8 else ""
            if not desc:
                continue
            if withdrawal:
                try:
                    amount = str(float(withdrawal.replace(",", ".")))
                except ValueError:
                    amount = withdrawal
            elif deposit:
                try:
                    amount = str(-float(deposit.replace(",", ".")))
                except ValueError:
                    amount = deposit
            else:
                amount = "0"
            rows.append(
                {
                    "source_file": path.name,
                    "account_type": "debit",
                    "date": date_str,
                    "description": desc,
                    "amount": amount,
                    "cashback_pct": "",
                }
            )
    return rows


def load_all_transactions(statements_dir: Path) -> list[dict]:
    transactions: list[dict] = []
    csv_files = sorted(statements_dir.rglob("*.csv"))
    # Exclude the output file itself if it lives nearby
    csv_files = [p for p in csv_files if p.resolve() != OUTPUT_FILE.resolve()]
    for csv_path in csv_files:
        if _is_parsed_debit_csv(csv_path):
            batch = _read_parsed_debit_csv(csv_path)
        elif _is_credit_csv(csv_path):
            batch = _read_credit_csv(csv_path)
        else:
            batch = _read_debit_csv(csv_path)
        print(f"  {csv_path.relative_to(PROJECT_ROOT)}: {len(batch)} rows")
        transactions.extend(batch)

    # Deduplicate: same (account_type, date, description, amount) from different files
    seen: set[tuple] = set()
    unique: list[dict] = []
    for t in transactions:
        key = (
            t.get("account_type"),
            t.get("date"),
            t.get("description"),
            t.get("amount"),
        )
        if key not in seen:
            seen.add(key)
            unique.append(t)
    if len(unique) < len(transactions):
        print(f"  ({len(transactions) - len(unique)} duplicate rows removed)")
    return unique


# ---------------------------------------------------------------------------
# Category corrections (written by the UI, consumed here)
# ---------------------------------------------------------------------------


def load_corrections(path: Path = CORRECTIONS_FILE) -> dict[str, str]:
    """Return {description: category} from the corrections JSON file."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def load_user_notes(path: Path = USER_NOTES_FILE) -> dict[str, str]:
    """Return {description: notes_text} from user_notes.json."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_trash_keys(path: Path = TRASH_FILE) -> set[tuple]:
    """Return a set of (date, amount, description, source_file) keys for all trashed rows."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
        return {
            (
                r.get("date", ""),
                r.get("amount", ""),
                r.get("description", ""),
                r.get("source_file", ""),
            )
            for r in rows
            if isinstance(r, dict)
        }
    except Exception:
        return set()


def _build_system_prompt(
    corrections: dict[str, str], notes: dict[str, str] | None = None
) -> str:
    """Append known corrections and user notes as few-shot context to the base system prompt."""
    prompt = _GEMINI_SYSTEM
    if corrections:
        examples = "\n".join(
            f"  - {json.dumps(desc)} -> category: {json.dumps(cat)}"
            for desc, cat in list(corrections.items())[
                :40
            ]  # cap at 40 to stay within token limits
        )
        prompt += (
            "\n\nUser-confirmed category corrections (treat as ground truth for similar descriptions):\n"
            + examples
        )
    if notes:
        note_lines = "\n".join(
            f"  - {json.dumps(desc)}: {json.dumps(note)}"
            for desc, note in list(notes.items())[:30]  # cap at 30
        )
        prompt += (
            "\n\nUser-provided context for specific transactions "
            "(use as guidance when classifying similar descriptions):\n" + note_lines
        )
    return prompt


# ---------------------------------------------------------------------------
# Gemini classification
# ---------------------------------------------------------------------------

_GEMINI_SYSTEM = """You are a bank transaction pre-processor.
Given a JSON array of raw bank statement description strings, return a JSON array
in the EXACT SAME ORDER with one object per description.

Each object must have exactly these keys:
  "purchase_mode": one of "online", "in_person", "transfer", "income", "fee", "other"
  "merchant_name": best human-readable guess at the business/entity/person name

Classification rules:
- "online"    = digital purchase, no physical store (Amazon, App Store, Netflix, PayPal merchants, etc.)
- "in_person" = physical store or local service (restaurants, grocery, dentist, etc.)
- "transfer"  = money sent to / received from a person or another account (Interac, AccesD bill pay)
- "income"    = payroll, salary, employer deposit
- "fee"       = bank service charge, account fee
- For "transfer": set merchant_name to the recipient/sender's name (clean up slashes and padding)
- For "income": set merchant_name to the employer name
- Return ONLY the raw JSON array — no markdown fences, no commentary."""


def _call_gemini(
    descriptions: list[str],
    model: str = GEMINI_MODEL,
    system_prompt: str = _GEMINI_SYSTEM,
) -> list[dict]:
    """Send one batch to Gemini; return the parsed list of classification dicts."""
    if not GEMINI_API_KEY:
        raise RuntimeError("GEMINI_API_KEY not set in .env")

    client = genai.Client(api_key=GEMINI_API_KEY)

    last_exc: Exception | None = None
    for attempt in range(GEMINI_MAX_RETRIES):
        try:
            response = client.models.generate_content(
                model=model,
                contents=json.dumps(descriptions, ensure_ascii=False),
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_prompt,
                ),
            )
            break
        except Exception as exc:
            last_exc = exc
            msg = str(exc)
            # Extract retry delay from error message if present
            delay_match = re.search(r"retry[^\d]*(\d+\.?\d*)", msg, re.IGNORECASE)
            wait = (
                float(delay_match.group(1)) + 2
                if delay_match
                else GEMINI_RETRY_BASE * (2**attempt)
            )
            if attempt < GEMINI_MAX_RETRIES - 1 and "429" in msg:
                print(
                    f"\n    [rate-limited] waiting {wait:.0f}s before retry {attempt + 2}/{GEMINI_MAX_RETRIES}...",
                    end=" ",
                    flush=True,
                )
                time.sleep(wait)
            else:
                raise
    else:
        raise last_exc  # type: ignore[misc]

    raw = response.text.strip()

    # Strip accidental markdown fences Gemini sometimes adds
    raw = re.sub(r"^```[a-z]*\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)

    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Gemini returned non-JSON ({exc}):\n{raw[:600]}") from exc

    if len(result) != len(descriptions):
        raise ValueError(
            f"Gemini returned {len(result)} items for {len(descriptions)} inputs"
        )
    return result


def _classify_batch(
    batch: list[str], model: str, label: str, system_prompt: str = _GEMINI_SYSTEM
) -> dict[str, dict]:
    """Classify one batch, splitting in half on count-mismatch and retrying."""
    try:
        classified = _call_gemini(batch, model=model, system_prompt=system_prompt)
        if len(classified) != len(batch):
            raise ValueError(f"count mismatch: got {len(classified)} for {len(batch)}")
        return dict(zip(batch, classified))
    except ValueError as exc:
        if len(batch) == 1:
            print(f"\n    [skip] {batch[0][:60]} — {exc}", end=" ", flush=True)
            return {}
        # Split and retry halves
        mid = len(batch) // 2
        print(f"\n    [retry split {label}]", end=" ", flush=True)
        left = _classify_batch(batch[:mid], model, label + "a", system_prompt)
        right = _classify_batch(batch[mid:], model, label + "b", system_prompt)
        return {**left, **right}


def classify_all(
    descriptions: list[str],
    model: str = GEMINI_MODEL,
    progress_cb: callable = None,
) -> dict[str, dict]:
    """Pre-process all unique descriptions: extract purchase_mode + merchant_name.

    Descriptions with an exact match in category_corrections.json get their
    category pre-set; all others will be fully classified after Places lookup.
    """
    unique = list(dict.fromkeys(descriptions))  # deduplicate, preserve insertion order

    # Split into known (user-corrected) vs unknown (needs Gemini)
    corrections = load_corrections()
    known: dict[str, dict] = {}
    unknown: list[str] = []
    for desc in unique:
        if desc in corrections:
            cat = corrections[desc]
            known[desc] = {"purchase_mode": "", "merchant_name": "", "category": cat}
        else:
            unknown.append(desc)

    if known:
        print(
            f"  {len(known)} descriptions resolved from corrections file (skipping Gemini)"
        )

    results: dict[str, dict] = dict(known)

    if not unknown:
        return results

    print(
        f"\nGemini ({model}): pre-processing {len(unknown)} descriptions (mode + merchant)..."
    )

    batches = [
        unknown[i : i + GEMINI_BATCH_SIZE]
        for i in range(0, len(unknown), GEMINI_BATCH_SIZE)
    ]

    for bi, batch in enumerate(batches, 1):
        if progress_cb:
            progress_cb(
                "Pre-processing", f"Batch {bi}/{len(batches)} ({len(batch)} items)"
            )
        print(
            f"  Batch {bi}/{len(batches)} ({len(batch)} items)...", end=" ", flush=True
        )
        results.update(_classify_batch(batch, model=model, label=str(bi)))
        print("done")

    return results


# ---------------------------------------------------------------------------
# Google Places enrichment
# ---------------------------------------------------------------------------

_EMPTY_PLACE: dict[str, str] = {
    "place_name": "",
    "place_address": "",
    "place_phone": "",
    "place_website": "",
    "place_rating": "",
    "place_google_maps_url": "",
    "place_types": "",
}


def _search_place(merchant_name: str, city: str) -> dict | None:
    """Call Places Text Search API; return first result or None."""
    if not PLACES_API_KEY:
        raise RuntimeError("GOOGLE_PLACES_API_KEY not set in .env")

    response = requests.post(
        PLACES_URL,
        json={
            "textQuery": f"{merchant_name} {city}",
            "maxResultCount": 1,
            "languageCode": "en",
            "locationBias": _QC_LOCATION_BIAS,
        },
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": PLACES_API_KEY,
            "X-Goog-FieldMask": PLACES_FIELDS,
        },
        timeout=10,
    )
    response.raise_for_status()
    places = response.json().get("places", [])
    return places[0] if places else None


def _extract_place(place: dict) -> dict:
    types = place.get("types", [])
    return {
        "place_name": place.get("displayName", {}).get("text", ""),
        "place_address": place.get("formattedAddress", ""),
        "place_phone": place.get("nationalPhoneNumber", ""),
        "place_website": place.get("websiteUri", ""),
        "place_rating": str(place.get("rating", "")),
        "place_google_maps_url": place.get("googleMapsUri", ""),
        "place_types": "|".join(types),
    }


def _load_places_cache() -> dict[str, dict]:
    """Load the persistent places cache from disk; return {} on missing/corrupt file."""
    try:
        if PLACES_CACHE_FILE.exists():
            return json.loads(PLACES_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_places_cache(cache: dict[str, dict]) -> None:
    """Write the places cache atomically to disk."""
    try:
        PLACES_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        PLACES_CACHE_FILE.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        print(f"Warning: could not save places cache: {exc}")


def enrich_with_places(
    classifications: dict[str, dict],
    city: str,
    existing_cache: dict[str, dict] | None = None,
    progress_cb: callable = None,
) -> dict[str, dict]:
    """Query Places API for every unique in_person description.
    Skips descriptions already present in *existing_cache*.
    Returns the merged cache (existing + newly fetched).
    """
    cache: dict[str, dict] = dict(existing_cache) if existing_cache else {}

    in_person = [
        desc
        for desc, info in classifications.items()
        if info.get("purchase_mode") == "in_person" and desc not in cache
    ]

    if not in_person:
        print("\nGoogle Places: all results served from cache (0 API calls).")
        return cache

    print(
        f"\nGoogle Places: looking up {len(in_person)} new in-person merchants (cache has {len(cache)})..."
    )

    for i, desc in enumerate(in_person, 1):
        merchant = classifications[desc].get("merchant_name") or desc
        quota_reached = False
        try:
            place = _search_place(merchant, city)
            cache[desc] = _extract_place(place) if place else _EMPTY_PLACE.copy()
            status = cache[desc]["place_name"] or "(no result)"
        except Exception as exc:
            # Left out of the cache so the lookup is retried on a later run
            status = f"ERROR: {exc}"
            response = getattr(exc, "response", None)
            quota_reached = response is not None and response.status_code == 429
        print(f"  [{i}/{len(in_person)}] {merchant[:50]:<50} → {status}")
        if progress_cb:
            progress_cb("Google Places", f"{i}/{len(in_person)} merchants")
        if quota_reached:
            print(
                f"  Google Places quota reached — {len(in_person) - i + 1} "
                "merchants left for a later run"
            )
            break
        if i < len(in_person):
            time.sleep(PLACES_RATE_LIMIT_SEC)

    _save_places_cache(cache)
    return cache


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------


def _load_cached_enrichment() -> tuple[dict[str, dict], dict[str, dict]]:
    """Load previously enriched rows from OUTPUT_FILE.
    Returns (cached_classifications, cached_places) keyed by description.
    """
    cached_cls: dict[str, dict] = {}
    cached_places: dict[str, dict] = {}
    if not OUTPUT_FILE.exists():
        return cached_cls, cached_places
    try:
        with OUTPUT_FILE.open(encoding="utf-8", errors="replace") as f:
            for row in csv.DictReader(f):
                desc = row.get("description", "").strip()
                if not desc:
                    continue
                if desc not in cached_cls:
                    cached_cls[desc] = {
                        "purchase_mode": row.get("purchase_mode", ""),
                        "merchant_name": row.get("gemini_merchant", ""),
                        "category": row.get("gemini_category", ""),
                        "user_notes": row.get("user_notes", ""),
                        "ai_description": row.get("ai_description", ""),
                    }
                if desc not in cached_places:
                    cached_places[desc] = {k: row.get(k, "") for k in _EMPTY_PLACE}
    except Exception:
        pass
    return cached_cls, cached_places


def _save_category_emojis(classifications: dict[str, dict]) -> None:
    """Collect category→emoji map from enrichment results and save to disk."""
    emoji_map: dict[str, str] = {}
    # Load existing file first so we don't lose manually added emojis
    try:
        if CATEGORY_EMOJI_FILE.exists():
            emoji_map = json.loads(CATEGORY_EMOJI_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    for cls in classifications.values():
        cat = cls.get("category", "")
        emoji = cls.get("emoji", "")
        if cat and emoji and cat not in emoji_map:
            emoji_map[cat] = emoji
    try:
        CATEGORY_EMOJI_FILE.parent.mkdir(parents=True, exist_ok=True)
        CATEGORY_EMOJI_FILE.write_text(
            json.dumps(emoji_map, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception:
        pass


def _generate_summary_descriptions(
    transactions: list[dict],
    classifications: dict[str, dict],
    model: str = GEMINI_MODEL,
    progress_cb: callable = None,
) -> None:
    """Generate short summary descriptions for each category and merchant,
    based on the individual transaction ai_descriptions. Save to disk."""
    if not GEMINI_API_KEY:
        return

    # Group transaction descriptions by category and by (category, merchant)
    cat_txn_descs: dict[str, list[str]] = defaultdict(list)
    merch_txn_descs: dict[str, dict[str, list[str]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for txn in transactions:
        desc = txn.get("description", "")
        cls = classifications.get(desc, {})
        ai_desc = cls.get("ai_description", "") or desc
        cat = cls.get("category", "") or "Other"
        merchant = cls.get("merchant_name", "") or desc
        if ai_desc:
            cat_txn_descs[cat].append(ai_desc)
            merch_txn_descs[cat][merchant].append(ai_desc)

    # Load existing descriptions so we only request missing ones
    existing_summary: dict = {"categories": {}, "merchants": {}}
    try:
        if SUMMARY_DESC_FILE.exists():
            existing_summary = json.loads(SUMMARY_DESC_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    existing_cats = existing_summary.get("categories", {})
    existing_merchs = existing_summary.get("merchants", {})

    # Build a single Gemini prompt for all categories + merchants
    items = []
    for cat, descs in sorted(cat_txn_descs.items()):
        if cat in existing_cats:
            continue  # already have a description
        unique = list(dict.fromkeys(descs))[:15]  # up to 15 unique samples
        items.append({"type": "category", "name": cat, "samples": unique})
    for cat, merchants in sorted(merch_txn_descs.items()):
        cat_bucket = existing_merchs.get(cat, {})
        for merchant, descs in sorted(merchants.items()):
            if merchant in cat_bucket:
                continue  # already have a description
            unique = list(dict.fromkeys(descs))[:10]
            items.append(
                {
                    "type": "merchant",
                    "category": cat,
                    "name": merchant,
                    "samples": unique,
                }
            )

    if not items:
        return

    prompt = """Given a JSON array of items, each with a "type" (category or merchant),
a "name", and "samples" (transaction descriptions belonging to it), generate a short
summary description (max 8 words) for each item.

For categories: describe the type of expenses (e.g. "Coffee shops, fast food, and restaurants").
For merchants: describe what the business is (e.g. "Canadian coffee chain", "Online streaming service").

Return a JSON array in the EXACT SAME ORDER with one object per item:
  {"description": "short summary"}

Return ONLY the raw JSON array — no markdown fences, no commentary."""

    print("\nGenerating category/merchant summary descriptions...")

    def _describe_batch(batch: list[dict], label: str) -> list[dict]:
        """Call Gemini for a batch; on count-mismatch split and retry halves."""
        try:
            result = _call_gemini(batch, model=model, system_prompt=prompt)
            if len(result) != len(batch):
                raise ValueError(f"count mismatch: got {len(result)} for {len(batch)}")
            return result
        except ValueError:
            if len(batch) == 1:
                return [{}]
            mid = len(batch) // 2
            print(f"\n    [retry split {label}]", end=" ", flush=True)
            left = _describe_batch(batch[:mid], label + "a")
            right = _describe_batch(batch[mid:], label + "b")
            return left + right

    # Process in batches (same as other Gemini calls)
    batches = [
        items[i : i + GEMINI_BATCH_SIZE]
        for i in range(0, len(items), GEMINI_BATCH_SIZE)
    ]

    results: list[dict] = []
    for bi, batch in enumerate(batches, 1):
        if progress_cb:
            progress_cb(
                "Summary descriptions",
                f"Batch {bi}/{len(batches)} ({len(batch)} items)",
            )
        print(
            f"  Batch {bi}/{len(batches)} ({len(batch)} items)...", end=" ", flush=True
        )
        try:
            result = _describe_batch(batch, str(bi))
            results.extend(result)
            print("done")
        except Exception as exc:
            print(f"ERROR: {exc}")
            results.extend([{}] * len(batch))
        if bi < len(batches):
            time.sleep(1)

    # Build the output structure — merge into existing data so failed batches
    # don't blow away previously good descriptions.
    summary: dict = {"categories": {}, "merchants": {}}
    try:
        if SUMMARY_DESC_FILE.exists():
            summary = json.loads(SUMMARY_DESC_FILE.read_text(encoding="utf-8"))
            summary.setdefault("categories", {})
            summary.setdefault("merchants", {})
    except Exception:
        pass

    for item, res in zip(items, results):
        desc_text = res.get("description", "") if isinstance(res, dict) else ""
        if not desc_text:
            continue
        if item["type"] == "category":
            summary["categories"][item["name"]] = desc_text
        else:
            cat = item.get("category", "")
            summary.setdefault("merchants", {}).setdefault(cat, {})
            summary["merchants"][cat][item["name"]] = desc_text

    try:
        SUMMARY_DESC_FILE.parent.mkdir(parents=True, exist_ok=True)
        SUMMARY_DESC_FILE.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"  ✓ {len(summary['categories'])} category + "
            f"{sum(len(v) for v in summary['merchants'].values())} merchant descriptions saved"
        )
    except Exception as exc:
        print(f"  Warning: could not save summary descriptions: {exc}")


# ---------------------------------------------------------------------------
# Post-Places enrichment: AI description + category in a single Gemini pass
# ---------------------------------------------------------------------------

_ENRICH_SYSTEM = """You are a bank transaction enricher.
Given a JSON array of objects, each with:
  "raw_desc"       — the original bank statement line
  "merchant_name"  — a preliminary merchant name guess
  "purchase_mode"  — one of "online", "in_person", "transfer", "income", "fee", "other"
  "place_name"     — from Google Places (may be empty)
  "place_address"  — from Google Places (may be empty)
  "place_types"    — from Google Places (may be empty)

Return a JSON array in the EXACT SAME ORDER with one object per item.
Each object must have exactly this key:
  "ai_description": a clear, human-readable description of the transaction
      (e.g. "Coffee at Starbucks on Ste-Catherine St", "Monthly Netflix subscription",
       "Interac transfer to John Smith"). Max 100 characters.

Use the Places data (place_types, place_name) when available — they are very reliable.
Return ONLY the raw JSON array — no markdown fences, no commentary."""


def _enrich_post_places(
    classifications: dict[str, dict],
    place_results: dict[str, dict],
    corrections: dict[str, str],
    notes: dict[str, str] | None = None,
    model: str = GEMINI_MODEL,
    progress_cb: callable = None,
) -> dict[str, dict]:
    """Gemini pass after Places: generates ai_description only.
    Category assignment is handled separately by _categorize_transactions()."""
    needs_enrichment = [
        desc
        for desc, cls in classifications.items()
        if not cls.get("ai_description") and desc not in corrections
    ]
    if not needs_enrichment:
        print("\nPost-Places enrichment: all served from cache (0 API calls).")
        return classifications

    # Build system prompt with user corrections as few-shot examples
    prompt = _ENRICH_SYSTEM
    if corrections:
        examples = "\n".join(
            f"  - {json.dumps(desc)} -> category: {json.dumps(cat)}"
            for desc, cat in list(corrections.items())[:40]
        )
        prompt += (
            "\n\nUser-confirmed category corrections (treat as ground truth):\n"
            + examples
        )
    if notes:
        note_lines = "\n".join(
            f"  - {json.dumps(desc)}: {json.dumps(note)}"
            for desc, note in list(notes.items())[:30]
        )
        prompt += "\n\nUser-provided context for specific transactions:\n" + note_lines

    print(
        f"\nGemini: enriching {len(needs_enrichment)} transactions (ai_description)..."
    )

    batches = [
        needs_enrichment[i : i + GEMINI_BATCH_SIZE]
        for i in range(0, len(needs_enrichment), GEMINI_BATCH_SIZE)
    ]

    for bi, batch in enumerate(batches, 1):
        if progress_cb:
            progress_cb(
                "AI descriptions", f"Batch {bi}/{len(batches)} ({len(batch)} items)"
            )
        items = []
        for desc in batch:
            cls = classifications.get(desc, {})
            place = place_results.get(desc, {})
            items.append(
                {
                    "raw_desc": desc,
                    "merchant_name": cls.get("merchant_name", ""),
                    "purchase_mode": cls.get("purchase_mode", ""),
                    "place_name": place.get("place_name", ""),
                    "place_address": place.get("place_address", ""),
                    "place_types": place.get("place_types", ""),
                }
            )
        print(
            f"  Batch {bi}/{len(batches)} ({len(batch)} items)...", end=" ", flush=True
        )
        try:
            result = _call_gemini(items, model=model, system_prompt=prompt)
            for desc, enriched in zip(batch, result):
                if isinstance(enriched, dict):
                    classifications[desc]["ai_description"] = str(
                        enriched.get("ai_description", "")
                    )[:120]
            print("done")
        except Exception as exc:
            print(f"ERROR: {exc}")
        if bi < len(batches):
            time.sleep(1)

    return classifications


# ---------------------------------------------------------------------------
# Final categorization: dedicated Gemini pass using ALL enriched data
# ---------------------------------------------------------------------------


def _build_known_categories_list() -> list[str]:
    """The two-level list's categories followed by the person's own."""
    return categories.known_categories(load_custom_categories())


def load_custom_categories() -> dict[str, str]:
    """The person's own categories, name -> group. A list saved by an older version
    (names without groups) is read as belonging to the group "Other"."""
    try:
        if CUSTOM_CATEGORIES_FILE.exists():
            data = json.loads(CUSTOM_CATEGORIES_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items() if k}
            if isinstance(data, list):
                # Older versions saved the whole list; only the person's additions count
                return {
                    str(name): categories.OTHER_GROUP
                    for name in data
                    if name and name not in categories.LEGACY
                }
    except Exception:
        pass
    return {}


def save_custom_categories(custom: dict[str, str]) -> None:
    """Persist the person's own categories (name -> group) to disk."""
    CUSTOM_CATEGORIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    CUSTOM_CATEGORIES_FILE.write_text(
        json.dumps(custom, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _categories_for_prompt(custom: dict[str, str]) -> str:
    """The list as Gemini sees it: one line per group."""
    return "\n".join(
        f"    {group}: " + "; ".join(members)
        for group, members in categories.grouped(custom)
    )


_CATEGORIZE_SYSTEM_TEMPLATE = """You are a bank transaction categorizer.
Given a JSON array of objects, each with:
  "raw_desc"       — the original bank statement line
  "merchant_name"  — merchant/entity name
  "purchase_mode"  — one of "online", "in_person", "transfer", "income", "fee", "other"
  "place_name"     — from Google Places (may be empty)
  "place_address"  — from Google Places (may be empty)
  "place_types"    — from Google Places (may be empty)
  "ai_description" — human-readable description of the transaction

Return a JSON array in the EXACT SAME ORDER with one object per item.
Each object must have exactly one key:
  "category": the single best category for this transaction, copied EXACTLY
    (same spelling, same punctuation) from this list. The list has groups; each line
    is one group followed by its categories:
{categories}

    Rules:
    - Categories describe WHO WAS PAID, not what was bought. A supermarket line is
      "Supermarkets & grocery stores" whatever was in the basket.
    - Merchants that sell everything have their own category (Amazon -> "Online
      marketplaces", Costco and Walmart -> "Big-box, department & warehouse stores").
    - Use place_types and place_name when available — they are very reliable.
    - Use merchant_name, ai_description and purchase_mode to inform your choice.
    - Money coming in, credit-card payments, transfers between the person's own
      accounts and savings contributions belong to the "Not expenses" group.
    - When the line shows the group but not the specific type, use that group's
      "… – Other" category. Use "Unrecognised merchants" only when the line gives
      no clue at all.
    - Never invent a category and never answer with a group name alone.

Return ONLY the raw JSON array — no markdown fences, no commentary."""


def _categorize_transactions(
    classifications: dict[str, dict],
    place_results: dict[str, dict],
    corrections: dict[str, str],
    notes: dict[str, str] | None = None,
    model: str = GEMINI_MODEL,
    progress_cb: callable = None,
) -> dict[str, dict]:
    """Dedicated final categorization pass: assigns category + emoji to every description.

    Skips descriptions in corrections (user ground truth).
    Also skips descriptions that already have a category cached.
    """
    needs_categorization = []
    for desc, cls in classifications.items():
        if desc in corrections:
            # User correction is ground truth — keep it, ensure it's set
            cls["category"] = corrections[desc]
            continue
        if cls.get("category"):
            continue
        needs_categorization.append(desc)

    if not needs_categorization:
        print("\nCategorization: all served from cache / corrections (0 API calls).")
        return classifications

    # Build the category list string for the prompt
    custom = load_custom_categories()
    all_cats = _build_known_categories_list()
    prompt = _CATEGORIZE_SYSTEM_TEMPLATE.format(
        categories=_categories_for_prompt(custom)
    )

    # Add user corrections as few-shot examples
    if corrections:
        examples = "\n".join(
            f"  - {json.dumps(desc)} -> category: {json.dumps(cat)}"
            for desc, cat in list(corrections.items())[:40]
        )
        prompt += (
            "\n\nUser-confirmed category corrections (treat as ground truth):\n"
            + examples
        )
    if notes:
        note_lines = "\n".join(
            f"  - {json.dumps(desc)}: {json.dumps(note)}"
            for desc, note in list(notes.items())[:30]
        )
        prompt += "\n\nUser-provided context for specific transactions:\n" + note_lines

    print(
        f"\nGemini: categorizing {len(needs_categorization)} transactions "
        f"({len(all_cats)} known categories)..."
    )

    batches = [
        needs_categorization[i : i + GEMINI_BATCH_SIZE]
        for i in range(0, len(needs_categorization), GEMINI_BATCH_SIZE)
    ]

    for bi, batch in enumerate(batches, 1):
        if progress_cb:
            progress_cb(
                "Categorizing", f"Batch {bi}/{len(batches)} ({len(batch)} items)"
            )
        items = []
        for desc in batch:
            cls = classifications.get(desc, {})
            place = place_results.get(desc, {})
            items.append(
                {
                    "raw_desc": desc,
                    "merchant_name": cls.get("merchant_name", ""),
                    "purchase_mode": cls.get("purchase_mode", ""),
                    "place_name": place.get("place_name", ""),
                    "place_address": place.get("place_address", ""),
                    "place_types": place.get("place_types", ""),
                    "ai_description": cls.get("ai_description", ""),
                }
            )
        print(
            f"  Batch {bi}/{len(batches)} ({len(batch)} items)...", end=" ", flush=True
        )
        try:
            result = _call_gemini(items, model=model, system_prompt=prompt)
            for desc, categorized in zip(batch, result):
                if isinstance(categorized, dict):
                    cat = categorized.get("category", "")
                    if cat:
                        # Only names from the list are kept, however Gemini spelt them
                        classifications[desc]["category"] = categories.normalise(
                            cat, custom
                        )
            print("done")
        except Exception as exc:
            print(f"ERROR: {exc}")
        if bi < len(batches):
            time.sleep(1)

    return classifications


def translate_corrections(
    corrections: dict[str, str], custom: dict[str, str] | None = None
) -> dict[str, str]:
    """The person's corrections renamed to the two-level list.

    A correction to a category of the list (or one of the person's own) is kept; one to an
    old name with a clear new equivalent is renamed; the rest are dropped so that Gemini
    chooses afresh.
    """
    known = set(categories.known_categories(custom))
    kept: dict[str, str] = {}
    for description, category in corrections.items():
        if category in known:
            kept[description] = category
        elif categories.LEGACY.get(category):
            kept[description] = categories.LEGACY[category]
    return kept


def recategorize_all(model: str = GEMINI_MODEL, progress_cb: callable = None) -> int:
    """Move this Mac's data to the two-level list: translate the corrections, then have
    Gemini categorise every description again. Returns how many descriptions were redone.
    """
    corrections = load_corrections()
    translated = translate_corrections(corrections, load_custom_categories())
    if translated != corrections:
        CORRECTIONS_FILE.write_text(
            json.dumps(translated, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            f"Corrections: {len(translated)} kept or renamed, "
            f"{len(corrections) - len(translated)} left for Gemini to decide."
        )
    cached, _ = _load_cached_enrichment()
    descriptions = [d for d in cached if d not in translated]
    recategorize_descriptions(descriptions, model=model, progress_cb=progress_cb)
    return len(descriptions)


def recategorize_descriptions(
    descriptions: list[str],
    model: str = GEMINI_MODEL,
    progress_cb: callable = None,
) -> dict[str, dict]:
    """Re-categorize a specific set of descriptions using the current category list.

    Loads existing enrichment data from the CSV, clears the category for the
    given descriptions, runs the categorization pass, and writes back.
    Returns {description: {"category": ..., "emoji": ...}} for updated items.
    """
    if not descriptions:
        return {}

    # Load existing enrichment data from the CSV
    cached_cls, cached_places_csv = _load_cached_enrichment()
    corrections = load_corrections()
    notes = load_user_notes()

    # Clear category for targeted descriptions so _categorize_transactions will re-do them
    for desc in descriptions:
        if desc in cached_cls:
            cached_cls[desc].pop("category", None)
            cached_cls[desc].pop("emoji", None)

    # Build place_results from CSV cache
    places_cache = _load_places_cache()
    for desc, place_data in cached_places_csv.items():
        if desc not in places_cache and any(place_data.values()):
            places_cache[desc] = place_data

    # Run categorization only (no other pipeline steps)
    updated = _categorize_transactions(
        cached_cls,
        places_cache,
        corrections=corrections,
        notes=notes,
        model=model,
        progress_cb=progress_cb,
    )

    # Re-write the CSV with updated categories
    transactions = load_all_transactions(STATEMENTS_DIR)
    trash_keys = _load_trash_keys()
    if trash_keys:
        transactions = [
            t
            for t in transactions
            if (
                t.get("date", ""),
                str(t.get("amount", "")),
                t.get("description", ""),
                t.get("source_file", ""),
            )
            not in trash_keys
        ]

    user_notes = notes
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for txn in transactions:
            desc = txn["description"]
            cls = updated.get(desc, {})
            place = places_cache.get(desc, _EMPTY_PLACE.copy())
            writer.writerow(
                {
                    **txn,
                    "purchase_mode": cls.get("purchase_mode", ""),
                    "gemini_merchant": cls.get("merchant_name", ""),
                    "gemini_category": cls.get("category", ""),
                    **place,
                    "ai_description": cls.get("ai_description", ""),
                    "user_notes": user_notes.get(desc, cls.get("user_notes", "")),
                }
            )

    # Update emoji file
    _save_category_emojis(updated)

    print(f"\n✓ Recategorized {len(descriptions)} descriptions")
    return {d: updated.get(d, {}) for d in descriptions}


# ---------------------------------------------------------------------------
# Category consolidation: merge similar small categories
# ---------------------------------------------------------------------------

_MERGE_SYSTEM = """You are a spending category consolidator.
You will receive a JSON object with two keys:
  "small_categories": categories with relatively few transactions that are candidates for merging
  "all_categories": the full list of all categories (including larger ones) that can serve as merge targets

Your job: identify which small categories are duplicates, near-duplicates, or subsets of
other categories (from either list) and should be MERGED.

Rules:
  1. Only merge categories from the "small_categories" list. Never merge a category
     from "all_categories" that is not also in "small_categories".
  2. The merge TARGET can be any category from "all_categories" (including large ones).
  3. If two small categories mean the same thing (e.g. "Refund" and "Refunds"),
     merge the smaller into the larger.
  4. If a small category is a clear subset of another (e.g. "Golf Equipment" → "Golf",
     "Online Gambling" → "Gaming" or "Entertainment"), merge it into the parent.
  5. Do NOT merge categories that represent genuinely different spending types,
     even if they are small. Keep specific, useful categories.
  6. Do NOT merge into "Other" — ever.
  7. Do NOT merge "Interac Transfer" — it is a special category.
  8. Do NOT merge any income-related categories (e.g. "Payroll / Income", "Income").

Return a JSON object mapping each category-to-be-merged to its merge target:
  {"old_category": "target_category", ...}

Only include categories that SHOULD be merged. Omit categories that should stay as-is.
If no merges are needed, return an empty object: {}
Return ONLY the raw JSON object — no markdown fences, no commentary."""


def _consolidate_categories(
    classifications: dict[str, dict],
    transactions: list[dict],
    model: str = GEMINI_MODEL,
) -> dict[str, dict]:
    """Merge similar categories, only considering those in the bottom 50% by count."""
    from collections import defaultdict

    cat_counts: dict[str, int] = defaultdict(int)
    cat_samples: dict[str, list[str]] = defaultdict(list)
    for txn in transactions:
        desc = txn.get("description", "")
        cls = classifications.get(desc, {})
        cat = cls.get("category", "") or "Other"
        cat_counts[cat] += 1
        ai_desc = cls.get("ai_description", "")
        if ai_desc and len(cat_samples[cat]) < 5:
            cat_samples[cat].append(ai_desc)

    if len(cat_counts) <= 2:
        return classifications

    # Sort by count ascending; bottom 50% are merge candidates
    sorted_cats = sorted(cat_counts.items(), key=lambda x: x[1])
    cutoff_idx = len(sorted_cats) // 2
    small_cats = sorted_cats[:cutoff_idx]
    small_cat_names = {cat for cat, _ in small_cats}

    if not small_cats:
        return classifications

    # Build input for Gemini
    small_items = [
        {
            "category": cat,
            "count": count,
            "sample_descriptions": cat_samples.get(cat, []),
        }
        for cat, count in small_cats
    ]
    all_cat_list = [cat for cat, _ in sorted_cats]

    payload = json.dumps(
        {"small_categories": small_items, "all_categories": all_cat_list},
        ensure_ascii=False,
    )

    median_count = sorted_cats[cutoff_idx][1] if cutoff_idx < len(sorted_cats) else 0
    print(
        f"\nGemini: consolidating categories (bottom 50%: {len(small_cats)} categories "
        f"with ≤{median_count} txns each)..."
    )

    try:
        if not GEMINI_API_KEY:
            raise RuntimeError("GEMINI_API_KEY not set")
        client = genai.Client(api_key=GEMINI_API_KEY)
        last_exc: Exception | None = None
        for attempt in range(GEMINI_MAX_RETRIES):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=payload,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=_MERGE_SYSTEM,
                    ),
                )
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc)
                wait = GEMINI_RETRY_BASE * (2**attempt)
                if attempt < GEMINI_MAX_RETRIES - 1 and "429" in msg:
                    print(
                        f"\n    [rate-limited] waiting {wait:.0f}s...",
                        end=" ",
                        flush=True,
                    )
                    time.sleep(wait)
                else:
                    raise
        else:
            raise last_exc  # type: ignore[misc]

        raw = response.text.strip()
        raw = re.sub(r"^```[a-z]*\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
        result = json.loads(raw)
        if not isinstance(result, dict):
            print(f"  Unexpected response type: {type(result).__name__}, skipping.")
            return classifications
    except Exception as exc:
        print(f"  ERROR: {exc}")
        return classifications

    # Filter: only merge small categories, never self-merge, never merge into Other
    merges: dict[str, str] = {}
    for old_cat, new_cat in result.items():
        if (
            isinstance(old_cat, str)
            and isinstance(new_cat, str)
            and old_cat != new_cat
            and new_cat != "Other"
            and old_cat in small_cat_names
        ):
            merges[old_cat] = new_cat

    if not merges:
        print("  No merges needed.")
        return classifications

    print(f"  Merging {len(merges)} categories:")
    for old_cat, new_cat in sorted(merges.items()):
        print(f"    {old_cat} ({cat_counts[old_cat]} txns) → {new_cat}")

    merged_count = 0
    for cls in classifications.values():
        cat = cls.get("category", "")
        if cat in merges:
            cls["category"] = merges[cat]
            merged_count += 1

    # Re-key summary_descriptions.json so merchant descriptions follow their categories
    try:
        if SUMMARY_DESC_FILE.exists():
            summary = json.loads(SUMMARY_DESC_FILE.read_text(encoding="utf-8"))
            merch_buckets = summary.get("merchants", {})
            changed = False
            for old_cat, new_cat in merges.items():
                old_bucket = merch_buckets.pop(old_cat, None)
                if old_bucket:
                    merch_buckets.setdefault(new_cat, {}).update(old_bucket)
                    changed = True
            if changed:
                summary["merchants"] = merch_buckets
                SUMMARY_DESC_FILE.write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
    except Exception as exc:
        print(f"  Warning: could not re-key summary descriptions: {exc}")

    print(f"  ✓ {merged_count} transactions re-assigned")
    return classifications


def enrich(
    statements_dir: Path = STATEMENTS_DIR,
    city: str = "Montreal, QC",
    model: str = GEMINI_MODEL,
    progress_cb: callable = None,
) -> None:
    if progress_cb:
        progress_cb("Loading", "Reading statement files…")
    print(f"Loading transactions from {statements_dir.relative_to(PROJECT_ROOT)}/...")
    transactions = load_all_transactions(statements_dir)
    if not transactions:
        print("No transactions found. Nothing to do.")
        return
    print(f"Total: {len(transactions)} transactions")

    # Remove transactions the user has trashed so they don't reappear after re-enrichment
    trash_keys = _load_trash_keys()
    if trash_keys:
        before = len(transactions)
        transactions = [
            t
            for t in transactions
            if (
                t.get("date", ""),
                str(t.get("amount", "")),
                t.get("description", ""),
                t.get("source_file", ""),
            )
            not in trash_keys
        ]
        skipped = before - len(transactions)
        if skipped:
            print(f"  {skipped} trashed transaction(s) excluded")
    print()

    # Reuse previously enriched results — only call APIs for new descriptions
    cached_cls, cached_places_csv = _load_cached_enrichment()
    all_descs = [t["description"] for t in transactions]
    new_descs = [d for d in dict.fromkeys(all_descs) if d not in cached_cls]
    cached_count = len(dict.fromkeys(all_descs)) - len(new_descs)
    if cached_count:
        print(
            f"  {cached_count} descriptions reused from cache, {len(new_descs)} new\n"
        )

    if progress_cb:
        progress_cb("Pre-processing", f"{len(new_descs)} new descriptions to classify")
    if new_descs:
        new_cls = classify_all(new_descs, model=model, progress_cb=progress_cb)
    else:
        new_cls = {}

    classifications = {**cached_cls, **new_cls}

    # Load persistent places cache; seed it with any place data already in the CSV
    # (backward compatibility: first run after upgrade populates from existing CSV)
    places_cache = _load_places_cache()
    for desc, place_data in cached_places_csv.items():
        if desc not in places_cache and any(place_data.values()):
            places_cache[desc] = place_data

    if progress_cb:
        progress_cb("Google Places", "Looking up merchants…")
    place_results = enrich_with_places(
        classifications, city, existing_cache=places_cache, progress_cb=progress_cb
    )

    # ── Post-Places enrichment: generate ai_description ──
    corrections = load_corrections()
    notes = load_user_notes()
    if progress_cb:
        progress_cb("AI descriptions", "Generating descriptions…")
    classifications = _enrich_post_places(
        classifications,
        place_results,
        corrections=corrections,
        notes=notes,
        model=model,
        progress_cb=progress_cb,
    )

    # ── Final categorization: dedicated pass using ALL enriched data ──
    if progress_cb:
        progress_cb("Categorizing", "Assigning categories…")
    classifications = _categorize_transactions(
        classifications,
        place_results,
        corrections=corrections,
        notes=notes,
        model=model,
        progress_cb=progress_cb,
    )

    # User notes are the source of truth: user_notes.json overrides anything in the CSV cache
    user_notes = notes

    # Save category → emoji mapping from enrichment results
    _save_category_emojis(classifications)

    # Generate category / merchant summary descriptions
    if progress_cb:
        progress_cb("Summary descriptions", "Generating summaries…")
    _generate_summary_descriptions(
        transactions, classifications, model=model, progress_cb=progress_cb
    )

    if progress_cb:
        progress_cb("Writing CSV", f"Saving {len(transactions)} transactions…")
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for txn in transactions:
            desc = txn["description"]
            cls = classifications.get(desc, {})
            place = place_results.get(desc, _EMPTY_PLACE.copy())
            writer.writerow(
                {
                    **txn,
                    "purchase_mode": cls.get("purchase_mode", ""),
                    "gemini_merchant": cls.get("merchant_name", ""),
                    "gemini_category": cls.get("category", ""),
                    **place,
                    "ai_description": cls.get("ai_description", ""),
                    "user_notes": user_notes.get(desc, cls.get("user_notes", "")),
                }
            )

    if progress_cb:
        progress_cb("Done", f"✓ {len(transactions)} transactions enriched")
    print(
        f"\n✓ {len(transactions)} transactions written to {OUTPUT_FILE.relative_to(PROJECT_ROOT)}"
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Enrich bank transactions with Gemini + Google Places",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--city",
        default="Montreal, QC",
        help="City used as context for Google Places queries",
    )
    parser.add_argument(
        "--model",
        default=GEMINI_MODEL,
        help="Gemini model name (e.g. gemini-2.0-flash, gemini-1.5-flash)",
    )
    parser.add_argument(
        "--statements-dir",
        type=Path,
        default=STATEMENTS_DIR,
        help="Root folder containing statement CSVs",
    )
    parser.add_argument(
        "--recategorize-all",
        action="store_true",
        help="Categorise every transaction again with the two-level list, then stop",
    )
    args = parser.parse_args()
    if args.recategorize_all:
        count = recategorize_all(model=args.model)
        print(f"Done: {count} descriptions categorised again.")
        return
    enrich(statements_dir=args.statements_dir, city=args.city, model=args.model)


if __name__ == "__main__":
    main()
