"""Ρυθμίσεις → «Email προς πελάτες»: SMTP του γραφείου, δοκιμαστικό email, αυτόματες αποστολές (οφειλές / νέα) και
λήψη οφειλών στον καθημερινό έλεγχο. Λογική: `taxmatch/mailer.py`."""
from __future__ import annotations

from typing import Any

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QWidget,
)

from .. import db as dbmod
from .. import mailer, settings_store
from .theme import CURRENT
from .toast import toast
from .workers import run_task

_WEEKDAYS = ("Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή")


class EmailSettingsBox(QGroupBox):
    def __init__(self, conn, window: QWidget) -> None:
        super().__init__("Email προς πελάτες (οφειλές, νέα)")
        self.conn, self.window_ = conn, window
        self._tasks: list[Any] = []
        form = QFormLayout(self)
        hint = QLabel("Με τον λογαριασμό email του γραφείου (SMTP). Για Gmail/Outlook με διπλό έλεγχο ταυτότητας "
                      "χρειάζεται «κωδικός εφαρμογής». Τα email των πελατών έρχονται από το Μητρώο ΑΑΔΕ με την ανάκτηση "
                      "στοιχείων — διορθώνονται στην καρτέλα πελάτη.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        form.addRow(hint)
        self.host = QLineEdit()
        self.host.setPlaceholderText("π.χ. smtp.gmail.com")
        self.port = QLineEdit()
        self.port.setFixedWidth(70)
        self.security = QComboBox()
        for label, value in (("STARTTLS (587)", "starttls"), ("SSL/TLS (465)", "ssl"), ("Χωρίς κρυπτογράφηση", "none")):
            self.security.addItem(label, value)
        row = QHBoxLayout()
        row.addWidget(self.host, 1)
        row.addWidget(QLabel("Θύρα"))
        row.addWidget(self.port)
        row.addWidget(self.security)
        form.addRow("Διακομιστής SMTP", row)
        self.user = QLineEdit()
        form.addRow("Χρήστης", self.user)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Κωδικός", self.password)
        self.sender = QLineEdit()
        self.sender.setPlaceholderText("αν μείνει κενό: το email του γραφείου ή ο χρήστης SMTP")
        form.addRow("Αποστολέας", self.sender)
        self.bcc = QLineEdit()
        self.bcc.setPlaceholderText("προαιρετικά — αντίγραφο κάθε email στο γραφείο")
        form.addRow("Κρυφή κοινοποίηση", self.bcc)
        self.signature = QPlainTextEdit()
        self.signature.setPlaceholderText("αν μείνει κενό: «Με εκτίμηση» + επωνυμία, τηλέφωνο, email γραφείου")
        self.signature.setFixedHeight(64)
        form.addRow("Υπογραφή", self.signature)
        buttons = QHBoxLayout()
        save = QPushButton("Αποθήκευση")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        buttons.addWidget(save)
        test = QPushButton("Δοκιμαστικό email")
        test.setToolTip("Στέλνει ένα δοκιμαστικό μήνυμα στη διεύθυνση του αποστολέα")
        test.clicked.connect(self._test)
        buttons.addWidget(test)
        buttons.addStretch()
        form.addRow("", buttons)

        auto_head = QLabel("Αυτόματες αποστολές (στον καθημερινό έλεγχο)")
        auto_head.setObjectName("h2")
        form.addRow(auto_head)
        self.auto_debts = QCheckBox("Οφειλές ΑΑΔΕ — μόνο όταν αλλάξουν από το τελευταίο email (με τις ταυτότητες οφειλής σε PDF)")
        form.addRow(self.auto_debts)
        self.daily_debts = QCheckBox("Λήψη οφειλών ΑΑΔΕ για όλους τους πελάτες με κωδικούς TAXISnet")
        form.addRow(self.daily_debts)
        self.auto_news = QCheckBox("Νέα που αφορούν τον πελάτη — μόνο όσα δεν του έχουν σταλεί")
        form.addRow(self.auto_news)
        freq = QHBoxLayout()
        self.freq = QComboBox()
        self.freq.addItem("Κάθε ημέρα", "daily")
        self.freq.addItem("Μία φορά την εβδομάδα", "weekly")
        self.weekday = QComboBox()
        for i, name in enumerate(_WEEKDAYS, 1):
            self.weekday.addItem(name, str(i))
        self.freq.currentIndexChanged.connect(lambda _i: self.weekday.setEnabled(self.freq.currentData() == "weekly"))
        freq.addWidget(self.freq)
        freq.addWidget(self.weekday)
        freq.addStretch()
        form.addRow("Συχνότητα νέων", freq)
        note = QLabel("Οι αυτόματες αποστολές γίνονται μόνο σε πελάτες με email που δεν έχουν εξαιρεθεί (καρτέλα πελάτη → "
                      "Προφίλ) και καταγράφονται στο ιστορικό email του πελάτη. Χρειάζεται ενεργός καθημερινός έλεγχος.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        form.addRow(note)
        self.reload()

    def reload(self) -> None:
        g = lambda k: settings_store.get(self.conn, k)  # noqa: E731
        self.host.setText(g("smtp_host"))
        self.port.setText(g("smtp_port") or "587")
        self.security.setCurrentIndex(max(0, self.security.findData(g("smtp_security") or "starttls")))
        self.user.setText(g("smtp_user"))
        self.password.clear()
        self.password.setPlaceholderText("•••••••• (αφήστε κενό για να μείνει ως έχει)" if settings_store.is_set(
            self.conn, "smtp_pass") else "κωδικός ή κωδικός εφαρμογής")
        self.sender.setText(g("smtp_from"))
        self.bcc.setText(g("email_bcc"))
        self.signature.setPlainText(g("email_signature"))
        self.auto_debts.setChecked(g("auto_email_debts") == "1")
        self.daily_debts.setChecked(g("daily_debts") == "1")
        self.auto_news.setChecked(g("auto_email_news") == "1")
        self.freq.setCurrentIndex(max(0, self.freq.findData(g("auto_email_news_freq") or "weekly")))
        self.weekday.setCurrentIndex(max(0, self.weekday.findData(g("auto_email_weekday") or "1")))
        self.weekday.setEnabled(self.freq.currentData() == "weekly")

    def save(self) -> None:
        port = self.port.text().strip() or "587"
        if not port.isdigit():
            toast(self.window_, "Η θύρα SMTP πρέπει να είναι αριθμός.", "warn")
            return
        for key, value in (("smtp_host", self.host.text().strip()), ("smtp_port", port),
                           ("smtp_security", self.security.currentData()), ("smtp_user", self.user.text().strip()),
                           ("smtp_from", self.sender.text().strip()), ("email_bcc", self.bcc.text().strip()),
                           ("email_signature", self.signature.toPlainText().strip()),
                           ("auto_email_debts", "1" if self.auto_debts.isChecked() else "0"),
                           ("daily_debts", "1" if self.daily_debts.isChecked() else "0"),
                           ("auto_email_news", "1" if self.auto_news.isChecked() else "0"),
                           ("auto_email_news_freq", self.freq.currentData()),
                           ("auto_email_weekday", self.weekday.currentData())):
            settings_store.set_value(self.conn, key, value)
        if self.password.text():
            settings_store.set_value(self.conn, "smtp_pass", self.password.text())
        toast(self.window_, "Οι ρυθμίσεις email αποθηκεύτηκαν (ο κωδικός κρυπτογραφημένος).", "ok")
        self.reload()

    def _test(self) -> None:
        self.save()
        to = mailer.sender(self.conn)
        if not mailer.configured(self.conn):
            toast(self.window_, "Συμπληρώστε διακομιστή SMTP και αποστολέα.", "warn")
            return

        def work(_p):
            conn = dbmod.connect()                     # δικό του thread — ποτέ η σύνδεση του UI
            try:
                return mailer.send(conn, "", "test", mailer.compose_test(conn, to))
            finally:
                conn.close()

        def done(result):
            ok, msg = result
            toast(self.window_, msg, "ok" if ok else "danger")

        toast(self.window_, f"Αποστολή δοκιμαστικού email στο {to}…", "info")
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self.window_, m, "danger")))


def status_text(conn) -> str:
    return ("" if mailer.configured(conn) else
            f"<span style='color:{CURRENT.warn}'>Δεν έχουν οριστεί στοιχεία SMTP (Ρυθμίσεις → Email).</span>")
