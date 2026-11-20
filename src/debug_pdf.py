"""
debug_pdf.py — Dump raw layout of a Desjardins PDF so the parser can be tuned.

Usage:
    conda run -n budgettracker python -m src.debug_pdf path/to/statement.pdf
"""

import sys
from pathlib import Path

import pdfplumber


def main():
    if len(sys.argv) < 2:
        print("Usage: python -m src.debug_pdf <path/to/statement.pdf>")
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    if not pdf_path.exists():
        print(f"File not found: {pdf_path}")
        sys.exit(1)

    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            print(f"\n{'=' * 70}")
            print(f"PAGE {i + 1}  (width={page.width:.0f}, height={page.height:.0f})")
            print(f"{'=' * 70}")

            # --- raw text ---
            print("\n--- extract_text() ---")
            print(page.extract_text(x_tolerance=3, y_tolerance=3) or "(empty)")

            # --- words with coordinates ---
            print("\n--- extract_words() [first 120] ---")
            print(f"{'x0':>8}  {'top':>8}  {'x1':>8}  text")
            print("-" * 50)
            for w in page.extract_words(x_tolerance=3, y_tolerance=3)[:120]:
                print(f"{w['x0']:8.1f}  {w['top']:8.1f}  {w['x1']:8.1f}  {w['text']}")

            # --- tables ---
            tables = page.extract_tables()
            print(f"\n--- extract_tables() found {len(tables)} table(s) ---")
            for ti, table in enumerate(tables):
                print(f"\n  Table {ti + 1} ({len(table)} rows):")
                for row in table[:30]:
                    cells = [repr(c) for c in row]
                    print(f"    {' | '.join(cells)}")


if __name__ == "__main__":
    main()
