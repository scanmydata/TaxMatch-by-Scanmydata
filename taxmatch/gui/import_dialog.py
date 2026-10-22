"""Προεπισκόπηση εισαγωγής Excel ΠΡΙΝ γραφτεί οτιδήποτε — όπως το `import_dialog.py` του Timologio Downloader (ρητό
αίτημα χρήστη 2026-10-22): ποιοι πελάτες είναι νέοι, ποιοι ΥΠΑΡΧΟΥΝ ΗΔΗ και θα ενημερωθούν (και με τι), ποιοι μένουν
ίδιοι. Οι κωδικοί δεν εμφανίζονται ποτέ — μόνο ότι υπάρχουν/αλλάζουν."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from ..business_profiles.import_excel import ImportResult
from .theme import CURRENT

_COLS = ["ΑΦΜ", "Επωνυμία", "Κωδικοί στο αρχείο", "Ενέργεια", "Τι αλλάζει"]
ACTION_LABEL = {"new": "Νέος πελάτης", "update": "Υπάρχει ήδη — θα ενημερωθεί", "same": "Υπάρχει ήδη — χωρίς αλλαγές"}


def summary_text(preview: list[dict[str, Any]]) -> str:
    n = {k: sum(1 for p in preview if p["action"] == k) for k in ACTION_LABEL}
    parts = [f"<b style='color:{CURRENT.ok}'>{n['new']} νέοι</b>"]
    if n["update"]:
        parts.append(f"<b style='color:{CURRENT.accent}'>{n['update']} υπάρχουν ήδη και θα ενημερωθούν</b>")
    if n["same"]:
        parts.append(f"<span style='color:{CURRENT.muted}'>{n['same']} υπάρχουν ήδη χωρίς αλλαγές</span>")
    return " · ".join(parts)


class ImportPreviewDialog(QDialog):
    """`choice` μετά το exec(): 'lookup' (εισαγωγή + ενημέρωση στοιχείων) | 'import' (μόνο εισαγωγή) | ''."""

    def __init__(self, path: Path, res: ImportResult, preview: list[dict[str, Any]], lookup_count: int,
                 with_creds: bool, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Εισαγωγή πελατών από Excel")
        self.resize(1000, 600)
        self.choice = ""
        root = QVBoxLayout(self)
        counts = [f"{len(res.rows)} πελάτες"]
        for n, label in ((res.credential_count, "με κωδικούς TAXISnet"), (res.gemi_count, "με κωδικούς Γ.Ε.ΜΗ."),
                         (res.mydata_count, "με κωδικούς myDATA"), (res.ika_count, "με κωδικούς ΙΚΑ εργοδότη / ΑΜΕ")):
            if n:
                counts.append(f"{n} {label}")
        if res.invalid:
            counts.append(f"{len(res.invalid)} άκυρες γραμμές")
        head = QLabel(f"<b>{path.name}</b><br>{', '.join(counts)}<br><span style='font-size:14px'>{summary_text(preview)}"
                      "</span>")
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setWordWrap(True)
        root.addWidget(head)
        for w in res.warnings:
            note = QLabel("ℹ " + w)
            note.setWordWrap(True)
            note.setStyleSheet(f"color:{CURRENT.warn};")
            root.addWidget(note)

        by_afm = {r.afm: r for r in res.rows}
        colour = {"new": CURRENT.ok, "update": CURRENT.accent, "same": CURRENT.muted}
        # Πρώτα όσοι υπάρχουν ήδη και αλλάζουν — αυτούς πρέπει να δει ο χρήστης.
        order = sorted(preview, key=lambda p: {"update": 0, "new": 1, "same": 2}[p["action"]])
        self.table = QTableWidget(len(order), len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        for i, p in enumerate(order):
            r = by_afm[p["afm"]]
            have = [label for ok, label in ((r.has_credentials, "TAXISnet"), (r.has_gemi, "Γ.Ε.ΜΗ."),
                                            (r.has_mydata, "myDATA"), (r.has_ika, "ΙΚΑ εργοδότη"), (bool(r.ame), "ΑΜΕ"))
                    if ok]
            name = p["name"] or "—"
            if p["existing_name"] and p["existing_name"] != p["name"]:
                name += f"  (στην εφαρμογή: {p['existing_name']})"
            cells = [p["afm"], name, ", ".join(have) or "—", ACTION_LABEL[p["action"]], ", ".join(p["changes"])]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 3:
                    item.setForeground(QColor(colour[p["action"]]))
                    if p["action"] == "update":
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
                if not r.checksum_ok and col == 0:
                    item.setForeground(QColor(CURRENT.bad))
                    item.setToolTip("Ο ΑΦΜ δεν περνά τον έλεγχο ψηφίου ελέγχου")
                self.table.setItem(i, col, item)
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        root.addWidget(self.table, 1)

        hint = QLabel(f"Τίποτα δεν γράφεται πριν πατήσετε εισαγωγή. Η «ενημέρωση στοιχείων» (ΑΑΔΕ → ΓΕΜΗ → VIES) αφορά "
                      f"{lookup_count} {'πελάτες με κωδικούς TAXISnet' if with_creds else 'πελάτες'} και τρέχει στο "
                      "παρασκήνιο — μπορεί να γίνει και αργότερα από «Ανανέωση στοιχείων».")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        root.addWidget(hint)
        buttons = QHBoxLayout()
        buttons.addStretch()
        both = QPushButton("Εισαγωγή & ενημέρωση στοιχείων")
        both.setObjectName("primary")
        both.setDefault(True)
        both.clicked.connect(lambda: self._done("lookup"))
        only = QPushButton("Μόνο εισαγωγή")
        only.clicked.connect(lambda: self._done("import"))
        cancel = QPushButton("Άκυρο")
        cancel.clicked.connect(self.reject)
        for b in (both, only, cancel):
            buttons.addWidget(b)
        root.addLayout(buttons)

    def _done(self, choice: str) -> None:
        self.choice = choice
        self.accept()
