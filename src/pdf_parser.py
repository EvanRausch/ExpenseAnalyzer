"""
pdf_parser.py — Parse a Desjardins VISA credit card statement PDF.

Extracts all transaction rows and writes them to a CSV with columns:
  date, description, amount, cashback_pct, transaction_type

Strategy: coordinate-based word grouping.
  1. Extract every word with its (x, y) bounding box via pdfplumber.
  2. Cluster words into rows by vertical (top) position.
  3. Within each row, identify tokens by their horizontal order.
  4. Detect transaction rows by a leading DD MM numeric date.
  5. Rightmost amount token = amount; optional "X,XX %" = cashback; rest = description.

Run the debug helper to inspect a PDF's raw layout before tuning:
    conda run -n budgettracker python -m src.debug_pdf path/to/statement.pdf

Usage:
    from src.pdf_parser import pdf_to_csv
    csv_path = pdf_to_csv(Path("data/statements/credit/statement.pdf"))
"""

import csv
import re
from pathlib import Path

import pdfplumber

# ---------------------------------------------------------------------------
# Constants / patterns
# ---------------------------------------------------------------------------

# Desjardins credit statements use DD MM (numeric) dates, e.g. "19 02"
_DATE_NUM_RE = re.compile(r"^(\d{1,2})\s+(\d{1,2})$")

# Amount token: digits, optional FR thousands space, comma/dot decimal
# e.g. "102,00"  "1 234,56"  "12.50"  "-45,00"
_AMOUNT_RE = re.compile(r"^-?\d[\d\s]*[.,]\d{2}$")

# Cashback rate value token preceding a "%" word, e.g. "0,50" or "2,00"
_PCT_VAL_RE = re.compile(r"^\d+[.,]\d+$")

# Rows to discard (totals, headers, balance lines, column headings)
_SKIP_RE = re.compile(
    r"solde|total|report[\xe9e]?|balance|brought\s+forward|"
    r"paiement|payment|minimum|int[e\xe9]r[e\xea]t|interest|"
    r"cr[e\xe9]dit\s+disponible|credit\s+limit|limite|"
    r"transactions|date|description|montant|amount|"
    r"nouveau\s+solde|new\s+balance|taux|rate|"
    r"c\s*o\s*u\s*r\s*a\s*n\s*t",
    re.IGNORECASE,
)

# Y-axis tolerance: words within this many points share the same row
_ROW_TOL = 4

# ---------------------------------------------------------------------------
# Debit PDF column zone X-boundaries (measured from real statements)
# ---------------------------------------------------------------------------
_DEBIT_ZONE_DATE = (45, 83)  # two-digit days (12,22,..) start at ~54.7
_DEBIT_ZONE_CODE = (83, 110)
_DEBIT_ZONE_DESC = (104, 382)  # desc words (Achat, etc.) start at ~107.9
_DEBIT_ZONE_RETRAIT = (382, 462)  # covers Frais (~338) and Retrait (~388)
_DEBIT_ZONE_DEPOT = (458, 530)
_DEBIT_ZONE_SOLDE = (528, 595)


# ---------------------------------------------------------------------------
# Row grouping
# ---------------------------------------------------------------------------


def _group_rows(words: list[dict]) -> list[list[dict]]:
    """Group pdfplumber word dicts into horizontal rows by y-coordinate."""
    if not words:
        return []
    sorted_words = sorted(words, key=lambda w: (w["top"], w["x0"]))
    rows: list[list[dict]] = []
    current_row = [sorted_words[0]]
    current_top: float = sorted_words[0]["top"]
    for word in sorted_words[1:]:
        if abs(word["top"] - current_top) <= _ROW_TOL:
            current_row.append(word)
        else:
            rows.append(sorted(current_row, key=lambda w: w["x0"]))
            current_row = [word]
            current_top = word["top"]
    if current_row:
        rows.append(sorted(current_row, key=lambda w: w["x0"]))
    return rows


# ---------------------------------------------------------------------------
# Amount helpers
# ---------------------------------------------------------------------------


def _parse_amount(token: str) -> float:
    """Parse a French-format amount string (e.g. '1 234,56') to float."""
    cleaned = token.replace(" ", "").replace(",", ".")
    parts = cleaned.split(".")
    if len(parts) > 2:
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    return float(cleaned)


def _classify(description: str, amount: float) -> str:
    if amount < 0:
        return "credit"
    if re.search(r"paiement|payment|remboursement|refund", description, re.IGNORECASE):
        return "credit"
    return "debit"


# ---------------------------------------------------------------------------
# Row → transaction
# ---------------------------------------------------------------------------


def _row_to_transaction(row: list[dict]) -> dict | None:
    """
    Try to extract a transaction from a word row.

    Expected left-to-right column order in a Desjardins VISA statement:
      [txn_day] [txn_month]  (optionally [post_day] [post_month])
      [description words ...]
      [cashback_value] [%]          <- optional
      [amount]
    """
    texts = [w["text"] for w in row]
    joined = " ".join(texts)

    if _SKIP_RE.search(joined):
        return None

    # --- Detect leading DD MM date ---
    date_str: str | None = None
    date_end_idx = -1
    for i in range(min(4, len(texts) - 1)):
        candidate = f"{texts[i]} {texts[i + 1]}"
        if _DATE_NUM_RE.match(candidate):
            date_str = candidate
            date_end_idx = i + 1
            break

    if date_str is None:
        return None

    remaining = texts[date_end_idx + 1 :]

    # Skip optional second (posting) date
    if len(remaining) >= 2 and _DATE_NUM_RE.match(f"{remaining[0]} {remaining[1]}"):
        remaining = remaining[2:]

    if not remaining:
        return None

    # --- Find rightmost amount token ---
    amount_str: str | None = None
    amount_idx: int | None = None
    for i in range(len(remaining) - 1, -1, -1):
        if _AMOUNT_RE.match(remaining[i]):
            amount_str = remaining[i]
            amount_idx = i
            break

    if amount_str is None:
        return None

    try:
        amount = _parse_amount(amount_str)
    except ValueError:
        return None

    desc_tokens = remaining[:amount_idx]

    # --- Strip trailing cashback "X,XX %" if present ---
    cashback_pct = ""
    if (
        len(desc_tokens) >= 2
        and desc_tokens[-1] == "%"
        and _PCT_VAL_RE.match(desc_tokens[-2])
    ):
        cashback_pct = f"{desc_tokens[-2]} %"
        desc_tokens = desc_tokens[:-2]
    elif desc_tokens and re.match(r"^\d+[.,]\d+\s*%$", desc_tokens[-1]):
        cashback_pct = desc_tokens[-1]
        desc_tokens = desc_tokens[:-1]

    description = " ".join(desc_tokens).strip()
    if not description:
        return None

    return {
        "date": date_str,
        "description": description,
        "amount": amount,
        "cashback_pct": cashback_pct,
        "transaction_type": _classify(description, amount),
    }


# ---------------------------------------------------------------------------
# Per-page extraction
# ---------------------------------------------------------------------------


def _extract_from_page(page) -> list[dict]:
    words = page.extract_words(
        x_tolerance=3,
        y_tolerance=_ROW_TOL,
        keep_blank_chars=False,
        use_text_flow=False,
    )
    rows = _group_rows(words)
    return [txn for txn in (_row_to_transaction(r) for r in rows) if txn]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Debit PDF support
# ---------------------------------------------------------------------------

# French month abbreviations used in Desjardins debit statements
_FR_MONTHS: dict[str, int] = {
    "jan": 1,
    "fev": 2,
    "mar": 3,
    "avr": 4,
    "mai": 5,
    "jun": 6,
    "jul": 7,
    "aou": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

# Full French month names for period-header detection in the watchdog
_FR_MONTH_NAMES: dict[str, int] = {
    "janvier": 1,
    "fevrier": 2,
    "février": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "aout": 8,
    "août": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "decembre": 12,
    "décembre": 12,
}

# "D MMM" at the start of a transaction line
_DEBIT_DATE_RE = re.compile(
    r"^(\d{1,2})\s+(JAN|FEV|MAR|AVR|MAI|JUN|JUL|AOU|SEP|OCT|NOV|DEC)\b",
    re.IGNORECASE,
)

# French number with optional space-thousands separator: "1 663.00", "36.43"
_DEBIT_AMOUNT_RE = re.compile(r"\d{1,3}(?:\s\d{3})*[.,]\d{2}")

# 4-digit year for header extraction
_DEBIT_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Lines that signal the end of the transaction section
_DEBIT_SECTION_END_RE = re.compile(
    r"Date\s+Code\s+Description|SOMMAIRE|Votre\s+compte|Aviser|Veuillez|"
    r"Frais\s+avant|bonification|¹|²",
    re.IGNORECASE,
)


def _parse_debit_amount(s: str) -> float:
    """Parse a French-format amount string: '1 663.00' or '1 663,00' → 1663.0."""
    cleaned = s.replace("\xa0", " ").replace(" ", "").replace(",", ".")
    return float(cleaned)


def detect_pdf_type(pdf_path: Path) -> str:
    """Return 'credit', 'debit', or 'unknown' from first-page text content."""
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[:3]:
                text = (page.extract_text() or "").upper()
                if "VISA" in text:
                    return "credit"
                if "RELEV" in text and "COMPTE" in text:
                    return "debit"
    except Exception:
        pass
    return "unknown"


def read_debit_pdf_period(pdf_path: Path) -> "tuple[str, str] | None":
    """Extract the statement period from a debit PDF header.

    Returns (start_date, end_date) as 'YYYY-MM-DD' strings, or None.
    Reads only the first two pages; does not parse transactions.
    """
    period_re = re.compile(
        r"du\s+(\d+)\w*\s+(\w+)\s+au\s+(\d+)\s+(\w+)\s+(20\d{2})",
        re.IGNORECASE,
    )
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages[:2]:
                text = page.extract_text() or ""
                m = period_re.search(text)
                if m:
                    d1, mo1, d2, mo2, yr = m.groups()
                    mo1n = _FR_MONTH_NAMES.get(mo1.lower())
                    mo2n = _FR_MONTH_NAMES.get(mo2.lower())
                    year = int(yr)
                    if mo1n and mo2n:
                        start = f"{year}-{mo1n:02d}-{int(d1):02d}"
                        end = f"{year}-{mo2n:02d}-{int(d2):02d}"
                        return start, end
    except Exception:
        pass
    return None


def _zone_words(row_words: list[dict], x_min: float, x_max: float) -> list[dict]:
    """Return words whose left edge is within [x_min, x_max)."""
    return [w for w in row_words if x_min <= w["x0"] < x_max]


def _extract_fr_amount(zone_words: list[dict]) -> "float | None":
    """Extract a French-format decimal amount from a set of column-zone words.

    Joins the word tokens (handles thousands separator: e.g. '1' + '663.00')
    and returns the numeric value, or None if no amount is found.
    """
    if not zone_words:
        return None
    tokens = [w["text"] for w in sorted(zone_words, key=lambda w: w["x0"])]
    joined = " ".join(tokens)
    m = _DEBIT_AMOUNT_RE.search(joined)
    if m:
        return _parse_debit_amount(m.group())
    return None


def _extract_debit_transactions(
    text: str, year: int
) -> "tuple[list[dict], float | None]":
    """Kept for API compatibility — delegates to the coordinate-based extractor."""
    # This overload accepts pre-extracted text; the real work happens inside
    # debit_pdf_to_csv which uses _extract_debit_page_coord with the page object.
    return [], None


def _extract_debit_page_coord(
    page, year: int, prev_balance: "float | None" = None
) -> "tuple[list[dict], float | None]":
    """Coordinate-based debit transaction extractor.

    Uses x-axis column zones to distinguish description text from amounts,
    preventing store numbers (e.g. 'JEAN COUTU 347') from being misread as
    French thousands-format amounts.

    Returns (transactions, last_balance).  last_balance is carried to the
    next page so deposit/withdrawal direction can be derived correctly.
    """
    words = page.extract_words(
        x_tolerance=3, y_tolerance=_ROW_TOL, keep_blank_chars=False
    )
    rows = _group_rows(words)

    in_section = False
    transactions: list[dict] = []

    # Current in-progress transaction
    cur_date: "str | None" = None
    cur_desc_parts: list[str] = []
    cur_amount: "float | None" = None
    cur_balance: "float | None" = None

    def _finalise():
        nonlocal cur_date, cur_desc_parts, cur_amount, cur_balance
        if cur_date and cur_amount is not None and cur_balance is not None:
            desc = " ".join(cur_desc_parts).strip()
            if desc:
                transactions.append(
                    {
                        "date": cur_date,
                        "description": desc,
                        "amount": str(round(cur_amount, 2)),
                        "account_type": "debit",
                    }
                )
        cur_date = None
        cur_desc_parts = []
        cur_amount = None
        cur_balance = None

    for row in rows:
        row_text = " ".join(w["text"] for w in row)

        # Detect column-header row ─ start of a transaction section
        if re.search(r"Date\s+Code\s+Description", row_text):
            _finalise()
            in_section = True
            continue

        # Opening balance row
        if in_section and re.search(r"Solde\s+report[eé]", row_text, re.IGNORECASE):
            amounts = _DEBIT_AMOUNT_RE.findall(row_text)
            if amounts:
                prev_balance = _parse_debit_amount(amounts[-1])
            continue

        # Section-end markers (new account section or fee summary)
        if in_section and _DEBIT_SECTION_END_RE.search(row_text):
            _finalise()
            in_section = False
            continue

        if not in_section:
            continue

        # ── Extract per-zone information ──────────────────────────────────
        date_words = _zone_words(row, *_DEBIT_ZONE_DATE)
        code_words = _zone_words(row, *_DEBIT_ZONE_CODE)
        desc_words = _zone_words(row, *_DEBIT_ZONE_DESC)
        retrait_words = _zone_words(row, *_DEBIT_ZONE_RETRAIT)
        depot_words = _zone_words(row, *_DEBIT_ZONE_DEPOT)
        solde_words = _zone_words(row, *_DEBIT_ZONE_SOLDE)

        date_text = " ".join(w["text"] for w in date_words).strip()
        dm = _DEBIT_DATE_RE.match(date_text)
        is_new_txn = dm is not None

        retrait_amt = _extract_fr_amount(retrait_words)
        depot_amt = _extract_fr_amount(depot_words)
        balance = _extract_fr_amount(solde_words)
        desc_text = " ".join(w["text"] for w in desc_words).strip()

        # ── New transaction start ─────────────────────────────────────────
        if is_new_txn:
            _finalise()

            day = int(dm.group(1))
            month = _FR_MONTHS.get(dm.group(2).lower())
            if not month:
                continue
            cur_date = f"{year:04d}-{month:02d}-{day:02d}"

            # Strip leading 2–4-letter transaction code from description
            rest = desc_text
            code_m = re.match(r"^[A-Z]{2,4}\s+", rest)
            if code_m:
                rest = rest[code_m.end() :]
            cur_desc_parts = [rest] if rest else []

            # Determine amount and direction
            if retrait_amt is not None or depot_amt is not None:
                raw_amt = retrait_amt if retrait_amt is not None else depot_amt
                if balance is not None and prev_balance is not None:
                    cur_amount = raw_amt if balance < prev_balance else -raw_amt
                else:
                    cur_amount = raw_amt  # assume withdrawal if no prior balance

            if balance is not None:
                cur_balance = balance
                prev_balance = balance

        # ── Continuation row (no date) ────────────────────────────────────
        else:
            if cur_date is None:
                continue  # before any transaction started

            if desc_text:
                cur_desc_parts.append(desc_text)

            # Amount may be on the continuation row (multi-line description)
            if cur_amount is None:
                raw_amt = None
                if retrait_amt is not None:
                    raw_amt = retrait_amt
                elif depot_amt is not None:
                    raw_amt = -depot_amt
                if raw_amt is not None:
                    if balance is not None and prev_balance is not None:
                        cur_amount = raw_amt if balance < prev_balance else -raw_amt
                    else:
                        cur_amount = raw_amt

            if cur_balance is None and balance is not None:
                cur_balance = balance

            if balance is not None:
                prev_balance = balance

    _finalise()
    return transactions, prev_balance


def debit_pdf_to_csv(pdf_path: Path) -> Path:
    """Parse a Desjardins debit account statement PDF and write a CSV.

    Output columns: date, description, amount, account_type
      - amount: positive = withdrawal, negative = deposit
        (same sign convention as the raw debit CSV export)

    Returns the path to the generated CSV.
    Raises ValueError if no transactions are found.
    """
    csv_path = pdf_path.with_suffix(".csv")
    all_transactions: list[dict] = []
    year: "int | None" = None

    prev_balance: "float | None" = None
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=3, y_tolerance=3) or ""
            if year is None:
                ym = _DEBIT_YEAR_RE.search(text)
                if ym:
                    year = int(ym.group(1))
            if year is None:
                continue
            txns, prev_balance = _extract_debit_page_coord(page, year, prev_balance)
            all_transactions.extend(txns)

    # Deduplicate (same logic as credit parser)
    seen: set[tuple] = set()
    unique: list[dict] = []
    for txn in all_transactions:
        key = (txn["date"], txn["description"], txn["amount"])
        if key not in seen:
            seen.add(key)
            unique.append(txn)

    if not unique:
        raise ValueError(
            f"No transactions found in {pdf_path.name}. "
            "Run 'python -m src.debug_pdf <path>' to inspect the raw layout."
        )

    fieldnames = ["date", "description", "amount", "account_type"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique)

    return csv_path


def pdf_to_csv(pdf_path: Path) -> Path:
    """
    Parse a Desjardins credit card statement PDF and write a CSV next to it.

    Columns: date, description, amount, cashback_pct, transaction_type
    Returns the path to the generated CSV file.
    Raises ValueError if no transactions are found.
    """
    csv_path = pdf_path.with_suffix(".csv")
    transactions: list[dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            transactions.extend(_extract_from_page(page))

    # Deduplicate consecutive-page repeats
    seen: set[tuple] = set()
    unique: list[dict] = []
    for txn in transactions:
        key = (txn["date"], txn["description"], txn["amount"])
        if key not in seen:
            seen.add(key)
            unique.append(txn)

    if not unique:
        raise ValueError(
            f"No transactions found in {pdf_path.name}. "
            "Run 'python -m src.debug_pdf <path>' to inspect the raw layout."
        )

    fieldnames = ["date", "description", "amount", "cashback_pct", "transaction_type"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(unique)

    return csv_path
