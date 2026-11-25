"""
ledger.py — the rules and stored data behind BudgetTracker, with no window code.

Everything here works on plain transaction rows (dicts from enriched_transactions.csv),
so it can run without the Mac app.
"""

from __future__ import annotations

import csv
import datetime
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

from src import categories

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

if os.environ.get("BUDGETTRACKER_HOME"):
    # The server runs the pipeline for each person in a private folder laid out like this project
    PROJECT_ROOT = Path(os.environ["BUDGETTRACKER_HOME"])
elif getattr(sys, "frozen", False):
    # Running inside a PyInstaller bundle — use the fixed macOS app-support location.
    # build_app.sh creates a symlink there pointing back to the project data/ folder.
    PROJECT_ROOT = Path.home() / "Library" / "Application Support" / "BudgetTracker"
else:
    PROJECT_ROOT = Path(__file__).parent.parent

OUTPUT_FILE = PROJECT_ROOT / "data" / "enriched_transactions.csv"
CORRECTIONS_FILE = PROJECT_ROOT / "data" / "category_corrections.json"
STATEMENTS_DIR = PROJECT_ROOT / "data" / "statements"
AI_ANALYSIS_CACHE = PROJECT_ROOT / "data" / "ai_analysis_cache.json"
TRASH_FILE = PROJECT_ROOT / "data" / "trash.json"
USER_NOTES_FILE = PROJECT_ROOT / "data" / "user_notes.json"
OFFICIAL_INCOME_FILE = PROJECT_ROOT / "data" / "official_income_sources.json"
SUMMARY_DESC_FILE = PROJECT_ROOT / "data" / "summary_descriptions.json"
TRANSFER_DESCRIPTIONS_FILE = PROJECT_ROOT / "data" / "transfer_descriptions.json"

# ---------------------------------------------------------------------------
# Optional Gemini client (used by Statistics tab AI analysis + coverage label)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _load_dotenv
    from google import genai as _genai

    _load_dotenv(PROJECT_ROOT / ".env")
    _GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
    _gemini_client = _genai.Client(api_key=_GEMINI_KEY) if _GEMINI_KEY else None
except Exception:
    _genai = None  # type: ignore
    _gemini_client = None
    _GEMINI_KEY = ""

# ---------------------------------------------------------------------------
# Categories: the two-level list in src/categories.py, plus the person's own
# ---------------------------------------------------------------------------

KNOWN_CATEGORIES = list(categories.ALL_CATEGORIES)

# Emoji icons shown next to each category name in tables: the list's, then the names
# from before the list (still found in old data), then the group names
_LEGACY_EMOJI: dict[str, str] = {
    "Amazon Purchase": "📦",
    "Apple Services": "🍎",
    "Streaming": "🎬",
    "Software/SaaS": "💻",
    "Rent": "🏠",
    "Ride-sharing": "🚗",
    "Food Delivery": "🛵",
    "Groceries": "🛒",
    "Food & Dining": "🍴",
    "Pharmacy": "💊",
    "Health & Medical": "🏥",
    "Dentist/Medical": "🦷",
    "Clothing & Fashion": "👕",
    "Electronics": "🔌",
    "Entertainment": "🎨",
    "Transport": "🚌",
    "Automotive": "🚘",
    "Travel & Accommodation": "🧳",
    "Utilities": "💡",
    "Telecom & Internet": "📶",
    "Payroll / Income": "💰",
    "Interac Transfer": "💸",
    "Bill Payment": "📋",
    "Investment": "📈",
    "Insurance": "🛡️",
    "Bank Fee": "🏦",
    "Government": "🏛️",
    "Education": "🎓",
    "Charity": "💛",
    "Sports & Fitness": "🏋️",
    "Pets": "🐾",
    "Personal Care": "🧴",
    "e-Commerce / Online Shopping": "🛍️",
    "Other": "💳",
    # Extra categories found in data
    "ATM": "🏧",
    "Bars/Festivals/Alcohol": "🍺",
    "Cat": "🐱",
    "Golf": "⛳",
    "Hydro": "⚡",
    "Ski": "⛷️",
    "Weed": "🌿",
}
_CATEGORY_EMOJI: dict[str, str] = {
    **_LEGACY_EMOJI,
    **categories.CATEGORY_EMOJI,
    **categories.GROUP_EMOJI,
}
# The table above, before any emojis saved for one person's own categories are merged in
BUILT_IN_EMOJI = dict(_CATEGORY_EMOJI)


def _is_emoji(s: str) -> bool:
    """Return True if *s* looks like a valid emoji (1-2 codepoints, non-ASCII, non-text)."""
    s = s.strip()
    if not s or len(s) > 8:
        return False
    return any(ord(c) > 0x2000 for c in s)


def _reload_ai_emojis() -> None:
    """Merge emojis from category_emojis.json into _CATEGORY_EMOJI, validating each."""
    try:
        _emoji_file = PROJECT_ROOT / "data" / "category_emojis.json"
        if _emoji_file.exists():
            import json as _json_emoji

            _ai_emojis = _json_emoji.loads(_emoji_file.read_text(encoding="utf-8"))
            for _cat, _em in _ai_emojis.items():
                if _cat not in _CATEGORY_EMOJI and _is_emoji(_em):
                    _CATEGORY_EMOJI[_cat] = _em
    except Exception:
        pass


_reload_ai_emojis()


def _ensure_emoji(label: str) -> str:
    """Return an emoji for *label*, generating one via Gemini only if needed.
    Never blocks on Gemini during table rendering — uses 🏷️ as instant fallback.
    """
    if label in _CATEGORY_EMOJI and _is_emoji(_CATEGORY_EMOJI[label]):
        return _CATEGORY_EMOJI[label]
    # Immediate fallback — don't block the UI
    _CATEGORY_EMOJI[label] = "🏷️"
    return "🏷️"


# ---------------------------------------------------------------------------
# Data layer
# ---------------------------------------------------------------------------


def load_csv(path: Path = OUTPUT_FILE) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _statements_fingerprint() -> str:
    """Return a stable string that changes when any statement file is added or removed."""
    files = sorted(p.name for p in STATEMENTS_DIR.rglob("*.csv") if p.is_file())
    return "|".join(files)


def _load_ai_cache() -> tuple[str, str]:
    """Return (fingerprint, analysis) from cache, or ('', '') if missing/invalid."""
    try:
        with AI_ANALYSIS_CACHE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data.get("fingerprint", ""), data.get("analysis", "")
    except Exception:
        return "", ""


def _save_ai_cache(fingerprint: str, analysis: str) -> None:
    try:
        with AI_ANALYSIS_CACHE.open("w", encoding="utf-8") as f:
            json.dump(
                {"fingerprint": fingerprint, "analysis": analysis},
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception:
        pass


def _load_summary_descs() -> dict:
    """Return {categories: {...}, merchants: {...}} from summary_descriptions.json."""
    try:
        if SUMMARY_DESC_FILE.exists():
            return json.loads(SUMMARY_DESC_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_csv(rows: list[dict], path: Path = OUTPUT_FILE) -> None:
    if not rows:
        return
    # Exclude internal transient UI keys (prefixed "_") such as "_prev_category"
    # stamped by TransactionModel.setData — extra keys on only *some* rows would
    # cause csv.DictWriter to raise ValueError and truncate the file mid-write.
    fieldnames = [k for k in rows[0].keys() if not k.startswith("_")]
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    if path == OUTPUT_FILE:
        export_for_phone(rows)


def load_trash() -> list[dict]:
    """Return the list of trashed transaction dicts, or [] if the file is missing/corrupt."""
    try:
        with TRASH_FILE.open(encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_trash(rows: list[dict]) -> None:
    """Persist the trash list to TRASH_FILE."""
    try:
        TRASH_FILE.parent.mkdir(parents=True, exist_ok=True)
        with TRASH_FILE.open("w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _tx_key(row: dict) -> tuple:
    """Return a composite identity key for a transaction row."""
    return (
        row.get("date", ""),
        row.get("amount", ""),
        row.get("description", ""),
        row.get("source_file", ""),
    )


def load_corrections(path: Path = CORRECTIONS_FILE) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_correction(
    description: str, new_category: str, path: Path = CORRECTIONS_FILE
) -> None:
    corrections = load_corrections(path)
    corrections[description] = new_category
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(corrections, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_user_notes(path: Path = USER_NOTES_FILE) -> dict[str, str]:
    """Return {description: notes_text} from user_notes.json."""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_user_note(description: str, notes: str, path: Path = USER_NOTES_FILE) -> None:
    """Persist a user note keyed by raw bank description. Removes the entry when notes is empty."""
    data = load_user_notes(path)
    if notes:
        data[description] = notes
    else:
        data.pop(description, None)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_official_sources() -> set:
    """Return set of source names marked as official income sources."""
    try:
        with open(OFFICIAL_INCOME_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def _save_official_sources(sources: set) -> None:
    """Persist set of official income source names to JSON."""
    with open(OFFICIAL_INCOME_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(sources), f, ensure_ascii=False, indent=2)


def _load_transfer_descriptions() -> set:
    """Return the descriptions learned to be transfers (see learn_transfers)."""
    try:
        with open(TRANSFER_DESCRIPTIONS_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return set()


def _save_transfer_descriptions(descriptions: set) -> None:
    TRANSFER_DESCRIPTIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(TRANSFER_DESCRIPTIONS_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(descriptions), f, ensure_ascii=False, indent=2)


def _parse_amount(s: str) -> float:
    try:
        return float(str(s).replace(",", ".").strip())
    except (ValueError, TypeError):
        return 0.0


def row_date(row: dict) -> datetime.date | None:
    """Best-effort date parse from the 'date' field of either CSV format."""
    date_str = (row.get("date") or "").strip()
    if not date_str:
        return None
    # Debit: "YYYY/MM/DD" or "YYYY-MM-DD"
    m = re.fullmatch(r"(\d{4})([/-])(\d{2})\2(\d{2})", date_str)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(3)), int(m.group(4)))
        except ValueError:
            return None
    # Credit: "DD MM" with year inferred from source_file
    parts = date_str.split()
    if len(parts) == 2:
        src = row.get("source_file", "")
        year_m = re.search(r"(20\d{2})", src)
        today = datetime.date.today()
        year = int(year_m.group(1)) if year_m else today.year
        try:
            d = datetime.date(year, int(parts[1]), int(parts[0]))
            # If the date is in the future, it likely belongs to the prior year
            if d > today:
                d = datetime.date(year - 1, int(parts[1]), int(parts[0]))
            return d
        except (ValueError, TypeError):
            return None
    return None


_MONTH_ABBREVS = [
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def _format_date_display(raw: str, source_file: str = "") -> str:
    """Convert a raw CSV date string to a friendly 'Jan 15, 2024' display string."""
    raw = raw.strip()
    if not raw:
        return raw
    # Debit: YYYY/MM/DD or YYYY-MM-DD
    m = re.match(r"^(\d{4})[/-](\d{2})[/-](\d{2})$", raw)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12:
            return f"{_MONTH_ABBREVS[mo]} {d:02d}, {y}"
        return raw
    # Credit: "DD MM" (day and month number, year from source_file)
    m2 = re.match(r"^(\d{1,2})\s+(\d{1,2})$", raw)
    if m2:
        day, month = int(m2.group(1)), int(m2.group(2))
        year_m = re.search(r"(20\d{2})", source_file)
        year = int(year_m.group(1)) if year_m else 0
        if 1 <= month <= 12:
            suffix = f", {year}" if year else ""
            return f"{_MONTH_ABBREVS[month]} {day:02d}{suffix}"
    return raw


# ---------------------------------------------------------------------------
# Row classification: every row is spending, income, or a transfer
# ---------------------------------------------------------------------------

# A transfer between the bank account and the credit card appears once on each, for the
# same amount. The two lines are rarely dated more than a weekend apart, and a wider
# window starts matching unrelated lines that happen to share an amount.
TRANSFER_WINDOW_DAYS = 3
_CARD_PAYMENT_RE = re.compile(r"\b(paiement|payment)\b", re.IGNORECASE)
_CASH_ADVANCE_RE = re.compile(
    r"cash advance|avance d'argent|avance de fonds", re.IGNORECASE
)


def _is_cc_bill_payment(row: dict) -> bool:
    """Debit Bill Payment to pay off the Desjardins Visa credit card."""
    if row.get("account_type") != "debit":
        return False
    merchant = (row.get("gemini_merchant") or "").lower()
    # "Remises" is the French name of the Cash Back card
    return any(
        name in merchant for name in ("visa", "desjardins", "cash back", "remises")
    ) and row.get("gemini_category") in (
        "Bill Payment",
        "Credit-card payments",
        "Bill payments to unrecognised payees",
    )


def _is_garbled_credit_transfer(row: dict) -> bool:
    """Credit PDF header rows misread as Interac Transfer."""
    return (
        row.get("account_type") == "credit"
        and row.get("gemini_merchant") == "Interac Transfer"
    )


TRANSFER_CATEGORIES = ("Transfers to people", "Interac Transfer")


def _is_incoming_transfer(row: dict) -> bool:
    """Interac e-Transfer received (amount is negative in debit format)."""
    return (
        row.get("gemini_category") in TRANSFER_CATEGORIES
        and _parse_amount(row.get("amount", 0)) < 0
        and row.get("account_type") == "debit"
    )


def _is_income(row: dict) -> bool:
    """Any transaction where money came *in* (negative amount on debit side)."""
    if row.get("account_type") == "debit" and _parse_amount(row.get("amount", 0)) < 0:
        return True
    return False


def _paired_transfers(rows: list[dict]) -> set[int]:
    """ids of the rows that are the two sides of one transfer: a bank line and a card
    line where the amount leaving one account reaches the other within
    TRANSFER_WINDOW_DAYS. Each line is matched once, closest dates first."""
    bank: dict[int, list] = defaultdict(list)
    card: dict[int, list] = defaultdict(list)
    for row in rows:
        side = {"debit": bank, "credit": card}.get(row.get("account_type"))
        cents = round(_parse_amount(row.get("amount", 0)) * 100)
        day = row_date(row)
        if side is not None and cents and day:
            side[cents].append((day, row))
    candidates = []
    for cents, bank_lines in bank.items():
        # Both accounts count money leaving as positive, so the two sides have opposite signs
        for bank_day, bank_row in bank_lines:
            for card_day, card_row in card.get(-cents, ()):
                gap = abs((card_day - bank_day).days)
                if gap <= TRANSFER_WINDOW_DAYS:
                    candidates.append((gap, bank_day, bank_row, card_row))
    candidates.sort(key=lambda c: c[:2])
    paired: set[int] = set()
    for _, _, bank_row, card_row in candidates:
        if id(bank_row) not in paired and id(card_row) not in paired:
            paired.update((id(bank_row), id(card_row)))
    return paired


def _reads_as_transfer(row: dict) -> bool:
    """A transfer recognised from the bank's wording alone, for when the line on the
    other account is not in the data (an unlinked account, or a day it does not cover).
    """
    amount = _parse_amount(row.get("amount", 0))
    description = row.get("description") or ""
    if row.get("account_type") == "credit":
        return amount < 0 and bool(_CARD_PAYMENT_RE.search(description))
    if row.get("account_type") == "debit":
        cash_advance = amount < 0 and bool(_CASH_ADVANCE_RE.search(description))
        return cash_advance or _is_cc_bill_payment(row)
    return False


def _wording(row: dict) -> str:
    """The row's description without accents, case or spacing differences: the same
    line reads "AccèsD" from the bank link and "AccesD" in a downloaded statement."""
    text = unicodedata.normalize("NFKD", row.get("description") or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(text.casefold().split())


def learn_transfers(rows: list[dict], store=None) -> None:
    """Remember the descriptions of the lines matched as transfers by amount and date.

    A bank words every card payment or cash advance the same way, so a later line with
    a remembered description is a transfer even when its other side is missing. Trashed
    rows count too: they are still the person's transactions.
    """
    store = store or FileStore()
    rows = rows + store.trash()
    paired = _paired_transfers(rows)
    lines: Counter = Counter()
    matched: Counter = Counter()
    for row in rows:
        wording = _wording(row)
        lines[wording] += 1
        matched[wording] += id(row) in paired
    # A remembered description hides every later line that carries it, so one chance
    # match must not be enough: it has to be matched repeatedly, on most of its lines
    learned = {w for w, n in matched.items() if w and n >= 2 and 2 * n > lines[w]}
    known = store.transfer_descriptions()
    if not learned <= known:
        store.save_transfer_descriptions(known | learned)


def transfer_rows(rows: list[dict], store=None) -> list[dict]:
    """Rows that only move money between the person's own bank account and credit card
    (card payments, cash advances), so they are neither spending nor income.

    Money sent to or received from other people (Interac e-Transfers) is not a transfer.
    """
    paired = _paired_transfers(rows)
    known = (store or FileStore()).transfer_descriptions()
    return [
        r
        for r in rows
        if id(r) in paired or _wording(r) in known or _reads_as_transfer(r)
    ]


def _left_out(rows: list[dict], store=None) -> set[int]:
    """ids of the rows no view shows: transfers and garbled credit rows."""
    return {id(r) for r in transfer_rows(rows, store)} | {
        id(r) for r in rows if _is_garbled_credit_transfer(r)
    }


def spending_rows(rows: list[dict], store=None) -> list[dict]:
    """Rows that represent actual money going out (no income, no transfers, no noise)."""
    left_out = _left_out(rows, store)
    return [r for r in rows if id(r) not in left_out and not _is_income(r)]


def income_rows(rows: list[dict], store=None) -> list[dict]:
    """Income and positive inflows (payroll + money received from other people)."""
    left_out = _left_out(rows, store)
    return [r for r in rows if id(r) not in left_out and _is_income(r)]


def visible_rows(rows: list[dict], store=None) -> list[dict]:
    """All rows except transfers and noise (garbled credit rows)."""
    left_out = _left_out(rows, store)
    return [r for r in rows if id(r) not in left_out]


# Interac transfer recipient name substrings (lowercase) that map to Rent
_RENT_INTERAC_NAMES: frozenset[str] = frozenset({"eugen", "eugen rus", "razvan"})


def _effective_category(row: dict) -> str:
    """Return the display category; maps known rent payees to 'Rent' and
    groups all other Interac e-transfers under 'Interac Transfer'."""
    cat = (row.get("gemini_category") or "Other").strip() or "Other"
    if cat in TRANSFER_CATEGORIES:
        merchant = (row.get("gemini_merchant") or "").strip()
        if merchant and merchant != "Interac Transfer":
            if any(name in merchant.lower() for name in _RENT_INTERAC_NAMES):
                return "Rent"
    return cat


class FileStore:
    """What the rules need besides the rows, kept in the Mac app's files.

    The server passes its own object with these methods to keep each person's data apart.
    """

    def trash(self) -> list[dict]:
        return load_trash()

    def save_trash(self, rows: list[dict]) -> None:
        save_trash(rows)

    def official_sources(self) -> set:
        return _load_official_sources()

    def save_official_sources(self, sources: set) -> None:
        _save_official_sources(sources)

    def transfer_descriptions(self) -> set:
        return _load_transfer_descriptions()

    def save_transfer_descriptions(self, descriptions: set) -> None:
        _save_transfer_descriptions(descriptions)

    def save_correction(self, description: str, category: str) -> None:
        save_correction(description, category)

    def save_note(self, description: str, note: str) -> None:
        save_user_note(description, note)

    def known_categories(self) -> list[str]:
        return _get_known_categories()

    def custom_categories(self) -> dict[str, str]:
        """The person's own categories: name -> group."""
        from src.enrich_transactions import load_custom_categories

        return load_custom_categories()

    def save_custom_category(self, name: str, group: str) -> None:
        from src.enrich_transactions import save_custom_categories

        save_custom_categories({**self.custom_categories(), name: group})

    def emoji(self, category: str) -> str:
        return _ensure_emoji(category)

    def group_of(self, category: str) -> str:
        return categories.group_of(category, self.custom_categories())

    def summary_descriptions(self) -> dict:
        return _load_summary_descs()

    def ai_analysis(self) -> str:
        fingerprint, analysis = _load_ai_cache()
        return analysis if fingerprint == _statements_fingerprint() else ""


def apply_category_change(
    all_rows: list[dict], row: dict, new_cat: str, old_cat: str, store=None
) -> list[str]:
    """Set row's category, give the same change to the merchant's other rows, and
    record the corrections. Returns the descriptions of the other rows changed."""
    store = store or FileStore()
    row["gemini_category"] = new_cat
    merchant = (row.get("gemini_merchant") or "").strip()
    similar: list[str] = []
    # Transfers (Interac, PayPal…) are never bulk-updated: each one to the same
    # person can be for a completely different purpose.
    if merchant and row.get("purchase_mode", "") != "transfer":
        for r in all_rows:
            if r is row:
                continue
            r_merchant = (r.get("gemini_merchant") or "").strip()
            r_cat = r.get("gemini_category", "")
            if r_merchant == merchant and r_cat != new_cat:
                # Only rows still in the old category (all of them if it is unknown)
                if not old_cat or r_cat == old_cat:
                    r["gemini_category"] = new_cat
                    store.save_correction(r.get("description", ""), new_cat)
                    similar.append(r.get("description", merchant)[:40])
    store.save_correction(row.get("description", ""), new_cat)
    return similar


# ---------------------------------------------------------------------------
# iPhone app: data goes out as transactions.json, changes come back as phone_edits.json
# ---------------------------------------------------------------------------

PHONE_FOLDER = (
    Path.home()
    / "Library"
    / "Mobile Documents"
    / "com~apple~CloudDocs"
    / "ExpenseAnalyzer"
)
PHONE_EXPORT_NAME = "transactions.json"
PHONE_EDITS_NAME = "phone_edits.json"
PHONE_EDITS_APPLIED_FILE = PROJECT_ROOT / "data" / "phone_edits_applied.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _phone_key(row: dict) -> str:
    """The row's identity (same fields as _tx_key) as one string the phone can send back."""
    return "\x1f".join(str(part) for part in _tx_key(row))


def _phone_row(row: dict) -> dict | None:
    day = row_date(row)
    if day is None:
        return None
    merchant = (row.get("gemini_merchant") or row.get("description") or "").strip()
    return {
        "key": _phone_key(row),
        "date": day.isoformat(),
        "merchant": merchant or "Unknown",
        "summary": row.get("ai_description") or "",
        "bankDescription": row.get("description") or "",
        "amount": _parse_amount(row.get("amount", 0)),
        "category": _effective_category(row),
        "account": row.get("account_type") or "",
        "isIncome": _is_income(row),
        "mode": (row.get("purchase_mode") or "").strip(),
        "place": row.get("place_name") or "",
        "phone": row.get("place_phone") or "",
        "website": row.get("place_website") or "",
        "notes": row.get("user_notes") or "",
    }


def _phone_edits(folder: Path) -> list[dict]:
    edits = _read_json(folder / PHONE_EDITS_NAME, {})
    edits = edits.get("edits", []) if isinstance(edits, dict) else []
    return [e for e in edits if isinstance(e, dict) and e.get("id")]


def phone_export(rows: list[dict], store, applied_edits=()) -> dict:
    """Everything the iPhone app displays."""
    transactions = [p for r in visible_rows(rows, store) if (p := _phone_row(r))]
    trash = [p for r in store.trash() if (p := _phone_row(r))]
    names = sorted(
        set(all_categories(rows, store)) | {t["category"] for t in transactions + trash}
    )
    groups = categories.grouped(store.custom_categories(), names)
    descriptions = store.summary_descriptions()
    return {
        "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "appliedEdits": sorted(applied_edits),
        "transactions": transactions,
        "trash": trash,
        "categories": names,
        "groups": [
            {
                "name": group,
                "emoji": categories.GROUP_EMOJI.get(group, "🏷️"),
                "categories": members,
            }
            for group, members in groups
        ],
        "emojis": {
            **{c: store.emoji(c) for c in names},
            **{group: categories.GROUP_EMOJI.get(group, "🏷️") for group, _ in groups},
        },
        "categoryDescriptions": descriptions.get("categories", {}),
        "merchantDescriptions": descriptions.get("merchants", {}),
        "officialIncomeSources": sorted(store.official_sources()),
        "aiAnalysis": store.ai_analysis(),
    }


def export_for_phone(
    rows: list[dict],
    folder: Path = PHONE_FOLDER,
    applied_file: Path = PHONE_EDITS_APPLIED_FILE,
) -> None:
    """Save what the iPhone app displays to iCloud Drive; does nothing if iCloud Drive is off."""
    if not folder.parent.is_dir():
        return
    sent = {e["id"] for e in _phone_edits(folder)}
    # Listing the applied ids lets the phone stop re-sending changes this Mac has already made
    export = phone_export(rows, FileStore(), sent & set(_read_json(applied_file, [])))
    try:
        folder.mkdir(exist_ok=True)
        # Dot-prefixed so iCloud never syncs the half-written file
        tmp = folder / f".{PHONE_EXPORT_NAME}.tmp"
        tmp.write_text(json.dumps(export, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, folder / PHONE_EXPORT_NAME)
    except OSError as exc:
        print(f"[BudgetTracker] Could not update the iPhone copy: {exc}")


def apply_edit(rows: list[dict], edit: dict, store) -> None:
    """Make one change requested from the iPhone app, in place."""
    kind = edit.get("type")
    value = str(edit.get("value", ""))
    keys = {k for k in edit.get("keys", []) if isinstance(k, str)}
    matching = [r for r in rows if _phone_key(r) in keys]
    if kind == "createCategory" and value:
        group = str(edit.get("group", ""))
        store.save_custom_category(
            value, group if group in categories.GROUP_EMOJI else categories.OTHER_GROUP
        )
    elif kind == "setCategory" and value:
        for row in matching:
            apply_category_change(
                rows, row, value, row.get("gemini_category", ""), store
            )
    elif kind == "setNote":
        # Notes belong to the bank description, as when edited on the Mac
        for description in {r.get("description", "") for r in matching}:
            for r in rows:
                if r.get("description") == description:
                    r["user_notes"] = value
            store.save_note(description, value)
    elif kind == "trash" and matching:
        rows[:] = [r for r in rows if _phone_key(r) not in keys]
        store.save_trash(store.trash() + matching)
    elif kind == "restore":
        trash = store.trash()
        restored = [r for r in trash if _phone_key(r) in keys]
        if restored:
            store.save_trash([r for r in trash if _phone_key(r) not in keys])
            rows.extend(restored)
    elif kind in ("markOfficial", "unmarkOfficial") and value:
        sources = store.official_sources()
        (sources.add if kind == "markOfficial" else sources.discard)(value)
        store.save_official_sources(sources)


def apply_phone_edits(
    rows: list[dict],
    folder: Path = PHONE_FOLDER,
    applied_file: Path = PHONE_EDITS_APPLIED_FILE,
) -> int:
    """Make the changes requested from the iPhone app, in place; returns how many were new."""
    edits = _phone_edits(folder)
    applied = set(_read_json(applied_file, []))
    new = [e for e in edits if e["id"] not in applied]
    store = FileStore()
    for edit in new:
        apply_edit(rows, edit, store)
    if new:
        # Forget ids the phone no longer lists, so this file cannot grow forever
        remembered = (applied | {e["id"] for e in new}) & {e["id"] for e in edits}
        applied_file.write_text(json.dumps(sorted(remembered)), encoding="utf-8")
    return len(new)


# ---------------------------------------------------------------------------
# Totals and statistics
# ---------------------------------------------------------------------------


def _get_known_categories() -> list[str]:
    """The list's categories followed by the person's own."""
    from src.enrich_transactions import load_custom_categories

    return categories.known_categories(load_custom_categories())


def all_categories(rows: list[dict], store=None) -> list[str]:
    """Sorted list of all categories present in the data, plus known categories."""
    seen = set((store or FileStore()).known_categories())
    for row in rows:
        cat = (row.get("gemini_category") or "").strip()
        if cat:
            seen.add(cat)
    return sorted(seen)


def compute_totals(rows: list[dict]) -> dict[str, dict]:
    """Return {category: {count, total, abs_total}} sorted by abs_total desc.

    Outgoing Interac transfers are split per recipient; transfers to known
    rent payees (e.g. Eugen Rus) are folded into the "Rent" category.
    """
    acc: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "total": 0.0, "abs_total": 0.0}
    )
    for row in rows:
        cat = _effective_category(row)
        amt = _parse_amount(row.get("amount", 0))
        acc[cat]["count"] += 1
        acc[cat]["total"] += amt
        acc[cat]["abs_total"] += abs(amt)
    return dict(sorted(acc.items(), key=lambda kv: kv[1]["abs_total"], reverse=True))


def compute_merchant_totals(rows: list[dict]) -> dict[str, dict]:
    """Group rows by merchant → {merchant: {count, total, abs_total}} sorted by abs_total."""
    acc: dict[str, dict] = defaultdict(
        lambda: {"count": 0, "total": 0.0, "abs_total": 0.0}
    )
    for row in rows:
        merchant = (
            row.get("gemini_merchant") or row.get("description") or "Unknown"
        ).strip() or "Unknown"
        amt = _parse_amount(row.get("amount", 0))
        acc[merchant]["count"] += 1
        acc[merchant]["total"] += amt
        acc[merchant]["abs_total"] += abs(amt)
    return dict(sorted(acc.items(), key=lambda kv: kv[1]["abs_total"], reverse=True))


def compute_transaction_slices(rows: list[dict]) -> dict[str, dict]:
    """Return {label: {count, total, abs_total, _row}} for each individual
    transaction.  Labels are "{date} \u2014 {merchant/desc}" and deduplicated
    with a numeric suffix when multiple transactions share the same label.
    Sorted by abs_total descending.
    """
    result: dict[str, dict] = {}
    label_counts: dict[str, int] = {}
    for row in rows:
        amt = _parse_amount(row.get("amount", 0))
        merchant = (
            row.get("gemini_merchant") or row.get("description") or "Unknown"
        ).strip() or "Unknown"
        date = (row.get("date") or "").strip()
        base_label = f"{_format_date_display(date, row.get('source_file') or '')} \u2014 {merchant}"
        seen = label_counts.get(base_label, 0)
        label_counts[base_label] = seen + 1
        label = base_label if seen == 0 else f"{base_label} ({seen + 1})"
        result[label] = {"count": 1, "total": amt, "abs_total": abs(amt), "_row": row}
    return dict(sorted(result.items(), key=lambda kv: kv[1]["abs_total"], reverse=True))


def compute_stats(rows: list[dict], category: str) -> dict:
    filtered = [
        r for r in rows if (r.get("gemini_category") or "Other").strip() == category
    ]
    if not filtered:
        return {}
    amounts = [_parse_amount(r.get("amount", 0)) for r in filtered]
    merchant_totals: dict[str, float] = defaultdict(float)
    for r in filtered:
        m = (
            r.get("gemini_merchant")
            or r.get("place_name")
            or r.get("description")
            or "Unknown"
        ).strip()
        merchant_totals[m] += abs(_parse_amount(r.get("amount", 0)))
    top_merchants = sorted(merchant_totals.items(), key=lambda kv: kv[1], reverse=True)[
        :10
    ]
    return {
        "count": len(amounts),
        "total": sum(amounts),
        "avg": sum(amounts) / len(amounts),
        "max": max(amounts),
        "min": min(amounts),
        "top_merchants": top_merchants,
    }


def compute_stats_overall(rows: list[dict], store=None) -> dict:
    """Compute high-level statistics across all rows.

    Returns a dict with keys:
      total_expense, total_income, net, n_months, avg_monthly_expense,
      top_category, top_category_pct, mode_counts, biggest_txn_amount,
      biggest_txn_merchant, monthly (dict month_str→expense_total)
    """
    spend = spending_rows(rows, store)
    inc = income_rows(rows, store)

    total_expense = sum(abs(_parse_amount(r.get("amount", 0))) for r in spend)
    total_income = sum(abs(_parse_amount(r.get("amount", 0))) for r in inc)
    net = total_income - total_expense

    # Monthly breakdown of expenses
    monthly: dict[str, float] = defaultdict(float)
    for r in spend:
        d = row_date(r)
        if d:
            key = f"{d.year:04d}-{d.month:02d}"
            monthly[key] += abs(_parse_amount(r.get("amount", 0)))

    n_months = len(monthly)
    avg_monthly_expense = (sum(monthly.values()) / n_months) if n_months else 0.0

    # Top category
    cat_totals: dict[str, float] = defaultdict(float)
    for r in spend:
        cat_totals[_effective_category(r)] += abs(_parse_amount(r.get("amount", 0)))
    top_category = max(cat_totals, key=lambda k: cat_totals[k]) if cat_totals else ""
    top_category_pct = (
        (cat_totals[top_category] / total_expense * 100) if total_expense else 0.0
    )

    # Payment mode split
    mode_counts: dict[str, int] = defaultdict(int)
    for r in spend:
        mode_counts[(r.get("purchase_mode") or "unknown").strip()] += 1

    # Biggest single expense
    biggest = max(
        spend, key=lambda r: abs(_parse_amount(r.get("amount", 0))), default=None
    )
    biggest_txn_amount = (
        abs(_parse_amount(biggest.get("amount", 0))) if biggest else 0.0
    )
    biggest_txn_merchant = (
        (
            biggest.get("gemini_merchant") or biggest.get("description") or "Unknown"
        ).strip()
        if biggest
        else ""
    )

    return {
        "total_expense": total_expense,
        "total_income": total_income,
        "net": net,
        "n_months": n_months,
        "avg_monthly_expense": avg_monthly_expense,
        "top_category": top_category,
        "top_category_pct": top_category_pct,
        "mode_counts": dict(mode_counts),
        "biggest_txn_amount": biggest_txn_amount,
        "biggest_txn_merchant": biggest_txn_merchant,
        "monthly": dict(sorted(monthly.items())),
    }


# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------


def _gemini_spending_analysis(rows: list[dict], store=None) -> str:
    """Ask Gemini for a structured expense analysis. Income is intentionally excluded."""
    if not _gemini_client:
        return "No Gemini API key configured."

    stats = compute_stats_overall(rows, store)
    spend = spending_rows(rows, store)

    # Top 10 categories by spend
    cat_totals: dict[str, float] = defaultdict(float)
    for r in spend:
        cat_totals[_effective_category(r)] += abs(_parse_amount(r.get("amount", 0)))
    top_cats = sorted(cat_totals.items(), key=lambda kv: kv[1], reverse=True)[:10]

    # Top 10 merchants by spend
    merch_totals: dict[str, float] = defaultdict(float)
    for r in spend:
        m = (r.get("gemini_merchant") or r.get("description") or "Unknown").strip()
        merch_totals[m] += abs(_parse_amount(r.get("amount", 0)))
    top_merch = sorted(merch_totals.items(), key=lambda kv: kv[1], reverse=True)[:10]

    monthly = stats["monthly"]
    monthly_lines = "\n".join(f"  {m}: ${v:,.0f}" for m, v in sorted(monthly.items()))
    cats_lines = "\n".join(f"  {c}: ${t:,.0f}" for c, t in top_cats)
    merch_lines = "\n".join(f"  {m}: ${t:,.0f}" for m, t in top_merch)
    mode_lines = "\n".join(
        f"  {mode}: {cnt}" for mode, cnt in stats["mode_counts"].items()
    )

    summary = (
        f"EXPENSE SUMMARY (income data is incomplete and excluded)\n"
        f"Period: {stats['n_months']} months\n"
        f"Total expenses: ${stats['total_expense']:,.2f}\n"
        f"Avg monthly spend: ${stats['avg_monthly_expense']:,.2f}\n"
        f"Biggest single expense: ${stats['biggest_txn_amount']:,.2f} at {stats['biggest_txn_merchant']}\n"
        f"\nMonthly expenses:\n{monthly_lines}\n"
        f"\nTop categories:\n{cats_lines}\n"
        f"\nTop merchants:\n{merch_lines}\n"
        f"\nPurchase modes:\n{mode_lines}\n"
    )

    prompt = (
        "You are reviewing someone's expense data from their bank statements.\n"
        "IMPORTANT: Income data is incomplete and not provided — do NOT comment on savings rate, "
        "net balance, or income. Focus exclusively on spending behaviour.\n\n"
        "Based on the expense data below, produce a short structured analysis with these sections:\n"
        "## Monthly Trend — 2-3 bullet points on how spending evolved month to month\n"
        "## Top Categories — brief observation on the biggest spend categories\n"
        "## Notable Merchants — call out any merchants that stand out (frequency or amount)\n"
        "## Habits & Patterns — 2-3 observations about spending habits (online vs in-person, recurring vs one-off, etc.)\n"
        "## Suggestions — 2-3 concrete, actionable suggestions to reduce or optimise spending\n\n"
        "Be concise. Use bullet points under each heading. Do not invent data.\n\n"
        + summary
    )

    try:
        resp = _gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return (resp.text or "").strip()
    except Exception as exc:
        return f"Analysis failed: {exc}"


def _pick_emoji_for_category(name: str) -> str:
    """Ask Gemini to pick one emoji for a brand-new category name.
    Falls back to 🏷️ if Gemini is unavailable or the response is unusable.
    """
    if _gemini_client:
        try:
            prompt = (
                f"Pick exactly ONE emoji that best represents the personal-finance "
                f'spending category "{name}". Reply with only the single emoji, nothing else.'
            )
            resp = _gemini_client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt,
            )
            emoji = (resp.text or "").strip()
            if emoji and len(emoji) <= 8:
                return emoji
        except Exception:
            pass
    return "🏷️"
