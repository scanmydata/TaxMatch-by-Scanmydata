"""Ομοιόμορφοι πίνακες (ρητό αίτημα χρήστη 2026-10-23: «όλοι οι πίνακες sortable, searchable, resizable και με το φίλτρο
στήλης που έχουμε στους πελάτες»): ταξινόμηση με κλικ στην επικεφαλίδα, πεδίο αναζήτησης, χωνί φίλτρου σε κάθε στήλη,
στήλες που σύρονται και θυμούνται το πλάτος τους, δεξί κλικ σε κελί (αντιγραφή / φίλτρο με την τιμή).

Χρήση:
    kit = TableKit(table, [("ΑΦΜ", 95), ("Επωνυμία", 0), …], "debts")   # 0 = η στήλη που παίρνει ό,τι περισσεύει
    root.addWidget(kit.bar)                                              # αναζήτηση + «Διαγραφή φίλτρων»
    root.addWidget(table)
    …
    with kit.filling():                                                  # ΠΑΝΤΑ γύρω από το γέμισμα του πίνακα:
        table.setRowCount(n); table.setItem(…)                            # με ενεργή ταξινόμηση οι γραμμές θα «έφευγαν»

Η ΜΟΝΗ συνάρτηση που αγγίζει `setRowHidden` είναι η `TableKit.apply` (βλ. σχόλιο του `TableColumnFilter`)· όποιος
χρειάζεται δικό του κριτήριο το δίνει ως `extra_filter(row) -> bool`.
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Callable, Iterable, Iterator, Optional

from PySide6.QtCore import QObject, QSettings, Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QHeaderView, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem, QWidget,
)

from .. import config, textutil
from .table_filter import TableColumnFilter, install_cell_menu
from .widgets import setup_columns


def ui_prefs() -> QSettings:
    """Ίδιες προτιμήσεις οθόνης με το κύριο παράθυρο (`MainWindow._prefs`) — και για διαλόγους χωρίς πρόσβαση σε αυτό."""
    if os.getenv("TAXMATCH_DATA_DIR"):
        return QSettings(str(config.data_dir() / "ui_prefs.ini"), QSettings.Format.IniFormat)
    return QSettings("scanmydata", "TaxMatch")


def _fold(text: str) -> str:
    return textutil.strip_accents(text or "").lower()


class TableKit(QObject):
    def __init__(self, table: QTableWidget, columns: list[tuple[str, int]], key: str, *,
                 prefs: Optional[QSettings] = None, filterable: Optional[Iterable[int]] = None, sortable: bool = True,
                 search: bool = True, cell_menu: bool = True, placeholder: str = "Αναζήτηση…",
                 extra_filter: Optional[Callable[[int], bool]] = None) -> None:
        super().__init__(table)
        self.table = table
        self.sortable = sortable
        self.extra_filter = extra_filter
        self._prefs = prefs or ui_prefs()
        table.setColumnCount(len(columns))
        table.setHorizontalHeaderLabels([c[0] for c in columns])
        cols = list(filterable) if filterable is not None else list(range(len(columns)))
        self.col_filter = TableColumnFilter(table, cols, apply_fn=self.apply)
        self.col_filter.filtersChanged.connect(self.apply)
        setup_columns(table, [(name, width, "") for name, width in columns], self._prefs, key)
        if not any(width == 0 for _n, width in columns):
            table.horizontalHeader().setStretchLastSection(True)
        # Χωρίς ένδειξη ταξινόμησης (-1): η σειρά του κώδικα μένει ώσπου ο χρήστης να πατήσει επικεφαλίδα — η
        # προεπιλογή της Qt (στήλη 0, φθίνουσα) θα ταξινομούσε μόνη της κάθε πίνακα με το που ανοίγει
        table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        table.setSortingEnabled(sortable)
        if cell_menu:
            install_cell_menu(table, self.col_filter)

        # ΜΕ γονέα (τον γονέα του πίνακα, αν έχει): ένα QWidget χωρίς γονέα που γίνεται setVisible(True) ανοίγει ως
        # ΞΕΧΩΡΙΣΤΟ παράθυρο για μια στιγμή, ώσπου να μπει στο layout — αναφορά χρήστη 2026-10-27: «ανοίγουν πρώτα 3-4
        # παράθυρα» στην εκκίνηση και στο διπλό κλικ σε πελάτη. Και ΠΟΤΕ setVisible(True) εδώ· μόνο απόκρυψη.
        self.bar = QWidget(table.parentWidget())
        row = QHBoxLayout(self.bar)
        row.setContentsMargins(0, 0, 0, 0)
        self.search = QLineEdit()
        self.search.setPlaceholderText(placeholder)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _t: self.apply())
        row.addWidget(self.search, 1)
        self.clear_btn = QPushButton("Διαγραφή φίλτρων")
        self.clear_btn.setToolTip("Σβήνει την αναζήτηση και όλα τα φίλτρα στηλών")
        self.clear_btn.clicked.connect(self.clear)
        self.clear_btn.setVisible(False)
        row.addWidget(self.clear_btn)
        if not search:
            self.bar.hide()

    @contextmanager
    def filling(self) -> Iterator[None]:
        """Γέμισμα πίνακα: χωρίς ταξινόμηση στη διάρκεια (αλλιώς το `setItem(i, …)` πάει σε λάθος γραμμή), μετά
        ξαναταξινόμηση με την ίδια στήλη/σειρά και ξαναφιλτράρισμα."""
        self.begin()
        try:
            yield
        finally:
            self.end()

    def begin(self) -> None:
        """Ίδιο με το `filling()` για μακριές μεθόδους γεμίσματος: `begin()` πριν, `end()` μετά."""
        self.table.setSortingEnabled(False)

    def end(self) -> None:
        if self.sortable:
            self.table.setSortingEnabled(True)              # ξαναταξινομεί με την τρέχουσα στήλη/σειρά της επικεφαλίδας
        self.apply()

    def apply(self) -> None:
        words = _fold(self.search.text()).split()
        filters = self.col_filter.filters
        t = self.table
        for r in range(t.rowCount()):
            ok = all(not allowed or (t.item(r, c).text() if t.item(r, c) else "") in allowed
                     for c, allowed in filters.items())
            if ok and words:
                hay = _fold(" ".join(t.item(r, c).text() for c in range(t.columnCount()) if t.item(r, c)))
                ok = all(w in hay for w in words)
            if ok and self.extra_filter is not None:
                ok = self.extra_filter(r)
            t.setRowHidden(r, not ok)
        active = len([c for c, v in filters.items() if v]) + (1 if words else 0)
        self.clear_btn.setVisible(bool(active))
        self.clear_btn.setText(f"Διαγραφή φίλτρων ({active})")

    def clear(self) -> None:
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self.col_filter.clear()
        self.apply()


def interactive(table: QTableWidget) -> None:
    """Για πίνακες καταχώρισης (επεξεργάσιμα κελιά — εκεί ταξινόμηση/φίλτρα θα «πετούσαν» τη γραμμή που γράφει ο
    χρήστης): μόνο στήλες που σύρονται."""
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)


class SortItem(QTableWidgetItem):
    """Κελί που ταξινομείται με τιμή (ποσά «1.234,56 €», ημερομηνίες «dd/mm/yyyy») και όχι αλφαβητικά με το κείμενο."""

    def __init__(self, text: str, key=None) -> None:
        super().__init__(text)
        self.key = key if key is not None else sort_key(text)

    def __lt__(self, other) -> bool:                     # noqa: D105
        mine, theirs = self.key, getattr(other, "key", None)
        if theirs is None:
            theirs = sort_key(other.text())
        try:
            return mine < theirs
        except TypeError:
            return str(mine) < str(theirs)


_DATE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})")
_MONEY = re.compile(r"^-?[\d.]+(,\d+)?\s*€?$")


def sort_key(text: str):
    """«31/12/2026» → (1, '2026-12-31')· «1.234,56 €» → (0, 1234.56)· αλλιώς (2, κείμενο χωρίς τόνους)."""
    t = (text or "").strip()
    m = _DATE.match(t)
    if m:
        return (1, f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}")
    if t and _MONEY.match(t):
        try:
            return (0, float(t.replace("€", "").replace(".", "").replace(",", ".").strip()))
        except ValueError:
            pass
    return (2, _fold(t))
