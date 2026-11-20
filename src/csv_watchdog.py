"""
csv_watchdog.py — Monitor ~/Downloads for Desjardins bank statement files.

Detection is content-based (no reliance on specific filenames):
  CSV  — column[3] matches YYYY/MM/DD → debit export → data/statements/debit/
  PDF  — first-page text contains "RELEV" + "COMPTE" → debit statement → parsed → data/statements/debit/
  PDF  — first-page text contains "VISA" → credit card → parsed → data/statements/credit/
  ZIP  — extracted; each PDF inside is classified and processed above

Filename-based rules are kept as a fallback:
  CSV keyword "releve" → debit
  PDF prefix "############8000" → credit

Duplicate handling:
  If a CSV already exists for the same period, an incoming PDF is skipped.
  If a PDF already exists for the same period and a new CSV arrives, the PDF is deleted.

Usage:
    python -m src.csv_watchdog                # watch ~/Downloads
    python -m src.csv_watchdog --watch /some/other/folder

Press Ctrl+C to stop.
"""

import argparse
import csv
import re
import shutil
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path
from threading import Lock, Timer

from watchdog.events import FileCreatedEvent, FileMovedEvent, FileSystemEventHandler
from watchdog.observers import Observer

from src.pdf_parser import (
    debit_pdf_to_csv,
    detect_pdf_type,
    pdf_to_csv,
    read_debit_pdf_period,
)

PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_WATCH_DIR = Path.home() / "Downloads"
DEST_DEBIT = PROJECT_ROOT / "data" / "statements" / "debit"
DEST_CREDIT = PROJECT_ROOT / "data" / "statements" / "credit"

CSV_KEYWORD = "releve"  # filename-based fallback for debit CSVs
PDF_PREFIX = "############8000"  # filename-based fallback for credit PDFs (old format)
# New credit PDF format: 453091######8028_YYYYMMDD.pdf (digits + hashes + underscore + date)
_CREDIT_PDF_RE = re.compile(r"^[\d#]{6,}[_-]\d{8}", re.IGNORECASE)
_COOLDOWN = 3.0  # seconds — ignore duplicate events for the same source path
_ENRICH_DELAY = 10.0  # seconds to wait after last file before re-enriching

_enrich_timer: Timer | None = None
_enrich_lock = Lock()


def _schedule_enrich() -> None:
    """(Re)schedule a background enrichment run, debounced by _ENRICH_DELAY.
    Multiple files arriving quickly (e.g. from a ZIP) share one enrichment run.
    """
    global _enrich_timer
    with _enrich_lock:
        if _enrich_timer is not None:
            _enrich_timer.cancel()
        _enrich_timer = Timer(_ENRICH_DELAY, _run_enrich)
        _enrich_timer.daemon = True
        _enrich_timer.start()


def _run_enrich() -> None:
    """Run the enrichment pipeline and print a summary."""
    from src.enrich_transactions import STATEMENTS_DIR, enrich

    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] Enriching transactions...")
    try:
        enrich(statements_dir=STATEMENTS_DIR)
        ts2 = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts2}] Enrichment complete — enriched_transactions.csv updated")
    except Exception as exc:
        print(f"         ✗ Enrichment failed: {exc}")


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------


def _date_range_from_debit_csv(csv_path: Path) -> tuple[str, str] | None:
    """Read a Desjardins debit CSV (no header) and return (min_date, max_date)
    as 'YYYY-MM-DD' strings. Date is in column index 3 as 'YYYY/MM/DD'."""
    dates = []
    try:
        with csv_path.open(encoding="utf-8", errors="replace") as f:
            for row in csv.reader(f):
                if len(row) > 3:
                    m = re.match(r"(\d{4})/(\d{2})/(\d{2})", row[3].strip())
                    if m:
                        dates.append(f"{m.group(1)}-{m.group(2)}-{m.group(3)}")
    except Exception:
        return None
    if not dates:
        return None
    return min(dates), max(dates)


def _date_range_from_credit_csv(
    csv_path: Path, source_name: str
) -> tuple[str, str] | None:
    """Read a parsed credit CSV and return (min_date, max_date) as 'YYYY-MM-DD'.
    Dates are stored as 'DD MM'; year is inferred from source_name or current year."""
    year_match = re.search(r"(20\d{2})", source_name)
    year = int(year_match.group(1)) if year_match else datetime.now().year
    dates = []
    try:
        with csv_path.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                m = re.match(
                    r"^(\d{1,2})\s+(\d{1,2})$", (row.get("date") or "").strip()
                )
                if m:
                    day, month = int(m.group(1)), int(m.group(2))
                    try:
                        dates.append(datetime(year, month, day))
                    except ValueError:
                        pass
    except Exception:
        return None
    if not dates:
        return None
    return min(dates).strftime("%Y-%m-%d"), max(dates).strftime("%Y-%m-%d")


def _rename_with_date_range(
    path: Path, prefix: str, date_range: tuple[str, str] | None
) -> Path:
    """Rename path to '<prefix>_<from>_to_<to><suffix>' if date_range is available."""
    if not date_range:
        return path
    start, end = date_range
    new_name = f"{prefix}_{start}_to_{end}{path.suffix}"
    new_path = path.parent / new_name
    if new_path.exists() and new_path != path:
        stamp = datetime.now().strftime("%H%M%S")
        new_path = path.parent / f"{prefix}_{start}_to_{end}_{stamp}{path.suffix}"
    path.rename(new_path)
    return new_path


def _date_range_from_parsed_debit_csv(csv_path: Path) -> tuple[str, str] | None:
    """Extract date range from a debit PDF output CSV ('date' column is YYYY-MM-DD)."""
    dates = []
    try:
        with csv_path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                d = (row.get("date") or "").strip()
                if re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    dates.append(d)
    except Exception:
        return None
    if not dates:
        return None
    return min(dates), max(dates)


def _sniff_is_debit_csv(path: Path) -> bool:
    """Return True if the file looks like a Desjardins debit CSV by content.
    Checks that column[3] of the first few data rows matches YYYY/MM/DD.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for i, row in enumerate(csv.reader(f)):
                if i >= 8:
                    break
                if len(row) > 3:
                    if re.match(r"^\d{4}/\d{2}/\d{2}$", (row[3] or "").strip()):
                        return True
        return False
    except Exception:
        return False


def _find_same_period(dest_dir: Path, date_range: tuple[str, str]) -> list[Path]:
    """Return all files in dest_dir whose filename contains '<start>_to_<end>'."""
    start, end = date_range
    pattern = f"{start}_to_{end}"
    return [p for p in dest_dir.iterdir() if p.is_file() and pattern in p.name]


class StatementHandler(FileSystemEventHandler):
    """Route Desjardins statement files to the correct destination subfolder."""

    def __init__(self):
        super().__init__()
        DEST_DEBIT.mkdir(parents=True, exist_ok=True)
        DEST_CREDIT.mkdir(parents=True, exist_ok=True)
        self._seen: dict[str, float] = {}
        self._lock = Lock()

    # ------------------------------------------------------------------
    # File classification (content-first, filename fallback)
    # ------------------------------------------------------------------

    def _dest_for(self, path: Path) -> Path | None:
        """Return the destination folder for path, or None if not a bank statement.
        Returns None for ZIP files (use _handle_zip instead).
        """
        suffix = path.suffix.lower()
        if suffix == ".zip":
            return None  # handled separately
        if suffix == ".csv":
            if _sniff_is_debit_csv(path):
                return DEST_DEBIT
            if CSV_KEYWORD in path.stem.lower():
                return DEST_DEBIT
            return None
        if suffix == ".pdf":
            try:
                pdf_type = detect_pdf_type(path)
            except Exception:
                pdf_type = "unknown"
            if pdf_type == "credit":
                return DEST_CREDIT
            if pdf_type == "debit":
                return DEST_DEBIT
            # Filename fallback for credit PDFs
            if path.stem.lower().startswith(PDF_PREFIX.lower()):
                return DEST_CREDIT
            if _CREDIT_PDF_RE.match(path.stem):
                return DEST_CREDIT
            return None
        return None

    def _pdf_type(self, path: Path, dest_dir: Path) -> str:
        """Return the detected PDF type, defaulting by dest dir if unknown."""
        try:
            t = detect_pdf_type(path)
            if t != "unknown":
                return t
        except Exception:
            pass
        return "credit" if dest_dir == DEST_CREDIT else "debit"

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _handle(self, src_path: str):
        path = Path(src_path)
        suffix = path.suffix.lower()
        if suffix not in (".csv", ".pdf", ".zip"):
            return

        now = time.monotonic()
        with self._lock:
            last = self._seen.get(src_path, 0)
            if now - last < _COOLDOWN:
                return
            self._seen[src_path] = now

        if suffix == ".zip":
            self._handle_zip(path)
        elif suffix == ".pdf":
            dest_dir = self._dest_for(path)
            if dest_dir is not None:
                self._parse_pdf(path, dest_dir)
        else:
            dest_dir = self._dest_for(path)
            if dest_dir is not None:
                self._copy(path, dest_dir)

    def on_created(self, event: FileCreatedEvent):
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event: FileMovedEvent):
        # Triggered when a browser finishes writing and renames the temp file
        if not event.is_directory:
            self._handle(event.dest_path)

    # ------------------------------------------------------------------
    # Processors
    # ------------------------------------------------------------------

    def _parse_pdf(self, src: Path, dest_dir: Path):
        """Parse a statement PDF and save transactions as a CSV."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Parsing  {src.name}")

        pdf_type = self._pdf_type(src, dest_dir)

        # For debit PDFs: check for an existing CSV covering the same period
        if pdf_type == "debit":
            period = read_debit_pdf_period(src)
            if period:
                existing = _find_same_period(dest_dir, period)
                csv_exists = any(p.suffix == ".csv" for p in existing)
                if csv_exists:
                    print(
                        f"         ↷ Skipped: CSV for period "
                        f"{period[0]} → {period[1]} already exists"
                    )
                    return

        pdf_dest = dest_dir / src.name
        if pdf_dest.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            pdf_dest = dest_dir / f"{src.stem}_{stamp}{src.suffix}"
        shutil.copy2(src, pdf_dest)

        try:
            if pdf_type == "credit":
                csv_path = pdf_to_csv(pdf_dest)
                date_range = _date_range_from_credit_csv(csv_path, src.name)
                csv_path = _rename_with_date_range(csv_path, "credit", date_range)
                new_pdf = pdf_dest.parent / (csv_path.stem + ".pdf")
                pdf_dest.rename(new_pdf)
            else:  # debit
                csv_path = debit_pdf_to_csv(pdf_dest)
                date_range = _date_range_from_parsed_debit_csv(csv_path)
                csv_path = _rename_with_date_range(csv_path, "debit", date_range)
                new_pdf = pdf_dest.parent / (csv_path.stem + ".pdf")
                pdf_dest.rename(new_pdf)

            n = sum(1 for _ in csv_path.open()) - 1
            print(f"         → {csv_path.relative_to(PROJECT_ROOT)} ({n} transactions)")
            _schedule_enrich()
        except Exception as exc:
            print(f"         ✗ Parse failed: {exc}")
            if pdf_dest.exists():
                pdf_dest.unlink(missing_ok=True)

    def _copy(self, src_file: Path, dest_dir: Path):
        dest = dest_dir / src_file.name
        if dest.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            dest = dest_dir / f"{src_file.stem}_{stamp}{src_file.suffix}"
        shutil.copy2(src_file, dest)
        date_range = _date_range_from_debit_csv(dest)
        dest = _rename_with_date_range(dest, "debit", date_range)

        # Duplicate resolution: CSV wins over any existing PDF for same period
        if date_range:
            dupes = _find_same_period(dest_dir, date_range)
            pdf_dupes = [p for p in dupes if p.suffix == ".pdf" and p != dest]
            for pdf_dupe in pdf_dupes:
                print(
                    f"         ↷ Removing duplicate PDF (CSV preferred): {pdf_dupe.name}"
                )
                pdf_dupe.unlink(missing_ok=True)
            csv_dupes = [p for p in dupes if p.suffix == ".csv" and p != dest]
            if csv_dupes:
                print(
                    f"         ⚠ Duplicate CSV already exists: {csv_dupes[0].name} — skipping"
                )
                dest.unlink(missing_ok=True)
                return

        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Copied  {src_file.name}")
        print(f"         → {dest.relative_to(PROJECT_ROOT)}")
        _schedule_enrich()

    def _handle_zip(self, zip_path: Path):
        """Extract a ZIP and process each PDF inside."""
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] Extracting ZIP: {zip_path.name}")
        try:
            with tempfile.TemporaryDirectory() as tmp_dir:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    # Security: reject paths with directory traversal
                    for member in zf.namelist():
                        if ".." in member.split("/") or member.startswith("/"):
                            print(
                                f"         \u2717 Unsafe path in ZIP, aborting: {member}"
                            )
                            return
                    zf.extractall(tmp_dir)
                tmp = Path(tmp_dir)
                found = sorted(tmp.rglob("*.pdf"))
                if not found:
                    print("         ✗ No PDF files found in ZIP")
                    return
                for pdf_file in found:
                    dest_dir = self._dest_for(pdf_file)
                    if dest_dir is None:
                        print(f"         ? Unrecognised PDF (skipped): {pdf_file.name}")
                        continue
                    self._parse_pdf(pdf_file, dest_dir)
                # PDFs each scheduled their own enrich; one shared run will fire
        except zipfile.BadZipFile:
            print(f"         ✗ Invalid ZIP file: {zip_path.name}")
        except Exception as exc:
            print(f"         ✗ ZIP extraction failed: {exc}")


def scan_existing(watch_dir: Path) -> None:
    """Process any matching files already present in watch_dir."""
    handler = StatementHandler()
    print(f"Scanning existing files in {watch_dir}...")
    matched = 0
    for path in sorted(watch_dir.iterdir()):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix == ".zip":
            matched += 1
            handler._handle_zip(path)
        elif suffix in (".pdf", ".csv") and handler._dest_for(path) is not None:
            matched += 1
            dest_dir = handler._dest_for(path)
            if suffix == ".pdf":
                handler._parse_pdf(path, dest_dir)
            else:
                handler._copy(path, dest_dir)
    if matched == 0:
        print("  No matching files found.")
    print()


def watch(watch_dir: Path, scan_first: bool = False):
    handler = StatementHandler()

    if scan_first:
        scan_existing(watch_dir)

    observer = Observer()
    observer.schedule(handler, str(watch_dir), recursive=False)
    observer.start()

    print(f"Watching  {watch_dir}")
    print(f"  CSV  (debit export)        → data/statements/debit/  (copied)")
    print(f"  PDF  (credit Visa)         → data/statements/credit/ (parsed → CSV)")
    print(f"  PDF  (debit account)       → data/statements/debit/  (parsed → CSV)")
    print(f"  ZIP  (bundle of PDFs)      → extracted, each PDF classified + parsed")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        observer.stop()
        observer.join()
        print("\nWatchdog stopped.")


def main():
    parser = argparse.ArgumentParser(
        description="Watch Downloads for Desjardins statement files"
    )
    parser.add_argument(
        "--watch",
        type=Path,
        default=DEFAULT_WATCH_DIR,
        help=f"Folder to monitor (default: {DEFAULT_WATCH_DIR})",
    )
    parser.add_argument(
        "--scan-existing",
        action="store_true",
        help="Process matching files already present in the watch folder before watching",
    )
    parser.add_argument(
        "--scan-only",
        action="store_true",
        help="Process existing files in the watch folder and exit (no ongoing watch)",
    )
    args = parser.parse_args()

    watch_dir = args.watch.expanduser().resolve()
    if not watch_dir.is_dir():
        parser.error(f"Watch directory does not exist: {watch_dir}")

    if args.scan_only:
        scan_existing(watch_dir)
    else:
        watch(watch_dir, scan_first=args.scan_existing)


if __name__ == "__main__":
    main()
