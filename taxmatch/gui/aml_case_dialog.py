"""Υπόθεση δέουσας επιμέλειας: εσωτερική αναφορά ύποπτης συναλλαγής πελάτη (→ απόφαση για την Αρχή) ή εσωτερική
καταγγελία παράβασης (ν. 4990/2022). Η επικύρωση ζει στο `aml.cases.validate` — εδώ μόνο η φόρμα."""
from __future__ import annotations

from datetime import date
from typing import Optional

from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import settings_store
from ..aml import cases as cases_mod
from ..aml.content import (
    CASE_CHANNELS, CASE_KIND_LABEL, CASE_STATUSES, RED_FLAGS, SUSPICION_DECISIONS, WHISTLE_CATEGORIES, WHISTLE_OUTCOMES,
)
from ..business_profiles import service as clients
from .theme import CURRENT
from .toast import toast
from .widgets import GrDateEdit


class OptionalDate(QWidget):
    """Ημερομηνία που μπορεί να μείνει κενή (π.χ. «δεν έχει σταλεί ακόμη βεβαίωση»)."""

    def __init__(self, iso: str = "") -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.enabled = QCheckBox()
        self.edit = GrDateEdit(date.fromisoformat(iso) if iso else date.today())
        self.edit.setMinimumDate(QDate(2000, 1, 1))
        self.enabled.setChecked(bool(iso))
        self.edit.setEnabled(bool(iso))
        self.enabled.toggled.connect(self.edit.setEnabled)
        row.addWidget(self.enabled)
        row.addWidget(self.edit)
        row.addStretch()

    def iso(self) -> str:
        return self.edit.date().toPython().isoformat() if self.enabled.isChecked() else ""


class AmlCaseDialog(QDialog):
    def __init__(self, conn, parent=None, *, kind: str = "suspicion", case_id: Optional[int] = None,
                 afm: str = "") -> None:
        super().__init__(parent)
        self.conn = conn
        self.case_id = case_id
        self.saved_id: Optional[int] = None
        c = cases_mod.get(conn, case_id) if case_id else None
        self.case = c or {"kind": kind, "received_on": date.today().isoformat(), "afm": afm, "red_flags": [],
                          "handler": settings_store.get(conn, "aml_officer"), "status": "received"}
        self.kind = self.case["kind"]
        self.setWindowTitle(f"{CASE_KIND_LABEL[self.kind]}" + (f" — υπόθεση #{case_id}" if case_id else ""))
        self.resize(860, 720)

        root = QVBoxLayout(self)
        warn = QLabel("ΕΜΠΙΣΤΕΥΤΙΚΟ — μην ενημερώσετε τον πελάτη ή τρίτους ότι εξετάζεται ή θα αναφερθεί (άρθρο 27, "
                      "tipping-off). Η αναφορά γίνεται στην Αρχή Καταπολέμησης της Νομιμοποίησης Εσόδων, όχι στην ΑΑΔΕ."
                      if self.kind == "suspicion" else
                      "Προστασία ταυτότητας καταγγέλλοντος και απαγόρευση αντιποίνων (ν. 4990/2022). Βεβαίωση παραλαβής "
                      "εντός 7 εργάσιμων — ενημέρωση για την έκβαση εντός 3 μηνών.")
        warn.setWordWrap(True)
        warn.setStyleSheet(f"color:{CURRENT.bad if self.kind == 'suspicion' else CURRENT.warn}; font-weight:600;")
        root.addWidget(warn)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        box = QVBoxLayout(body)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)

        main = QGroupBox("Στοιχεία")
        form = QFormLayout(main)
        self.received = GrDateEdit(date.fromisoformat(self.case["received_on"]))
        form.addRow("Παραλαβή / εντοπισμός", self.received)
        self.channel = QComboBox()
        for key, label in CASE_CHANNELS:
            self.channel.addItem(label, key)
        self.channel.setCurrentIndex(max(0, self.channel.findData(self.case.get("channel", ""))))
        form.addRow("Τρόπος", self.channel)
        self.client = QComboBox()
        self.client.addItem("— κανένας —" if self.kind == "whistle" else "— επιλέξτε πελάτη —", "")
        for r in clients.list_all(conn):
            self.client.addItem(f"{r['name'] or '—'} ({r['afm']})", r["afm"])
        if self.case.get("afm") and self.client.findData(self.case["afm"]) < 0:
            self.client.addItem(f"{self.case.get('client_name') or '—'} ({self.case['afm']}) — πρώην πελάτης", self.case["afm"])
        self.client.setCurrentIndex(max(0, self.client.findData(self.case.get("afm", ""))))
        form.addRow("Πελάτης", self.client)
        self.category = QComboBox()
        if self.kind == "whistle":
            self.category.addItem("— επιλέξτε —", "")
            for key, label in WHISTLE_CATEGORIES:
                self.category.addItem(label, key)
            self.category.setCurrentIndex(max(0, self.category.findData(self.case.get("category", ""))))
            form.addRow("Κατηγορία παράβασης", self.category)
        self.description = QPlainTextEdit(self.case.get("description", ""))
        self.description.setFixedHeight(90)
        form.addRow("Περιγραφή", self.description)
        self.anonymous = QCheckBox("Ανώνυμη καταγγελία")
        self.anonymous.setChecked(bool(self.case.get("anonymous")))
        self.reporter = QLineEdit(self.case.get("reporter", ""))
        self.reporter.setPlaceholderText("Ονοματεπώνυμο (υπάλληλος/καταγγέλλων)")
        self.anonymous.toggled.connect(lambda on: (self.reporter.setEnabled(not on), on and self.reporter.clear()))
        self.reporter.setEnabled(not self.anonymous.isChecked())
        if self.kind == "whistle":
            form.addRow("", self.anonymous)
        form.addRow("Αναφέρων" if self.kind == "suspicion" else "Καταγγέλλων", self.reporter)
        self.handler = QLineEdit(self.case.get("handler", ""))
        form.addRow("Υπεύθυνος χειρισμού", self.handler)
        self.status = QComboBox()
        for key, label in CASE_STATUSES:
            self.status.addItem(label, key)
        self.status.setCurrentIndex(max(0, self.status.findData(self.case.get("status", "received"))))
        form.addRow("Κατάσταση", self.status)
        box.addWidget(main)

        if self.kind == "whistle":
            dl = cases_mod.deadlines(self.case)
            dates = QGroupBox(f"Προθεσμίες ν. 4990/2022 — βεβαίωση έως {dl.get('ack_due', '')}, "
                              f"ενημέρωση έως {dl.get('feedback_due', '')}")
            dform = QFormLayout(dates)
            self.ack_on = OptionalDate(self.case.get("ack_on", ""))
            dform.addRow("Βεβαίωση παραλαβής στάλθηκε", self.ack_on)
            self.feedback_on = OptionalDate(self.case.get("feedback_on", ""))
            dform.addRow("Ενημέρωση καταγγέλλοντος", self.feedback_on)
            box.addWidget(dates)
        else:
            tx = QGroupBox("Συναλλαγή / δραστηριότητα")
            tform = QFormLayout(tx)
            self.tx_description = QLineEdit(self.case.get("tx_description", ""))
            self.tx_description.setPlaceholderText("π.χ. κατάθεση μετρητών, έμβασμα από τρίτη χώρα, αγορά ακινήτου")
            tform.addRow("Είδος", self.tx_description)
            self.tx_amount = QLineEdit(self.case.get("tx_amount", ""))
            tform.addRow("Ποσό", self.tx_amount)
            self.tx_date = OptionalDate(self.case.get("tx_date", ""))
            tform.addRow("Ημερομηνία", self.tx_date)
            box.addWidget(tx)
            flags = QGroupBox("Ενδείξεις (red flags)")
            grid = QGridLayout(flags)
            self.flag_checks: dict[str, QCheckBox] = {}
            for i, (key, label) in enumerate(RED_FLAGS):
                chk = QCheckBox(label)
                chk.setChecked(key in self.case.get("red_flags", []))
                grid.addWidget(chk, i // 2, i % 2)
                self.flag_checks[key] = chk
            box.addWidget(flags)

        dec = QGroupBox("Απόφαση για αναφορά στην Αρχή (άρθρο 22)" if self.kind == "suspicion" else "Αποτέλεσμα")
        dform = QFormLayout(dec)
        self.actions = QPlainTextEdit(self.case.get("actions", ""))
        self.actions.setPlaceholderText("Τι εξετάστηκε: ιστορικό πελάτη, έγγραφα που ζητήθηκαν, συνεντεύξεις…")
        self.actions.setFixedHeight(70)
        dform.addRow("Ενέργειες διερεύνησης", self.actions)
        self.decision = QComboBox()
        for key, label in (SUSPICION_DECISIONS if self.kind == "suspicion" else WHISTLE_OUTCOMES):
            self.decision.addItem(label, key)
        self.decision.setCurrentIndex(max(0, self.decision.findData(self.case.get("decision", ""))))
        dform.addRow("Απόφαση" if self.kind == "suspicion" else "Αποτέλεσμα", self.decision)
        self.reason = QPlainTextEdit(self.case.get("decision_reason", ""))
        self.reason.setFixedHeight(60)
        self.reason.setPlaceholderText("Αιτιολογία — υποχρεωτική αν ΔΕΝ γίνει αναφορά (άρθρο 30 παρ. 1γ)"
                                       if self.kind == "suspicion" else "Σύνοψη ευρημάτων και διορθωτικών ενεργειών")
        dform.addRow("Αιτιολογία", self.reason)
        self.authority_ref = QLineEdit(self.case.get("authority_ref", ""))
        if self.kind == "suspicion":
            dform.addRow("Αρ. πρωτοκόλλου Αρχής", self.authority_ref)
        box.addWidget(dec)
        box.addStretch()

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Άκυρο")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("Αποθήκευση")
        save.setObjectName("primary")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        root.addLayout(buttons)

    def values(self) -> dict:
        v = {
            "kind": self.kind, "received_on": self.received.date().toPython().isoformat(),
            "channel": self.channel.currentData(), "afm": self.client.currentData() or "",
            "category": self.category.currentData() if self.kind == "whistle" else "",
            "description": self.description.toPlainText().strip(), "reporter": self.reporter.text().strip(),
            "anonymous": self.anonymous.isChecked() if self.kind == "whistle" else False,
            "handler": self.handler.text().strip(), "status": self.status.currentData(),
            "actions": self.actions.toPlainText().strip(), "decision": self.decision.currentData(),
            "decision_reason": self.reason.toPlainText().strip(), "authority_ref": self.authority_ref.text().strip(),
            "decided_on": self.case.get("decided_on", "") if self.decision.currentData() == self.case.get("decision") else "",
            "closed_on": self.case.get("closed_on", ""),
        }
        if self.kind == "whistle":
            v.update(ack_on=self.ack_on.iso(), feedback_on=self.feedback_on.iso())
        else:
            v.update(tx_description=self.tx_description.text().strip(), tx_amount=self.tx_amount.text().strip(),
                     tx_date=self.tx_date.iso(), red_flags=[k for k, c in self.flag_checks.items() if c.isChecked()])
        return v

    def _save(self) -> None:
        try:
            self.saved_id = cases_mod.save(self.conn, self.values(), self.case_id)
        except ValueError as exc:
            toast(self, str(exc), "warn")
            return
        self.accept()
