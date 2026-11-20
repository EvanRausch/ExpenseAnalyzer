"""
test_pdf_parser.py — Compare debit PDF parser output against reference CSVs.

Usage:
    # Compare a specific PDF against a specific CSV:
    python -m tests.test_pdf_parser --pdf path/to/statement.pdf --csv path/to/reference.csv

    # Auto-match pairs in a directory by date range in filename:
    python -m tests.test_pdf_parser --dir data/statements/debit/

    # Process raw PDFs from Downloads, compare to reference CSVs in statements/debit/:
    python -m tests.test_pdf_parser \\
        --pdf ~/Downloads/statement_*.pdf \\
        --refdir data/statements/debit/

Exit code 0 = all matched; 1 = mismatches found or errors.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import tempfile
from pathlib import Path

# Allow running as  python -m tests.test_pdf_parser  from the project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.pdf_parser import debit_pdf_to_csv, read_debit_pdf_period

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATE_RANGE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})")


def _load_csv(path: Path) -> list[dict]:
    """Load any CSV with a header row into a list of dicts."""
    with path.open(encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _load_debit_export_csv(path: Path) -> list[dict]:
    """Load a raw Desjardins debit export (no header, fixed columns)."""
    rows = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for raw in csv.reader(f):
            if len(raw) < 6:
                continue
            date_str = raw[3].strip() if len(raw) > 3 else ""
            if not re.match(r"\d{4}/\d{2}/\d{2}", date_str):
                continue  # skip non-transaction rows
            desc = raw[5].strip() if len(raw) > 5 else ""
            w = raw[7].strip() if len(raw) > 7 else ""
            d = raw[8].strip() if len(raw) > 8 else ""
            amount = float(w) if w else (-float(d) if d else 0.0)
            rows.append(
                {
                    "date": date_str.replace("/", "-"),
                    "description": desc,
                    "amount": amount,
                }
            )
    return rows


def _date_range_from_filename(path: Path) -> tuple[str, str] | None:
    m = _DATE_RANGE_RE.search(path.name)
    if m:
        return m.group(1), m.group(2)
    return None


def _parse_pdf_to_tmp(pdf_path: Path) -> tuple[Path, list[dict]] | tuple[None, None]:
    """Parse the PDF into a temp CSV and return (csv_path, rows)."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_pdf = Path(tmp_dir) / pdf_path.name
        import shutil

        shutil.copy2(pdf_path, tmp_pdf)
        try:
            csv_path = debit_pdf_to_csv(tmp_pdf)
            rows = _load_csv(csv_path)
            # Normalise amount to float
            for r in rows:
                try:
                    r["amount"] = float(r.get("amount", 0))
                except (ValueError, TypeError):
                    r["amount"] = 0.0
            # Copy results out before tmp_dir is deleted
            return (csv_path, rows)
        except Exception as exc:
            print(f"  ERROR parsing PDF: {exc}")
            return None, None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare_pair(pdf_path: Path, ref_csv_path: Path) -> bool:
    """Parse pdf_path and compare against ref_csv_path. Returns True if match."""
    print(f"\n{'=' * 70}")
    print(f"PDF : {pdf_path.name}")
    print(f"REF : {ref_csv_path.name}")
    print("=" * 70)

    # Determine ref format
    try:
        first_line = ref_csv_path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[0]
    except Exception:
        first_line = ""

    if first_line.startswith("date,description,amount"):
        ref_rows = _load_csv(ref_csv_path)
        for r in ref_rows:
            try:
                r["amount"] = float(r.get("amount", 0))
            except (ValueError, TypeError):
                r["amount"] = 0.0
    else:
        ref_rows = _load_debit_export_csv(ref_csv_path)

    # Parse PDF
    _, parsed_rows = _parse_pdf_to_tmp(pdf_path)
    if parsed_rows is None:
        print("  FAIL: PDF parse error")
        return False

    ok = True

    # --- Count ---
    n_ref = len(ref_rows)
    n_pdf = len(parsed_rows)
    count_ok = n_ref == n_pdf
    count_sym = "✓" if count_ok else "✗"
    print(f"  {count_sym} Transaction count: PDF={n_pdf}, REF={n_ref}")
    if not count_ok:
        ok = False

    # --- Total amounts ---
    sum_ref = sum(r["amount"] for r in ref_rows)
    sum_pdf = sum(r["amount"] for r in parsed_rows)
    diff = abs(sum_ref - sum_pdf)
    sum_ok = diff < 0.02  # tolerate float rounding
    sum_sym = "✓" if sum_ok else "✗"
    print(
        f"  {sum_sym} Total amount:       PDF=${sum_pdf:,.2f}, REF=${sum_ref:,.2f}  (diff={diff:.2f})"
    )
    if not sum_ok:
        ok = False

    # --- Date range ---
    if parsed_rows and ref_rows:
        pdf_dates = sorted(r.get("date", "") for r in parsed_rows)
        ref_dates = sorted(r.get("date", "") for r in ref_rows)
        range_ok = pdf_dates[0] == ref_dates[0] and pdf_dates[-1] == ref_dates[-1]
        range_sym = "✓" if range_ok else "✗"
        print(
            f"  {range_sym} Date range:         PDF={pdf_dates[0]} → {pdf_dates[-1]}, "
            f"REF={ref_dates[0]} → {ref_dates[-1]}"
        )
        if not range_ok:
            ok = False

    # --- Row-level diff (when counts match) ---
    if n_ref == n_pdf and n_ref > 0:
        # Sort both by (date, amount) for comparison
        s_pdf = sorted(parsed_rows, key=lambda r: (r.get("date", ""), r["amount"]))
        s_ref = sorted(ref_rows, key=lambda r: (r.get("date", ""), r["amount"]))
        mismatches = 0
        for i, (p, r) in enumerate(zip(s_pdf, s_ref)):
            if p.get("date") != r.get("date") or abs(p["amount"] - r["amount"]) > 0.01:
                mismatches += 1
                if mismatches <= 10:
                    print(
                        f"    MISMATCH #{mismatches}: "
                        f"PDF [{p.get('date')} ${p['amount']:+.2f} {p.get('description', '')[:30]}] "
                        f"vs REF [{r.get('date')} ${r['amount']:+.2f} {r.get('description', '')[:30]}]"
                    )
        if mismatches:
            print(f"  ✗ {mismatches} row mismatches (sorted by date+amount)")
            ok = False
        else:
            print(f"  ✓ All rows match (date + amount)")
    elif n_ref != n_pdf:
        # Show transactions only in one source
        pdf_keys = {(r.get("date", ""), round(r["amount"], 2)) for r in parsed_rows}
        ref_keys = {(r.get("date", ""), round(r["amount"], 2)) for r in ref_rows}
        only_pdf = pdf_keys - ref_keys
        only_ref = ref_keys - pdf_keys
        if only_pdf:
            print(f"  Only in PDF ({len(only_pdf)}): {sorted(only_pdf)[:5]}")
        if only_ref:
            print(f"  Only in REF ({len(only_ref)}): {sorted(only_ref)[:5]}")

    print(f"  {'PASS' if ok else 'FAIL'}")
    return ok


# ---------------------------------------------------------------------------
# Auto-matching
# ---------------------------------------------------------------------------


def find_pairs_in_dir(directory: Path) -> list[tuple[Path, Path]]:
    """Find PDF+CSV pairs in directory that share the same date range in filename."""
    pdfs = {(_date_range_from_filename(p), p) for p in directory.glob("*.pdf")}
    csvs = {(_date_range_from_filename(p), p) for p in directory.glob("*.csv")}
    pairs = []
    for dr, pdf in sorted(pdfs, key=lambda x: str(x)):
        if dr is None:
            continue
        for dr2, csv_path in csvs:
            if dr == dr2:
                pairs.append((pdf, csv_path))
                break
    return pairs


def find_pairs_by_period(pdfs: list[Path], refdir: Path) -> list[tuple[Path, Path]]:
    """Match PDFs to CSVs in refdir by reading the PDF period header."""
    pairs = []
    for pdf in pdfs:
        period = read_debit_pdf_period(pdf)
        if period is None:
            print(f"  Could not read period from {pdf.name}, skipping")
            continue
        start, end = period
        pattern = f"{start}_to_{end}"
        matches = list(refdir.glob(f"*{pattern}*.csv"))
        if not matches:
            print(
                f"  No matching CSV found for period {start} → {end} (PDF: {pdf.name})"
            )
        else:
            pairs.append((pdf, matches[0]))
    return pairs


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Compare debit PDF parser output vs reference CSVs"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--dir",
        type=Path,
        metavar="DIR",
        help="Directory with both PDFs and CSVs; auto-matched by date range in filename",
    )
    group.add_argument(
        "--pdf",
        type=Path,
        nargs="+",
        metavar="PDF",
        help="One or more PDF files to test",
    )

    parser.add_argument(
        "--csv",
        type=Path,
        nargs="+",
        metavar="CSV",
        help="Reference CSV file(s) when using --pdf (must match count)",
    )
    parser.add_argument(
        "--refdir",
        type=Path,
        metavar="REFDIR",
        help="Directory with reference CSVs (auto-matched to --pdf by period)",
    )

    args = parser.parse_args()

    pairs: list[tuple[Path, Path]] = []

    if args.dir:
        pairs = find_pairs_in_dir(args.dir.expanduser().resolve())
        if not pairs:
            print(f"No matched PDF+CSV pairs found in {args.dir}")
            sys.exit(1)
    else:
        pdfs = [p.expanduser().resolve() for p in args.pdf]
        if args.csv:
            if len(args.csv) != len(pdfs):
                parser.error("--csv count must match --pdf count")
            pairs = [
                (pdfs[i], args.csv[i].expanduser().resolve()) for i in range(len(pdfs))
            ]
        elif args.refdir:
            pairs = find_pairs_by_period(pdfs, args.refdir.expanduser().resolve())
        else:
            parser.error("Provide --csv or --refdir when using --pdf")

    results = []
    for pdf_path, ref_csv_path in pairs:
        results.append(compare_pair(pdf_path, ref_csv_path))

    print(f"\n{'=' * 70}")
    passed = sum(results)
    total = len(results)
    print(f"Results: {passed}/{total} pairs matched")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
