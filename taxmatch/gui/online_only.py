"""Καθοδηγούμενη λήψη «μόνο online» παραστατικών μέσω του browser του χρήστη — port του Timologio Downloader
(`desktop/src/timologio/gui/online_only.py`), ρητό αίτημα χρήστη 2026-10-27: «να στηθεί όπως στο timologio downloader,
να ορίζεται φάκελος όπου κατεβαίνει το PDF και να το παίρνει μετά το πρόγραμμα στη βάση του».

Κάποιοι πάροχοι (eskap, Epsilon…) δείχνουν το παραστατικό μόνο σε δική τους online προβολή, συχνά πίσω από έλεγχο
«είστε άνθρωπος». Δεν τον παρακάμπτουμε: (1) ανοίγει στον ΔΙΚΟ του browser, ο χρήστης το αποθηκεύει ως PDF
(Ctrl+P → «Αποθήκευση ως PDF» ή το κουμπί του παρόχου)· (2) παρακολουθούμε τον φάκελο λήψεων (ρύθμιση
`online_download_dir`, αλλιώς «Λήψεις») και μόλις εμφανιστεί νέο, ΟΛΟΚΛΗΡΩΜΕΝΟ PDF το αρχειοθετούμε στον φάκελο του
πελάτη (`pdfs.save_online_pdf`: ίδιο όνομα με τη «Λήψη μήνα», σήμανση «PDF» στη βάση) και σβήνουμε το διπλότυπο.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import QStandardPaths, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QFileDialog, QHBoxLayout, QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import settings_store
from ..mydata import pdfs
from .table_kit import SortItem, TableKit
from .theme import CURRENT
from .toast import toast
from .widgets import fit_to_screen

log = logging.getLogger(__name__)

_POLL_MS = 1500
_STATUS, _DATE, _PARTY, _HOST, _ERR = range(5)


def downloads_dir(conn: sqlite3.Connection) -> Path:
    chosen = settings_store.get(conn, "online_download_dir")
    if chosen and Path(chosen).is_dir():
        return Path(chosen)
    loc = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)
    return Path(loc) if loc else Path.home() / "Downloads"


def _gr_date(iso: str) -> str:
    return f"{iso[8:10]}/{iso[5:7]}/{iso[:4]}" if iso and len(iso) >= 10 else (iso or "—")


class OnlineOnlyDialog(QDialog):
    """Άνοιγμα στον browser → αυτόματη αρχειοθέτηση από τον φάκελο λήψεων· παράκαμψη / σήμανση σφάλματος."""

    def __init__(self, conn: sqlite3.Connection, afm: str, docs: list[dict[str, Any]], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.conn, self.afm = conn, afm
        self._docs = [d for d in docs if d.get("url")]
        self._downloads = downloads_dir(conn)
        self._done: set[str] = set()
        self._errors: set[str] = set()
        self._skipped: set[str] = set()
        self._watch: str | None = None
        self._snapshot: dict[str, float] = {}
        self._opened_at = 0.0
        self._candidate: tuple[str, int] | None = None
        self._loading = False
        self.setWindowTitle("Παραστατικά «μόνο online» — λήψη μέσω του browser σας")
        self._build()
        self._populate()
        self._timer = QTimer(self)
        self._timer.setInterval(_POLL_MS)
        self._timer.timeout.connect(self._scan)
        fit_to_screen(self, 860, 560)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        root = QVBoxLayout(self)
        intro = QLabel("Αυτά τα παραστατικά ο πάροχος τα δείχνει <b>μόνο online</b>. Επιλέξτε γραμμή και <b>«Άνοιγμα στον "
                       "browser»</b> (αλλιώς ανοίγει το επόμενο): στον browser σας αποθηκεύστε το ως PDF (Ctrl+P → "
                       "«Αποθήκευση ως PDF» ή το κουμπί του παρόχου) <b>στον φάκελο λήψεων</b> παρακάτω — η εφαρμογή το "
                       "παίρνει <b>μόνη της</b>, το αρχειοθετεί στον φάκελο του πελάτη και το σημειώνει «PDF». Αν κάτι "
                       "δεν ανοίγει: «Παράκαμψη» ή τσεκάρετε «Σφάλμα».")
        intro.setWordWrap(True)
        intro.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(intro)
        self.table = QTableWidget(0, 5)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.kit = TableKit(self.table, [("Κατάσταση", 170), ("Ημ/νία", 90), ("Αντισυμβαλλόμενος", 0), ("Πάροχος", 150),
                                         ("Σφάλμα", 70)], "online_only", filterable=range(0, 4), search=False)
        self.table.itemChanged.connect(self._error_toggled)
        root.addWidget(self.table, 1)
        folder = QHBoxLayout()
        self.folder_label = QLabel("")
        self.folder_label.setObjectName("muted")
        folder.addWidget(self.folder_label, 1)
        change = QPushButton("Αλλαγή φακέλου λήψεων…")
        change.setToolTip("Ο φάκελος όπου αποθηκεύει ο browser σας — θυμάται την επιλογή")
        change.clicked.connect(self._choose_folder)
        folder.addWidget(change)
        root.addLayout(folder)
        self.status = QLabel("")
        self.status.setStyleSheet("font-weight:600;")
        root.addWidget(self.status)
        buttons = QHBoxLayout()
        self.open_btn = QPushButton("Άνοιγμα στον browser")
        self.open_btn.setObjectName("primary")
        self.open_btn.clicked.connect(self._open_next)
        buttons.addWidget(self.open_btn)
        scan = QPushButton("Έλεγχος φακέλου τώρα")
        scan.setToolTip("Ψάχνει αμέσως το PDF που αποθηκεύσατε")
        scan.clicked.connect(lambda: self._scan(force=True))
        buttons.addWidget(scan)
        skip = QPushButton("Παράκαμψη →")
        skip.setToolTip("Μένει «μόνο online» για αργότερα· ανοίγει το επόμενο")
        skip.clicked.connect(self._skip)
        buttons.addWidget(skip)
        buttons.addStretch()
        close = QPushButton("Κλείσιμο")
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self._update_folder()

    def _party(self, d: dict) -> str:
        name, vat = pdfs.other_party(d, self.afm)
        return " · ".join(x for x in (name, vat) if x) or "—"

    def _state(self, mark: str) -> tuple[str, str]:
        if mark in self._done:
            return "✓ Αρχειοθετήθηκε", CURRENT.ok
        if mark in self._errors:
            return "✗ Σφάλμα", CURRENT.bad
        if mark in self._skipped:
            return "⤳ Παράκαμψη", CURRENT.muted
        if mark == self._watch:
            return "▶ Ανοιχτό — αποθηκεύστε το PDF", CURRENT.accent
        return "⧗ Αναμονή", ""

    def _populate(self) -> None:
        self._loading = True
        self.kit.begin()
        self.table.setRowCount(len(self._docs))
        for i, d in enumerate(self._docs):
            text, colour = self._state(d["mark"])
            status = SortItem(text)
            status.setData(Qt.ItemDataRole.UserRole, d["mark"])
            if colour:
                status.setForeground(QColor(colour))
            self.table.setItem(i, _STATUS, status)
            self.table.setItem(i, _DATE, SortItem(_gr_date(d["issue_date"])))
            self.table.setItem(i, _PARTY, SortItem(self._party(d)))
            from urllib.parse import urlparse
            self.table.setItem(i, _HOST, SortItem(urlparse(d["url"]).netloc or "—"))
            err = QTableWidgetItem()
            err.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            err.setCheckState(Qt.CheckState.Checked if d["mark"] in self._errors else Qt.CheckState.Unchecked)
            err.setData(Qt.ItemDataRole.UserRole, d["mark"])
            err.setToolTip("Σήμανση ως σφάλμα (δεν ανοίγει ή δεν υπάρχει PDF)")
            self.table.setItem(i, _ERR, err)
        self.kit.end()
        self._loading = False
        self._update_status()

    def _refresh(self) -> None:
        """Ενημέρωση επί τόπου — κρατά την ταξινόμηση του χρήστη (η σειρά επεξεργασίας ακολουθεί τον πίνακα)."""
        self._loading = True
        for i in range(self.table.rowCount()):
            item = self.table.item(i, _STATUS)
            if item is None:
                continue
            text, colour = self._state(item.data(Qt.ItemDataRole.UserRole))
            item.setText(text)
            item.setForeground(QColor(colour) if colour else QColor(CURRENT.txt))
        self._loading = False
        self._update_status()

    def _update_folder(self) -> None:
        self.folder_label.setText(f"Φάκελος λήψεων: {self._downloads}")

    def _update_status(self) -> None:
        parts = [f"Αρχειοθετήθηκαν {len(self._done)} από {len(self._docs)}"]
        if self._errors:
            parts.append(f"{len(self._errors)} σφάλματα")
        if self._watch:
            d = self._doc(self._watch)
            if d:
                parts.append(f"Αναμονή PDF: {self._party(d)}")
        self.status.setText("   ·   ".join(parts))

    def _doc(self, mark: str) -> dict | None:
        return next((d for d in self._docs if d["mark"] == mark), None)

    def _pending(self, mark: str) -> bool:
        return mark not in self._done and mark not in self._errors and mark not in self._skipped

    def _next_doc(self) -> dict | None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if rows:
            mark = self.table.item(rows[0], _STATUS).data(Qt.ItemDataRole.UserRole)
            if self._pending(mark):
                return self._doc(mark)
        for i in range(self.table.rowCount()):
            mark = self.table.item(i, _STATUS).data(Qt.ItemDataRole.UserRole)
            if self._pending(mark) and mark != self._watch:
                return self._doc(mark)
        return None

    # ------------------------------------------------------------------ ροή
    def _choose_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Φάκελος λήψεων του browser", str(self._downloads))
        if path:
            self._downloads = Path(path)
            settings_store.set_value(self.conn, "online_download_dir", path)
            self._update_folder()

    def _snapshot_pdfs(self) -> dict[str, float]:
        out: dict[str, float] = {}
        try:
            for p in self._downloads.glob("*.pdf"):
                try:
                    out[str(p)] = p.stat().st_mtime
                except OSError:
                    continue
        except OSError:
            pass
        return out

    def _open_next(self) -> None:
        d = self._next_doc()
        if d is None:
            self._timer.stop()
            self._watch = None
            toast(self, "Δεν έμειναν άλλα παραστατικά «μόνο online».", "info")
            self._refresh()
            return
        self._snapshot = self._snapshot_pdfs()
        self._opened_at = time.time()
        self._candidate = None
        self._watch = d["mark"]
        QDesktopServices.openUrl(QUrl(pdfs.openable_url(d["url"])))   # ρητό κλικ· capability URL του παρόχου
        self._timer.start()
        self._refresh()

    def _skip(self) -> None:
        d = self._doc(self._watch) if self._watch else self._next_doc()
        if d is None:
            return
        self._timer.stop()
        self._skipped.add(d["mark"])
        self._watch = None
        self._refresh()
        if self._next_doc() is not None:
            self._open_next()

    def _scan(self, force: bool = False) -> None:
        if self._watch is None:
            if force:
                toast(self, "Ανοίξτε πρώτα ένα παραστατικό στον browser.", "info")
            return
        found: list[tuple[float, Path]] = []
        for p in self._downloads.glob("*.pdf"):
            try:
                mtime = p.stat().st_mtime
            except OSError:
                continue
            prev = self._snapshot.get(str(p))
            if (prev is None or mtime > prev) and mtime >= self._opened_at - 1 and pdfs.is_complete_pdf(p):
                found.append((mtime, p))
        if not found:
            if force:
                toast(self, f"Δεν βρέθηκε νέο PDF στο {self._downloads}.", "warn")
            return
        newest = sorted(found)[-1][1]
        try:
            size = newest.stat().st_size
        except OSError:
            return
        if self._candidate != (str(newest), size) and not force:
            self._candidate = (str(newest), size)      # σταθερό μέγεθος σε δύο ελέγχους — όχι μισογραμμένο
            return
        self._file(newest)

    def _file(self, source: Path) -> None:
        d = self._doc(self._watch) if self._watch else None
        if d is None:
            return
        self._timer.stop()
        try:
            target = pdfs.save_online_pdf(self.conn, self.afm, d, source.read_bytes())
        except Exception as exc:                        # noqa: BLE001
            log.warning("μόνο online %s: %s", source, exc)
            toast(self, f"Το {source.name} δεν αρχειοθετήθηκε: {exc}", "danger")
            self._watch = None
            self._refresh()
            return
        self._done.add(d["mark"])
        self._watch = None
        self._candidate = None
        self._delete_download(source)
        toast(self, f"Αρχειοθετήθηκε: {target.name}", "ok")
        self._refresh()
        self.open_btn.setFocus()

    def _delete_download(self, source: Path, attempt: int = 0) -> None:
        """Το αντίγραφο στις «Λήψεις» είναι πλέον διπλότυπο· ο browser συχνά το κρατά ανοιχτό λίγο ακόμη (Windows),
        άρα ξαναπροσπαθούμε με καθυστέρηση — ποτέ σφάλμα."""
        try:
            source.unlink(missing_ok=True)
        except OSError:
            delays = (2, 5, 10, 20)
            if attempt < len(delays):
                QTimer.singleShot(delays[attempt] * 1000, lambda: self._delete_download(source, attempt + 1))

    def _error_toggled(self, item: QTableWidgetItem) -> None:
        if self._loading or item.column() != _ERR:
            return
        mark = item.data(Qt.ItemDataRole.UserRole)
        if item.checkState() == Qt.CheckState.Checked:
            self._errors.add(mark)
            self._skipped.discard(mark)
            if self._watch == mark:
                self._timer.stop()
                self._watch = None
            self.conn.execute("UPDATE mydata_docs SET pdf_status='failed' WHERE afm=? AND mark=?", (self.afm, mark))
        else:
            self._errors.discard(mark)
            self.conn.execute("UPDATE mydata_docs SET pdf_status='viewer' WHERE afm=? AND mark=?", (self.afm, mark))
        self._refresh()

    def done(self, result: int) -> None:                # noqa: D401 — Qt override (accept/reject)
        self._timer.stop()
        super().done(result)

    @property
    def changed(self) -> bool:
        return bool(self._done or self._errors)
