"""
budget_ui.py — PySide6 desktop interface for BudgetTracker.

Tabs:
  1. Summary   — pie + bar charts (live-filtered), category totals table
  2. Transactions — filterable table, editable category column
  3. Statistics  — per-category deep stats + top merchants

Category edits:
  - Update enriched_transactions.csv in-place
  - Append to data/category_corrections.json for future Gemini runs

The rules and stored data the windows show live in ledger.py.

Usage:
    python -m src.budget_ui
"""

from __future__ import annotations

import difflib
import re
import time
from collections import defaultdict
from pathlib import Path

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QDateTimeAxis,
    QLineSeries,
    QPieSeries,
    QStackedBarSeries,
    QValueAxis,
)
from PySide6.QtCore import (
    QAbstractTableModel,
    QDate,
    QDateTime,
    QFileSystemWatcher,
    QModelIndex,
    QPersistentModelIndex,
    QRect,
    QSettings,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import QAction, QColor, QCursor, QDesktopServices, QFont, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStatusBar,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextBrowser,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from src import plaid_sync
from src.ledger import (
    OUTPUT_FILE,
    PHONE_FOLDER,
    PROJECT_ROOT,
    STATEMENTS_DIR,
    _effective_category,
    _ensure_emoji,
    _format_date_display,
    _gemini_client,
    _gemini_spending_analysis,
    _load_ai_cache,
    _load_official_sources,
    _load_summary_descs,
    _parse_amount,
    _reload_ai_emojis,
    _save_ai_cache,
    _save_official_sources,
    _statements_fingerprint,
    _tx_key,
    all_categories,
    apply_category_change,
    apply_phone_edits,
    compute_merchant_totals,
    compute_stats_overall,
    compute_totals,
    compute_transaction_slices,
    export_for_phone,
    income_rows,
    load_corrections,
    load_csv,
    load_trash,
    row_date,
    save_correction,
    save_csv,
    save_trash,
    save_user_note,
    spending_rows,
)


def _category_item(label: str) -> "QTableWidgetItem":
    """Return a QTableWidgetItem for a category with its emoji prefix.
    Stores the raw label in Qt.UserRole for drill-down logic.
    """
    emoji = _ensure_emoji(label) if label else ""
    display = f"{emoji}  {label}" if emoji else label
    item = QTableWidgetItem(display)
    item.setData(Qt.UserRole, label)
    return item


def _restore_column_widths(
    header: "QHeaderView", key: str, defaults: list[int]
) -> None:
    """Restore per-column widths from QSettings; fall back to *defaults*."""
    s = QSettings("BudgetTracker", "BudgetTracker")
    for col, default_w in enumerate(defaults):
        w = s.value(f"colwidths/{key}/{col}", default_w, type=int)
        header.resizeSection(col, w)


def _save_column_widths(header: "QHeaderView", key: str) -> None:
    """Persist all column widths to QSettings."""
    s = QSettings("BudgetTracker", "BudgetTracker")
    for col in range(header.count()):
        s.setValue(f"colwidths/{key}/{col}", header.sectionSize(col))


def _date_bounds(rows: list[dict]) -> tuple[QDate, QDate]:
    """Return (min_date, max_date) across all rows; falls back to sensible defaults."""
    dates = [d for r in rows if (d := _parse_date(r)) is not None]
    if dates:
        return (
            min(dates, key=lambda d: d.toJulianDay()),
            max(dates, key=lambda d: d.toJulianDay()),
        )
    return QDate(2020, 1, 1), QDate.currentDate()


def _parse_date(row: dict) -> QDate | None:
    """The row's date for Qt widgets, or None if it cannot be read."""
    day = row_date(row)
    return QDate(day.year, day.month, day.day) if day else None


def _build_tooltip(
    cat: str,
    data: dict,
    grand_total: float,
    rows: list[dict],
    matched_rows: list[dict] | None = None,
) -> str:
    """Return an HTML tooltip string for a chart slice/bar with key stats."""
    matching = (
        matched_rows
        if matched_rows is not None
        else [r for r in rows if _effective_category(r) == cat]
    )
    if not matching:
        return f"<b>{cat}</b>"
    amounts = [abs(_parse_amount(r.get("amount", 0))) for r in matching]
    avg = sum(amounts) / len(amounts)
    pct = data["abs_total"] / grand_total * 100 if grand_total else 0.0
    top5 = sorted(
        matching,
        key=lambda r: abs(_parse_amount(r.get("amount", 0))),
        reverse=True,
    )[:5]
    lines = [
        f"<b>{cat}</b>",
        f"Total: <b>${data['abs_total']:,.2f}</b> ({pct:.1f}%)",
        f"Transactions: {data['count']} &nbsp;&bull;&nbsp; Average: ${avg:,.2f}",
        "<b>Top transactions:</b>",
    ]
    for r in top5:
        amt = abs(_parse_amount(r.get("amount", 0)))
        label = (r.get("gemini_merchant") or r.get("description") or "Unknown")[:42]
        date = _format_date_display(
            (r.get("date") or "").strip(), r.get("source_file") or ""
        )
        lines.append(f"&nbsp;&nbsp;${amt:,.2f} &mdash; <i>{date} \u2014 {label}</i>")
    return "<br>".join(lines)


def _build_txn_tooltip(lbl: str, data: dict, grand_total: float) -> str:
    """Return an HTML tooltip for a single-transaction slice (level-2 drill)."""
    row = data.get("_row") or {}
    amt = data["abs_total"]
    pct = amt / grand_total * 100 if grand_total else 0.0
    date = _format_date_display(
        (row.get("date") or "").strip(), row.get("source_file") or ""
    )
    desc = (row.get("description") or "").strip()
    merchant = (row.get("gemini_merchant") or "").strip()
    place = (row.get("place_name") or "").strip()
    mode = (row.get("purchase_mode") or "").strip()
    acct = (row.get("account_type") or "").strip()
    cat = (row.get("gemini_category") or "").strip()
    lines = [f"<b>${amt:,.2f}</b> ({pct:.1f}%)"]
    if date:
        lines.append(f"Date: {date}")
    if merchant:
        lines.append(f"Merchant: <i>{merchant}</i>")
    if desc and desc != merchant:
        lines.append(f"Description: <i>{desc[:60]}</i>")
    if place:
        lines.append(f"Place: {place}")
    if mode:
        lines.append(f"Mode: {mode}")
    if acct:
        lines.append(f"Account: {acct}")
    if cat:
        lines.append(f"Category: {cat}")
    return "<br>".join(lines)


# ---------------------------------------------------------------------------
# Category delegate (editable combo in transactions table)
# ---------------------------------------------------------------------------

COL_CATEGORY = 3  # index in TransactionModel columns
COL_NOTES = 9  # user context / correction hint for Gemini


# ---------------------------------------------------------------------------
# Category Manager Dialog
# ---------------------------------------------------------------------------

_SEARCH_THRESHOLD = 0.6  # minimum score to show a row


def _fuzzy_score(query: str, blob: str) -> float:
    """Return a 0–1 word-aware similarity score between *query* and *blob*.

    Strategy per query word:
    - Short words (≤ 4 chars): must appear as a literal substring of some blob
      token.  Prevents character-overlap false positives like "pet" -> "eats"
      (both share 'e' and 't' but are unrelated words).
    - Longer words: best SequenceMatcher ratio across all blob tokens.
    The overall score is the average across all query words, so multi-word
    queries like "uber eats" must each match independently.
    Fast-path: if query is a literal substring of blob, return 1.0.
    """
    if not query:
        return 1.0
    if query in blob:
        return 1.0
    tokens = blob.split()

    def _word_score(qw: str) -> float:
        if not tokens:
            return 0.0
        if len(qw) <= 4:
            # Short words must literally appear inside a blob token
            return 1.0 if any(qw in t for t in tokens) else 0.0
        # Longer words: best SequenceMatcher ratio across all blob tokens
        return max(difflib.SequenceMatcher(None, qw, t).ratio() for t in tokens)

    query_words = query.split()
    if not query_words:
        return 0.0
    scores = [_word_score(w) for w in query_words]
    return sum(scores) / len(scores)


# ---------------------------------------------------------------------------
# Category List Editor — view / add / delete categories
# ---------------------------------------------------------------------------


class CategoryListEditor(QDialog):
    """Let the user view, add, and delete categories from the known list.

    Returns the set of deleted category names so the caller can recategorize
    only the transactions that belonged to those categories.
    """

    def __init__(self, rows: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Category List")
        self.resize(480, 600)
        self._rows = rows
        self._deleted: set[str] = set()
        self._added: set[str] = set()
        self._build_ui()

    def _current_categories(self) -> list[str]:
        """Get the current category list (custom file or hardcoded default)."""
        from src.enrich_transactions import (
            _KNOWN_CATEGORIES,
            load_custom_categories,
        )

        return load_custom_categories() or list(_KNOWN_CATEGORIES)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        layout.addWidget(
            QLabel("Categories used by Gemini for transaction categorization:")
        )

        self._list_widget = QListWidget()
        self._list_widget.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._list_widget.setSortingEnabled(True)

        # Count transactions per category
        cat_counts: dict[str, int] = defaultdict(int)
        for row in self._rows:
            cat = (row.get("gemini_category") or "Other").strip()
            cat_counts[cat] += 1

        cats = self._current_categories()
        # Also include categories in data that aren't in the list
        data_cats = set(cat_counts.keys())
        for c in data_cats:
            if c not in cats:
                cats.append(c)

        for cat in sorted(cats):
            count = cat_counts.get(cat, 0)
            emoji = _ensure_emoji(cat)
            suffix = f"({count} txns)" if count else "(unused)"
            label = f"{emoji}  {cat}  {suffix}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, cat)
            self._list_widget.addItem(item)

        layout.addWidget(self._list_widget)

        # Add category row
        add_row = QHBoxLayout()
        self._add_input = QLineEdit()
        self._add_input.setPlaceholderText("New category name…")
        self._add_input.returnPressed.connect(self._add_category)
        add_row.addWidget(self._add_input)
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self._add_category)
        add_row.addWidget(add_btn)
        layout.addLayout(add_row)

        # Delete button
        del_btn = QPushButton("Delete selected")
        del_btn.clicked.connect(self._delete_selected)
        layout.addWidget(del_btn)

        # Info label
        self._info = QLabel("")
        self._info.setStyleSheet("color: #666; font-style: italic;")
        layout.addWidget(self._info)

        # Dialog buttons
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def _add_category(self) -> None:
        name = self._add_input.text().strip()
        if not name:
            return
        # Check for duplicates
        for i in range(self._list_widget.count()):
            if self._list_widget.item(i).data(Qt.UserRole) == name:
                self._info.setText(f'"{name}" already exists.')
                return
        emoji = _ensure_emoji(name)
        item = QListWidgetItem(f"{emoji}  {name}  (new)")
        item.setData(Qt.UserRole, name)
        self._list_widget.addItem(item)
        self._added.add(name)
        self._add_input.clear()
        self._info.setText(f'Added "{name}".')

    def _delete_selected(self) -> None:
        selected = self._list_widget.selectedItems()
        if not selected:
            return
        names = [item.data(Qt.UserRole) for item in selected]
        # Count affected transactions
        cat_counts: dict[str, int] = defaultdict(int)
        for row in self._rows:
            cat = (row.get("gemini_category") or "Other").strip()
            cat_counts[cat] += 1
        affected = sum(cat_counts.get(n, 0) for n in names)

        msg = f"Delete {len(names)} categor{'y' if len(names) == 1 else 'ies'}?"
        if affected:
            msg += f"\n\n{affected} transaction(s) will be recategorized."
        reply = QMessageBox.question(
            self,
            "Confirm deletion",
            msg,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        for item in selected:
            cat_name = item.data(Qt.UserRole)
            self._deleted.add(cat_name)
            self._added.discard(cat_name)
            self._list_widget.takeItem(self._list_widget.row(item))
        self._info.setText(
            f"Deleted {len(names)} categor{'y' if len(names) == 1 else 'ies'}."
        )

    def _on_ok(self) -> None:
        # Build the final category list from what's in the list widget
        final = []
        for i in range(self._list_widget.count()):
            final.append(self._list_widget.item(i).data(Qt.UserRole))
        # Ensure "Other" is always present as fallback
        if "Other" not in final:
            final.append("Other")
        self._final_categories = sorted(final)
        self.accept()

    @property
    def deleted_categories(self) -> set[str]:
        return self._deleted

    @property
    def final_categories(self) -> list[str]:
        return getattr(self, "_final_categories", [])


class CategoryManagerDialog(QDialog):
    """Batch-edit merchant→category mappings.

    One row per unique merchant.  Use the search bar to filter, check any set
    of rows, then type a new category into the "Set selected to:" box and
    click Apply to reassign all checked merchants at once.
    """

    def __init__(self, all_rows: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manage Categories")
        self.resize(1100, 640)
        self._all_rows = all_rows
        self._pending: dict[str, str] = {}  # merchant → new category (unsaved)
        self._desc_cache = _load_summary_descs()
        self._build_ui()
        self._populate()

    # ── Build UI ─────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ── Search bar ───────────────────────────────────────────────────
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self._search = QLineEdit()
        self._search.setPlaceholderText("Filter by merchant or description…")
        self._search.textChanged.connect(self._apply_filter)
        search_row.addWidget(self._search)
        layout.addLayout(search_row)

        # ── Table: checkbox | Merchant | Example description | AI Description | Txns | Category ──
        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            [
                "",
                "Merchant",
                "Example description",
                "AI Description",
                "Txns",
                "Category",
            ]
        )
        hdr = self._table.horizontalHeader()
        for _c in range(6):
            hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        _restore_column_widths(hdr, "catmgr_table", [28, 180, 200, 220, 50, 180])
        hdr.sectionResized.connect(lambda *_: _save_column_widths(hdr, "catmgr_table"))
        self._table.verticalHeader().hide()
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSortingEnabled(True)
        layout.addWidget(self._table)

        # ── Bulk-assign bar ───────────────────────────────────────────────
        bulk_row = QHBoxLayout()
        self._select_all_btn = QPushButton("Select all visible")
        self._select_all_btn.clicked.connect(self._select_all)
        self._clear_btn = QPushButton("Clear selection")
        self._clear_btn.clicked.connect(self._clear_selection)
        bulk_row.addWidget(self._select_all_btn)
        bulk_row.addWidget(self._clear_btn)
        bulk_row.addStretch()

        bulk_row.addWidget(QLabel("Set selected to:"))
        self._bulk_cat = QComboBox()
        self._bulk_cat.setEditable(True)
        self._bulk_cat.setMinimumWidth(180)
        self._bulk_cat.addItems(all_categories(self._all_rows))
        bulk_row.addWidget(self._bulk_cat)
        apply_bulk_btn = QPushButton("Apply to selected")
        apply_bulk_btn.clicked.connect(self._apply_bulk)
        bulk_row.addWidget(apply_bulk_btn)
        layout.addLayout(bulk_row)

        # ── Dialog buttons ────────────────────────────────────────────────
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    # ── Data population ───────────────────────────────────────────────────

    def _build_merchant_data(self) -> list[tuple[str, str, int, str, str, str]]:
        """Return [(merchant, example_desc, txn_count, current_category, ai_desc, blob), …] sorted by txns desc.

        *ai_desc* is the Gemini-generated merchant description from the local cache.
        *blob* is a lowercased string used by the fuzzy search scorer.
        """
        corrections = load_corrections()
        from collections import Counter
        from collections import defaultdict as _dd

        all_merch_descs: dict[str, dict] = self._desc_cache.get("merchants", {})
        merchant_descs: dict[str, list[str]] = _dd(list)
        merchant_cats: dict[str, Counter] = _dd(Counter)
        for row in self._all_rows:
            m = (
                row.get("gemini_merchant") or row.get("description") or "Unknown"
            ).strip()
            merchant_descs[m].append(row.get("description", ""))
            cat = (row.get("gemini_category") or "Other").strip()
            merchant_cats[m][cat] += 1

        result = []
        for merchant, descs in merchant_descs.items():
            txn_count = len(descs)
            # Current category: correction file takes priority, else most common in data
            example = next((d for d in descs if d), "")
            if example in corrections:
                cat = corrections[example]
            else:
                cat = merchant_cats[merchant].most_common(1)[0][0]
            # AI description: look up across all category buckets in the cache
            ai_desc = all_merch_descs.get(cat, {}).get(merchant, "")
            if not ai_desc:  # fallback: search all category buckets
                for bucket in all_merch_descs.values():
                    if merchant in bucket:
                        ai_desc = bucket[merchant]
                        break
            # Blob: all unique raw descriptions + merchant + category + ai description
            unique_descs = " ".join(d for d in dict.fromkeys(descs) if d)
            blob = f"{merchant} {unique_descs} {cat} {ai_desc}".lower()
            result.append((merchant, example, txn_count, cat, ai_desc, blob))

        result.sort(key=lambda x: x[2], reverse=True)
        return result

    def _populate(self) -> None:
        self._merchant_data = self._build_merchant_data()
        self._table.setSortingEnabled(False)
        self._table.setRowCount(len(self._merchant_data))
        for row_i, (merchant, example, txn_count, cat, ai_desc, blob) in enumerate(
            self._merchant_data
        ):
            # Col 0 — checkbox
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
            chk.setCheckState(Qt.Unchecked)
            self._table.setItem(row_i, 0, chk)
            # Col 1 — merchant (blob stored in UserRole for fuzzy search)
            merch_item = QTableWidgetItem(merchant)
            merch_item.setData(Qt.UserRole, blob)
            self._table.setItem(row_i, 1, merch_item)
            # Col 2 — example raw description
            self._table.setItem(row_i, 2, QTableWidgetItem(example))
            # Col 3 — AI description (italic grey when missing)
            ai_item = QTableWidgetItem(ai_desc if ai_desc else "…")
            if not ai_desc:
                f = ai_item.font()
                f.setItalic(True)
                ai_item.setFont(f)
                ai_item.setForeground(QColor("#888888"))
            self._table.setItem(row_i, 3, ai_item)
            # Col 4 — txn count (_NumericItem for sort)
            cnt_item = _NumericItem("")
            cnt_item.setData(Qt.UserRole, txn_count)
            cnt_item.setData(Qt.DisplayRole, txn_count)
            self._table.setItem(row_i, 4, cnt_item)
            # Col 5 — category (editable combo via delegate)
            cat_item = _category_item(cat)
            self._table.setItem(row_i, 5, cat_item)
        # Make category column editable
        delegate = _CatEditDelegate(all_categories(self._all_rows), self)
        self._table.setItemDelegateForColumn(5, delegate)
        self._table.setSortingEnabled(True)
        self._table.sortItems(4, Qt.DescendingOrder)

    # ── Filter ─────────────────────────────────────────────────────────────

    def _apply_filter(self, text: str) -> None:
        query = text.strip().lower()
        for row_i in range(self._table.rowCount()):
            merch_item = self._table.item(row_i, 1)
            blob = merch_item.data(Qt.UserRole) if merch_item else ""
            score = _fuzzy_score(query, blob or "")
            self._table.setRowHidden(row_i, score < _SEARCH_THRESHOLD)

    # ── Bulk operations ───────────────────────────────────────────────────

    def _select_all(self) -> None:
        for row_i in range(self._table.rowCount()):
            if not self._table.isRowHidden(row_i):
                item = self._table.item(row_i, 0)
                if item:
                    item.setCheckState(Qt.Checked)

    def _clear_selection(self) -> None:
        for row_i in range(self._table.rowCount()):
            item = self._table.item(row_i, 0)
            if item:
                item.setCheckState(Qt.Unchecked)

    def _apply_bulk(self) -> None:
        new_cat = self._bulk_cat.currentText().strip()
        if not new_cat:
            return
        _ensure_emoji(new_cat)
        for row_i in range(self._table.rowCount()):
            if self._table.isRowHidden(row_i):
                continue
            chk = self._table.item(row_i, 0)
            if chk and chk.checkState() == Qt.Checked:
                self._table.setItem(row_i, 5, _category_item(new_cat))

    # ── OK / Apply ────────────────────────────────────────────────────────

    def get_changes(self) -> dict[str, str]:
        """Return {merchant: new_category} for rows whose category was changed."""
        corrections = load_corrections()
        from collections import Counter
        from collections import defaultdict as _dd

        merchant_cats: dict[str, Counter] = _dd(Counter)
        for row in self._all_rows:
            m = (
                row.get("gemini_merchant") or row.get("description") or "Unknown"
            ).strip()
            merchant_cats[m][(row.get("gemini_category") or "Other").strip()] += 1

        changes: dict[str, str] = {}
        for row_i in range(self._table.rowCount()):
            merchant_item = self._table.item(row_i, 1)
            cat_item = self._table.item(row_i, 5)
            if not merchant_item or not cat_item:
                continue
            merchant = merchant_item.text()
            # Use Qt.UserRole which always holds the raw category name (no emoji prefix)
            new_cat = (cat_item.data(Qt.UserRole) or cat_item.text()).strip()
            orig_cat = (
                merchant_cats[merchant].most_common(1)[0][0]
                if merchant_cats.get(merchant)
                else "Other"
            )
            if new_cat != orig_cat:
                changes[merchant] = new_cat
        return changes

    def _on_ok(self) -> None:
        self._pending = self.get_changes()
        self.accept()


class _CatEditDelegate(QStyledItemDelegate):
    """Editable combo for the Category column inside CategoryManagerDialog."""

    def __init__(self, categories: list[str], parent=None):
        super().__init__(parent)
        self._categories = categories

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(True)
        combo.addItems(self._categories)
        raw = index.data(Qt.UserRole) or ""
        combo.setCurrentText(raw)
        return combo

    def setEditorData(self, editor, index):
        raw = index.data(Qt.UserRole) or ""
        editor.setCurrentText(raw)

    def setModelData(self, editor, model, index):
        raw = editor.currentText().strip()
        emoji = _ensure_emoji(raw) if raw else ""
        display = f"{emoji}  {raw}" if emoji else raw
        model.setData(index, display, Qt.DisplayRole)
        model.setData(index, raw, Qt.UserRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


class CategoryDelegate(QStyledItemDelegate):
    def __init__(self, categories: list[str], parent=None):
        super().__init__(parent)
        self._categories = categories

    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        combo.setEditable(True)
        combo.addItems(self._categories)
        raw = index.data(Qt.UserRole) or index.data(Qt.DisplayRole) or ""
        combo.setCurrentText(raw)
        return combo

    def setEditorData(self, editor, index):
        raw = index.data(Qt.UserRole) or index.data(Qt.DisplayRole) or ""
        editor.setCurrentText(raw)

    def setModelData(self, editor, model, index):
        raw = editor.currentText().strip()
        _ensure_emoji(raw)
        model.setData(index, raw, Qt.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(option.rect)


# ---------------------------------------------------------------------------
# Transaction table model (MVC — fast for large datasets)
# ---------------------------------------------------------------------------

_TXN_HEADERS = [
    "Date",
    "Description",
    "Amount ($)",
    "Category",
    "Merchant",
    "Mode",
    "Place",
    "Phone",
    "Account",
    "Notes",
]
_TXN_KEYS = [
    "date",
    "description",
    "amount",
    "gemini_category",
    "gemini_merchant",
    "purchase_mode",
    "place_name",
    "place_phone",
    "account_type",
    "user_notes",
]


class TransactionModel(QAbstractTableModel):
    def __init__(self, rows: list[dict], parent=None):
        super().__init__(parent)
        self._rows = rows
        self._all_rows = rows  # kept for reset

    def rowCount(self, parent=QModelIndex()):
        return len(self._rows)

    def columnCount(self, parent=QModelIndex()):
        return len(_TXN_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return _TXN_HEADERS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role in (Qt.DisplayRole, Qt.EditRole):
            val = row.get(_TXN_KEYS[col], "")
            if col == 1:  # Description — prefer ai_description
                return row.get("ai_description") or row.get("description") or ""
            if col == 0:  # Date — apply friendly formatting
                return _format_date_display(
                    str(val or ""), row.get("source_file") or ""
                )
            if col == 2:  # Amount — format nicely
                try:
                    return f"{-float(val):+.2f}"
                except (ValueError, TypeError):
                    return val
            return val
        url = str(row.get("place_website", "") or "")
        if col == 6 and url:  # Place column — hyperlink styling
            if role == Qt.ForegroundRole:
                return QColor("#1565c0")
            if role == Qt.FontRole:
                f = QFont()
                f.setUnderline(True)
                return f
            if role == Qt.UserRole:
                return url
            if role == Qt.ToolTipRole:
                return url
        if col == COL_NOTES:
            notes = row.get("user_notes", "")
            if role == Qt.ToolTipRole and notes:
                return notes
            if role == Qt.ForegroundRole and notes:
                return QColor("#aaaaff")  # subtle blue tint when a note exists
        if role == Qt.ForegroundRole and col == 2:
            try:
                amt = float(str(row.get("amount", "0")).replace(",", "."))
                return QColor("#2e7d32") if amt < 0 else QColor("#e65100")
            except (ValueError, TypeError):
                return None
        if role == Qt.TextAlignmentRole and col == 2:
            return Qt.AlignRight | Qt.AlignVCenter
        return None

    def flags(self, index):
        base = super().flags(index)
        if index.column() in (COL_CATEGORY, COL_NOTES):
            return base | Qt.ItemIsEditable
        return base

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.EditRole and index.column() == COL_CATEGORY:
            row = self._rows[index.row()]
            # Stash previous category so the change handler can use it for bulk matching
            row["_prev_category"] = row.get("gemini_category", "")
            row["gemini_category"] = value
            self.dataChanged.emit(index, index, [Qt.EditRole])
            return True
        if role == Qt.EditRole and index.column() == COL_NOTES:
            row = self._rows[index.row()]
            row["user_notes"] = value
            self.dataChanged.emit(index, index, [Qt.EditRole])
            return True
        return False

    def get_row(self, row_index: int) -> dict:
        return self._rows[row_index]

    def set_rows(self, rows: list[dict]):
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        """Header-click sort for QTableViews using this model."""
        reverse = order == Qt.DescendingOrder
        key_name = _TXN_KEYS[column]

        def _key(row: dict):
            if column == 2:  # Amount — sort by absolute value
                try:
                    return abs(float(str(row.get(key_name, "0")).replace(",", ".")))
                except (ValueError, TypeError):
                    return 0.0
            if column == 0:  # Date — sort chronologically
                d = _parse_date(row)
                return (d.year(), d.month(), d.day()) if d else (0, 0, 0)
            return str(row.get(key_name, "") or "").lower()

        self.beginResetModel()
        self._rows.sort(key=_key, reverse=reverse)
        self.endResetModel()


# ---------------------------------------------------------------------------
# Chart helper
# ---------------------------------------------------------------------------

_CHART_COLORS = [
    "#4e79a7",
    "#f28e2b",
    "#e15759",
    "#76b7b2",
    "#59a14f",
    "#edc948",
    "#b07aa1",
    "#ff9da7",
    "#9c755f",
    "#bab0ac",
    "#86bcb6",
    "#d37295",
    "#fabfd2",
    "#8cd17d",
    "#b6992d",
]


def _color(i: int) -> QColor:
    return QColor(_CHART_COLORS[i % len(_CHART_COLORS)])


# ---------------------------------------------------------------------------
# Statement Coverage Bar
# ---------------------------------------------------------------------------

_COVERAGE_HEIGHT = 42
_COVERAGE_DEBIT_COLOR = QColor("#76b7b2")  # teal
_COVERAGE_CREDIT_COLOR = QColor("#f28e2b")  # orange
_COVERAGE_GAP_COLOR = QColor("#f8d7da")  # soft red
_COVERAGE_BG_COLOR = QColor("#f0f0f0")


def _scan_statement_periods(
    statements_dir: Path,
) -> dict[str, list[tuple[QDate, QDate]]]:
    """Scan filenames for debit/credit date ranges."""
    result: dict[str, list[tuple[QDate, QDate]]] = {"debit": [], "credit": []}
    pat = re.compile(r"(debit|credit)_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})")
    if not statements_dir.exists():
        return result
    for p in statements_dir.rglob("*.csv"):
        m = pat.search(p.stem)
        if m:
            kind = m.group(1)
            d1 = QDate.fromString(m.group(2), "yyyy-MM-dd")
            d2 = QDate.fromString(m.group(3), "yyyy-MM-dd")
            if d1.isValid() and d2.isValid():
                result[kind].append((d1, d2))
    for kind in result:
        result[kind].sort(key=lambda t: t[0].toJulianDay())
    return result


def _scan_statement_periods_full(
    statements_dir: Path,
) -> dict[str, list]:
    """Scan statement CSVs, deduplicating files that share identical date ranges.

    Returns {kind: [(d1, d2, primary_fname, primary_path, all_fnames)]} where
    all_fnames lists every CSV filename covering that exact period (e.g. duplicate
    account-number variants like _004234).  Wide-span files (>60 days) are kept
    but flagged so callers can render them separately.
    """
    raw: dict[str, dict] = {"debit": {}, "credit": {}}
    pat = re.compile(r"(debit|credit)_(\d{4}-\d{2}-\d{2})_to_(\d{4}-\d{2}-\d{2})")
    if not statements_dir.exists():
        return {"debit": [], "credit": []}
    for p in sorted(statements_dir.rglob("*.csv")):
        m = pat.search(p.stem)
        if m:
            kind = m.group(1)
            d1 = QDate.fromString(m.group(2), "yyyy-MM-dd")
            d2 = QDate.fromString(m.group(3), "yyyy-MM-dd")
            if d1.isValid() and d2.isValid():
                key = (d1.toJulianDay(), d2.toJulianDay())
                if key not in raw[kind]:
                    raw[kind][key] = [d1, d2, p.name, p, [p.name]]
                else:
                    if p.name not in raw[kind][key][4]:
                        raw[kind][key][4].append(p.name)
    result: dict[str, list] = {}
    for kind in ("debit", "credit"):
        entries = sorted(raw[kind].values(), key=lambda t: t[0].toJulianDay())
        result[kind] = entries
    return result


def _find_gaps(
    periods: list[tuple[QDate, QDate]], span_start: QDate, span_end: QDate
) -> list[tuple[QDate, QDate]]:
    """Return date ranges within [span_start, span_end] not covered by any period."""
    if not periods:
        return [(span_start, span_end)]
    merged: list[tuple[QDate, QDate]] = []
    for s, e in sorted(periods, key=lambda t: t[0].toJulianDay()):
        if merged and s <= merged[-1][1].addDays(1):
            merged[-1] = (
                merged[-1][0],
                max(merged[-1][1], e, key=lambda d: d.toJulianDay()),
            )
        else:
            merged.append((s, e))
    gaps: list[tuple[QDate, QDate]] = []
    cur = span_start
    for s, e in merged:
        if cur < s:
            gaps.append((cur, s.addDays(-1)))
        cur = e.addDays(1)
    if cur <= span_end:
        gaps.append((cur, span_end))
    return gaps


class _CoverageBar(QWidget):
    """Horizontal timeline showing debit/credit statement coverage and gaps."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(58)
        self._debit: list[tuple[QDate, QDate]] = []
        self._credit: list[tuple[QDate, QDate]] = []
        self._span_start = QDate(2020, 1, 1)
        self._span_end = QDate.currentDate()

    def update_coverage(self, span_start: QDate, span_end: QDate) -> None:
        self._span_start = span_start
        self._span_end = span_end
        periods = _scan_statement_periods(STATEMENTS_DIR)
        self._debit = periods["debit"]
        self._credit = periods["credit"]
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        W = self.width()
        margin_l, margin_r = 50, 6
        bar_w = max(W - margin_l - margin_r, 1)
        band_h = 14
        span = max(self._span_start.daysTo(self._span_end), 1)

        def x_for(date: QDate) -> int:
            days = max(0, self._span_start.daysTo(date))
            return margin_l + int(bar_w * min(days, span) / span)

        # Background
        p.fillRect(margin_l, 0, bar_w, band_h * 2 + 6, _COVERAGE_BG_COLOR)

        # Gaps (combined debit+credit) — soft red backdrop
        all_p = sorted(self._debit + self._credit, key=lambda t: t[0].toJulianDay())
        for gs, ge in _find_gaps(all_p, self._span_start, self._span_end):
            x1 = x_for(gs)
            x2 = max(x_for(ge) + 1, x1 + 3)
            p.fillRect(x1, 0, x2 - x1, band_h * 2 + 6, _COVERAGE_GAP_COLOR)

        # Debit row (top)
        for s, e in self._debit:
            x1 = x_for(s)
            x2 = max(x_for(e) + 1, x1 + 2)
            p.fillRect(x1, 1, x2 - x1, band_h, _COVERAGE_DEBIT_COLOR)

        # Credit row (bottom)
        for s, e in self._credit:
            x1 = x_for(s)
            x2 = max(x_for(e) + 1, x1 + 2)
            p.fillRect(x1, band_h + 5, x2 - x1, band_h, _COVERAGE_CREDIT_COLOR)

        # Row labels
        p.setPen(QPen(QColor("#444")))
        p.drawText(0, 1, margin_l - 4, band_h, Qt.AlignVCenter | Qt.AlignRight, "Debit")
        p.drawText(
            0,
            band_h + 5,
            margin_l - 4,
            band_h,
            Qt.AlignVCenter | Qt.AlignRight,
            "Credit",
        )

        # Axis: start / end date labels
        p.setPen(QPen(QColor("#888")))
        y_axis = band_h * 2 + 9
        p.drawText(
            margin_l,
            y_axis,
            bar_w // 2,
            14,
            Qt.AlignLeft,
            self._span_start.toString("MMM yyyy"),
        )
        p.drawText(
            margin_l,
            y_axis,
            bar_w,
            14,
            Qt.AlignRight,
            self._span_end.toString("MMM yyyy"),
        )
        p.end()


class _CoverageGanttWidget(QWidget):
    """Discrete monthly grid: rows = Credit / Debit, columns = calendar months.

    Each cell is filled when a statement covers that month; empty cells are
    gaps in coverage.  Today's month is highlighted with a coloured ring.
    Hover a cell for file details.
    """

    _CELL_W = 46
    _CELL_H = 80
    _ROW_LABEL_W = 100
    _MONTH_LBL_H = 30
    _YEAR_H = 28
    _PAD_T = 14
    _PAD_B = 14
    _ROWS = ["credit", "debit"]
    _COVERED: dict = {
        "credit": QColor("#4e79a7"),
        "debit": QColor("#59a14f"),
    }
    _EMPTY = QColor("#d5dae0")
    _GRID = QColor("#2e3b4a")
    _TODAY_RING = QColor("#d32f2f")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._months: list[tuple[int, int]] = []
        self._coverage: dict[str, set] = {k: set() for k in self._ROWS}
        self._month_data: dict[str, dict] = {k: {} for k in self._ROWS}
        # Kept so CoverageTab._update_summary() can count gaps
        self._gaps: dict[str, list] = {k: [] for k in self._ROWS}
        self._overlaps: dict[str, list] = {k: [] for k in self._ROWS}
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._update_size()

    # ── Size ──────────────────────────────────────────────────────────────

    def _update_size(self) -> None:
        n = max(len(self._months), 1)
        self.setFixedHeight(
            self._PAD_T
            + self._YEAR_H
            + self._MONTH_LBL_H
            + len(self._ROWS) * self._CELL_H
            + self._PAD_B
        )
        self.setMinimumWidth(self._ROW_LABEL_W + n * self._CELL_W + 8)

    # ── Data ──────────────────────────────────────────────────────────────

    def set_data(
        self,
        entries: dict[str, list],
        txn_counts: dict[str, int],
        rows: list[dict] | None = None,
    ) -> None:
        """entries: {kind: [(d1, d2, primary_fname, primary_path, all_fnames)]}

        If *rows* is provided the actual parsed transaction dates are used to
        determine which calendar months each file covers, so that files with
        misleading filename dates (e.g. an annual export) show only the months
        they genuinely contain.
        """
        all_months: set[tuple[int, int]] = set()
        self._coverage = {k: set() for k in self._ROWS}
        self._month_data = {k: {} for k in self._ROWS}
        self._gaps = {k: [] for k in self._ROWS}
        self._overlaps = {k: [] for k in self._ROWS}

        # Build per-file actual month sets from transaction rows
        file_months: dict[str, set] = {}
        if rows:
            from collections import defaultdict as _dd

            _fm: dict = _dd(set)
            for row in rows:
                d = _parse_date(row)
                if d and d.isValid():
                    sf = row.get("source_file", "")
                    if sf:
                        _fm[sf].add((d.year(), d.month()))
            file_months = dict(_fm)

        today_ym = (QDate.currentDate().year(), QDate.currentDate().month())

        for kind in self._ROWS:
            for item in entries.get(kind, []):
                d1, d2 = item[0], item[1]
                fname = item[2]
                all_fnames = item[4] if len(item) > 4 else [fname]
                count = sum(txn_counts.get(f, 0) for f in all_fnames)

                # Months this entry actually covers
                if file_months:
                    months = set()
                    for fn in all_fnames:
                        months |= file_months.get(fn, set())
                    if not months:
                        # File not in enriched data yet — fall back to filename
                        # dates but cap at today so future months don't appear
                        d = QDate(d1.year(), d1.month(), 1)
                        while d <= d2:
                            ym = (d.year(), d.month())
                            if ym <= today_ym:
                                months.add(ym)
                            d = d.addMonths(1)
                else:
                    months = set()
                    d = QDate(d1.year(), d1.month(), 1)
                    while d <= d2:
                        months.add((d.year(), d.month()))
                        d = d.addMonths(1)

                for ym in months:
                    all_months.add(ym)
                    self._coverage[kind].add(ym)
                    self._month_data[kind].setdefault(ym, []).append(
                        (fname, count, d1, d2)
                    )

        # Gap months: within each kind's span, months with no coverage
        for kind in self._ROWS:
            covered = self._coverage.get(kind, set())
            if covered:
                ym_min, ym_max = min(covered), max(covered)
                d = QDate(ym_min[0], ym_min[1], 1)
                end = QDate(ym_max[0], ym_max[1], 1)
                while d <= end:
                    ym = (d.year(), d.month())
                    if ym not in covered:
                        self._gaps[kind].append((d, d))
                    d = d.addMonths(1)

        today = QDate.currentDate()
        all_months.add((today.year(), today.month()))
        # Fill the full min→max range so no months are skipped
        if all_months:
            ym_min = min(all_months)
            ym_max = max(all_months)
            d = QDate(ym_min[0], ym_min[1], 1)
            end = QDate(ym_max[0], ym_max[1], 1)
            while d <= end:
                all_months.add((d.year(), d.month()))
                d = d.addMonths(1)
        self._months = sorted(all_months)
        # Add 2 trailing empty months for visual padding
        if self._months:
            last_d = QDate(self._months[-1][0], self._months[-1][1], 1)
            for _ in range(2):
                last_d = last_d.addMonths(1)
                self._months.append((last_d.year(), last_d.month()))
        self._update_size()
        self.update()

    # ── Coordinate helper ─────────────────────────────────────────────────

    def _cell_rect(self, ri: int, mi: int) -> QRect:
        x = self._ROW_LABEL_W + mi * self._CELL_W
        y = self._PAD_T + self._YEAR_H + self._MONTH_LBL_H + ri * self._CELL_H
        return QRect(x + 1, y + 1, self._CELL_W - 2, self._CELL_H - 2)

    # ── Painting ──────────────────────────────────────────────────────────

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), QColor("#8a9bb0"))

        if not self._months:
            p.setPen(QColor("#888"))
            p.drawText(self.rect(), Qt.AlignCenter, "No statement files found.")
            p.end()
            return

        today_ym = (QDate.currentDate().year(), QDate.currentDate().month())

        # Year labels — skip a year if its December doesn't appear on the timeline
        _months_set = set(self._months)
        fnt_yr = QFont()
        fnt_yr.setPointSize(20)
        fnt_yr.setBold(True)
        p.setFont(fnt_yr)
        p.setPen(QColor("#0d1b2a"))
        prev_year = None
        for mi, (y, m) in enumerate(self._months):
            if y != prev_year:
                # Only draw label if December of this year is present
                if (y, 12) in _months_set:
                    x = self._ROW_LABEL_W + mi * self._CELL_W
                    p.drawText(
                        x,
                        self._PAD_T,
                        60,
                        self._YEAR_H,
                        Qt.AlignVCenter | Qt.AlignLeft,
                        str(y),
                    )
                prev_year = y

        # Month labels
        fnt_m = QFont()
        fnt_m.setPointSize(16)
        p.setFont(fnt_m)
        p.setPen(QColor("#1a2a3a"))
        lbl_y = self._PAD_T + self._YEAR_H
        for mi, (y, m) in enumerate(self._months):
            x = self._ROW_LABEL_W + mi * self._CELL_W
            p.drawText(
                x,
                lbl_y,
                self._CELL_W,
                self._MONTH_LBL_H,
                Qt.AlignCenter,
                QDate(y, m, 1).toString("MMM"),
            )

        # Row labels
        fnt_row = QFont()
        fnt_row.setPointSize(20)
        fnt_row.setBold(True)
        p.setFont(fnt_row)
        for ri, kind in enumerate(self._ROWS):
            cell_y = self._PAD_T + self._YEAR_H + self._MONTH_LBL_H + ri * self._CELL_H
            p.setPen(self._COVERED[kind])
            p.drawText(
                2,
                cell_y,
                self._ROW_LABEL_W - 6,
                self._CELL_H,
                Qt.AlignVCenter | Qt.AlignRight,
                kind.capitalize(),
            )

        # Cells
        pen_grid = QPen(self._GRID, 1)
        pen_today = QPen(self._TODAY_RING, 2)
        pen_year = QPen(QColor("#0d1b2a"), 2, Qt.DotLine)  # dotted year-boundary line
        cells_top = self._PAD_T + self._YEAR_H + self._MONTH_LBL_H
        cells_bottom = cells_top + len(self._ROWS) * self._CELL_H
        for ri, kind in enumerate(self._ROWS):
            covered = self._coverage.get(kind, set())
            for mi, ym in enumerate(self._months):
                rect = self._cell_rect(ri, mi)
                fill = (
                    self._COVERED[kind].lighter(165) if ym in covered else self._EMPTY
                )
                p.fillRect(rect, fill)
                p.setPen(pen_grid)
                p.setBrush(Qt.NoBrush)
                p.drawRect(rect)
                if ym == today_ym:
                    p.setPen(pen_today)
                    p.drawRect(rect.adjusted(1, 1, -1, -1))

        # Year-boundary vertical lines (drawn on top of cells)
        p.setPen(pen_year)
        prev_year = None
        for mi, (y, m) in enumerate(self._months):
            if y != prev_year and mi > 0:
                lx = self._ROW_LABEL_W + mi * self._CELL_W
                p.drawLine(lx, cells_top - self._MONTH_LBL_H, lx, cells_bottom)
            prev_year = y

        p.end()

    # ── Mouse events ──────────────────────────────────────────────────────

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._months:
            QToolTip.hideText()
            return
        pos = event.pos()
        mi = (pos.x() - self._ROW_LABEL_W) // self._CELL_W
        ri = (
            pos.y() - (self._PAD_T + self._YEAR_H + self._MONTH_LBL_H)
        ) // self._CELL_H
        if 0 <= mi < len(self._months) and 0 <= ri < len(self._ROWS):
            ym = self._months[mi]
            kind = self._ROWS[ri]
            month_str = QDate(ym[0], ym[1], 1).toString("MMMM yyyy")
            data = self._month_data.get(kind, {}).get(ym, [])
            if data:
                lines = [f"<b>{kind.capitalize()} — {month_str}</b>"]
                for fname, count, d1, d2 in data:
                    lines.append(
                        f"{fname}<br>"
                        f"{d1.toString('MMM d')} – {d2.toString('MMM d, yyyy')} "
                        f"· <b>{count}</b> txn"
                    )
                QToolTip.showText(event.globalPos(), "<br>".join(lines), self)
            else:
                QToolTip.showText(
                    event.globalPos(),
                    f"<b>{kind.capitalize()} — {month_str}</b><br>No statement data",
                    self,
                )
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:  # noqa: N802
        QToolTip.hideText()


class _MonthlyHeatmapWidget(QWidget):
    """Calendar grid: rows = (year × account type), columns = months.

    Colours:
      light pink  — no statement file covers that month
      light blue  — statement exists, 0 transactions recorded
      green ramp  — transaction count, low (pale) to high (deep green)
      grey        — future month

    Hover a cell for detailed counts and covering file names.
    """

    _CELL_W = 82
    _CELL_H = 42
    _SUBLABEL_W = 66
    _YEAR_W = 52
    _HEADER_H = 30
    _MONTH_NAMES = [
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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._heat: dict = {}
        self._covered: dict = {}
        self._files_for: dict = {}
        self._years: list[int] = []
        self._max_count: dict[str, int] = {"credit": 1, "debit": 1}
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)

    def set_data(self, entries: dict[str, list], rows: list[dict]) -> None:
        from collections import defaultdict as _dd

        heat: dict = _dd(int)
        files_for: dict = _dd(list)
        for row in rows:
            d = _parse_date(row)
            if d and d.isValid():
                kind = row.get("account_type", "").lower()
                sf = row.get("source_file", "")
                if kind in ("credit", "debit"):
                    heat[(d.year(), d.month(), kind)] += 1
                    if sf and sf not in files_for[(d.year(), d.month(), kind)]:
                        files_for[(d.year(), d.month(), kind)].append(sf)
        self._heat = heat
        self._files_for = files_for

        covered: dict = {}
        for key, fnames in files_for.items():
            if fnames:
                covered[key] = True
        self._covered = covered

        all_keys = list(heat.keys()) + list(covered.keys())
        if all_keys:
            year_set = {y for y, _, _ in all_keys}
            # Fill the full min→max year range so no years are skipped
            self._years = list(range(min(year_set), max(year_set) + 1))
        else:
            self._years = [QDate.currentDate().year()]
        for kind in ("credit", "debit"):
            vals = [v for (y, m, k), v in heat.items() if k == kind and v > 0]
            self._max_count[kind] = max(vals, default=1)

        self.setFixedHeight(self._HEADER_H + len(self._years) * 2 * self._CELL_H + 6)
        self.setMinimumWidth(self._YEAR_W + self._SUBLABEL_W + 12 * self._CELL_W)
        self.update()

    def _cell_color(self, year: int, month: int, kind: str) -> QColor:
        today = QDate.currentDate()
        if QDate(year, month, 1) > QDate(today.year(), today.month(), 1):
            return QColor("#dde1e7")  # future — greyed out
        if not self._covered.get((year, month, kind), False):
            return QColor("#f0a8a8")  # no data — muted red on grey bg
        count = self._heat.get((year, month, kind), 0)
        if count == 0:
            return QColor("#b8d4ea")  # covered but no transactions
        t = min(count / max(self._max_count.get(kind, 1), 1), 1.0)
        return QColor(int(200 - t * 150), int(230 - t * 95), int(200 - t * 165))

    def paintEvent(self, event) -> None:  # noqa: N802
        from PySide6.QtGui import QPen

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        p.fillRect(self.rect(), QColor("#8a9bb0"))

        # Month header row
        fnt_h = QFont()
        fnt_h.setPointSize(9)
        fnt_h.setBold(True)
        p.setFont(fnt_h)
        for mi, mn in enumerate(self._MONTH_NAMES):
            x = self._YEAR_W + self._SUBLABEL_W + mi * self._CELL_W
            p.fillRect(x, 0, self._CELL_W, self._HEADER_H, QColor("#dde1e7"))
            p.setPen(QColor("#0d1b2a"))
            p.drawText(x, 0, self._CELL_W, self._HEADER_H, Qt.AlignCenter, mn)
            p.setPen(QPen(QColor("#2e3b4a"), 1))
            p.drawRect(x, 0, self._CELL_W - 1, self._HEADER_H - 1)

        fnt_c = QFont()
        fnt_c.setPointSize(10)
        today = QDate.currentDate()

        for yi, year in enumerate(self._years):
            for ki, kind in enumerate(["debit", "credit"]):
                ri = yi * 2 + ki
                y_pos = self._HEADER_H + ri * self._CELL_H

                # Year label (spans both sub-rows)
                if ki == 0:
                    p.fillRect(
                        0, y_pos, self._YEAR_W, self._CELL_H * 2, QColor("#dde1e7")
                    )
                    fnt_y = QFont()
                    fnt_y.setPointSize(10)
                    fnt_y.setBold(True)
                    p.setFont(fnt_y)
                    p.setPen(QColor("#0d1b2a"))
                    p.drawText(
                        0,
                        y_pos,
                        self._YEAR_W,
                        self._CELL_H * 2,
                        Qt.AlignCenter,
                        str(year),
                    )
                    p.setPen(QPen(QColor("#2e3b4a"), 1))
                    p.drawRect(0, y_pos, self._YEAR_W - 1, self._CELL_H * 2 - 1)

                # Sub-label
                sub_bg = QColor("#cfe8d0") if kind == "debit" else QColor("#c9dff0")
                sub_fg = QColor("#1b5e20") if kind == "debit" else QColor("#0d3c61")
                p.fillRect(self._YEAR_W, y_pos, self._SUBLABEL_W, self._CELL_H, sub_bg)
                p.setPen(sub_fg)
                fnt_s = QFont()
                fnt_s.setPointSize(9)
                fnt_s.setBold(True)
                p.setFont(fnt_s)
                p.drawText(
                    self._YEAR_W,
                    y_pos,
                    self._SUBLABEL_W,
                    self._CELL_H,
                    Qt.AlignCenter,
                    kind[:1].upper() + kind[1:],
                )
                p.setPen(QPen(QColor("#2e3b4a"), 1))
                p.drawRect(self._YEAR_W, y_pos, self._SUBLABEL_W - 1, self._CELL_H - 1)

                # Month cells
                p.setFont(fnt_c)
                for month in range(1, 13):
                    x = self._YEAR_W + self._SUBLABEL_W + (month - 1) * self._CELL_W
                    p.fillRect(
                        x,
                        y_pos,
                        self._CELL_W,
                        self._CELL_H,
                        self._cell_color(year, month, kind),
                    )
                    count = self._heat.get((year, month, kind), 0)
                    is_future = QDate(year, month, 1) > QDate(
                        today.year(), today.month(), 1
                    )
                    if count > 0:
                        p.setPen(QColor("#1a1a1a"))
                        p.drawText(
                            x,
                            y_pos,
                            self._CELL_W,
                            self._CELL_H,
                            Qt.AlignCenter,
                            str(count),
                        )
                    elif (
                        not self._covered.get((year, month, kind), False)
                        and not is_future
                    ):
                        p.setPen(QColor("#e57373"))
                        p.drawText(
                            x, y_pos, self._CELL_W, self._CELL_H, Qt.AlignCenter, "—"
                        )
                    p.setPen(QPen(QColor("#2e3b4a"), 1))
                    p.drawRect(x, y_pos, self._CELL_W - 1, self._CELL_H - 1)

        p.end()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        mx, my = event.pos().x(), event.pos().y()
        if my < self._HEADER_H:
            QToolTip.hideText()
            return
        ri = (my - self._HEADER_H) // self._CELL_H
        col = (mx - self._YEAR_W - self._SUBLABEL_W) // self._CELL_W
        if not (0 <= col < 12):
            QToolTip.hideText()
            return
        yi, ki = ri // 2, ri % 2
        if not (0 <= yi < len(self._years)):
            QToolTip.hideText()
            return
        year, month = self._years[yi], col + 1
        kind = ["debit", "credit"][ki]
        count = self._heat.get((year, month, kind), 0)
        covered = self._covered.get((year, month, kind), False)
        files = self._files_for.get((year, month, kind), [])
        today = QDate.currentDate()
        if QDate(year, month, 1) > QDate(today.year(), today.month(), 1):
            status = "Future period"
        elif covered and count > 0:
            status = "✓ Statement coverage + transactions"
        elif covered:
            status = "Statement file present, no transactions recorded"
        else:
            status = "⚠ No statement file covers this month"
        files_html = (
            "<br>".join(f"&nbsp;• {f}" for f in files) if files else "&nbsp;(none)"
        )
        QToolTip.showText(
            event.globalPos(),
            f"<b>{kind.capitalize()} — "
            f"{QDate(year, month, 1).toString('MMMM yyyy')}</b><br>"
            f"Status: {status}<br>"
            f"Transactions: <b>{count}</b><br>"
            f"Files:<br>{files_html}",
            self,
        )

    def leaveEvent(self, event) -> None:  # noqa: N802
        QToolTip.hideText()

    def sizeHint(self) -> QSize:
        w = self._YEAR_W + self._SUBLABEL_W + 12 * self._CELL_W + 20
        h = self._HEADER_H + max(len(self._years), 1) * 2 * self._CELL_H + 6
        return QSize(w, h)


# ---------------------------------------------------------------------------
# Background Gemini worker + helper callables
# ---------------------------------------------------------------------------


class _GeminiWorker(QThread):
    """Run a blocking callable in a background thread and emit the result."""

    result_ready = Signal(str)
    error = Signal(str)

    def __init__(self, func, parent=None):
        super().__init__(parent)
        self._func = func

    def run(self):
        try:
            self.result_ready.emit(self._func())
        except Exception as exc:  # noqa: BLE001
            self.error.emit(str(exc))


class _EnrichWorker(QThread):
    """Run the enrichment pipeline in a background thread with progress signals."""

    progress = Signal(str, str)  # (phase, detail)
    finished_ok = Signal()
    finished_err = Signal(str)

    def __init__(self, statements_dir, parent=None):
        super().__init__(parent)
        self._statements_dir = statements_dir

    def run(self):
        from src.enrich_transactions import enrich

        try:
            enrich(
                statements_dir=self._statements_dir,
                progress_cb=lambda phase, detail: self.progress.emit(phase, detail),
            )
            self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001
            print(f"[BudgetTracker] Enrichment failed: {exc}")
            self.finished_err.emit(str(exc))


class _PlaidWorker(QThread):
    """Optionally link a bank through Plaid, then fetch its transactions."""

    link_ready = Signal(str)  # URL of the bank login page
    finished_ok = Signal(int, bool)  # (new transactions, full history loaded)
    cancelled = Signal()
    finished_err = Signal(str)

    def __init__(self, link: bool, parent=None):
        super().__init__(parent)
        self.link = link

    def run(self):
        try:
            if self.link:
                token, url = plaid_sync.create_link_session()
                self.link_ready.emit(url)
                linked = plaid_sync.wait_for_link(token, self.isInterruptionRequested)
                if linked is None:
                    self.cancelled.emit()
                    return
                plaid_sync.add_item(*linked)
            result = plaid_sync.sync(self.isInterruptionRequested)
            self.finished_ok.emit(result.new_rows, result.complete)
        except Exception as exc:  # noqa: BLE001
            print(f"[BudgetTracker] Bank sync failed: {exc}")
            self.finished_err.emit(str(exc))


class _RecatWorker(QThread):
    """Recategorize a specific set of descriptions in a background thread."""

    progress = Signal(str, str)
    finished_ok = Signal()
    finished_err = Signal(str)

    def __init__(self, descriptions: list[str], parent=None):
        super().__init__(parent)
        self._descriptions = descriptions

    def run(self):
        from src.enrich_transactions import recategorize_descriptions

        try:
            recategorize_descriptions(
                self._descriptions,
                progress_cb=lambda phase, detail: self.progress.emit(phase, detail),
            )
            self.finished_ok.emit()
        except Exception as exc:  # noqa: BLE001
            print(f"[BudgetTracker] Recategorization failed: {exc}")
            self.finished_err.emit(str(exc))


def _gemini_coverage_summary() -> str:
    """Ask Gemini for a short phrase describing the statement file coverage."""
    if not _gemini_client:
        return ""
    periods = _scan_statement_periods(STATEMENTS_DIR)
    all_dates = [
        d.toString("MMM yyyy")
        for kind in ("debit", "credit")
        for start, end in periods[kind]
        for d in (start, end)
    ]
    if not all_dates:
        return ""
    debit_ranges = ", ".join(
        f"{s.toString('MMM yyyy')}–{e.toString('MMM yyyy')}"
        for s, e in periods["debit"]
    )
    credit_ranges = ", ".join(
        f"{s.toString('MMM yyyy')}–{e.toString('MMM yyyy')}"
        for s, e in periods["credit"]
    )
    prompt = (
        "Given this bank statement coverage:\n"
        f"  Debit statements: {debit_ranges or 'none'}\n"
        f"  Credit statements: {credit_ranges or 'none'}\n"
        "Write a single short phrase (max 12 words) describing the coverage "
        "period and completeness (e.g. 'Sep 2025 – Mar 2026 · full history'). "
        "No quotes, no period at end."
    )
    try:
        resp = _gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        return (resp.text or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Chart Tooltip
# ---------------------------------------------------------------------------


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts numerically via Qt.UserRole (a float)."""

    def __lt__(self, other: QTableWidgetItem) -> bool:  # type: ignore[override]
        try:
            return float(self.data(Qt.UserRole)) < float(other.data(Qt.UserRole))
        except (TypeError, ValueError):
            return super().__lt__(other)


def _make_amount_item(total: float, abs_total: float) -> _NumericItem:
    """Return a right-aligned, colour-coded amount cell.

    Expenses (total >= 0 in CSV convention) are dark orange with a minus sign;
    income (total < 0) is green with a plus sign.
    """
    is_expense = total >= 0
    display_val = abs(total)
    text = f"-${display_val:,.2f}" if is_expense else f"+${display_val:,.2f}"
    item = _NumericItem(text)
    item.setData(Qt.UserRole, display_val)  # numeric sort key
    item.setForeground(QColor("#e65100") if is_expense else QColor("#2e7d32"))
    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return item


class _ChartTooltip(QLabel):
    """Persistent floating tooltip for charts.

    Unlike QToolTip, this widget stays visible until explicitly hidden —
    the OS system timeout does not apply.
    """

    def __init__(self):
        super().__init__(
            None, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setWordWrap(True)
        self.setContentsMargins(10, 7, 10, 7)
        self.setStyleSheet(
            "QLabel {"
            "  background-color: #ffffee;"
            "  color: #111111;"
            "  border: 1px solid #aaaaaa;"
            "  border-radius: 5px;"
            "  font-size: 12px;"
            "}"
        )
        self.hide()

    def show_at(self, pos, html: str) -> None:
        self.setText(html)
        self.adjustSize()
        # Offset from cursor so the tooltip doesn't sit under the pointer
        self.move(pos.x() + 18, pos.y() + 18)
        self.show()
        self.raise_()


# ---------------------------------------------------------------------------
# Summary Tab
# ---------------------------------------------------------------------------


class SummaryTab(QWidget):
    def __init__(self, all_rows: list[dict], parent=None):
        super().__init__(parent)
        self._all_rows = all_rows
        _s = QSettings("BudgetTracker", "BudgetTracker")
        _saved = _s.value("summary/hidden_cats", [])
        self._hidden_cats: set[str] = set(_saved) if _saved else set()
        self._drill_cat: str | None = None
        self._drill_merchant: str | None = None
        self._table_items: list[tuple[str, dict]] = []
        self._table_level: int = 0
        self._chart_tooltip = _ChartTooltip()
        self._coverage_worker: _GeminiWorker | None = None
        self._build_ui()
        self._init_date_range()
        self.refresh()
        self._start_coverage_worker()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Filter bar ────────────────────────────────────────────────────
        filter_frame = QWidget()
        fl = QHBoxLayout(filter_frame)
        fl.setContentsMargins(0, 0, 0, 0)

        fl.addWidget(QLabel("Account:"))
        self._acct_combo = QComboBox()
        self._acct_combo.addItems(["All", "credit", "debit"])
        self._acct_combo.currentIndexChanged.connect(self.refresh)
        fl.addWidget(self._acct_combo)

        self._back_btn = QPushButton("\u2190 Back to all categories")
        self._back_btn.clicked.connect(self._back_to_top)
        self._back_btn.hide()
        fl.addWidget(self._back_btn)

        self._hide_btn = QPushButton("Hide categories\u2026")
        self._hide_btn.clicked.connect(self._open_hide_dialog)
        fl.addWidget(self._hide_btn)

        fl.addStretch()

        # ── Mutually exclusive filter modes ───────────────────────────────
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)

        # 1) Date Range
        self._radio_range = QRadioButton("Range")
        self._radio_range.setChecked(True)
        self._filter_group.addButton(self._radio_range, 0)
        fl.addWidget(self._radio_range)
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDisplayFormat("MMM d, yyyy")
        self._date_from.setDate(QDate(2020, 1, 1))
        self._date_from.dateChanged.connect(self._on_range_changed)
        fl.addWidget(self._date_from)
        fl.addWidget(QLabel("\u2013"))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDisplayFormat("MMM d, yyyy")
        self._date_to.setDate(QDate.currentDate())
        self._date_to.dateChanged.connect(self._on_range_changed)
        fl.addWidget(self._date_to)

        fl.addSpacing(16)

        # 2) Year
        self._radio_year = QRadioButton("Year")
        self._filter_group.addButton(self._radio_year, 1)
        fl.addWidget(self._radio_year)
        self._year_combo = QComboBox()
        self._year_combo.setMinimumWidth(80)
        self._year_combo.currentIndexChanged.connect(self._on_year_changed)
        fl.addWidget(self._year_combo)

        fl.addSpacing(16)

        # 3) Month
        self._radio_month = QRadioButton("Month")
        self._filter_group.addButton(self._radio_month, 2)
        fl.addWidget(self._radio_month)
        self._month_combo = QComboBox()
        self._month_combo.setMinimumWidth(110)
        self._month_combo.currentIndexChanged.connect(self._on_month_changed)
        fl.addWidget(self._month_combo)

        self._radio_range.toggled.connect(lambda on: self._sync_filter_controls())
        self._radio_year.toggled.connect(lambda on: self._sync_filter_controls())
        self._radio_month.toggled.connect(lambda on: self._sync_filter_controls())

        fl.addSpacing(16)
        self._count_label = QLabel()
        fl.addWidget(self._count_label)
        layout.addWidget(filter_frame)

        # ── Charts: vertical splitter — top (table | pie) / bottom (bar full-width) ─────
        self._v_splitter = QSplitter(Qt.Vertical)
        self._v_splitter.setChildrenCollapsible(False)

        # Top row: horizontal splitter — table (1/3) | pie (2/3)
        _top_widget = QWidget()
        _top_layout = QHBoxLayout(_top_widget)
        _top_layout.setContentsMargins(0, 0, 0, 0)
        _top_layout.setSpacing(0)
        _top_splitter = QSplitter(Qt.Horizontal)
        _top_splitter.setChildrenCollapsible(False)
        self._table = self._make_table("summary_pie_table")
        self._pie_view = QChartView()
        self._pie_view.setRenderHint(QPainter.Antialiasing)
        _top_splitter.addWidget(self._table)
        _top_splitter.addWidget(self._pie_view)
        _top_splitter.setStretchFactor(0, 1)
        _top_splitter.setStretchFactor(1, 2)
        _top_layout.addWidget(_top_splitter)

        # Bottom: bar chart full-width (scrollable)
        self._bar_view = QChartView()
        self._bar_view.setRenderHint(QPainter.Antialiasing)
        self._bar_scroll = QScrollArea()
        self._bar_scroll.setWidget(self._bar_view)
        self._bar_scroll.setWidgetResizable(False)
        self._bar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._bar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        self._bar_scroll.setFixedHeight(220)

        self._v_splitter.addWidget(_top_widget)
        self._v_splitter.addWidget(self._bar_scroll)
        self._v_splitter.setStretchFactor(0, 1)
        self._v_splitter.setStretchFactor(1, 0)  # bar stays at fixed height

        layout.addWidget(self._v_splitter, stretch=1)

    def reset_to_root(self) -> None:
        """Reset drill-down to the root category view."""
        if self._drill_cat is not None or self._drill_merchant is not None:
            self._chart_tooltip.hide()
            self._drill_cat = None
            self._drill_merchant = None
            self.refresh()

    def _make_table(self, settings_key: str) -> QTableWidget:
        t = QTableWidget(0, 4)
        t.setHorizontalHeaderLabels(["Category", "Count", "Total ($)", "%"])
        hdr = t.horizontalHeader()
        for _c in range(4):
            hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        hdr.setStretchLastSection(False)
        _restore_column_widths(hdr, settings_key, [200, 70, 100, 60])
        hdr.sectionResized.connect(lambda *_: _save_column_widths(hdr, settings_key))
        t.setEditTriggers(QAbstractItemView.NoEditTriggers)
        t.setAlternatingRowColors(True)
        t.setSortingEnabled(True)
        t.verticalHeader().hide()
        t.cellClicked.connect(self._on_table_clicked)
        return t

    def _sync_filter_controls(self) -> None:
        """Enable/disable filter controls based on active radio.
        When switching to Range mode, always reset to the global data bounds."""
        is_range = self._radio_range.isChecked()
        is_year = self._radio_year.isChecked()
        is_month = self._radio_month.isChecked()
        if is_range:
            lo, hi = _date_bounds(self._all_rows)
            self._date_from.blockSignals(True)
            self._date_to.blockSignals(True)
            self._date_from.setDate(lo)
            self._date_to.setDate(hi)
            self._date_from.blockSignals(False)
            self._date_to.blockSignals(False)
        self._date_from.setEnabled(is_range)
        self._date_to.setEnabled(is_range)
        self._year_combo.setEnabled(is_year)
        self._month_combo.setEnabled(is_month)
        self.refresh()

    def _on_range_changed(self) -> None:
        if self._radio_range.isChecked():
            self.refresh()

    def _on_year_changed(self) -> None:
        if self._radio_year.isChecked():
            self.refresh()

    def _on_month_changed(self) -> None:
        if self._radio_month.isChecked():
            self.refresh()

    def _filtered_rows(self) -> list[dict]:
        acct = self._acct_combo.currentText()
        year_sel = (
            self._year_combo.currentText() if self._radio_year.isChecked() else ""
        )
        month_sel = (
            self._month_combo.currentText() if self._radio_month.isChecked() else ""
        )
        d_from = self._date_from.date() if self._radio_range.isChecked() else None
        d_to = self._date_to.date() if self._radio_range.isChecked() else None
        result = []
        for row in spending_rows(self._all_rows):
            if acct != "All" and row.get("account_type") != acct:
                continue
            if year_sel or month_sel or d_from is not None:
                d = _parse_date(row)
                if d is None:
                    continue
                if year_sel and d.toString("yyyy") != year_sel:
                    continue
                if month_sel and d.toString("yyyy-MM") != month_sel:
                    continue
                if d_from is not None and (d < d_from or d > d_to):
                    continue
            result.append(row)
        return result

    def refresh(self):
        rows = self._filtered_rows()

        # ── Determine display data: level 0 / 1 / 2 ────────────────────
        if self._drill_cat is not None and self._drill_merchant is not None:
            # Level 2 — individual transactions for one merchant within a category
            cat_rows = [r for r in rows if _effective_category(r) == self._drill_cat]
            merchant_rows = [
                r
                for r in cat_rows
                if (r.get("gemini_merchant") or r.get("description") or "").strip()
                == self._drill_merchant
            ]
            totals_list = list(compute_transaction_slices(merchant_rows).items())
            grand = sum(v["abs_total"] for _, v in totals_list) or 1.0
            chart_title = f"{self._drill_cat} / {self._drill_merchant} — Transactions"
            tooltip_source = merchant_rows
            in_drill = True
            level = 2
            self._count_label.setText(
                f"{len(merchant_rows)} transactions — {self._drill_merchant}"
            )
            self._back_btn.setText(f"← {self._drill_cat} merchants")
            self._back_btn.show()
            self._hide_btn.setVisible(False)
        elif self._drill_cat is not None:
            # Level 1 — merchants within a category
            cat_rows = [r for r in rows if _effective_category(r) == self._drill_cat]
            totals_list = list(compute_merchant_totals(cat_rows).items())
            grand = sum(v["abs_total"] for _, v in totals_list) or 1.0
            chart_title = f"{self._drill_cat} — by Merchant"
            tooltip_source = cat_rows
            in_drill = True
            level = 1
            self._count_label.setText(
                f"{len(cat_rows)} transactions in «{self._drill_cat}»"
            )
            self._back_btn.setText("← All categories")
            self._back_btn.show()
            self._hide_btn.setVisible(False)
        else:
            # Level 0 — all categories
            cat_rows = []
            totals_all = compute_totals(rows)
            # Stable color index: based on full sort order (before hiding)
            _cat_color_idx = {cat: i for i, cat in enumerate(totals_all.keys())}
            totals = {k: v for k, v in totals_all.items() if k not in self._hidden_cats}
            totals_list = list(totals.items())
            grand = sum(v["abs_total"] for v in totals.values()) or 1.0
            chart_title = "Expenses"
            tooltip_source = rows
            in_drill = False
            level = 0
            self._count_label.setText(f"{len(rows)} transactions")
            self._back_btn.hide()
            self._hide_btn.setVisible(True)

            # ── Build descriptive filter subtitle ─────────────────────────
            if self._radio_range.isChecked():
                d_from = self._date_from.date()
                d_to = self._date_to.date()
                filter_subtitle = (
                    f"{d_from.toString('MMM d, yyyy')} – {d_to.toString('MMM d, yyyy')}"
                )
            elif self._radio_year.isChecked():
                filter_subtitle = self._year_combo.currentText()
            elif self._radio_month.isChecked():
                raw = self._month_combo.currentText()  # "yyyy-MM"
                try:
                    parts = raw.split("-")
                    from calendar import month_name

                    filter_subtitle = f"{month_name[int(parts[1])]} {parts[0]}"
                except Exception:
                    filter_subtitle = raw
            else:
                filter_subtitle = ""
            if filter_subtitle:
                chart_title = f"Expenses\n{filter_subtitle}"

        tt = self._chart_tooltip

        # ── Pie chart ─────────────────────────────────────────────────────
        # _cat_color_idx maps each category to a stable color index (level 0 only).
        # At levels 1/2 (drill-downs) no hiding is active so use positional index.
        if "_cat_color_idx" not in dir():
            _cat_color_idx = {}
        pie_series = QPieSeries()
        slice_map: dict[int, tuple[str, dict]] = {}
        for i, (label, data) in enumerate(totals_list):
            colour_i = _cat_color_idx.get(label, i)
            slc = pie_series.append(label, data["abs_total"])
            slc.setBrush(_color(colour_i))
            frac = data["abs_total"] / grand
            if frac >= 0.05:
                slc.setLabel(f"{label}\n${data['abs_total']:,.0f}")
                slc.setLabelVisible(True)
            else:
                slc.setLabel(label)
            slice_map[id(slc)] = (label, data)

        def _on_pie_hover(
            slc,
            state,
            sm=slice_map,
            r=tooltip_source,
            g=grand,
            tooltip=tt,
            lv=level,
        ):
            if state:
                entry = sm.get(id(slc))
                if entry:
                    lbl, d = entry
                    if lv == 2:
                        tooltip.show_at(QCursor.pos(), _build_txn_tooltip(lbl, d, g))
                    elif lv == 1:
                        matched = [
                            row
                            for row in r
                            if (
                                row.get("gemini_merchant")
                                or row.get("description")
                                or ""
                            ).strip()
                            == lbl
                        ]
                        tooltip.show_at(
                            QCursor.pos(),
                            _build_tooltip(lbl, d, g, r, matched_rows=matched),
                        )
                    else:
                        tooltip.show_at(QCursor.pos(), _build_tooltip(lbl, d, g, r))
            else:
                tooltip.hide()

        pie_series.hovered.connect(_on_pie_hover)

        if level == 2:

            def _on_pie_click(slc, sm=slice_map):
                entry = sm.get(id(slc))
                if entry:
                    row = entry[1].get("_row")
                    if row:
                        self._show_transaction_detail(row)

            pie_series.clicked.connect(_on_pie_click)

        elif level == 1:

            def _on_pie_click(slc, sm=slice_map):  # noqa: F811
                entry = sm.get(id(slc))
                if entry:
                    self._drill_into_merchant(entry[0])

            pie_series.clicked.connect(_on_pie_click)

        else:

            def _on_pie_click(slc, sm=slice_map):  # noqa: F811
                entry = sm.get(id(slc))
                if entry:
                    self._drill_into(entry[0])

            pie_series.clicked.connect(_on_pie_click)

        pie_chart = QChart()
        pie_chart.addSeries(pie_series)
        pie_chart.setTitle(chart_title)
        pie_chart.legend().setAlignment(Qt.AlignRight)
        pie_chart.setAnimationOptions(QChart.SeriesAnimations)
        # Append total amount to each legend entry
        for i, marker in enumerate(pie_chart.legend().markers(pie_series)):
            if i < len(totals_list):
                lbl, d = totals_list[i]
                marker.setLabel(f"{lbl}  ${d['abs_total']:,.0f}")
        self._pie_view.setChart(pie_chart)

        # ── Bar chart ─────────────────────────────────────────────────────
        # One QBarSet per category: each set only has a non-zero value at its
        # own position so QStackedBarSeries renders full-width single-coloured
        # bars that match the pie-slice colours exactly.
        top_items = totals_list  # show all categories
        n = len(top_items)
        _BAR_W = 110  # px per bar — wider for label breathing room
        chart_w = max(600, n * _BAR_W + 90)
        chart_h = self._bar_scroll.viewport().height() or 200
        self._bar_view.setFixedSize(chart_w, chart_h)
        bar_series = QStackedBarSeries()
        bar_series.setLabelsVisible(True)
        bar_series.setLabelsPosition(QStackedBarSeries.LabelsOutsideEnd)
        for i, (label, data) in enumerate(top_items):
            bs = QBarSet(label)
            for j in range(n):
                bs.append(data["abs_total"] if j == i else 0.0)
            bs.setColor(_color(_cat_color_idx.get(label, i)))
            bs.setLabelColor(QColor("#000000"))
            bs.setLabel(f"${data['abs_total']:,.0f}")
            bar_series.append(bs)

        cats_axis = QBarCategoryAxis()
        cats_axis.append([c for c, _ in top_items])
        cats_axis.setLabelsAngle(-40)

        val_axis = QValueAxis()
        val_axis.setLabelFormat("%.0f$")
        if top_items:
            max_val = max(d["abs_total"] for _, d in top_items)
            val_axis.setRange(0, max_val * 1.15)  # 15% headroom for labels above bars

        bar_chart = QChart()
        bar_chart.addSeries(bar_series)
        bar_chart.addAxis(cats_axis, Qt.AlignBottom)
        bar_chart.addAxis(val_axis, Qt.AlignLeft)
        bar_series.attachAxis(cats_axis)
        bar_series.attachAxis(val_axis)
        bar_chart.setTitle(chart_title)
        bar_chart.legend().hide()
        bar_chart.setAnimationOptions(QChart.SeriesAnimations)

        def _on_bar_hover(
            status,
            index,
            barset,
            ti=top_items,
            r=tooltip_source,
            g=grand,
            tooltip=tt,
            lv=level,
        ):
            if status and 0 <= index < len(ti):
                lbl, d = ti[index]
                if lv == 2:
                    tooltip.show_at(QCursor.pos(), _build_txn_tooltip(lbl, d, g))
                elif lv == 1:
                    matched = [
                        row
                        for row in r
                        if (
                            row.get("gemini_merchant") or row.get("description") or ""
                        ).strip()
                        == lbl
                    ]
                    tooltip.show_at(
                        QCursor.pos(),
                        _build_tooltip(lbl, d, g, r, matched_rows=matched),
                    )
                else:
                    tooltip.show_at(QCursor.pos(), _build_tooltip(lbl, d, g, r))
            else:
                tooltip.hide()

        bar_series.hovered.connect(_on_bar_hover)

        if level == 2:

            def _on_bar_click(index, barset, ti=top_items):
                if 0 <= index < len(ti):
                    row = ti[index][1].get("_row")
                    if row:
                        self._show_transaction_detail(row)

            bar_series.clicked.connect(_on_bar_click)

        elif level == 1:

            def _on_bar_click(index, barset, ti=top_items):  # noqa: F811
                if 0 <= index < len(ti):
                    self._drill_into_merchant(ti[index][0])

            bar_series.clicked.connect(_on_bar_click)

        else:

            def _on_bar_click(index, barset, ti=top_items):  # noqa: F811
                if 0 <= index < len(ti):
                    self._drill_into(ti[index][0])

            bar_series.clicked.connect(_on_bar_click)

        self._bar_view.setChart(bar_chart)
        # keep bar_view width in sync after chart is assigned
        self._bar_view.setFixedSize(chart_w, chart_h)

        # ── Table ─────────────────────────────────────────────────────────
        self._table.setHorizontalHeaderItem(
            0,
            QTableWidgetItem(
                "Transaction"
                if level == 2
                else ("Merchant" if level == 1 else "Category")
            ),
        )
        col0_label = (
            "Transaction" if level == 2 else ("Merchant" if level == 1 else "Category")
        )
        for tbl in (self._table,):
            tbl.setHorizontalHeaderItem(0, QTableWidgetItem(col0_label))
            tbl.setSortingEnabled(False)
            tbl.setRowCount(len(totals_list))
            for row_i, (label, data) in enumerate(totals_list):
                pct = data["abs_total"] / grand * 100
                if level == 0:
                    label_item = _category_item(label)
                    label_item.setData(Qt.UserRole, (label, data))
                else:
                    label_item = QTableWidgetItem(label)
                    label_item.setData(
                        Qt.UserRole, (label, data)
                    )  # survives sort reorder
                tbl.setItem(row_i, 0, label_item)
                cnt_item = _NumericItem("")
                cnt_item.setData(Qt.UserRole, data["count"])
                cnt_item.setData(Qt.DisplayRole, data["count"])
                tbl.setItem(row_i, 1, cnt_item)
                amt_item = _make_amount_item(data["total"], data["abs_total"])
                tbl.setItem(row_i, 2, amt_item)
                pct_item = _NumericItem("")
                pct_item.setData(Qt.UserRole, round(pct, 1))
                pct_item.setData(Qt.DisplayRole, round(pct, 1))
                tbl.setItem(row_i, 3, pct_item)
            tbl.setSortingEnabled(True)
            tbl.sortItems(2, Qt.DescendingOrder)  # largest abs_total first
        self._table_level = level
        if not in_drill:
            self._update_hide_btn()

    def _drill_into(self, cat: str) -> None:
        """Switch charts to merchant breakdown for the given category."""
        self._chart_tooltip.hide()
        self._drill_cat = cat
        self._drill_merchant = None
        self.refresh()

    def _drill_into_merchant(self, merchant: str) -> None:
        """Switch charts to transaction breakdown for the given merchant."""
        self._chart_tooltip.hide()
        self._drill_merchant = merchant
        self.refresh()

    def _on_table_clicked(self, row_i: int, col: int) -> None:
        """Mirror pie/bar chart click behaviour for the summary table rows."""
        src = self.sender() or self._table
        item = src.item(row_i, 0)
        if item is None:
            return
        entry = item.data(Qt.UserRole)
        if not entry:
            return
        label, data = entry
        if self._table_level == 2:
            row = data.get("_row")
            if row:
                self._show_transaction_detail(row)
        elif self._table_level == 1:
            self._drill_into_merchant(label)
        else:
            self._drill_into(label)

    def _back_to_top(self) -> None:
        """Step up one drill level (merchant → category, or category → all)."""
        self._chart_tooltip.hide()
        if self._drill_merchant is not None:
            self._drill_merchant = None
        else:
            self._drill_cat = None
        self.refresh()

    def _show_transaction_detail(self, row: dict) -> None:
        """Show a dialog with full details for a single transaction."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Transaction Detail")
        dlg.resize(440, 300)
        layout = QVBoxLayout(dlg)
        grid = QGridLayout()
        fields = [
            (
                "Date",
                _format_date_display(
                    (row.get("date") or "").strip(), row.get("source_file") or ""
                ),
            ),
            ("Description", (row.get("description") or "").strip()),
            ("Amount", f"${abs(_parse_amount(row.get('amount', 0))):,.2f}"),
            ("Category", (row.get("gemini_category") or "").strip()),
            ("Merchant", (row.get("gemini_merchant") or "").strip()),
            ("Account", (row.get("account_type") or "").strip()),
            ("Mode", (row.get("purchase_mode") or "").strip()),
            ("Place", (row.get("place_name") or "").strip()),
        ]
        for i, (key, val) in enumerate(fields):
            key_lbl = QLabel(f"<b>{key}:</b>")
            val_lbl = QLabel(val)
            val_lbl.setWordWrap(True)
            grid.addWidget(key_lbl, i, 0)
            grid.addWidget(val_lbl, i, 1)
        layout.addLayout(grid)
        btns = QDialogButtonBox(QDialogButtonBox.Ok)
        btns.accepted.connect(dlg.accept)
        layout.addWidget(btns)
        dlg.exec()

    def _update_hide_btn(self):
        n = len(self._hidden_cats)
        self._hide_btn.setText(
            f"Hide categories ({n} hidden)" if n else "Hide categories…"
        )

    def _open_hide_dialog(self):
        # Gather all categories sorted by spend descending; hidden-only cats appended at end
        totals = compute_totals(self._filtered_rows())
        visible_cats = sorted(
            totals.keys(), key=lambda c: totals[c]["abs_total"], reverse=True
        )
        hidden_only = sorted(self._hidden_cats - set(totals.keys()))
        all_cats = visible_cats + hidden_only
        dlg = QDialog(self)
        dlg.setWindowTitle("Hide Categories from Charts")
        dlg.resize(320, 420)
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Checked categories are <b>hidden</b> from charts:"))
        lst = QListWidget()
        for cat in all_cats:
            item = QListWidgetItem(cat)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if cat in self._hidden_cats else Qt.Unchecked)
            lst.addItem(item)
        layout.addWidget(lst)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        if dlg.exec() == QDialog.Accepted:
            self._hidden_cats = {
                lst.item(i).text()
                for i in range(lst.count())
                if lst.item(i).checkState() == Qt.Checked
            }
            _s = QSettings("BudgetTracker", "BudgetTracker")
            _s.setValue("summary/hidden_cats", list(self._hidden_cats))
            self.refresh()

    def _init_date_range(self) -> None:
        """Set date pickers to actual data range; populate year and month combos."""
        lo, hi = _date_bounds(self._all_rows)
        self._date_from.blockSignals(True)
        self._date_to.blockSignals(True)
        self._date_from.setDate(lo)
        self._date_to.setDate(hi)
        self._date_from.blockSignals(False)
        self._date_to.blockSignals(False)

        dates = [
            _parse_date(r)
            for r in spending_rows(self._all_rows)
            if _parse_date(r) is not None
        ]

        years: list[str] = sorted({d.toString("yyyy") for d in dates}, reverse=True)
        prev_year = self._year_combo.currentText()
        self._year_combo.blockSignals(True)
        self._year_combo.clear()
        for y in years:
            self._year_combo.addItem(y)
        idx = self._year_combo.findText(prev_year)
        self._year_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._year_combo.blockSignals(False)

        months: list[str] = sorted({d.toString("yyyy-MM") for d in dates}, reverse=True)
        prev_month = self._month_combo.currentText()
        self._month_combo.blockSignals(True)
        self._month_combo.clear()
        for m in months:
            self._month_combo.addItem(m)
        idx = self._month_combo.findText(prev_month)
        self._month_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._month_combo.blockSignals(False)

        self._sync_filter_controls()

    def _start_coverage_worker(self) -> None:
        pass  # coverage label removed

    def _on_coverage_ready(self, text: str) -> None:
        pass

    def update_data(self, rows: list[dict]):
        self._all_rows = rows
        self._init_date_range()
        self.refresh()


# ---------------------------------------------------------------------------
# Transactions Tab
# ---------------------------------------------------------------------------


class TransactionsTab(QWidget):
    trash_requested = Signal(list)  # emits list of row dicts to trash

    def __init__(self, all_rows: list[dict], parent=None):
        super().__init__(parent)
        self._all_rows = all_rows
        self._drill_cat: str | None = None  # None = category table; "" = no filter
        self._drill_merchant: str | None = None  # None = merchant table; "" = no filter
        self._build_ui()
        self._init_date_range()

    def reset_to_root(self) -> None:
        """Reset drill-down state to the root category table."""
        if self._drill_cat is not None or self._drill_merchant is not None:
            self._drill_cat = None
            self._drill_merchant = None
            self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Filter bar ────────────────────────────────────────────────────
        filter_frame = QWidget()
        fl = QHBoxLayout(filter_frame)
        fl.setContentsMargins(0, 0, 0, 0)

        fl.addWidget(QLabel("Account:"))
        self._acct_combo = QComboBox()
        self._acct_combo.addItems(["All", "credit", "debit"])
        self._acct_combo.currentIndexChanged.connect(self.refresh)
        fl.addWidget(self._acct_combo)

        fl.addSpacing(8)

        self._back_btn = QPushButton("← Back")
        self._back_btn.setVisible(False)
        self._back_btn.clicked.connect(self._back)
        fl.addWidget(self._back_btn)

        view_all_btn = QPushButton("View all transactions")
        view_all_btn.clicked.connect(self._view_all_transactions)
        fl.addWidget(view_all_btn)

        self._breadcrumb = QLabel("")
        self._breadcrumb.setStyleSheet(
            "color: #cccccc; font-weight: bold; font-size: 13px;"
            " background: #2a2a2a; border-radius: 4px; padding: 2px 10px;"
        )
        self._breadcrumb.setVisible(False)
        fl.addWidget(self._breadcrumb)

        fl.addStretch()

        # 1) Range
        self._filter_group = QButtonGroup(self)
        self._radio_range = QRadioButton("Range")
        self._radio_range.setChecked(True)
        self._filter_group.addButton(self._radio_range, 0)
        fl.addWidget(self._radio_range)
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDisplayFormat("MMM d, yyyy")
        self._date_from.setDate(QDate(2020, 1, 1))
        self._date_from.dateChanged.connect(self._on_range_changed)
        fl.addWidget(self._date_from)
        fl.addWidget(QLabel("\u2013"))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDisplayFormat("MMM d, yyyy")
        self._date_to.setDate(QDate.currentDate())
        self._date_to.dateChanged.connect(self._on_range_changed)
        fl.addWidget(self._date_to)

        fl.addSpacing(16)

        # 2) Year
        self._radio_year = QRadioButton("Year")
        self._filter_group.addButton(self._radio_year, 1)
        fl.addWidget(self._radio_year)
        self._year_combo = QComboBox()
        self._year_combo.setMinimumWidth(80)
        self._year_combo.currentIndexChanged.connect(self._on_year_changed)
        fl.addWidget(self._year_combo)

        fl.addSpacing(16)

        # 3) Month
        self._radio_month = QRadioButton("Month")
        self._filter_group.addButton(self._radio_month, 2)
        fl.addWidget(self._radio_month)
        self._month_combo = QComboBox()
        self._month_combo.setMinimumWidth(110)
        self._month_combo.currentIndexChanged.connect(self._on_month_changed)
        fl.addWidget(self._month_combo)

        self._radio_range.toggled.connect(lambda _: self._sync_filter_controls())
        self._radio_year.toggled.connect(lambda _: self._sync_filter_controls())
        self._radio_month.toggled.connect(lambda _: self._sync_filter_controls())

        # Search — hidden at levels 0 & 1, shown at level 2
        self._search_widget = QWidget()
        sw_layout = QHBoxLayout(self._search_widget)
        sw_layout.setContentsMargins(8, 0, 0, 0)
        sw_layout.addWidget(QLabel("Search:"))
        self._search_edit = QLineEdit()
        self._search_edit.setPlaceholderText("Filter description…")
        self._search_edit.textChanged.connect(self.refresh)
        sw_layout.addWidget(self._search_edit, stretch=1)
        self._search_widget.setVisible(False)
        fl.addWidget(self._search_widget)

        self._count_label = QLabel()
        fl.addWidget(self._count_label)
        layout.addWidget(filter_frame)

        # ── Stacked area ─────────────────────────────────────────────────
        self._stacked = QStackedWidget()

        # Page 0: category / merchant summary table
        summary_page = QWidget()
        sp_layout = QVBoxLayout(summary_page)
        sp_layout.setContentsMargins(0, 0, 0, 0)
        self._summary_table = QTableWidget(0, 5)
        self._summary_table.setHorizontalHeaderLabels(
            ["Category", "Description", "Total", "Count", "%"]
        )
        _txn_sum_hdr = self._summary_table.horizontalHeader()
        for _c in range(5):
            _txn_sum_hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        _txn_sum_hdr.setStretchLastSection(False)
        _restore_column_widths(
            _txn_sum_hdr, "txn_summary_table", [180, 260, 90, 60, 60]
        )
        _txn_sum_hdr.sectionResized.connect(
            lambda *_: _save_column_widths(_txn_sum_hdr, "txn_summary_table")
        )
        self._summary_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._summary_table.setAlternatingRowColors(True)
        self._summary_table.setSortingEnabled(True)
        self._summary_table.verticalHeader().hide()
        self._summary_table.cellClicked.connect(self._on_summary_row_clicked)
        self._summary_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._summary_table.customContextMenuRequested.connect(
            self._on_summary_context_menu
        )
        sp_layout.addWidget(self._summary_table)
        self._stacked.addWidget(summary_page)

        # Page 1: full transaction table
        txn_page = QWidget()
        tp_layout = QVBoxLayout(txn_page)
        tp_layout.setContentsMargins(0, 0, 0, 0)
        self._model = TransactionModel([])
        self._view = QTableView()
        self._view.setModel(self._model)
        self._view.setAlternatingRowColors(True)
        self._view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._view.setSortingEnabled(True)
        _txn_hdr = self._view.horizontalHeader()
        for _c in range(10):
            _txn_hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        _txn_hdr.setStretchLastSection(False)
        _restore_column_widths(
            _txn_hdr, "txn_view", [90, 220, 90, 130, 130, 80, 160, 110, 70, 200]
        )
        _txn_hdr.sectionResized.connect(
            lambda *_: _save_column_widths(_txn_hdr, "txn_view")
        )
        self._view.verticalHeader().hide()
        self._delegate = CategoryDelegate(all_categories(self._all_rows))
        self._view.setItemDelegateForColumn(COL_CATEGORY, self._delegate)
        self._model.dataChanged.connect(self._on_category_changed)
        self._view.clicked.connect(self._on_cell_clicked)
        self._view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_txn_context_menu)
        tp_layout.addWidget(self._view)
        self._stacked.addWidget(txn_page)

        layout.addWidget(self._stacked)

    # ── Date-range helpers (mirrored from SummaryTab) ─────────────────────

    def _init_date_range(self) -> None:
        """Populate date pickers and year/month combos from the data, then refresh."""
        lo, hi = _date_bounds(self._all_rows)
        self._date_from.blockSignals(True)
        self._date_to.blockSignals(True)
        self._date_from.setDate(lo)
        self._date_to.setDate(hi)
        self._date_from.blockSignals(False)
        self._date_to.blockSignals(False)

        dates = [
            _parse_date(r)
            for r in spending_rows(self._all_rows)
            if _parse_date(r) is not None
        ]

        years: list[str] = sorted({d.toString("yyyy") for d in dates}, reverse=True)
        prev_year = self._year_combo.currentText()
        self._year_combo.blockSignals(True)
        self._year_combo.clear()
        for y in years:
            self._year_combo.addItem(y)
        idx = self._year_combo.findText(prev_year)
        self._year_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._year_combo.blockSignals(False)

        months: list[str] = sorted({d.toString("yyyy-MM") for d in dates}, reverse=True)
        prev_month = self._month_combo.currentText()
        self._month_combo.blockSignals(True)
        self._month_combo.clear()
        for m in months:
            self._month_combo.addItem(m)
        idx = self._month_combo.findText(prev_month)
        self._month_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self._month_combo.blockSignals(False)

        self._sync_filter_controls()

    def _sync_filter_controls(self) -> None:
        is_range = self._radio_range.isChecked()
        is_year = self._radio_year.isChecked()
        is_month = self._radio_month.isChecked()
        if is_range:
            lo, hi = _date_bounds(self._all_rows)
            self._date_from.blockSignals(True)
            self._date_to.blockSignals(True)
            self._date_from.setDate(lo)
            self._date_to.setDate(hi)
            self._date_from.blockSignals(False)
            self._date_to.blockSignals(False)
        self._date_from.setEnabled(is_range)
        self._date_to.setEnabled(is_range)
        self._year_combo.setEnabled(is_year)
        self._month_combo.setEnabled(is_month)
        self.refresh()

    def _on_range_changed(self) -> None:
        if self._radio_range.isChecked():
            self.refresh()

    def _on_year_changed(self) -> None:
        if self._radio_year.isChecked():
            self.refresh()

    def _on_month_changed(self) -> None:
        if self._radio_month.isChecked():
            self.refresh()

    def _time_filtered_rows(self) -> list[dict]:
        """Apply account + time filter only — no category/merchant/search."""
        acct = self._acct_combo.currentText()
        year_sel = (
            self._year_combo.currentText() if self._radio_year.isChecked() else ""
        )
        month_sel = (
            self._month_combo.currentText() if self._radio_month.isChecked() else ""
        )
        d_from = self._date_from.date() if self._radio_range.isChecked() else None
        d_to = self._date_to.date() if self._radio_range.isChecked() else None
        result = []
        for row in spending_rows(self._all_rows):
            if acct != "All" and row.get("account_type") != acct:
                continue
            if year_sel or month_sel or d_from is not None:
                d = _parse_date(row)
                if d is None:
                    continue
                if year_sel and d.toString("yyyy") != year_sel:
                    continue
                if month_sel and d.toString("yyyy-MM") != month_sel:
                    continue
                if d_from is not None and (d < d_from or d > d_to):
                    continue
            result.append(row)
        return result

    def refresh(self) -> None:
        rows = self._time_filtered_rows()

        if self._drill_cat is None:
            # ── Level 0: category overview ────────────────────────────────
            totals = compute_totals(rows)
            totals_list = list(totals.items())
            grand = sum(d["abs_total"] for _, d in totals_list) or 1.0
            # Load category descriptions from enrichment-generated summary file
            _summary_descs = _load_summary_descs()
            cat_descs = _summary_descs.get("categories", {})
            self._summary_table.setHorizontalHeaderItem(0, QTableWidgetItem("Category"))
            self._summary_table.setSortingEnabled(False)
            self._summary_table.setRowCount(len(totals_list) + 1)
            # ── "All" summary row (index 0) ──────────────────────────────────
            _grand_signed = sum(d["total"] for _, d in totals_list)
            _grand_count = sum(d["count"] for _, d in totals_list)
            _bf = QFont()
            _bf.setBold(True)
            _all_lbl = QTableWidgetItem("All")
            _all_lbl.setData(Qt.UserRole, "")
            _all_lbl.setFont(_bf)
            _all_lbl.setFlags(_all_lbl.flags() & ~Qt.ItemIsEditable)
            self._summary_table.setItem(0, 0, _all_lbl)
            self._summary_table.setItem(0, 1, QTableWidgetItem(""))
            _all_amt = self._make_amount_item(_grand_signed, grand)
            _all_amt.setFont(_bf)
            self._summary_table.setItem(0, 2, _all_amt)
            _all_cnt = _NumericItem("")
            _all_cnt.setData(Qt.UserRole, _grand_count)
            _all_cnt.setData(Qt.DisplayRole, _grand_count)
            _all_cnt.setFont(_bf)
            self._summary_table.setItem(0, 3, _all_cnt)
            _all_pct = _NumericItem("")
            _all_pct.setData(Qt.UserRole, 100.0)
            _all_pct.setData(Qt.DisplayRole, 100.0)
            _all_pct.setFont(_bf)
            self._summary_table.setItem(0, 4, _all_pct)
            # ── Data rows ───────────────────────────────────────────────────
            for row_i, (label, data) in enumerate(totals_list, start=1):
                pct = data["abs_total"] / grand * 100
                label_item = _category_item(label)
                self._summary_table.setItem(row_i, 0, label_item)
                desc_text = cat_descs.get(label, "")
                desc_item = QTableWidgetItem(desc_text if desc_text else "…")
                if not desc_text:
                    f = desc_item.font()
                    f.setItalic(True)
                    desc_item.setFont(f)
                    desc_item.setForeground(QColor("#888888"))
                self._summary_table.setItem(row_i, 1, desc_item)
                amt_item = self._make_amount_item(data["total"], data["abs_total"])
                self._summary_table.setItem(row_i, 2, amt_item)
                cnt_item = _NumericItem("")
                cnt_item.setData(Qt.UserRole, data["count"])
                cnt_item.setData(Qt.DisplayRole, data["count"])
                self._summary_table.setItem(row_i, 3, cnt_item)
                pct_item = _NumericItem("")
                pct_item.setData(Qt.UserRole, round(pct, 1))
                pct_item.setData(Qt.DisplayRole, round(pct, 1))
                self._summary_table.setItem(row_i, 4, pct_item)
            self._summary_table.setSortingEnabled(True)
            self._summary_table.sortItems(2, Qt.DescendingOrder)
            self._count_label.setText(f"{len(totals_list)} categories")
            self._back_btn.setVisible(False)
            self._breadcrumb.setVisible(False)
            self._search_widget.setVisible(False)
            self._stacked.setCurrentIndex(0)
        elif self._drill_merchant is None:
            # ── Level 1: merchant overview for selected category ───────────
            cat_rows = [
                r
                for r in rows
                if self._drill_cat == "" or _effective_category(r) == self._drill_cat
            ]
            totals = compute_merchant_totals(cat_rows)
            totals_list = list(totals.items())
            grand = sum(d["abs_total"] for _, d in totals_list) or 1.0
            # Load merchant descriptions from enrichment-generated summary file
            _summary_descs = _load_summary_descs()
            _all_merch_buckets = _summary_descs.get("merchants", {})
            merch_descs = _all_merch_buckets.get(self._drill_cat or "", {})
            self._summary_table.setHorizontalHeaderItem(0, QTableWidgetItem("Merchant"))
            self._summary_table.setSortingEnabled(False)
            self._summary_table.setRowCount(len(totals_list) + 1)
            # ── "All" summary row (index 0) ──────────────────────────────────
            _grand_signed = sum(d["total"] for _, d in totals_list)
            _grand_count = sum(d["count"] for _, d in totals_list)
            _bf = QFont()
            _bf.setBold(True)
            _all_lbl = QTableWidgetItem("All")
            _all_lbl.setData(Qt.UserRole, "")
            _all_lbl.setFont(_bf)
            _all_lbl.setFlags(_all_lbl.flags() & ~Qt.ItemIsEditable)
            self._summary_table.setItem(0, 0, _all_lbl)
            self._summary_table.setItem(0, 1, QTableWidgetItem(""))
            _all_amt = self._make_amount_item(_grand_signed, grand)
            _all_amt.setFont(_bf)
            self._summary_table.setItem(0, 2, _all_amt)
            _all_cnt = _NumericItem("")
            _all_cnt.setData(Qt.UserRole, _grand_count)
            _all_cnt.setData(Qt.DisplayRole, _grand_count)
            _all_cnt.setFont(_bf)
            self._summary_table.setItem(0, 3, _all_cnt)
            _all_pct = _NumericItem("")
            _all_pct.setData(Qt.UserRole, 100.0)
            _all_pct.setData(Qt.DisplayRole, 100.0)
            _all_pct.setFont(_bf)
            self._summary_table.setItem(0, 4, _all_pct)
            # ── Data rows ───────────────────────────────────────────────────
            for row_i, (label, data) in enumerate(totals_list, start=1):
                pct = data["abs_total"] / grand * 100
                label_item = QTableWidgetItem(label)
                label_item.setData(Qt.UserRole, label)
                self._summary_table.setItem(row_i, 0, label_item)
                desc_text = merch_descs.get(label, "")
                if not desc_text:  # fallback: search all category buckets
                    for _bucket in _all_merch_buckets.values():
                        if label in _bucket:
                            desc_text = _bucket[label]
                            break
                desc_item = QTableWidgetItem(desc_text if desc_text else "…")
                if not desc_text:
                    f = desc_item.font()
                    f.setItalic(True)
                    desc_item.setFont(f)
                    desc_item.setForeground(QColor("#888888"))
                self._summary_table.setItem(row_i, 1, desc_item)
                amt_item = self._make_amount_item(data["total"], data["abs_total"])
                self._summary_table.setItem(row_i, 2, amt_item)
                cnt_item = _NumericItem("")
                cnt_item.setData(Qt.UserRole, data["count"])
                cnt_item.setData(Qt.DisplayRole, data["count"])
                self._summary_table.setItem(row_i, 3, cnt_item)
                pct_item = _NumericItem("")
                pct_item.setData(Qt.UserRole, round(pct, 1))
                pct_item.setData(Qt.DisplayRole, round(pct, 1))
                self._summary_table.setItem(row_i, 4, pct_item)
            self._summary_table.setSortingEnabled(True)
            self._summary_table.sortItems(2, Qt.DescendingOrder)
            self._count_label.setText(f"{len(totals_list)} merchants")
            self._back_btn.setText("\u2190 Back to categories")
            self._back_btn.setVisible(True)
            self._breadcrumb.setText(self._drill_cat or "All")
            self._breadcrumb.setVisible(True)
            self._search_widget.setVisible(False)
            self._stacked.setCurrentIndex(0)
        else:
            # ── Level 2: individual transactions ──────────────────────────
            cat_rows = [
                r
                for r in rows
                if self._drill_cat == "" or _effective_category(r) == self._drill_cat
            ]
            merchant_rows = [
                r
                for r in cat_rows
                if self._drill_merchant == ""
                or (r.get("gemini_merchant") or r.get("description") or "").strip()
                == self._drill_merchant
            ]
            search = self._search_edit.text().lower()
            if search:
                merchant_rows = [
                    r
                    for r in merchant_rows
                    if search in (r.get("description") or "").lower()
                ]
            merchant_rows.sort(
                key=lambda r: abs(float(r.get("amount", 0) or 0)), reverse=True
            )
            self._model.set_rows(merchant_rows)
            self._view.horizontalHeader().setSortIndicator(2, Qt.DescendingOrder)
            self._count_label.setText(f"{len(merchant_rows)} transactions")
            self._back_btn.setText("\u2190 Back to merchants")
            self._back_btn.setVisible(True)
            cat_part = self._drill_cat or "All"
            merch_part = self._drill_merchant or "All"
            self._breadcrumb.setText(f"{cat_part}  \u203a  {merch_part}")
            self._breadcrumb.setVisible(True)
            self._search_widget.setVisible(True)
            self._stacked.setCurrentIndex(1)

    @staticmethod
    def _make_amount_item(total: float, abs_total: float) -> _NumericItem:
        """Delegate to the module-level helper for backwards compatibility."""
        return _make_amount_item(total, abs_total)

    def _on_summary_row_clicked(self, row_i: int, col: int) -> None:
        item = self._summary_table.item(row_i, 0)
        if item is None:
            return
        label = item.data(Qt.UserRole)
        if label is None:
            return
        if self._drill_cat is None:
            self._drill_cat = label
            self._drill_merchant = None
        else:
            self._drill_merchant = label
        self.refresh()

    def _back(self) -> None:
        if self._drill_merchant is not None:
            self._drill_merchant = None
            # "View all" case: skip merchant level on the way back
            if self._drill_cat == "":
                self._drill_cat = None
        else:
            self._drill_cat = None
        self.refresh()

    def _view_all_transactions(self) -> None:
        self._drill_cat = ""
        self._drill_merchant = ""
        self.refresh()

    def _on_cell_clicked(self, index):
        col = index.column()
        if col == 6:  # Place column — open maps URL
            url = self._model.data(index, Qt.UserRole)
            if url:
                QDesktopServices.openUrl(QUrl(url))
        elif self._drill_merchant is not None and col not in (
            COL_CATEGORY,
            COL_NOTES,
            6,
        ):
            # Leaf level — show detail popup (skip editable / URL columns)
            row = self._model.get_row(index.row())
            if row:
                self._show_transaction_detail(row)

    def _show_transaction_detail(self, row: dict) -> None:
        """Pop-up with full fields for a single transaction."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Transaction Detail")
        dlg.setMinimumWidth(460)
        layout = QVBoxLayout(dlg)
        layout.setSpacing(6)
        grid = QGridLayout()
        grid.setColumnMinimumWidth(0, 110)
        grid.setColumnStretch(1, 1)
        amt = _parse_amount(row.get("amount", 0))
        amt_str = f"${abs(amt):,.2f}"
        color = "#2e7d32" if amt < 0 else "#e65100"
        fields = [
            (
                "Date",
                _format_date_display(
                    (row.get("date") or "").strip(), row.get("source_file") or ""
                ),
            ),
            ("Description", (row.get("description") or "").strip()),
            ("Amount", amt_str, color),
            ("Category", (row.get("gemini_category") or "").strip()),
            ("Merchant", (row.get("gemini_merchant") or "").strip()),
            ("Mode", (row.get("purchase_mode") or "").strip()),
            ("Account", (row.get("account_type") or "").strip()),
            ("Place", (row.get("place_name") or "").strip()),
            ("Notes", (row.get("user_notes") or "").strip()),
        ]
        for i, field in enumerate(fields):
            key, val = field[0], field[1]
            fg = field[2] if len(field) > 2 else None
            if not val:
                continue
            key_lbl = QLabel(f"<b>{key}:</b>")
            key_lbl.setAlignment(Qt.AlignRight | Qt.AlignTop)
            val_lbl = QLabel(val)
            val_lbl.setWordWrap(True)
            if fg:
                val_lbl.setStyleSheet(f"color: {fg}; font-weight: bold;")
            grid.addWidget(key_lbl, i, 0)
            grid.addWidget(val_lbl, i, 1)
        layout.addLayout(grid)
        btns = QDialogButtonBox(QDialogButtonBox.Close)
        btns.rejected.connect(dlg.reject)
        layout.addWidget(btns)
        dlg.exec()

    def _on_txn_context_menu(self, pos) -> None:
        """Context menu on the individual-transactions view (level 2 only)."""
        if self._drill_merchant is None:
            return  # not at level 2 — no context menu
        index = self._view.indexAt(pos)
        if not index.isValid():
            return
        # Collect all selected rows; fall back to the right-clicked row
        selected_indexes = self._view.selectionModel().selectedRows()
        if selected_indexes:
            rows_to_trash = [self._model.get_row(i.row()) for i in selected_indexes]
        else:
            rows_to_trash = [self._model.get_row(index.row())]
        n = len(rows_to_trash)
        menu = QMenu(self)
        trash_action = menu.addAction(
            f"Move {n} transaction{'s' if n > 1 else ''} to Trash"
        )
        menu.addSeparator()
        stats_action = menu.addAction("View statistics for this merchant")
        chosen = menu.exec(self._view.viewport().mapToGlobal(pos))
        if chosen is trash_action:
            self.trash_requested.emit(rows_to_trash)
        elif chosen is stats_action:
            rows = self._time_filtered_rows()
            cat_rows = [
                r
                for r in rows
                if self._drill_cat == "" or _effective_category(r) == self._drill_cat
            ]
            merchant_rows = [
                r
                for r in cat_rows
                if self._drill_merchant == ""
                or (r.get("gemini_merchant") or r.get("description") or "").strip()
                == self._drill_merchant
            ]
            dlg = SpendingStatsDialog(
                self._drill_merchant or self._drill_cat or "All",
                merchant_rows,
                parent=self,
            )
            dlg.exec()

    def _on_summary_context_menu(self, pos) -> None:
        """Right-click on level-0 (category) or level-1 (merchant) summary rows → trash all."""
        index = self._summary_table.indexAt(pos)
        if not index.isValid():
            return
        item = self._summary_table.item(index.row(), 0)
        if item is None:
            return
        label = item.data(Qt.UserRole)
        if label is None:
            return

        rows = self._time_filtered_rows()

        if self._drill_cat is None:
            # Level 0 — trash all transactions in this category
            matching = [r for r in rows if _effective_category(r) == label]
            thing = f'category "{label}"'
        elif self._drill_merchant is None:
            # Level 1 — trash all transactions from this merchant
            cat_rows = [
                r
                for r in rows
                if self._drill_cat == "" or _effective_category(r) == self._drill_cat
            ]
            matching = [
                r
                for r in cat_rows
                if (r.get("gemini_merchant") or r.get("description") or "").strip()
                == label
            ]
            thing = f'merchant "{label}"'
        else:
            return  # level 2 has its own menu

        if not matching:
            return

        n = len(matching)
        menu = QMenu(self)
        trash_action = menu.addAction(
            f"Move all {n} transaction{'s' if n > 1 else ''} from {thing} to Trash"
        )
        menu.addSeparator()
        stats_action = menu.addAction(f"View statistics for {label}")
        chosen = menu.exec(self._summary_table.viewport().mapToGlobal(pos))
        if chosen is trash_action:
            self.trash_requested.emit(matching)
        elif chosen is stats_action:
            dlg = SpendingStatsDialog(label, matching, parent=self)
            dlg.exec()

    def _on_category_changed(self, top_left, bottom_right, roles):
        if Qt.EditRole not in roles and Qt.DisplayRole not in roles:
            return
        col = top_left.column()
        if col == COL_NOTES:
            # Notes edit: propagate to all rows with same description, then persist
            row_data = self._model.get_row(top_left.row())
            desc = row_data.get("description", "")
            notes = row_data.get("user_notes", "")
            for r in self._all_rows:
                if r.get("description") == desc:
                    r["user_notes"] = notes
            save_user_note(desc, notes)
            save_csv(self._all_rows)
            mw = self.window()
            if hasattr(mw, "statusBar"):
                mw.statusBar().showMessage(f'Note for "{desc[:50]}" saved.', 3000)
            return
        if col != COL_CATEGORY:
            return
        row_data = self._model.get_row(top_left.row())
        new_cat = row_data.get("gemini_category", "")
        desc = row_data.get("description", "")
        old_cat = row_data.get("_prev_category", "")
        similar = apply_category_change(self._all_rows, row_data, new_cat, old_cat)
        save_csv(self._all_rows)

        # Refresh table to reflect bulk changes
        if similar:
            self.refresh()

        # Notify main window via status bar
        mw = self.window()
        if hasattr(mw, "statusBar"):
            if similar:
                mw.statusBar().showMessage(
                    f'"{desc[:40]}" + {len(similar)} similar → "{new_cat}" saved.',
                    7000,
                )
            else:
                mw.statusBar().showMessage(
                    f'Category for "{desc[:50]}" → "{new_cat}" saved.', 5000
                )

    def update_data(self, rows: list[dict]) -> None:
        self._all_rows = rows
        self._delegate._categories = all_categories(rows)
        self._init_date_range()
        self.refresh()


# ---------------------------------------------------------------------------
# Statistics Tab
# ---------------------------------------------------------------------------


class StatisticsTab(QWidget):
    def __init__(self, all_rows: list[dict], parent=None):
        super().__init__(parent)
        self._all_rows = all_rows
        self._ai_worker: _GeminiWorker | None = None
        self._build_ui()
        self._refresh_all()
        self._load_cached_ai_analysis()

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        # Outer scroll area so the tab doesn't clip on smaller windows
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        outer.addWidget(scroll)

        container = QWidget()
        scroll.setWidget(container)
        layout = QVBoxLayout(container)
        layout.setSpacing(12)

        # ── Section helper ────────────────────────────────────────────
        def _section(title: str) -> tuple[QFrame, QGridLayout]:
            frame = QFrame()
            frame.setFrameShape(QFrame.StyledPanel)
            frame.setStyleSheet(
                "QFrame { border: 1px solid #ddd; border-radius: 4px; }"
            )
            inner = QVBoxLayout(frame)
            inner.setContentsMargins(10, 8, 10, 8)
            hdr = QLabel(f"<b>{title}</b>")
            hdr.setStyleSheet("font-size: 13px; border: none;")
            inner.addWidget(hdr)
            grid = QGridLayout()
            grid.setColumnStretch(1, 1)
            grid.setColumnStretch(3, 1)
            inner.addLayout(grid)
            return frame, grid

        def _kv(grid: QGridLayout, label: str, row: int, col_offset: int = 0) -> QLabel:
            lbl = QLabel(label)
            lbl.setStyleSheet("border: none; color: #555;")
            val = QLabel("—")
            val.setStyleSheet("border: none;")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(lbl, row, col_offset)
            grid.addWidget(val, row, col_offset + 1)
            return val

        # ── All-time overview ─────────────────────────────────────────
        ov_frame, ov_grid = _section("All-Time Overview")
        self._ov_expense = _kv(ov_grid, "Total expenses:", 0, 0)
        self._ov_months = _kv(ov_grid, "Months covered:", 0, 2)
        self._ov_avg = _kv(ov_grid, "Avg monthly spend:", 1, 0)
        self._ov_biggest = _kv(ov_grid, "Biggest expense:", 1, 2)
        self._ov_top_cat = _kv(ov_grid, "Top category:", 2, 0)
        self._ov_online = _kv(ov_grid, "Online / In-person:", 2, 2)
        layout.addWidget(ov_frame)

        # ── Monthly trend table ────────────────────────────────────────
        layout.addWidget(QLabel("<b>Monthly Spending Trend</b>"))
        self._monthly_table = QTableWidget(0, 3)
        self._monthly_table.setHorizontalHeaderLabels(
            ["Month", "Expenses ($)", "vs Prior"]
        )
        _mo_hdr = self._monthly_table.horizontalHeader()
        for _c in range(3):
            _mo_hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        _mo_hdr.setStretchLastSection(False)
        _restore_column_widths(_mo_hdr, "stats_monthly_table", [120, 120, 100])
        _mo_hdr.sectionResized.connect(
            lambda *_: _save_column_widths(_mo_hdr, "stats_monthly_table")
        )
        self._monthly_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._monthly_table.setAlternatingRowColors(True)
        self._monthly_table.setSortingEnabled(False)
        self._monthly_table.verticalHeader().hide()
        self._monthly_table.setMaximumHeight(220)
        layout.addWidget(self._monthly_table)

        # ── Monthly spending chart ─────────────────────────────────────
        layout.addWidget(QLabel("<b>Monthly Spending Chart</b>"))
        self._monthly_chart_view = QChartView()
        self._monthly_chart_view.setMinimumHeight(260)
        self._monthly_chart_view.setRenderHint(QPainter.Antialiasing)
        layout.addWidget(self._monthly_chart_view)

        # ── Selected-period overview ───────────────────────────────────
        pd_hdr = QHBoxLayout()
        pd_lbl = QLabel("<b>Selected Period</b>")
        pd_lbl.setStyleSheet("font-size: 13px;")
        pd_hdr.addWidget(pd_lbl)
        pd_hdr.addWidget(QLabel("  From:"))
        self._pd_from = QDateEdit()
        self._pd_from.setCalendarPopup(True)
        self._pd_from.setDisplayFormat("MMM d, yyyy")
        self._pd_from.dateChanged.connect(self._refresh_period)
        pd_hdr.addWidget(self._pd_from)
        pd_hdr.addWidget(QLabel("To:"))
        self._pd_to = QDateEdit()
        self._pd_to.setCalendarPopup(True)
        self._pd_to.setDisplayFormat("MMM d, yyyy")
        self._pd_to.dateChanged.connect(self._refresh_period)
        pd_hdr.addWidget(self._pd_to)
        pd_hdr.addStretch()
        layout.addLayout(pd_hdr)

        pd_frame, pd_grid = _section("")
        pd_frame.setStyleSheet("QFrame { border: 1px solid #ddd; border-radius: 4px; }")
        # hide the empty title label inside pd_frame
        pd_frame.layout().itemAt(0).widget().hide()
        self._pd_expense = _kv(pd_grid, "Total expenses:", 0, 0)
        self._pd_txn = _kv(pd_grid, "Transactions:", 0, 2)
        self._pd_avg = _kv(pd_grid, "Avg/transaction:", 1, 0)
        self._pd_biggest = _kv(pd_grid, "Biggest expense:", 1, 2)
        layout.addWidget(pd_frame)

        # ── Category breakdown ─────────────────────────────────────────
        cat_hdr = QHBoxLayout()
        cat_hdr.addWidget(QLabel("<b>Category Breakdown:</b>"))
        self._cat_combo = QComboBox()
        self._cat_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self._cat_combo.addItems(all_categories(spending_rows(self._all_rows)))
        self._cat_combo.currentIndexChanged.connect(self._refresh_category)
        cat_hdr.addWidget(self._cat_combo)
        cat_hdr.addStretch()
        layout.addLayout(cat_hdr)

        cat_frame, cat_grid = _section("")
        cat_frame.layout().itemAt(0).widget().hide()
        self._cat_count = _kv(cat_grid, "Transactions:", 0, 0)
        self._cat_total = _kv(cat_grid, "Total ($):", 0, 2)
        self._cat_avg_txn = _kv(cat_grid, "Avg/transaction:", 1, 0)
        self._cat_avg_mo = _kv(cat_grid, "Avg/active month:", 1, 2)
        self._cat_max = _kv(cat_grid, "Largest ($):", 2, 0)
        self._cat_min = _kv(cat_grid, "Smallest ($):", 2, 2)
        layout.addWidget(cat_frame)

        # Merchants + category-by-month side by side
        splitter = QSplitter(Qt.Horizontal)

        # Left: top merchants list
        merch_w = QWidget()
        merch_l = QVBoxLayout(merch_w)
        merch_l.setContentsMargins(0, 0, 0, 0)
        merch_l.addWidget(QLabel("<b>Top Merchants</b>"))
        self._merchant_list = QListWidget()
        self._merchant_list.setAlternatingRowColors(True)
        merch_l.addWidget(self._merchant_list)
        splitter.addWidget(merch_w)

        # Right: category by month
        cat_mo_w = QWidget()
        cat_mo_l = QVBoxLayout(cat_mo_w)
        cat_mo_l.setContentsMargins(0, 0, 0, 0)
        cat_mo_l.addWidget(QLabel("<b>This Category by Month</b>"))
        self._cat_monthly_table = QTableWidget(0, 2)
        self._cat_monthly_table.setHorizontalHeaderLabels(["Month", "Total ($)"])
        _cat_mo_hdr = self._cat_monthly_table.horizontalHeader()
        for _c in range(2):
            _cat_mo_hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        _cat_mo_hdr.setStretchLastSection(False)
        _restore_column_widths(_cat_mo_hdr, "stats_cat_monthly_table", [120, 120])
        _cat_mo_hdr.sectionResized.connect(
            lambda *_: _save_column_widths(_cat_mo_hdr, "stats_cat_monthly_table")
        )
        self._cat_monthly_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._cat_monthly_table.setAlternatingRowColors(True)
        self._cat_monthly_table.setSortingEnabled(False)
        self._cat_monthly_table.verticalHeader().hide()
        cat_mo_l.addWidget(self._cat_monthly_table)
        splitter.addWidget(cat_mo_w)

        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter)

        # ── AI spending analysis ───────────────────────────────────────
        layout.addWidget(QLabel("<b>AI Spending Analysis</b>"))
        self._ai_text = QTextBrowser()
        self._ai_text.setOpenExternalLinks(True)
        self._ai_text.setMinimumHeight(180)
        self._ai_text.setPlaceholderText("Generating AI analysis…")
        layout.addWidget(self._ai_text)

        layout.addStretch()

    # ------------------------------------------------------------------
    # Data refresh methods
    # ------------------------------------------------------------------

    def _refresh_all(self) -> None:
        """Refresh all three sections."""
        self._refresh_overall()
        lo, hi = _date_bounds(self._all_rows)
        self._pd_from.blockSignals(True)
        self._pd_to.blockSignals(True)
        self._pd_from.setDate(lo)
        self._pd_to.setDate(hi)
        self._pd_from.blockSignals(False)
        self._pd_to.blockSignals(False)
        self._refresh_period()
        self._refresh_category()

    def _refresh_overall(self) -> None:
        stats = compute_stats_overall(self._all_rows)

        self._ov_expense.setText(f"${stats['total_expense']:,.2f}")
        self._ov_months.setText(str(stats["n_months"]))
        self._ov_avg.setText(f"${stats['avg_monthly_expense']:,.2f}")
        self._ov_biggest.setText(
            f"${stats['biggest_txn_amount']:,.2f}  —  {stats['biggest_txn_merchant'][:30]}"
        )
        pct = stats["top_category_pct"]
        self._ov_top_cat.setText(f"{stats['top_category']}  ({pct:.1f}%)")

        mc = stats["mode_counts"]
        total_txn = sum(mc.values()) or 1
        online_pct = mc.get("online", 0) / total_txn * 100
        inperson_pct = mc.get("in_person", 0) / total_txn * 100
        self._ov_online.setText(
            f"{online_pct:.0f}% online  /  {inperson_pct:.0f}% in-person"
        )

        # Monthly trend table
        monthly = stats["monthly"]
        months = sorted(monthly.keys())
        self._monthly_table.setRowCount(len(months))
        for i, m in enumerate(months):
            amt = monthly[m]
            prior = monthly.get(months[i - 1]) if i > 0 else None
            delta_text = ""
            delta_color = ""
            if prior and prior > 0:
                delta = (amt - prior) / prior * 100
                delta_text = f"{'▲' if delta > 0 else '▼'} {abs(delta):.1f}%"
                delta_color = "#c62828" if delta > 0 else "#2e7d32"

            # Parse month key to friendly name
            ym = QDate.fromString(m + "-01", "yyyy-MM-dd")
            month_label = ym.toString("MMM yyyy") if ym.isValid() else m

            self._monthly_table.setItem(i, 0, QTableWidgetItem(month_label))
            amt_item = QTableWidgetItem(f"${amt:,.0f}")
            amt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._monthly_table.setItem(i, 1, amt_item)
            delta_item = QTableWidgetItem(delta_text)
            delta_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            if delta_color:
                delta_item.setForeground(QColor(delta_color))
            self._monthly_table.setItem(i, 2, delta_item)

        # Auto-scroll to bottom (most recent month)
        if months:
            self._monthly_table.scrollToBottom()

        self._update_monthly_chart(monthly)

    def _refresh_period(self) -> None:
        d_from = self._pd_from.date()
        d_to = self._pd_to.date()
        filtered = []
        for r in spending_rows(self._all_rows):
            d = _parse_date(r)
            if d is not None and d_from <= d <= d_to:
                filtered.append(r)

        total_exp = sum(abs(_parse_amount(r.get("amount", 0))) for r in filtered)
        biggest = max(
            filtered, key=lambda r: abs(_parse_amount(r.get("amount", 0))), default=None
        )
        biggest_amt = abs(_parse_amount(biggest.get("amount", 0))) if biggest else 0.0
        biggest_m = (
            (
                biggest.get("gemini_merchant") or biggest.get("description") or "—"
            ).strip()
            if biggest
            else "—"
        )
        avg = (total_exp / len(filtered)) if filtered else 0.0

        self._pd_expense.setText(f"${total_exp:,.2f}")
        self._pd_txn.setText(str(len(filtered)))
        self._pd_avg.setText(f"${avg:,.2f}")
        self._pd_biggest.setText(f"${biggest_amt:,.2f}  —  {biggest_m[:28]}")

    def _update_monthly_chart(self, monthly: dict) -> None:
        """Build/rebuild the monthly spending bar chart from {yyyy-MM: amount} data."""
        months = sorted(monthly.keys())
        if not months:
            self._monthly_chart_view.setChart(QChart())
            return

        bar_set = QBarSet("Spending")
        bar_set.setColor(QColor("#e65100"))
        for m in months:
            bar_set.append(monthly[m])

        series = QBarSeries()
        series.append(bar_set)

        chart = QChart()
        chart.addSeries(series)
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setVisible(False)

        month_labels = []
        for m in months:
            ym = QDate.fromString(m + "-01", "yyyy-MM-dd")
            month_labels.append(ym.toString("MMM yy") if ym.isValid() else m)

        axis_x = QBarCategoryAxis()
        axis_x.append(month_labels)
        chart.addAxis(axis_x, Qt.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setLabelFormat("$%.0f")
        axis_y.setMin(0)
        chart.addAxis(axis_y, Qt.AlignLeft)
        series.attachAxis(axis_y)

        # ── Hover tooltip (persistent — stays until mouse leaves bar) ────
        self._monthly_chart_tooltip = _ChartTooltip()
        _bar_data = list(zip(month_labels, [monthly[m] for m in months]))
        tt = self._monthly_chart_tooltip

        def _on_hover(status: bool, index: int, barset, bd=_bar_data, t=tt):
            if status and 0 <= index < len(bd):
                mo, amt = bd[index]
                t.show_at(QCursor.pos(), f"<b>{mo}</b><br>${amt:,.2f}")
            else:
                t.hide()

        series.hovered.connect(_on_hover)

        self._monthly_chart_view.setChart(chart)

    def _refresh_category(self) -> None:
        cat = self._cat_combo.currentText()
        if not cat:
            return
        rows = spending_rows(self._all_rows)
        filtered = [r for r in rows if _effective_category(r) == cat]

        if not filtered:
            for lbl in (
                self._cat_count,
                self._cat_total,
                self._cat_avg_txn,
                self._cat_avg_mo,
                self._cat_max,
                self._cat_min,
            ):
                lbl.setText("—")
            self._merchant_list.clear()
            self._cat_monthly_table.setRowCount(0)
            return

        amounts = [abs(_parse_amount(r.get("amount", 0))) for r in filtered]
        total = sum(amounts)
        avg_txn = total / len(amounts)

        # Monthly breakdown
        monthly: dict[str, float] = defaultdict(float)
        for r in filtered:
            d = _parse_date(r)
            if d:
                monthly[d.toString("yyyy-MM")] += abs(_parse_amount(r.get("amount", 0)))
        avg_mo = (total / len(monthly)) if monthly else 0.0

        self._cat_count.setText(str(len(filtered)))
        self._cat_total.setText(f"${total:,.2f}")
        self._cat_avg_txn.setText(f"${avg_txn:,.2f}")
        self._cat_avg_mo.setText(f"${avg_mo:,.2f}")
        self._cat_max.setText(f"${max(amounts):,.2f}")
        self._cat_min.setText(f"${min(amounts):,.2f}")

        # Top merchants
        merch_totals: dict[str, float] = defaultdict(float)
        for r in filtered:
            m = (r.get("gemini_merchant") or r.get("description") or "Unknown").strip()
            merch_totals[m] += abs(_parse_amount(r.get("amount", 0)))
        self._merchant_list.clear()
        for merch, total_m in sorted(
            merch_totals.items(), key=lambda kv: kv[1], reverse=True
        )[:10]:
            self._merchant_list.addItem(f"{merch}  —  ${total_m:,.2f}")

        # Category-by-month table
        months = sorted(monthly.keys())
        self._cat_monthly_table.setRowCount(len(months))
        for i, m in enumerate(months):
            ym = QDate.fromString(m + "-01", "yyyy-MM-dd")
            month_label = ym.toString("MMM yyyy") if ym.isValid() else m
            self._cat_monthly_table.setItem(i, 0, QTableWidgetItem(month_label))
            amt_item = QTableWidgetItem(f"${monthly[m]:,.0f}")
            amt_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self._cat_monthly_table.setItem(i, 1, amt_item)
        if months:
            self._cat_monthly_table.scrollToBottom()

    # ------------------------------------------------------------------
    # AI analysis
    # ------------------------------------------------------------------

    def _load_cached_ai_analysis(self) -> None:
        """Show cached analysis if fresh; auto-generate if missing or stale."""
        fp, analysis = _load_ai_cache()
        if analysis and fp == _statements_fingerprint():
            self._ai_text.setMarkdown(analysis)
        else:
            self._generate_ai_analysis()

    def _generate_ai_analysis(self) -> None:
        if not _gemini_client:
            self._ai_text.setPlainText(
                "No Gemini API key configured — set GEMINI_API_KEY in your .env file."
            )
            return
        if self._ai_worker and self._ai_worker.isRunning():
            return  # already in progress
        self._ai_text.setPlainText("Asking Gemini to analyse your spending habits…")
        rows = self._all_rows
        fp = _statements_fingerprint()

        def _call():
            return _gemini_spending_analysis(rows)

        worker = _GeminiWorker(_call, self)
        worker.result_ready.connect(lambda text, _fp=fp: self._on_ai_done(text, _fp))
        worker.error.connect(self._on_ai_error)
        self._ai_worker = worker
        worker.start()

    def _on_ai_done(self, text: str, fingerprint: str) -> None:
        self._ai_text.setMarkdown(text)
        _save_ai_cache(fingerprint, text)

    def _on_ai_error(self, err: str) -> None:
        self._ai_text.setPlainText(f"Error generating analysis: {err}")

    # ------------------------------------------------------------------
    # Data update hook (called from MainWindow when CSV is reloaded)
    # ------------------------------------------------------------------

    def update_data(self, rows: list[dict]) -> None:
        self._all_rows = rows
        prev = self._cat_combo.currentText()
        cats = all_categories(spending_rows(rows))
        self._cat_combo.blockSignals(True)
        self._cat_combo.clear()
        self._cat_combo.addItems(cats)
        if prev in cats:
            self._cat_combo.setCurrentText(prev)
        self._cat_combo.blockSignals(False)
        self._refresh_all()
        # Auto-regenerate if cache is missing or stale
        fp_cached, analysis_cached = _load_ai_cache()
        if not analysis_cached or fp_cached != _statements_fingerprint():
            self._generate_ai_analysis()


# ---------------------------------------------------------------------------
# Income Tab
# ---------------------------------------------------------------------------


class IncomeTab(QWidget):
    def __init__(self, all_rows: list[dict], parent=None):
        super().__init__(parent)
        self._all_rows = all_rows
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        self._selected_source: str | None = None
        self._current_rows: list[dict] = []
        self._official_sources: set = _load_official_sources()

        layout = QVBoxLayout(self)

        # ── Filter bar ──────────────────────────────────────────────────────
        filter_frame = QWidget()
        fl = QHBoxLayout(filter_frame)
        fl.setContentsMargins(0, 0, 0, 0)

        fl.addWidget(QLabel("Account:"))
        self._acct_combo = QComboBox()
        self._acct_combo.addItems(["All", "credit", "debit"])
        self._acct_combo.currentIndexChanged.connect(self.refresh)
        fl.addWidget(self._acct_combo)

        fl.addWidget(QLabel("From:"))
        self._date_from = QDateEdit()
        self._date_from.setCalendarPopup(True)
        self._date_from.setDisplayFormat("MMM d, yyyy")
        self._date_from.setDate(QDate(2020, 1, 1))
        self._date_from.dateChanged.connect(self.refresh)
        fl.addWidget(self._date_from)

        fl.addWidget(QLabel("To:"))
        self._date_to = QDateEdit()
        self._date_to.setCalendarPopup(True)
        self._date_to.setDisplayFormat("MMM d, yyyy")
        self._date_to.setDate(QDate.currentDate())
        self._date_to.dateChanged.connect(self.refresh)
        fl.addWidget(self._date_to)

        fl.addStretch()
        self._count_label = QLabel()
        fl.addWidget(self._count_label)
        layout.addWidget(filter_frame)

        # ── Summary by source ──────────────────────────────────────────────
        layout.addWidget(QLabel("<b>Income by Source</b>"))
        self._summary_table = QTableWidget(0, 3)
        self._summary_table.setHorizontalHeaderLabels(
            ["Source", "Transactions", "Total ($)"]
        )
        _inc_sum_hdr = self._summary_table.horizontalHeader()
        for _c in range(3):
            _inc_sum_hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        _inc_sum_hdr.setStretchLastSection(False)
        _restore_column_widths(_inc_sum_hdr, "income_summary_table", [250, 70, 100])
        _inc_sum_hdr.sectionResized.connect(
            lambda *_: _save_column_widths(_inc_sum_hdr, "income_summary_table")
        )
        self._summary_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._summary_table.setAlternatingRowColors(True)
        self._summary_table.setSortingEnabled(True)
        self._summary_table.verticalHeader().hide()
        self._summary_table.setMaximumHeight(200)
        self._summary_table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._summary_table.customContextMenuRequested.connect(
            self._on_source_context_menu
        )
        self._summary_table.itemSelectionChanged.connect(self._on_source_selected)
        layout.addWidget(self._summary_table)

        # ── Transaction list ───────────────────────────────────────────────
        layout.addWidget(QLabel("<b>Transactions</b>"))
        self._model = TransactionModel([])
        self._view = QTableView()
        self._view.setModel(self._model)
        self._view.setAlternatingRowColors(True)
        self._view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._view.setSortingEnabled(True)
        _inc_hdr = self._view.horizontalHeader()
        for _c in range(9):
            _inc_hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        _inc_hdr.setStretchLastSection(False)
        _restore_column_widths(
            _inc_hdr, "income_view", [90, 220, 90, 130, 130, 80, 160, 110, 70]
        )
        _inc_hdr.sectionResized.connect(
            lambda *_: _save_column_widths(_inc_hdr, "income_view")
        )
        self._view.setColumnHidden(6, True)  # Place — not relevant for income
        self._view.setColumnHidden(7, True)  # Phone — not relevant for income
        self._view.verticalHeader().hide()
        layout.addWidget(self._view)

    def _filtered_rows(self) -> list[dict]:
        acct = self._acct_combo.currentText()
        d_from = self._date_from.date()
        d_to = self._date_to.date()
        result = []
        for row in income_rows(self._all_rows):
            if acct != "All" and row.get("account_type") != acct:
                continue
            d = _parse_date(row)
            if d is not None:
                if d < d_from or d > d_to:
                    continue
            result.append(row)
        return result

    def refresh(self):
        rows = self._filtered_rows()
        self._current_rows = rows
        self._count_label.setText(f"{len(rows)} transactions")

        # Summary by source
        source_totals: dict[str, dict] = defaultdict(lambda: {"count": 0, "total": 0.0})
        for row in rows:
            source = (
                row.get("gemini_merchant") or row.get("description") or "Unknown"
            ).strip()
            amt = _parse_amount(row.get("amount", 0))
            source_totals[source]["count"] += 1
            source_totals[source]["total"] += abs(amt)

        sorted_sources = sorted(
            source_totals.items(), key=lambda kv: kv[1]["total"], reverse=True
        )
        self._summary_table.blockSignals(True)
        self._summary_table.setSortingEnabled(False)
        self._summary_table.setRowCount(len(sorted_sources) + 1)

        # Pinned "All" row at index 0
        all_total = sum(d["total"] for _, d in sorted_sources)
        all_count = sum(d["count"] for _, d in sorted_sources)
        all_src_item = QTableWidgetItem("All")
        all_src_item.setData(Qt.UserRole, "__all__")
        _af = all_src_item.font()
        _af.setBold(True)
        all_src_item.setFont(_af)
        self._summary_table.setItem(0, 0, all_src_item)
        all_cnt_item = QTableWidgetItem()
        all_cnt_item.setData(Qt.DisplayRole, all_count)
        self._summary_table.setItem(0, 1, all_cnt_item)
        all_amt_item = _NumericItem("")
        all_amt_item.setData(Qt.UserRole, all_total)
        all_amt_item.setData(Qt.DisplayRole, round(all_total, 2))
        self._summary_table.setItem(0, 2, all_amt_item)

        for row_i, (source, data) in enumerate(sorted_sources, start=1):
            official = source in self._official_sources
            src_item = QTableWidgetItem(f"\u2b50 {source}" if official else source)
            src_item.setData(Qt.UserRole, source)  # raw name for filtering
            if official:
                src_item.setForeground(QColor("#f9a825"))
                _f = src_item.font()
                _f.setBold(True)
                src_item.setFont(_f)
            self._summary_table.setItem(row_i, 0, src_item)
            cnt_item = QTableWidgetItem()
            cnt_item.setData(Qt.DisplayRole, data["count"])
            self._summary_table.setItem(row_i, 1, cnt_item)
            amt_item = _NumericItem("")
            amt_item.setData(Qt.UserRole, data["total"])
            amt_item.setData(Qt.DisplayRole, round(data["total"], 2))
            self._summary_table.setItem(row_i, 2, amt_item)
        self._summary_table.blockSignals(False)
        self._refresh_txn_table()

    def update_data(self, rows: list[dict]):
        self._all_rows = rows
        self.refresh()

    def _refresh_txn_table(self) -> None:
        """Populate the transaction table, filtered to _selected_source if set."""
        if self._selected_source is not None:
            filtered = [
                r
                for r in self._current_rows
                if (r.get("gemini_merchant") or r.get("description") or "").strip()
                == self._selected_source
            ]
        else:
            filtered = list(self._current_rows)
        filtered.sort(key=lambda r: abs(float(r.get("amount", 0) or 0)), reverse=True)
        self._model.set_rows(filtered)
        self._view.horizontalHeader().setSortIndicator(2, Qt.DescendingOrder)

    def _on_source_selected(self) -> None:
        """Filter transaction table to the clicked income source."""
        row_i = self._summary_table.currentRow()
        if row_i < 0:
            self._selected_source = None
        else:
            item = self._summary_table.item(row_i, 0)
            raw = item.data(Qt.UserRole) if item else None
            self._selected_source = None if raw == "__all__" else raw
        self._refresh_txn_table()

    def _on_source_context_menu(self, pos) -> None:
        """Right-click on an income source row — stats + mark/unmark official."""
        index = self._summary_table.indexAt(pos)
        if not index.isValid():
            return
        item = self._summary_table.item(index.row(), 0)
        if item is None:
            return
        source = item.data(Qt.UserRole)
        if not source or source == "__all__":
            return
        source_rows = [
            r
            for r in self._current_rows
            if (r.get("gemini_merchant") or r.get("description") or "").strip()
            == source
        ]
        is_official = source in self._official_sources
        mark_label = (
            "Unmark as official income source"
            if is_official
            else "\u2b50 Mark as official income source"
        )
        menu = QMenu(self)
        stats_action = menu.addAction(f"View statistics for {source}")
        menu.addSeparator()
        mark_action = menu.addAction(mark_label)
        chosen = menu.exec(self._summary_table.viewport().mapToGlobal(pos))
        if chosen is stats_action:
            SpendingStatsDialog(
                source, source_rows, parent=self, per_transaction=True
            ).exec()
        elif chosen is mark_action:
            if is_official:
                self._official_sources.discard(source)
            else:
                self._official_sources.add(source)
            _save_official_sources(self._official_sources)
            self.refresh()


# ---------------------------------------------------------------------------
# Spending Stats Dialog
# ---------------------------------------------------------------------------


class SpendingStatsDialog(QDialog):
    """Pop-up showing multi-tab spending/income statistics."""

    _CHART_H = 300  # standard chart height for all tabs

    def __init__(
        self,
        label: str,
        rows: list[dict],
        parent=None,
        per_transaction: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Stats \u2014 {label}")
        self.resize(920, 580)
        self._tooltips: list = []  # keep _ChartTooltip refs alive

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(6)

        # \u2500\u2500 Parse rows into (QDate, amount) tuples \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        dated: list = sorted(
            [
                (d, abs(_parse_amount(r.get("amount", 0))))
                for r in rows
                if (d := _parse_date(r)) is not None
            ],
            key=lambda x: x[0],
        )

        tab_widget = QTabWidget()
        outer.addWidget(tab_widget)
        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.reject)
        outer.addWidget(btn_box)

        if not dated:
            tab_widget.addTab(QLabel("  No data for the selected period.  "), "No Data")
            return

        tab_widget.addTab(
            self._tab_bar(label, dated, per_transaction),
            "Transactions" if per_transaction else "Monthly",
        )
        tab_widget.addTab(self._tab_dow(label, dated), "Day of Week")
        tab_widget.addTab(self._tab_histogram(label, dated), "Size Distribution")
        tab_widget.addTab(
            self._tab_cumulative(label, dated, per_transaction), "Cumulative"
        )
        if per_transaction:
            if len(dated) >= 2:
                tab_widget.addTab(self._tab_intervals(label, dated), "Pay Intervals")
        else:
            tab_widget.addTab(self._tab_mom(label, dated), "Month-over-Month")
            if len({d.year() for d, _ in dated}) >= 2:
                tab_widget.addTab(self._tab_yoy(label, dated), "Year-over-Year")

    # \u2500\u2500 Shared utilities \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _register_tt(self):
        tt = _ChartTooltip()
        self._tooltips.append(tt)
        return tt

    @staticmethod
    def _wrap_chart(chart: QChart, chart_w: int, h: int = 300) -> QWidget:
        """Return a QWidget containing a (possibly scrollable) QChartView."""
        container = QWidget()
        vb = QVBoxLayout(container)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(0)
        cv = QChartView(chart)
        cv.setRenderHint(QPainter.Antialiasing)
        cv.setFixedHeight(h)
        if chart_w > 760:
            cv.setFixedWidth(chart_w)
            scroll = QScrollArea()
            scroll.setWidget(cv)
            scroll.setWidgetResizable(False)
            scroll.setFixedHeight(h + 20)
            scroll.setMinimumWidth(760)
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            vb.addWidget(scroll)
        else:
            cv.setFixedWidth(chart_w)
            vb.addWidget(cv)
        return container

    @staticmethod
    def _tab_container(chart_widget: QWidget, stats_text: str) -> QWidget:
        """Wrap chart_widget + a centered stats label into a tab-page QWidget."""
        page = QWidget()
        vb = QVBoxLayout(page)
        vb.setContentsMargins(8, 8, 8, 4)
        vb.setSpacing(6)
        vb.addWidget(chart_widget)
        lbl = QLabel(stats_text)
        lbl.setAlignment(Qt.AlignCenter)
        vb.addWidget(lbl)
        return page

    # \u2500\u2500 Tab 1: Bar chart (monthly totals or per-transaction) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _tab_bar(self, label: str, dated: list, per_transaction: bool) -> QWidget:
        if per_transaction:
            bar_labels = [d.toString("MMM d yy") for d, _ in dated]
            bar_values = [amt for _, amt in dated]
            chart_title = f"Payments \u2014 {label}"
            bar_color = QColor("#2e7d32")
        else:
            monthly: dict = defaultdict(float)
            for d, amt in dated:
                monthly[d.toString("yyyy-MM")] += amt
            months = sorted(monthly)
            bar_labels = [
                (lambda ym: ym.toString("MMM yy") if ym.isValid() else m)(
                    QDate.fromString(m + "-01", "yyyy-MM-dd")
                )
                for m in months
            ]
            bar_values = [monthly[m] for m in months]
            chart_title = f"Monthly spending \u2014 {label}"
            bar_color = QColor("#e65100")

        bs = QBarSet(label)
        bs.setColor(bar_color)
        for v in bar_values:
            bs.append(v)
        series = QBarSeries()
        series.append(bs)
        series.setBarWidth(0.5)

        ax = QBarCategoryAxis()
        ax.append(bar_labels)
        ax.setLabelsAngle(-35)
        _f = ax.labelsFont()
        _f.setPointSize(9)
        ax.setLabelsFont(_f)
        ay = QValueAxis()
        ay.setLabelFormat("$%.0f")
        ay.setMin(0)
        ay.setMax(max(bar_values, default=1) * 1.18)

        chart = QChart()
        chart.addSeries(series)
        chart.setTitle(chart_title)
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setVisible(False)
        chart.addAxis(ax, Qt.AlignBottom)
        chart.addAxis(ay, Qt.AlignLeft)
        series.attachAxis(ax)
        series.attachAxis(ay)
        chart.setContentsMargins(0, 0, 0, 0)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        tt = self._register_tt()
        bd = list(zip(bar_labels, bar_values))

        def _hov(status, index, barset, t=tt, d=bd, lbl=label):
            if status and 0 <= index < len(d):
                x, v = d[index]
                t.show_at(QCursor.pos(), f"<b>{lbl}</b><br>{x}<br><b>${v:,.2f}</b>")
            else:
                t.hide()

        series.hovered.connect(_hov)

        cw = self._wrap_chart(chart, max(560, len(bar_labels) * 54 + 100))
        total = sum(bar_values)
        avg = total / len(bar_values) if bar_values else 0.0
        peak_i = bar_values.index(max(bar_values)) if bar_values else 0
        if per_transaction:
            stats = (
                f"Total: <b>${total:,.2f}</b> &nbsp;\u2502&nbsp; "
                f"Payments: <b>{len(bar_values)}</b> &nbsp;\u2502&nbsp; "
                f"Avg: <b>${avg:,.2f}</b> &nbsp;\u2502&nbsp; "
                f"Largest: <b>{bar_labels[peak_i]}</b> (${max(bar_values, default=0):,.2f})"
            )
        else:
            stats = (
                f"Total: <b>${total:,.2f}</b> &nbsp;\u2502&nbsp; "
                f"Monthly avg: <b>${avg:,.2f}</b> &nbsp;\u2502&nbsp; "
                f"Peak: <b>{bar_labels[peak_i]}</b> (${max(bar_values, default=0):,.2f})"
            )
        return self._tab_container(cw, stats)

    # \u2500\u2500 Tab 2: Day-of-week distribution \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _tab_dow(self, label: str, dated: list) -> QWidget:
        DOW = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        totals = [0.0] * 7
        counts = [0] * 7
        for d, amt in dated:
            i = d.dayOfWeek() - 1  # Qt: 1=Mon..7=Sun
            totals[i] += amt
            counts[i] += 1

        bs = QBarSet("Total ($)")
        bs.setColor(QColor("#5c6bc0"))
        for v in totals:
            bs.append(v)
        series = QBarSeries()
        series.append(bs)
        series.setBarWidth(0.6)

        ax = QBarCategoryAxis()
        ax.append(DOW)
        ay = QValueAxis()
        ay.setLabelFormat("$%.0f")
        ay.setMin(0)
        ay.setMax((max(totals) if any(totals) else 10) * 1.18)

        chart = QChart()
        chart.addSeries(series)
        chart.setTitle(f"Spending by Day of Week \u2014 {label}")
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setVisible(False)
        chart.addAxis(ax, Qt.AlignBottom)
        chart.addAxis(ay, Qt.AlignLeft)
        series.attachAxis(ax)
        series.attachAxis(ay)
        chart.setContentsMargins(0, 0, 0, 0)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        tt = self._register_tt()
        dow_data = list(zip(DOW, totals, counts))

        def _hov(status, index, barset, t=tt, d=dow_data):
            if status and 0 <= index < len(d):
                day, tot, cnt = d[index]
                avg_v = tot / cnt if cnt else 0.0
                t.show_at(
                    QCursor.pos(),
                    f"<b>{day}</b><br>"
                    f"{cnt} transaction{'s' if cnt != 1 else ''}"
                    f" \u00b7 avg ${avg_v:,.2f}<br>"
                    f"<b>Total: ${tot:,.2f}</b>",
                )
            else:
                t.hide()

        series.hovered.connect(_hov)
        cw = self._wrap_chart(chart, 640)
        active = [i for i in range(7) if counts[i] > 0]
        if active:
            busiest_i = max(active, key=lambda i: totals[i])
            quietest_i = min(active, key=lambda i: totals[i])
            stats = (
                f"Busiest: <b>{DOW[busiest_i]}</b> (${totals[busiest_i]:,.2f})"
                f" &nbsp;\u2502&nbsp; "
                f"Quietest: <b>{DOW[quietest_i]}</b> (${totals[quietest_i]:,.2f})"
            )
        else:
            stats = "No data."
        return self._tab_container(cw, stats)

    # \u2500\u2500 Tab 3: Transaction size histogram \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _tab_histogram(self, label: str, dated: list) -> QWidget:
        amts = [amt for _, amt in dated]
        BUCKETS = [
            (0, 25),
            (25, 50),
            (50, 100),
            (100, 200),
            (200, 500),
            (500, float("inf")),
        ]
        B_LABELS = [
            "$0\u201325",
            "$25\u201350",
            "$50\u2013100",
            "$100\u2013200",
            "$200\u2013500",
            "$500+",
        ]
        counts = [0] * len(BUCKETS)
        bucket_sums = [0.0] * len(BUCKETS)
        for amt in amts:
            for i, (lo, hi) in enumerate(BUCKETS):
                if lo <= amt < hi or (hi == float("inf") and amt >= lo):
                    counts[i] += 1
                    bucket_sums[i] += amt
                    break
        while counts and counts[-1] == 0:
            counts.pop()
            B_LABELS.pop()
            bucket_sums.pop()
        if not counts:
            page = QWidget()
            QVBoxLayout(page).addWidget(QLabel("  No data.  "))
            return page

        bs = QBarSet("Transactions")
        bs.setColor(QColor("#00838f"))
        for c in counts:
            bs.append(c)
        series = QBarSeries()
        series.append(bs)
        series.setBarWidth(0.6)

        ax = QBarCategoryAxis()
        ax.append(B_LABELS)
        ay = QValueAxis()
        ay.setLabelFormat("%.0f")
        ay.setMin(0)
        ay.setMax(max(counts) * 1.18)

        chart = QChart()
        chart.addSeries(series)
        chart.setTitle(f"Transaction Size Distribution \u2014 {label}")
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setVisible(False)
        chart.addAxis(ax, Qt.AlignBottom)
        chart.addAxis(ay, Qt.AlignLeft)
        series.attachAxis(ax)
        series.attachAxis(ay)
        chart.setContentsMargins(0, 0, 0, 0)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        tt = self._register_tt()
        hist_data = list(zip(B_LABELS, counts, bucket_sums))

        def _hov(status, index, barset, t=tt, d=hist_data):
            if status and 0 <= index < len(d):
                bucket, cnt, s = d[index]
                avg_v = s / cnt if cnt else 0.0
                t.show_at(
                    QCursor.pos(),
                    f"<b>{bucket}</b><br>"
                    f"{cnt} transaction{'s' if cnt != 1 else ''}<br>"
                    f"avg ${avg_v:,.2f}",
                )
            else:
                t.hide()

        series.hovered.connect(_hov)
        cw = self._wrap_chart(chart, 620)
        sorted_amts = sorted(amts)
        median = sorted_amts[len(sorted_amts) // 2]
        most_common_i = counts.index(max(counts))
        stats = (
            f"Most common: <b>{B_LABELS[most_common_i]}</b>"
            f" ({counts[most_common_i]} txns)"
            f" &nbsp;\u2502&nbsp; Median: <b>${median:,.2f}</b>"
            f" &nbsp;\u2502&nbsp; Total txns: <b>{len(amts)}</b>"
        )
        return self._tab_container(cw, stats)

    # \u2500\u2500 Tab 4: Cumulative line chart \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _tab_cumulative(
        self, label: str, dated: list, per_transaction: bool
    ) -> QWidget:
        clr = QColor("#2e7d32") if per_transaction else QColor("#e65100")
        running = 0.0
        cum_data: list = []
        for d, amt in dated:
            running += amt
            cum_data.append((d.startOfDay().toMSecsSinceEpoch(), running))

        line = QLineSeries()
        line.setName(label)
        lpen = line.pen()
        lpen.setColor(clr)
        lpen.setWidth(2)
        line.setPen(lpen)
        for ms, val in cum_data:
            line.append(ms, val)

        avg_line = QLineSeries()
        avg_line.setName("Avg rate")
        apen = avg_line.pen()
        apen.setColor(QColor("#aaaaaa"))
        apen.setWidth(1)
        apen.setStyle(Qt.DashLine)
        avg_line.setPen(apen)
        if cum_data:
            avg_line.append(cum_data[0][0], 0.0)
            avg_line.append(cum_data[-1][0], running)

        first_dt = QDateTime.fromMSecsSinceEpoch(int(cum_data[0][0]))
        last_dt = QDateTime.fromMSecsSinceEpoch(int(cum_data[-1][0]))
        axis_x = QDateTimeAxis()
        axis_x.setFormat("MMM yy")
        axis_x.setTitleText("Date")
        axis_x.setRange(first_dt, last_dt)
        axis_y = QValueAxis()
        axis_y.setLabelFormat("$%.0f")
        axis_y.setTitleText("Cumulative ($)")
        axis_y.setMin(0)
        axis_y.setMax(max(running * 1.12, 1))

        chart = QChart()
        chart.addSeries(line)
        chart.addSeries(avg_line)
        title_word = "income" if per_transaction else "spending"
        chart.setTitle(f"Cumulative {title_word} \u2014 {label}")
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setAlignment(Qt.AlignBottom)
        chart.legend().setVisible(True)
        chart.addAxis(axis_x, Qt.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignLeft)
        line.attachAxis(axis_x)
        line.attachAxis(axis_y)
        avg_line.attachAxis(axis_x)
        avg_line.attachAxis(axis_y)
        chart.setContentsMargins(0, 0, 0, 0)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        tt = self._register_tt()

        def _hov(point, state, t=tt, d=cum_data):
            if state and d:
                nearest = min(d, key=lambda p: abs(p[0] - point.x()))
                dt_str = QDateTime.fromMSecsSinceEpoch(int(nearest[0])).toString(
                    "MMM d, yyyy"
                )
                t.show_at(
                    QCursor.pos(),
                    f"<b>{dt_str}</b><br>Running total: <b>${nearest[1]:,.2f}</b>",
                )
            else:
                t.hide()

        line.hovered.connect(_hov)

        fd, ld = first_dt.date(), last_dt.date()
        months_span = max((ld.year() - fd.year()) * 12 + ld.month() - fd.month() + 1, 1)
        cw = self._wrap_chart(chart, max(560, months_span * 54 + 100), h=320)
        n = len(dated)
        stats = (
            f"Total: <b>${running:,.2f}</b> &nbsp;\u2502&nbsp; "
            f"Transactions: <b>{n}</b> &nbsp;\u2502&nbsp; "
            f"Avg per transaction: <b>${running / n:,.2f}</b>"
        )
        return self._tab_container(cw, stats)

    # \u2500\u2500 Tab 5 (spending): Month-over-Month % change \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _tab_mom(self, label: str, dated: list) -> QWidget:
        monthly: dict = defaultdict(float)
        for d, amt in dated:
            monthly[d.toString("yyyy-MM")] += amt
        months = sorted(monthly)
        if len(months) < 2:
            page = QWidget()
            QVBoxLayout(page).addWidget(QLabel("  Need at least 2 months of data.  "))
            return page

        mo_labels, mo_pcts, mo_ctx = [], [], []
        for i in range(1, len(months)):
            prev_v = monthly[months[i - 1]]
            curr_v = monthly[months[i]]
            pct = (curr_v - prev_v) / prev_v * 100.0 if prev_v else 0.0
            ym = QDate.fromString(months[i] + "-01", "yyyy-MM-dd")
            lbl = ym.toString("MMM yy") if ym.isValid() else months[i]
            ym_p = QDate.fromString(months[i - 1] + "-01", "yyyy-MM-dd")
            lbl_p = ym_p.toString("MMM yy") if ym_p.isValid() else months[i - 1]
            mo_labels.append(lbl)
            mo_pcts.append(pct)
            mo_ctx.append((lbl_p, prev_v, curr_v))

        bs = QBarSet("MoM % Change")
        bs.setColor(QColor("#7e57c2"))
        for v in mo_pcts:
            bs.append(v)
        series = QBarSeries()
        series.append(bs)
        series.setBarWidth(0.5)

        ax = QBarCategoryAxis()
        ax.append(mo_labels)
        ax.setLabelsAngle(-35)
        _f = ax.labelsFont()
        _f.setPointSize(9)
        ax.setLabelsFont(_f)
        mn, mx = min(mo_pcts), max(mo_pcts)
        pad = max(abs(mn), abs(mx), 1) * 0.2
        ay = QValueAxis()
        ay.setLabelFormat("%.0f%%")
        ay.setMin(mn - pad)
        ay.setMax(mx + pad)

        chart = QChart()
        chart.addSeries(series)
        chart.setTitle(f"Month-over-Month Change \u2014 {label}")
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setVisible(False)
        chart.addAxis(ax, Qt.AlignBottom)
        chart.addAxis(ay, Qt.AlignLeft)
        series.attachAxis(ax)
        series.attachAxis(ay)
        chart.setContentsMargins(0, 0, 0, 0)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        tt = self._register_tt()
        mom_data = list(zip(mo_labels, mo_pcts, mo_ctx))

        def _hov(status, index, barset, t=tt, d=mom_data):
            if status and 0 <= index < len(d):
                lbl, pct, (prev_lbl, prev_v, curr_v) = d[index]
                arrow = "\u2191" if pct >= 0 else "\u2193"
                t.show_at(
                    QCursor.pos(),
                    f"<b>{lbl}</b><br>{arrow} {pct:+.1f}% vs {prev_lbl}<br>"
                    f"${prev_v:,.2f} \u2192 <b>${curr_v:,.2f}</b>",
                )
            else:
                t.hide()

        series.hovered.connect(_hov)
        cw = self._wrap_chart(chart, max(560, len(mo_labels) * 54 + 100))
        max_i = mo_pcts.index(max(mo_pcts))
        min_i = mo_pcts.index(min(mo_pcts))
        stats = (
            f"Biggest increase: <b>{mo_labels[max_i]}</b> ({mo_pcts[max_i]:+.1f}%)"
            f" &nbsp;\u2502&nbsp; "
            f"Biggest drop: <b>{mo_labels[min_i]}</b> ({mo_pcts[min_i]:+.1f}%)"
        )
        return self._tab_container(cw, stats)

    # \u2500\u2500 Tab 6 (spending): Year-over-Year comparison \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _tab_yoy(self, label: str, dated: list) -> QWidget:
        year_month: dict = defaultdict(lambda: defaultdict(float))
        for d, amt in dated:
            year_month[d.year()][d.month()] += amt
        years = sorted(year_month)
        MONTH_NAMES = [
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
        YEAR_COLORS = ["#e65100", "#1565c0", "#6a1b9a", "#2e7d32", "#37474f"]

        series = QBarSeries()
        yoy_data: dict = {}
        for i, yr in enumerate(years):
            bs = QBarSet(str(yr))
            bs.setColor(QColor(YEAR_COLORS[i % len(YEAR_COLORS)]))
            vals = [year_month[yr].get(m, 0.0) for m in range(1, 13)]
            for v in vals:
                bs.append(v)
            series.append(bs)
            yoy_data[yr] = vals
        series.setBarWidth(0.7)

        ax = QBarCategoryAxis()
        ax.append(MONTH_NAMES)
        all_vals = [year_month[yr].get(m, 0.0) for yr in years for m in range(1, 13)]
        ay = QValueAxis()
        ay.setLabelFormat("$%.0f")
        ay.setMin(0)
        ay.setMax((max(all_vals) if any(all_vals) else 10) * 1.18)

        chart = QChart()
        chart.addSeries(series)
        chart.setTitle(f"Year-over-Year \u2014 {label}")
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setAlignment(Qt.AlignBottom)
        chart.legend().setVisible(True)
        chart.addAxis(ax, Qt.AlignBottom)
        chart.addAxis(ay, Qt.AlignLeft)
        series.attachAxis(ax)
        series.attachAxis(ay)
        chart.setContentsMargins(0, 0, 0, 0)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        tt = self._register_tt()

        def _hov(status, index, barset, t=tt, mn=MONTH_NAMES, yd=yoy_data):
            if status and 0 <= index < 12:
                yr_name = barset.label()
                try:
                    val = yd[int(yr_name)][index]
                except (ValueError, IndexError, KeyError):
                    val = 0.0
                t.show_at(
                    QCursor.pos(),
                    f"<b>{mn[index]} {yr_name}</b><br><b>${val:,.2f}</b>",
                )
            else:
                t.hide()

        series.hovered.connect(_hov)
        cw = self._wrap_chart(chart, 760)
        year_totals = {yr: sum(year_month[yr].values()) for yr in years}
        best_yr = max(years, key=lambda y: year_totals[y])
        parts = [f"{yr}: <b>${year_totals[yr]:,.2f}</b>" for yr in years]
        stats = (
            " &nbsp;\u2502&nbsp; ".join(parts)
            + f" &nbsp;\u2502&nbsp; Highest year: <b>{best_yr}</b>"
        )
        return self._tab_container(cw, stats)

    # \u2500\u2500 Tab 5 (income): Inter-payment interval histogram \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500

    def _tab_intervals(self, label: str, dated: list) -> QWidget:
        gaps = [dated[i - 1][0].daysTo(dated[i][0]) for i in range(1, len(dated))]
        BUCKETS = [(0, 7), (7, 14), (14, 21), (21, 35), (35, 60), (60, float("inf"))]
        B_LABELS = [
            "< 7 days",
            "7\u201314 days",
            "14\u201321 days",
            "21\u201335 days",
            "35\u201360 days",
            "60+ days",
        ]
        counts = [0] * len(BUCKETS)
        bucket_gaps: list = [[] for _ in BUCKETS]
        for gap in gaps:
            for i, (lo, hi) in enumerate(BUCKETS):
                if lo <= gap < hi or (hi == float("inf") and gap >= lo):
                    counts[i] += 1
                    bucket_gaps[i].append(gap)
                    break
        while counts and counts[-1] == 0:
            counts.pop()
            B_LABELS.pop()
            bucket_gaps.pop()
        if not counts:
            page = QWidget()
            QVBoxLayout(page).addWidget(QLabel("  No interval data.  "))
            return page

        bs = QBarSet("Occurrences")
        bs.setColor(QColor("#2e7d32"))
        for c in counts:
            bs.append(c)
        series = QBarSeries()
        series.append(bs)
        series.setBarWidth(0.6)

        ax = QBarCategoryAxis()
        ax.append(B_LABELS)
        ay = QValueAxis()
        ay.setLabelFormat("%.0f")
        ay.setMin(0)
        ay.setMax(max(counts) * 1.18)

        chart = QChart()
        chart.addSeries(series)
        chart.setTitle(f"Days Between Payments \u2014 {label}")
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setVisible(False)
        chart.addAxis(ax, Qt.AlignBottom)
        chart.addAxis(ay, Qt.AlignLeft)
        series.attachAxis(ax)
        series.attachAxis(ay)
        chart.setContentsMargins(0, 0, 0, 0)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        tt = self._register_tt()
        int_data = list(zip(B_LABELS, counts, bucket_gaps))

        def _hov(status, index, barset, t=tt, d=int_data):
            if status and 0 <= index < len(d):
                lbl, cnt, g_list = d[index]
                avg_g = sum(g_list) / len(g_list) if g_list else 0.0
                t.show_at(
                    QCursor.pos(),
                    f"<b>{lbl}</b><br>"
                    f"{cnt} gap{'s' if cnt != 1 else ''}"
                    f" \u00b7 avg {avg_g:.1f} days",
                )
            else:
                t.hide()

        series.hovered.connect(_hov)
        cw = self._wrap_chart(chart, 660)
        most_common_i = counts.index(max(counts))
        median_gap = sorted(gaps)[len(gaps) // 2]
        if 27 <= median_gap <= 32:
            schedule = "monthly"
        elif 13 <= median_gap <= 16:
            schedule = "bi-weekly"
        elif 6 <= median_gap <= 8:
            schedule = "weekly"
        else:
            schedule = f"~{median_gap}d intervals"
        stats = (
            f"Most common: <b>{B_LABELS[most_common_i]}</b>"
            f" ({counts[most_common_i]}\u00d7)"
            f" &nbsp;\u2502&nbsp; Median gap: <b>{median_gap} days</b>"
            f" &nbsp;\u2502&nbsp; Inferred: <b>{schedule}</b>"
        )
        return self._tab_container(cw, stats)

    def __init__(
        self,
        label: str,
        rows: list[dict],
        parent=None,
        per_transaction: bool = False,
    ):
        super().__init__(parent)
        self.setWindowTitle(f"Stats \u2014 {label}")
        self.resize(820, 480)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        # \u2500\u2500 Build data arrays \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        if per_transaction:
            txn_data = sorted(
                [
                    (d, abs(_parse_amount(r.get("amount", 0))))
                    for r in rows
                    if (d := _parse_date(r)) is not None
                ],
                key=lambda x: x[0],
            )
            if not txn_data:
                layout.addWidget(QLabel("No data for the selected period."))
                return
            bar_labels = [d.toString("MMM d yy") for d, _ in txn_data]
            bar_values = [amt for _, amt in txn_data]
            chart_title = f"Transactions \u2014 {label}"
            bar_color = QColor("#2e7d32")
        else:
            monthly: dict[str, float] = defaultdict(float)
            for r in rows:
                d = _parse_date(r)
                if d:
                    monthly[d.toString("yyyy-MM")] += abs(
                        _parse_amount(r.get("amount", 0))
                    )
            months = sorted(monthly)
            if not months:
                layout.addWidget(QLabel("No data for the selected period."))
                return
            bar_labels = [
                (lambda ym: ym.toString("MMM yy") if ym.isValid() else m)(
                    QDate.fromString(m + "-01", "yyyy-MM-dd")
                )
                for m in months
            ]
            bar_values = [monthly[m] for m in months]
            chart_title = f"Monthly spending \u2014 {label}"
            bar_color = QColor("#e65100")

        # \u2500\u2500 Bar chart \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        bar_set = QBarSet(label)
        bar_set.setColor(bar_color)
        for v in bar_values:
            bar_set.append(v)

        series = QBarSeries()
        series.append(bar_set)
        series.setBarWidth(0.5)

        axis_x = QBarCategoryAxis()
        axis_x.append(bar_labels)
        axis_x.setLabelsAngle(-35)
        f = axis_x.labelsFont()
        f.setPointSize(9)
        axis_x.setLabelsFont(f)

        axis_y = QValueAxis()
        axis_y.setLabelFormat("$%.0f")
        axis_y.setMin(0)
        if bar_values:
            axis_y.setMax(max(bar_values) * 1.18)

        chart = QChart()
        chart.addSeries(series)
        chart.setTitle(chart_title)
        chart.setAnimationOptions(QChart.SeriesAnimations)
        chart.legend().setVisible(False)
        chart.addAxis(axis_x, Qt.AlignBottom)
        chart.addAxis(axis_y, Qt.AlignLeft)
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)
        chart.setContentsMargins(0, 0, 0, 0)
        chart.layout().setContentsMargins(0, 0, 0, 0)

        # \u2500\u2500 Hover tooltip (persistent) \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        _tt = _ChartTooltip()
        _bar_data = list(zip(bar_labels, bar_values))

        def _on_hover(status: bool, index: int, barset, bd=_bar_data, t=_tt, lbl=label):
            if status and 0 <= index < len(bd):
                lbl_x, amt = bd[index]
                t.show_at(
                    QCursor.pos(), f"<b>{lbl}</b><br>{lbl_x}<br><b>${amt:,.2f}</b>"
                )
            else:
                t.hide()

        series.hovered.connect(_on_hover)

        # \u2500\u2500 Chart view \u2014 width scales with number of bars \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        n = len(bar_labels)
        chart_w = max(560, n * 54 + 100)
        chart_h = 380

        chart_view = QChartView(chart)
        chart_view.setRenderHint(QPainter.Antialiasing)
        chart_view.setFixedHeight(chart_h)
        chart_view.setMinimumWidth(min(chart_w, 760))

        if chart_w > 760:
            scroll = QScrollArea()
            scroll.setWidget(chart_view)
            scroll.setWidgetResizable(False)
            scroll.setFixedHeight(
                chart_h + scroll.horizontalScrollBar().sizeHint().height() + 2
            )
            scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            chart_view.setFixedWidth(chart_w)
            layout.addWidget(scroll)
        else:
            chart_view.setFixedWidth(chart_w)
            layout.addWidget(chart_view)

        # \u2500\u2500 Summary stats row \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        total = sum(bar_values)
        avg = total / len(bar_values)
        peak_idx = bar_values.index(max(bar_values))
        if per_transaction:
            stats_text = (
                f"Total: <b>${total:,.2f}</b> &nbsp;\u2502&nbsp; "
                f"Payments: <b>{len(bar_values)}</b> &nbsp;\u2502&nbsp; "
                f"Avg per payment: <b>${avg:,.2f}</b> &nbsp;\u2502&nbsp; "
                f"Largest: <b>{bar_labels[peak_idx]}</b> (${max(bar_values):,.2f})"
            )
        else:
            stats_text = (
                f"Total: <b>${total:,.2f}</b> &nbsp;\u2502&nbsp; "
                f"Monthly avg: <b>${avg:,.2f}</b> &nbsp;\u2502&nbsp; "
                f"Peak month: <b>{bar_labels[peak_idx]}</b> (${max(bar_values):,.2f})"
            )
        stats_lbl = QLabel(stats_text)
        stats_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(stats_lbl)

        # \u2500\u2500 Close button \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
        btn_box = QDialogButtonBox(QDialogButtonBox.Close)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)


# ---------------------------------------------------------------------------
# Trash Tab
# ---------------------------------------------------------------------------


class TrashTab(QWidget):
    restore_requested = Signal(list)  # emits list of row dicts to restore

    def __init__(self, parent=None):
        super().__init__(parent)
        self._build_ui()
        self.refresh()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ── Header bar ────────────────────────────────────────────────
        hbar = QHBoxLayout()
        self._count_label = QLabel("0 items in trash")
        hbar.addWidget(self._count_label)
        hbar.addStretch()
        layout.addLayout(hbar)

        info = QLabel(
            "Transactions in the trash are excluded from all calculations and charts. "
            "Right-click a row to restore it."
        )
        info.setStyleSheet("color: #666; font-style: italic;")
        info.setWordWrap(True)
        layout.addWidget(info)

        # ── Transaction table ─────────────────────────────────────────
        self._model = TransactionModel([])
        self._view = QTableView()
        self._view.setModel(self._model)
        self._view.setAlternatingRowColors(True)
        self._view.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._view.setSortingEnabled(True)
        _trash_hdr = self._view.horizontalHeader()
        for _c in range(9):
            _trash_hdr.setSectionResizeMode(_c, QHeaderView.Interactive)
        _trash_hdr.setStretchLastSection(False)
        _restore_column_widths(
            _trash_hdr, "trash_view", [90, 220, 90, 130, 130, 80, 160, 110, 70]
        )
        _trash_hdr.sectionResized.connect(
            lambda *_: _save_column_widths(_trash_hdr, "trash_view")
        )
        self._view.verticalHeader().hide()
        self._view.setContextMenuPolicy(Qt.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        layout.addWidget(self._view)

    def refresh(self):
        """Reload trash from disk and refresh the table."""
        rows = load_trash()
        self._model.set_rows(rows)
        n = len(rows)
        self._count_label.setText(f"{n} item{'s' if n != 1 else ''} in trash")

    def _on_context_menu(self, pos) -> None:
        index = self._view.indexAt(pos)
        if not index.isValid():
            return
        selected_indexes = self._view.selectionModel().selectedRows()
        if selected_indexes:
            target_rows = [self._model.get_row(i.row()) for i in selected_indexes]
        else:
            target_rows = [self._model.get_row(index.row())]
        n = len(target_rows)
        suffix = f"{n} transaction{'s' if n > 1 else ''}"

        menu = QMenu(self)
        restore_action = menu.addAction(f"Restore {suffix}")
        chosen = menu.exec(self._view.viewport().mapToGlobal(pos))

        if chosen is restore_action:
            self.restore_requested.emit(target_rows)


# ---------------------------------------------------------------------------
# Coverage Tab
# ---------------------------------------------------------------------------


class CoverageTab(QWidget):
    """Tab visualizing statement file coverage over time with three interactive views:

    1. Gantt timeline  — per-file bars, gap bands, overlap highlights, today marker
    2. Monthly heatmap — calendar grid coloured by per-month transaction density
    3. Statement table  — sortable list with gap-after durations and file details
    """

    def __init__(self, rows: list[dict]):
        super().__init__()
        self._rows = rows
        self._entries: dict[str, list] = {}
        self._txn_counts: dict[str, int] = {}
        self._gantt_scroll: QScrollArea | None = None
        self._build_ui()
        self.update_data(rows)

    def _build_ui(self) -> None:
        self.setStyleSheet("CoverageTab { background-color: #8a9bb0; }")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(10)

        # ── Header ─────────────────────────────────────────────────────────
        hdr_widget = QWidget()
        hdr_inner = QHBoxLayout(hdr_widget)
        hdr_inner.setContentsMargins(2, 4, 2, 4)
        title = QLabel("Statement Data Coverage")
        fnt = title.font()
        fnt.setPointSize(12)
        fnt.setBold(True)
        title.setFont(fnt)
        title.setStyleSheet("color: #0d1b2a;")
        hdr_inner.addWidget(title)
        hdr_inner.addStretch()
        self._summary_lbl = QLabel()
        self._summary_lbl.setStyleSheet("font-size: 11px; color: #1a2a3a;")
        hdr_inner.addWidget(self._summary_lbl)
        outer.addWidget(hdr_widget)

        # ── Legend ─────────────────────────────────────────────────────────
        outer.addWidget(self._build_legend())

        # ── Gantt timeline ─────────────────────────────────────────────────
        gantt_card = QFrame()
        gantt_card.setStyleSheet(
            "QFrame { background: #8a9bb0; border: 1px solid #c8cdd3;"
            " border-radius: 8px; }"
        )
        gantt_vbox = QVBoxLayout(gantt_card)
        gantt_vbox.setContentsMargins(10, 8, 10, 8)
        gantt_vbox.setSpacing(6)
        tl_hdr = QLabel("Timeline")
        tl_hdr.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #0d1b2a;"
            " padding: 4px 0 2px 0; border-bottom: 1px solid #9e9e9e;"
        )
        gantt_vbox.addWidget(tl_hdr)
        self._gantt = _CoverageGanttWidget()
        g_scroll = QScrollArea()
        g_scroll.setWidget(self._gantt)
        g_scroll.setWidgetResizable(True)
        g_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        g_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        g_scroll.setFrameShape(QFrame.NoFrame)
        _sb_style = (
            "QScrollBar:horizontal { background: #8a9bb0; height: 10px; margin: 0; }"
            "QScrollBar::handle:horizontal { background: #2e3b4a; border-radius: 4px; min-width: 20px; }"
            "QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }"
        )
        g_scroll.setStyleSheet("QScrollArea { background: #8a9bb0; } " + _sb_style)
        g_scroll.viewport().setStyleSheet("background: #8a9bb0;")
        self._gantt_scroll = g_scroll
        gantt_vbox.addWidget(g_scroll)
        outer.addWidget(gantt_card)

        # ── Bottom: heatmap (left) + files table (right) ───────────────────
        bottom = QSplitter(Qt.Horizontal)
        bottom.setChildrenCollapsible(False)

        # Heatmap card
        hm_card = QFrame()
        hm_card.setStyleSheet(
            "QFrame { background: #8a9bb0; border: 1px solid #c8cdd3;"
            " border-radius: 8px; }"
        )
        hm_vbox = QVBoxLayout(hm_card)
        hm_vbox.setContentsMargins(10, 8, 10, 8)
        hm_vbox.setSpacing(4)
        hm_hdr = QLabel("Monthly Transaction Density")
        hm_hdr.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #0d1b2a;"
            " padding: 4px 0 2px 0; border-bottom: 1px solid #9e9e9e;"
        )
        hm_vbox.addWidget(hm_hdr)
        self._heatmap = _MonthlyHeatmapWidget()
        hm_scroll = QScrollArea()
        hm_scroll.setWidget(self._heatmap)
        hm_scroll.setWidgetResizable(False)
        hm_scroll.setAlignment(Qt.AlignCenter)
        hm_scroll.setFrameShape(QFrame.NoFrame)
        hm_scroll.setStyleSheet("QScrollArea { background: #8a9bb0; } " + _sb_style)
        hm_scroll.viewport().setStyleSheet("background: #8a9bb0;")
        hm_vbox.addWidget(hm_scroll)
        bottom.addWidget(hm_card)

        # Files table card
        ft_card = QFrame()
        ft_card.setStyleSheet(
            "QFrame { background: #8a9bb0; border: 1px solid #c8cdd3;"
            " border-radius: 8px; }"
        )
        ft_vbox = QVBoxLayout(ft_card)
        ft_vbox.setContentsMargins(10, 8, 10, 8)
        ft_vbox.setSpacing(4)
        ft_hdr = QLabel("Statement Files")
        ft_hdr.setStyleSheet(
            "font-weight: bold; font-size: 12px; color: #0d1b2a;"
            " padding: 4px 0 2px 0; border-bottom: 1px solid #9e9e9e;"
        )
        ft_vbox.addWidget(ft_hdr)
        self._files_table = QTableWidget(0, 7)
        self._files_table.setHorizontalHeaderLabels(
            ["Type", "Filename", "Start", "End", "Days", "Transactions", "Gap After"]
        )
        self._files_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._files_table.setAlternatingRowColors(True)
        self._files_table.verticalHeader().hide()
        self._files_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._files_table.setStyleSheet(
            "QTableWidget { border: none; font-size: 11px; background: #8a9bb0; color: #0d1b2a; }"
            "QTableWidget::item { background: #8a9bb0; color: #0d1b2a; }"
            "QTableWidget::item:alternate { background: #7a8fa3; color: #0d1b2a; }"
            "QTableWidget::item:selected { background: #b8d4ea; color: #1a1a1a; }"
            "QHeaderView::section { background: #dde1e7; color: #0d1b2a;"
            " font-weight: bold; padding: 4px; border: none;"
            " border-bottom: 1px solid #2e3b4a; }"
        )
        fhdr = self._files_table.horizontalHeader()
        fhdr.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3, 4, 5, 6):
            fhdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self._files_table.setSortingEnabled(True)
        ft_vbox.addWidget(self._files_table)
        bottom.addWidget(ft_card)

        bottom.setStretchFactor(0, 3)
        bottom.setStretchFactor(1, 2)
        outer.addWidget(bottom, stretch=1)

    def _build_legend(self) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            "QFrame { background: #dfe3e8; border: 1px solid #c8cdd3;"
            " border-radius: 6px; }"
        )
        row = QHBoxLayout(frame)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(16)

        def _swatch(style: str, label: str) -> None:
            sw = QLabel()
            sw.setFixedSize(13, 13)
            sw.setStyleSheet(style)
            lbl = QLabel(label)
            lbl.setStyleSheet("font-size: 10px; color: #555;")
            row.addWidget(sw)
            row.addWidget(lbl)

        _swatch(
            "background: #8fb2cc; border: 1px solid #2e3b4a; border-radius: 2px;",
            "Credit covered",
        )
        _swatch(
            "background: #95c18e; border: 1px solid #2e3b4a; border-radius: 2px;",
            "Debit covered",
        )
        _swatch(
            "background: #d5dae0; border: 1px solid #2e3b4a; border-radius: 2px;",
            "No data",
        )
        _swatch(
            "background: #d5dae0; border: 2px solid #d32f2f; border-radius: 2px;",
            "Current month",
        )
        row.addStretch()
        return frame

    def update_data(self, rows: list[dict]) -> None:
        from collections import defaultdict as _dd

        self._rows = rows
        self._entries = _scan_statement_periods_full(STATEMENTS_DIR)
        # Count transactions per source_file, then aggregate per deduplicated group
        raw_counts: dict[str, int] = _dd(int)
        for row in rows:
            sf = row.get("source_file", "")
            if sf:
                raw_counts[sf] += 1
        # Build per-primary-fname count summing all files in each dedup group
        self._txn_counts = {}
        for kind, elist in self._entries.items():
            for item in elist:
                fname = item[2]
                all_fnames = item[4]
                self._txn_counts[fname] = sum(raw_counts.get(f, 0) for f in all_fnames)
        self._gantt.set_data(self._entries, self._txn_counts, rows)
        if self._gantt_scroll is not None:
            self._gantt_scroll.setFixedHeight(self._gantt.height() + 4)
        self._heatmap.set_data(self._entries, rows)
        self._populate_files_table()
        self._update_summary()

    def _update_summary(self) -> None:
        n_c = len(self._entries.get("credit", []))
        n_d = len(self._entries.get("debit", []))
        all_dates: list[QDate] = []
        for elist in self._entries.values():
            for d1, d2, *_ in elist:
                all_dates.extend([d1, d2])
        if all_dates:
            dmin = min(all_dates, key=lambda d: d.toJulianDay())
            dmax = max(all_dates, key=lambda d: d.toJulianDay())
            span = f"{dmin.toString('MMM yyyy')} – {dmax.toString('MMM yyyy')}"
        else:
            span = "No files found"
        n_gaps = sum(len(v) for v in self._gantt._gaps.values())
        n_ovs = sum(len(v) for v in self._gantt._overlaps.values())
        parts = [
            f"{n_c} credit file{'s' if n_c != 1 else ''}",
            f"{n_d} debit file{'s' if n_d != 1 else ''}",
            span,
        ]
        if n_gaps:
            parts.append(f"⚠ {n_gaps} gap{'s' if n_gaps != 1 else ''}")
        if n_ovs:
            parts.append(f"↔ {n_ovs} overlap{'s' if n_ovs != 1 else ''}")
        self._summary_lbl.setText("   |   ".join(parts))

    def _populate_files_table(self) -> None:
        self._files_table.setSortingEnabled(False)
        self._files_table.setRowCount(0)
        rows_data = []
        for kind, elist in self._entries.items():
            for i, item in enumerate(elist):
                d1, d2, fname, path, all_fnames = (
                    item[0],
                    item[1],
                    item[2],
                    item[3],
                    item[4],
                )
                gap_after = 0
                if i + 1 < len(elist):
                    nxt = elist[i + 1][0]
                    if nxt > d2.addDays(1):
                        gap_after = d2.daysTo(nxt) - 1
                rows_data.append((kind, d1, d2, fname, path, all_fnames, gap_after))
        rows_data.sort(key=lambda x: x[1].toJulianDay())

        for kind, d1, d2, fname, path, all_fnames, gap_after in rows_data:
            r = self._files_table.rowCount()
            self._files_table.insertRow(r)
            count = self._txn_counts.get(fname, 0)
            dur = d1.daysTo(d2) + 1
            is_wide = dur > 60

            ti = QTableWidgetItem(kind.capitalize())
            ti.setForeground(
                QColor("#1565c0") if kind == "credit" else QColor("#2e7d32")
            )
            fnt = QFont()
            fnt.setBold(True)
            ti.setFont(fnt)
            self._files_table.setItem(r, 0, ti)

            aliases = all_fnames[1:] if len(all_fnames) > 1 else []
            ni = QTableWidgetItem(fname)
            ni.setToolTip(
                str(path)
                + ("\n\nAlso available as:\n" + "\n".join(aliases) if aliases else "")
            )
            if is_wide:
                ni.setForeground(QColor("#7c5e00"))
                ni.setText(f"📅 {fname}")
                ni.setToolTip(
                    f"{str(path)}\n\nWide-span file ({dur} days) — "
                    "likely an annual or multi-month export"
                )
            if aliases:
                ni.setBackground(QColor("#fff9c4"))
            self._files_table.setItem(r, 1, ni)

            self._files_table.setItem(r, 2, QTableWidgetItem(d1.toString("yyyy-MM-dd")))
            self._files_table.setItem(r, 3, QTableWidgetItem(d2.toString("yyyy-MM-dd")))

            di = QTableWidgetItem()
            di.setData(Qt.DisplayRole, dur)
            if is_wide:
                di.setForeground(QColor("#7c5e00"))
            self._files_table.setItem(r, 4, di)

            ci = QTableWidgetItem()
            ci.setData(Qt.DisplayRole, count)
            if count == 0:
                ci.setForeground(QColor("#c62828"))
                ci.setToolTip(
                    "No transactions found — file may not have been enriched yet"
                )
            self._files_table.setItem(r, 5, ci)

            gi = QTableWidgetItem()
            if gap_after > 0:
                gi.setData(Qt.DisplayRole, gap_after)
                gi.setText(f"{gap_after}d")
                gi.setForeground(QColor("#c62828"))
                gi.setToolTip(
                    f"{gap_after} days of missing {kind} data before "
                    "the next statement begins"
                )
            else:
                gi.setText("—")
            self._files_table.setItem(r, 6, gi)

        self._files_table.setSortingEnabled(True)


# ---------------------------------------------------------------------------
# Main Window
# ---------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Budget Tracker")
        self.resize(1200, 800)
        self._rows: list[dict] = []
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(2000)  # 2 s debounce after CSV write
        self._reload_timer.timeout.connect(self._load)
        self._enriching = False  # True while background enrichment is running
        self._plaid_worker: _PlaidWorker | None = None
        self._watcher = QFileSystemWatcher(self)
        self._watcher.fileChanged.connect(self._on_csv_changed)

        # Watch statements directories — when new CSVs are placed there,
        # wait 30 s after the last drop and then run enrichment.
        self._enrich_timer = QTimer(self)
        self._enrich_timer.setSingleShot(True)
        self._enrich_timer.setInterval(30_000)  # 30 s of quiet → enrich
        self._enrich_timer.timeout.connect(self._run_enrich)
        self._watcher.directoryChanged.connect(self._on_statements_dir_changed)
        for _d in (STATEMENTS_DIR / "credit", STATEMENTS_DIR / "debit"):
            _d.mkdir(parents=True, exist_ok=True)
            self._watcher.addPath(str(_d))

        # Changes made in the iPhone app arrive as a file in the iCloud folder
        self._phone_timer = QTimer(self)
        self._phone_timer.setSingleShot(True)
        self._phone_timer.setInterval(1500)
        self._phone_timer.timeout.connect(self._apply_phone_edits)
        self._phone_watcher = QFileSystemWatcher(self)
        self._phone_watcher.directoryChanged.connect(
            lambda _path: self._phone_timer.start()
        )

        self._build_menu()
        self._build_ui()
        self._load()

    def _build_menu(self):
        bar = self.menuBar()
        file_menu = bar.addMenu("&File")

        reload_action = QAction("&Reload CSV", self)
        reload_action.setShortcut("Ctrl+R")
        reload_action.triggered.connect(self._load)
        file_menu.addAction(reload_action)

        reenrich_action = QAction("Re-enrich from &Scratch…", self)
        reenrich_action.setShortcut("Ctrl+Shift+R")
        reenrich_action.triggered.connect(self._reenrich_from_scratch)
        file_menu.addAction(reenrich_action)

        file_menu.addSeparator()

        connect_bank_action = QAction("Connect &Bank…", self)
        connect_bank_action.triggered.connect(self._connect_bank)
        file_menu.addAction(connect_bank_action)

        sync_bank_action = QAction("S&ync Bank Now", self)
        sync_bank_action.triggered.connect(self._sync_bank)
        file_menu.addAction(sync_bank_action)

        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut("Ctrl+Q")
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = bar.addMenu("&Edit")
        manage_cats_action = QAction("&Manage Categories…", self)
        manage_cats_action.setShortcut("Ctrl+K")
        manage_cats_action.triggered.connect(self._open_category_manager)
        edit_menu.addAction(manage_cats_action)

        edit_cat_list_action = QAction("Edit Category &List…", self)
        edit_cat_list_action.setShortcut("Ctrl+L")
        edit_cat_list_action.triggered.connect(self._open_category_list_editor)
        edit_menu.addAction(edit_cat_list_action)

    def _open_category_manager(self) -> None:
        """Open the category-manager dialog; apply any changes immediately."""
        if not self._rows:
            QMessageBox.information(self, "No data", "Load transactions first.")
            return
        dlg = CategoryManagerDialog(self._rows, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        changes = dlg._pending  # {merchant: new_category}
        if not changes:
            return

        # Apply in memory + write corrections for every matching description
        n_updated = 0
        for row in self._rows:
            merchant = (
                row.get("gemini_merchant") or row.get("description") or "Unknown"
            ).strip()
            if merchant in changes:
                new_cat = changes[merchant]
                if row.get("gemini_category") != new_cat:
                    row["gemini_category"] = new_cat
                    save_correction(row.get("description", ""), new_cat)
                    n_updated += 1

        if n_updated:
            save_csv(self._rows)
            # Propagate to all tabs
            if self._summary_tab:
                self._summary_tab.update_data(self._rows)
            if self._txn_tab:
                self._txn_tab.update_data(self._rows)
            if self._stats_tab:
                self._stats_tab.update_data(self._rows)
            if self._income_tab:
                self._income_tab.update_data(self._rows)
            self.statusBar().showMessage(
                f"Category changes applied to {n_updated} transactions across "
                f"{len(changes)} merchant(s).",
                7000,
            )

    def _open_category_list_editor(self) -> None:
        """Open the category list editor; save changes and recategorize if needed."""
        if not self._rows:
            QMessageBox.information(self, "No data", "Load transactions first.")
            return
        dlg = CategoryListEditor(self._rows, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return

        from src.enrich_transactions import save_custom_categories

        final = dlg.final_categories
        deleted = dlg.deleted_categories

        # Save the updated category list
        save_custom_categories(final)

        if not deleted:
            self.statusBar().showMessage("Category list saved.", 5000)
            return

        # Find descriptions that belong to deleted categories
        descs_to_recat = set()
        for row in self._rows:
            cat = (row.get("gemini_category") or "Other").strip()
            if cat in deleted:
                desc = row.get("description", "")
                if desc:
                    descs_to_recat.add(desc)

        if not descs_to_recat:
            self.statusBar().showMessage(
                "Category list saved (no transactions to recategorize).", 5000
            )
            return

        # Recategorize in a background thread
        if self._enriching:
            QMessageBox.information(self, "Busy", "Enrichment is already running.")
            return
        self._enriching = True
        self._enrich_start_time = time.time()
        self.statusBar().showMessage(
            f"🔄 Recategorizing {len(descs_to_recat)} descriptions from {len(deleted)} deleted categor{'y' if len(deleted) == 1 else 'ies'}…",
            0,
        )

        worker = _RecatWorker(list(descs_to_recat), parent=self)

        def _on_progress(phase: str, detail: str) -> None:
            elapsed = time.time() - self._enrich_start_time
            mins, secs = divmod(int(elapsed), 60)
            self.statusBar().showMessage(f"🔄 [{mins}:{secs:02d}] {phase}: {detail}", 0)

        def _on_done() -> None:
            self._enriching = False
            elapsed = time.time() - self._enrich_start_time
            mins, secs = divmod(int(elapsed), 60)
            self.statusBar().showMessage(
                f"✓ Recategorized {len(descs_to_recat)} descriptions in {mins}m {secs:02d}s — reloading…",
                10_000,
            )

        def _on_error(msg: str) -> None:
            self._enriching = False
            self.statusBar().showMessage(f"⚠️ Recategorization failed: {msg}", 0)

        worker.progress.connect(_on_progress)
        worker.finished_ok.connect(_on_done)
        worker.finished_err.connect(_on_error)
        self._recat_worker = worker
        worker.start()

    def _build_ui(self):
        self._tabs = QTabWidget()
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self.setCentralWidget(self._tabs)

        # Placeholder tabs — replaced after first load
        self._summary_tab: SummaryTab | None = None
        self._txn_tab: TransactionsTab | None = None
        self._stats_tab: StatisticsTab | None = None
        self._income_tab: IncomeTab | None = None
        self._trash_tab: TrashTab | None = None
        self._coverage_tab: CoverageTab | None = None

        self.setStatusBar(QStatusBar())

    def _on_csv_changed(self, path: str) -> None:
        """Re-add the file to the watcher (editors replace files) and debounce reload."""
        if OUTPUT_FILE.exists():
            self._watcher.addPath(str(OUTPUT_FILE))
        self._reload_timer.start()  # restart debounce timer

    def _on_statements_dir_changed(self, _path: str) -> None:
        """New file detected in statements dir — (re)start 30-second enrichment debounce."""
        self.statusBar().showMessage("New statements detected — enriching in 30 s…", 0)
        self._enrich_timer.start()  # restart 30-second countdown

    # Caches to delete for a from-scratch re-enrichment.
    # User data (category_corrections, user_notes, trash, official_income_sources)
    # is intentionally preserved.
    # NOTE: OUTPUT_FILE is NOT deleted upfront — it's kept so the UI stays
    # functional while enrichment runs.  The enrichment pipeline overwrites it
    # atomically when it finishes.
    _ENRICH_CACHES: list[Path] = [
        PROJECT_ROOT / "data" / "places_cache.json",
        PROJECT_ROOT / "data" / "summary_descriptions.json",
        PROJECT_ROOT / "data" / "category_emojis.json",
        PROJECT_ROOT / "data" / "ai_analysis_cache.json",
        PROJECT_ROOT / "data" / "txn_descriptions_cache.json",
    ]

    def _reenrich_from_scratch(self) -> None:
        """Clear all enrichment caches and re-run the full pipeline."""
        if self._enriching:
            QMessageBox.information(self, "Busy", "Enrichment is already running.")
            return
        reply = QMessageBox.question(
            self,
            "Re-enrich from scratch",
            "This will clear all enrichment caches and re-run the full "
            "Gemini + Google Places pipeline.\n\n"
            "Your manual category corrections, notes, and trashed items "
            "will be preserved.\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        deleted = []
        for p in self._ENRICH_CACHES:
            if p.exists():
                p.unlink()
                deleted.append(p.name)
        if deleted:
            print(f"[Re-enrich] Cleared caches: {', '.join(deleted)}")
        self._run_enrich()

    def _run_enrich(self) -> None:
        """Run the enrichment pipeline in a background QThread.

        When enrichment finishes it overwrites enriched_transactions.csv, which
        the existing QFileSystemWatcher catches and feeds into _reload_timer → _load().
        """
        if self._enriching:
            return  # already running
        self._enriching = True
        self._enrich_start_time = time.time()
        self._update_status_bar()

        worker = _EnrichWorker(STATEMENTS_DIR, parent=self)

        def _on_progress(phase: str, detail: str) -> None:
            elapsed = time.time() - self._enrich_start_time
            mins, secs = divmod(int(elapsed), 60)
            self.statusBar().showMessage(f"🔄 [{mins}:{secs:02d}] {phase}: {detail}", 0)

        def _on_done() -> None:
            self._enriching = False
            elapsed = time.time() - self._enrich_start_time
            mins, secs = divmod(int(elapsed), 60)
            self.statusBar().showMessage(
                f"✓ Enrichment complete in {mins}m {secs:02d}s — reloading…", 10_000
            )

        def _on_error(msg: str) -> None:
            self._enriching = False
            self.statusBar().showMessage(f"⚠️ Enrichment failed: {msg}", 0)

        worker.progress.connect(_on_progress)
        worker.finished_ok.connect(_on_done)
        worker.finished_err.connect(_on_error)
        # prevent GC of the worker while it's running
        self._enrich_worker = worker
        worker.start()

    @staticmethod
    def _linked_banks() -> list[str]:
        try:
            return plaid_sync.linked_banks()
        # A bad Plaid setting must not stop the app opening
        except Exception:  # noqa: BLE001
            return []

    def _plaid_busy(self) -> bool:
        return self._plaid_worker is not None and self._plaid_worker.isRunning()

    def _connect_bank(self) -> None:
        """Open the Plaid bank login page in the browser, then import the transactions."""
        if self._plaid_busy():
            if self._plaid_worker.link:
                reply = QMessageBox.question(
                    self,
                    "Bank login in progress",
                    "A bank login page is already open in your browser.\n\nCancel it?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply == QMessageBox.Yes:
                    self._plaid_worker.requestInterruption()
            return
        banks = self._linked_banks()
        if banks:
            reply = QMessageBox.question(
                self,
                "Bank already connected",
                f"{', '.join(banks)} is already connected. Connecting the same bank "
                "twice creates a duplicate connection.\n\nConnect another bank?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
        self._start_plaid(link=True)

    def _sync_bank(self) -> None:
        if self._plaid_busy():
            return
        if not self._linked_banks():
            QMessageBox.information(
                self, "No bank connected", "Use File > Connect Bank first."
            )
            return
        self._start_plaid(link=False)

    def _start_plaid(self, link: bool, quiet: bool = False) -> None:
        """Run a bank link/sync in the background, then enrich whatever it brought in."""
        worker = _PlaidWorker(link, parent=self)
        self.statusBar().showMessage(
            (
                "Opening the bank login page…"
                if link
                else "🔄 Checking the bank for new transactions…"
            ),
            0,
        )

        def _enrich_if_needed() -> None:
            if self._statements_newer_than_output():
                self._enrich_timer.stop()
                self._run_enrich()

        def _on_link_ready(url: str) -> None:
            QDesktopServices.openUrl(QUrl(url))
            self.statusBar().showMessage(
                "Finish the bank login in your browser — transactions import automatically.",
                0,
            )

        def _on_done(new_rows: int, complete: bool) -> None:
            msg = f"✓ Bank sync: {new_rows} new transaction(s)."
            if plaid_sync.environment() != "production":
                msg += " Practice mode: saved separately, not added to your data."
            if not complete:
                msg += " Older history is still loading; sync again in a few minutes."
            self.statusBar().showMessage(msg, 15_000)
            _enrich_if_needed()

        def _on_error(msg: str) -> None:
            self.statusBar().showMessage(f"⚠️ Bank sync failed: {msg}", 0)
            if not quiet:
                QMessageBox.warning(self, "Bank sync failed", msg)
            _enrich_if_needed()

        worker.link_ready.connect(_on_link_ready)
        worker.finished_ok.connect(_on_done)
        worker.finished_err.connect(_on_error)
        worker.cancelled.connect(
            lambda: self.statusBar().showMessage("Bank login cancelled.", 5000)
        )
        self._plaid_worker = worker
        worker.start()

    def closeEvent(self, event) -> None:
        # A bank login can wait on the browser for minutes; stop it before the window goes
        if self._plaid_busy():
            self._plaid_worker.requestInterruption()
            self._plaid_worker.wait(5000)
        super().closeEvent(event)

    def _update_status_bar(self) -> None:
        """Show the correct status bar message depending on current state."""
        if self._enriching:
            self.statusBar().showMessage(
                "🔄 Enriching new statements — UI will refresh automatically when complete…",
                0,
            )
        else:
            n = len(self._rows)
            if n:
                self.statusBar().showMessage(
                    f"Loaded {n} transactions from {OUTPUT_FILE.name}", 5000
                )

    @staticmethod
    def _statements_newer_than_output() -> bool:
        """Return True if any statement CSV is newer than enriched_transactions.csv.
        Used on startup to detect statements that arrived while the app was closed.
        """
        if not OUTPUT_FILE.exists():
            return True
        output_mtime = OUTPUT_FILE.stat().st_mtime
        return any(
            p.stat().st_mtime > output_mtime for p in STATEMENTS_DIR.rglob("*.csv")
        )

    def _load(self):
        _reload_ai_emojis()
        rows = load_csv()
        self._rows = rows
        # Watch the output CSV for external changes (enrichment pipeline)
        if OUTPUT_FILE.exists() and str(OUTPUT_FILE) not in self._watcher.files():
            self._watcher.addPath(str(OUTPUT_FILE))
        if not rows:
            self.statusBar().showMessage(f"No data found at {OUTPUT_FILE}", 0)
            return
        export_for_phone(rows)

        if self._summary_tab is None:
            # First load — create tabs
            # Kick off enrichment immediately if statements are newer than the cached CSV
            if self._linked_banks():
                # Enrichment follows once the bank's new transactions are in
                self._start_plaid(link=False, quiet=True)
            elif self._statements_newer_than_output():
                self._run_enrich()
            self._summary_tab = SummaryTab(rows)
            self._txn_tab = TransactionsTab(rows)
            self._stats_tab = StatisticsTab(rows)
            self._income_tab = IncomeTab(rows)
            self._coverage_tab = CoverageTab(rows)
            self._trash_tab = TrashTab()
            self._tabs.addTab(self._summary_tab, "📈 Summary")
            self._tabs.addTab(self._txn_tab, "💳 Transactions")
            self._tabs.addTab(self._stats_tab, "📉 Statistics")
            self._tabs.addTab(self._income_tab, "💰 Income & Inflows")
            self._tabs.addTab(self._coverage_tab, "📊 Coverage")
            self._tabs.addTab(self._trash_tab, "🗑 Trash")
            # Connect trash signals
            self._txn_tab.trash_requested.connect(self._on_trash_requested)
            self._trash_tab.restore_requested.connect(self._on_restore_requested)
        else:
            # Subsequent reload — push new data to all tabs
            self._summary_tab.update_data(rows)
            self._txn_tab.update_data(rows)
            self._stats_tab.update_data(rows)
            self._income_tab.update_data(rows)
            self._coverage_tab.update_data(rows)
            self._trash_tab.refresh()

        self._update_status_bar()
        self._apply_phone_edits()
        if PHONE_FOLDER.is_dir() and not self._phone_watcher.directories():
            self._phone_watcher.addPath(str(PHONE_FOLDER))

    def _apply_phone_edits(self) -> None:
        """Make the changes requested from the iPhone app and show them in every tab."""
        if self._summary_tab is None:
            return
        applied = apply_phone_edits(self._rows)
        if not applied:
            return
        save_csv(self._rows)
        self._income_tab._official_sources = _load_official_sources()
        self._all_tabs_update()
        self._trash_tab.refresh()
        self.statusBar().showMessage(
            f"{applied} change{'s' if applied > 1 else ''} from the iPhone app applied.",
            7000,
        )

    def _on_tab_changed(self, index: int) -> None:
        """Reset drill-downs when leaving Summary or Transactions tabs."""
        widget = self._tabs.widget(index)
        if self._txn_tab is not None and widget is not self._txn_tab:
            self._txn_tab.reset_to_root()
        if self._summary_tab is not None and widget is not self._summary_tab:
            self._summary_tab.reset_to_root()

    def _all_tabs_update(self) -> None:
        """Push the current self._rows to every non-trash tab."""
        if self._summary_tab:
            self._summary_tab.update_data(self._rows)
        if self._txn_tab:
            self._txn_tab.update_data(self._rows)
        if self._stats_tab:
            self._stats_tab.update_data(self._rows)
        if self._income_tab:
            self._income_tab.update_data(self._rows)

    def _on_trash_requested(self, rows_to_trash: list) -> None:
        """Move selected transactions to the trash."""
        keys = {_tx_key(r) for r in rows_to_trash}
        self._rows = [r for r in self._rows if _tx_key(r) not in keys]
        save_csv(self._rows)
        trash = load_trash()
        trash.extend(rows_to_trash)
        save_trash(trash)
        self._all_tabs_update()
        self._trash_tab.refresh()
        n = len(rows_to_trash)
        self.statusBar().showMessage(
            f"{n} transaction{'s' if n > 1 else ''} moved to trash.", 5000
        )

    def _on_restore_requested(self, rows_to_restore: list) -> None:
        """Restore trashed transactions back to enriched_transactions.csv."""
        keys = {_tx_key(r) for r in rows_to_restore}
        trash = [r for r in load_trash() if _tx_key(r) not in keys]
        save_trash(trash)
        self._rows.extend(rows_to_restore)
        save_csv(self._rows)
        self._all_tabs_update()
        self._trash_tab.refresh()
        n = len(rows_to_restore)
        self.statusBar().showMessage(
            f"{n} transaction{'s' if n > 1 else ''} restored.", 5000
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    import sys

    app = QApplication(sys.argv)
    app.setApplicationName("BudgetTracker")
    app.setStyle("Fusion")
    window = MainWindow()
    window.showMaximized()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
