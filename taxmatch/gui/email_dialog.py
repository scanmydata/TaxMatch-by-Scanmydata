"""Χειροκίνητη αποστολή email σε πελάτη (οφειλές / νέα): προεπισκόπηση, διόρθωση παραλήπτη/θέματος/κειμένου, επιλογή
συνημμένων — τίποτα δεν φεύγει πριν πατηθεί «Αποστολή». Η αποστολή τρέχει στο παρασκήνιο (`run_task`)."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton,
    QVBoxLayout,
)

from .. import db as dbmod
from .. import mailer
from .toast import toast
from .workers import run_task


class EmailDialog(QDialog):
    def __init__(self, parent, afm: str, kind: str, msg: mailer.Message,
                 on_sent: Optional[Callable[[], None]] = None) -> None:
        super().__init__(parent)
        self.afm, self.kind, self.msg, self.on_sent = afm, kind, msg, on_sent
        self._tasks: list[Any] = []
        self.setWindowTitle(f"Email — {mailer.KIND_LABEL.get(kind, kind)}")
        self.resize(760, 640)
        root = QVBoxLayout(self)
        form = QFormLayout()
        self.to = QLineEdit(msg.to)
        self.to.setPlaceholderText("email πελάτη")
        form.addRow("Προς", self.to)
        self.subject = QLineEdit(msg.subject)
        form.addRow("Θέμα", self.subject)
        root.addLayout(form)
        self.body = QPlainTextEdit(msg.text)
        root.addWidget(self.body, 1)
        self.files = QListWidget()
        self.files.setMaximumHeight(110)
        for path in msg.attachments:
            item = QListWidgetItem(path.name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, str(path))
            self.files.addItem(item)
        if msg.attachments:
            root.addWidget(QLabel(f"Συνημμένα ({len(msg.attachments)}):"))
            root.addWidget(self.files)
        else:
            self.files.hide()
        note = QLabel("Το κείμενο μπορεί να διορθωθεί πριν την αποστολή. Δεν περιέχει ποτέ κωδικούς πρόσβασης.")
        note.setObjectName("muted")
        root.addWidget(note)
        buttons = QHBoxLayout()
        buttons.addStretch()
        self.send_btn = QPushButton("Αποστολή")
        self.send_btn.setObjectName("primary")
        self.send_btn.clicked.connect(self._send)
        cancel = QPushButton("Άκυρο")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(self.send_btn)
        buttons.addWidget(cancel)
        root.addLayout(buttons)

    def final_message(self) -> mailer.Message:
        text = self.body.toPlainText()
        # Αν ο χρήστης άλλαξε το κείμενο, το HTML ξαναφτιάχνεται από αυτό (αλλιώς θα έφευγαν δύο διαφορετικές εκδοχές).
        page = self.msg.html if text == self.msg.text else (
            "<div style=\"font-family:system-ui,Arial,sans-serif;font-size:14px\">"
            + html.escape(text).replace("\n", "<br>") + "</div>")
        files = [Path(self.files.item(i).data(Qt.ItemDataRole.UserRole)) for i in range(self.files.count())
                 if self.files.item(i).checkState() == Qt.CheckState.Checked]
        return mailer.Message(self.to.text().strip(), self.subject.text().strip(), text, page, files,
                              self.msg.fingerprint, self.msg.items)

    def _send(self) -> None:
        msg = self.final_message()
        if not mailer.EMAIL_RE.match(msg.to):
            toast(self, "Δώστε έγκυρο email παραλήπτη.", "warn")
            return
        self.send_btn.setEnabled(False)

        def work(_p):
            conn = dbmod.connect()                     # δικό του thread — ποτέ η σύνδεση του UI
            try:
                return mailer.send(conn, self.afm, self.kind, msg)
            finally:
                conn.close()

        def done(result):
            ok, text = result
            parent = self.parentWidget() or self
            toast(parent, text, "ok" if ok else "danger")
            if ok:
                if self.on_sent:
                    self.on_sent()
                self.accept()
            else:
                self.send_btn.setEnabled(True)

        def failed(m):
            toast(self, m, "danger")
            self.send_btn.setEnabled(True)

        toast(self, f"Αποστολή email στο {msg.to}…", "info")
        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))
