"""Διάλογος «Νέος πελάτης»: ΑΦΜ με αυτόματη επωνυμία (VIES, χωρίς key) στο 9ο ψηφίο, προαιρετικοί κωδικοί TAXISnet.

Ίδια αλληλεπίδραση με το `_client_dialog.html`/`app.js` της web εκδοχής: η αναζήτηση VIES γίνεται σε background
thread (μην παγώνει το παράθυρο) και ΔΕΝ αντικαθιστά επωνυμία που έχει ήδη γράψει ο χρήστης.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget,
)

from .. import db as dbmod
from ..business_profiles import credentials as client_creds, service as clients, vies
from ..identifiers import is_valid_afm, normalize_afm
from .icons import icon
from .theme import CURRENT
from .workers import run_task


class ClientDialog(QDialog):
    """`result_afm` έχει το ΑΦΜ του πελάτη που μόλις προστέθηκε, μετά από `exec() == Accepted`."""

    def __init__(self, conn: sqlite3.Connection, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._conn = conn
        self.result_afm = ""
        self.excel_path = ""  # ο καλών ελέγχει αυτό ΠΡΩΤΑ μετά το exec() — βλ. _pick_excel
        self._last_looked = ""
        self._vies_task = None
        self.setWindowTitle("Νέος πελάτης")
        self.setMinimumWidth(420)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        title = QLabel("Νέος πελάτης")
        title.setObjectName("h1")
        root.addWidget(title)

        form = QFormLayout()
        form.setSpacing(8)
        self.afm = QLineEdit()
        self.afm.setPlaceholderText("9 ψηφία")
        self.afm.setMaxLength(9)
        self.afm.textChanged.connect(self._on_afm_changed)
        form.addRow("ΑΦΜ *", self.afm)

        self.name = QLineEdit()
        form.addRow("Επωνυμία", self.name)

        self.status_line = QLabel("")
        self.status_line.setWordWrap(True)
        self.status_line.setObjectName("muted")
        form.addRow("", self.status_line)

        self.taxis_user = QLineEdit()
        form.addRow("Χρήστης TAXISnet", self.taxis_user)
        self.taxis_pass = QLineEdit()
        self.taxis_pass.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Κωδικός TAXISnet", self.taxis_pass)
        hint = QLabel("Προαιρετικό. Χρησιμοποιείται μόνο για ανάκτηση στοιχείων (ΚΑΔ, ΔΟΥ, ΦΠΑ, βιβλία) από το "
                      "Μητρώο ΑΑΔΕ αυτού του πελάτη.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        form.addRow("", hint)
        root.addLayout(form)

        line = QFrame()
        line.setObjectName("line")
        line.setFrameShape(QFrame.Shape.HLine)
        root.addWidget(line)

        bulk = QHBoxLayout()
        note = QLabel("Έχετε πολλούς πελάτες;")
        note.setObjectName("muted")
        bulk.addWidget(note)
        excel_btn = QPushButton(icon("excel", CURRENT.muted, 16), "  Εισαγωγή από Excel…")
        excel_btn.setToolTip("Μαζική εισαγωγή πελατών (και προαιρετικά κωδικών TAXISnet) από αρχείο Excel/CSV")
        excel_btn.clicked.connect(self._pick_excel)
        bulk.addWidget(excel_btn)
        bulk.addStretch()
        root.addLayout(bulk)

        buttons = QDialogButtonBox()
        buttons.addButton("Προσθήκη", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Άκυρο", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.afm.setFocus()

    def _pick_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Εισαγωγή από Excel", "", "Excel/CSV (*.xlsx *.xlsm *.csv *.txt)")
        if not path:
            return
        self.excel_path = path
        self.accept()  # ο καλών (MainWindow.on_add_client) βλέπει το excel_path και τρέχει την εισαγωγή

    def _say(self, text: str, bad: bool = False) -> None:
        self.status_line.setText(text)
        self.status_line.setStyleSheet(f"color:{CURRENT.bad};" if bad else f"color:{CURRENT.muted};")

    def _on_afm_changed(self, text: str) -> None:
        digits = "".join(c for c in text if c.isdigit())[:9]
        if digits != text:
            self.afm.blockSignals(True)
            self.afm.setText(digits)
            self.afm.blockSignals(False)
        if len(digits) != 9:
            self._last_looked = ""
            self._say("")
            return
        if digits == self._last_looked or self.name.text().strip():
            return                                            # ήδη κοιτάχτηκε, ή ο χρήστης έγραψε ήδη όνομα
        self._lookup(digits)

    def _lookup(self, afm: str) -> None:
        self._last_looked = afm
        self._say("Αναζήτηση στο VIES…")
        row = self._conn.execute("SELECT name, address FROM businesses WHERE afm=?", (afm,)).fetchone()
        if row and row["name"]:
            self.name.setText(row["name"])
            self._say(f"Βρέθηκε στους πελάτες σας: {row['name']} (ο πελάτης υπάρχει ήδη)")
            return

        def work(_progress):
            return vies.lookup(afm)
        self._vies_task = run_task(self, work, on_done=lambda res: self._on_vies_result(afm, res),
                                   on_error=lambda msg: self._say(f"Δεν ήταν δυνατή η αναζήτηση: {msg}", bad=True))

    def _on_vies_result(self, afm: str, res) -> None:
        if afm != self._last_looked:
            return                                            # ο χρήστης άλλαξε το ΑΦΜ στο μεταξύ
        if res.error:
            self._say(f"{res.error} — γράψτε την επωνυμία χειροκίνητα.", bad=True)
            return
        if not res.valid or not res.name:
            self._say("Το ΑΦΜ δεν βρέθηκε στο VIES (π.χ. μη υπόχρεος ΦΠΑ)· γράψτε την επωνυμία χειροκίνητα.", bad=True)
            return
        if not self.name.text().strip():
            self.name.setText(res.name)
        checksum = "" if is_valid_afm(afm) else " — προσοχή: το ΑΦΜ δεν περνά τον έλεγχο ψηφίου"
        self._say(f"Βρέθηκε στο VIES: {res.name}{checksum}")

    def _save(self) -> None:
        afm = normalize_afm(self.afm.text())
        if not afm:
            self._say("Ο ΑΦΜ πρέπει να είναι 9ψήφιος αριθμός.", bad=True)
            return
        name = self.name.text().strip()
        user, pwd = self.taxis_user.text().strip(), self.taxis_pass.text()
        if bool(user) != bool(pwd):
            self._say("Οι κωδικοί TAXISnet θέλουν και χρήστη και κωδικό — δώστε και τα δύο, ή κανένα.", bad=True)
            return
        created = clients.add(self._conn, afm, name, source="manual")
        if user and pwd:
            client_creds.set_(self._conn, afm, user, pwd)
        if not created and not (user and pwd):
            self._say("Ο πελάτης υπάρχει ήδη.", bad=True)
            return
        self.result_afm = afm
        self.accept()
