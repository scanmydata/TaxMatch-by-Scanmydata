"""Κύριος κωδικός: ξεκλείδωμα στην εκκίνηση και διαχείριση από το μενού.

Δύο διαφορετικά παράθυρα, με διαφορετικό ρόλο:

``UnlockDialog``
    Ανοίγει **πριν** από το κύριο παράθυρο, όταν ο φάκελος δεδομένων είναι
    προστατευμένος. Χωρίς σωστό κωδικό η εφαρμογή δεν ξεκινά — δεν υπάρχει
    «άκυρο και συνεχίζουμε», γιατί χωρίς το κλειδί τα credentials δεν
    διαβάζονται ούτως ή άλλως.

``PasswordDialog``
    Ορισμός, αλλαγή ή αφαίρεση του κωδικού από την ίδια την εφαρμογή.

Και τα δύο τονίζουν το ίδιο: **δεν υπάρχει ανάκτηση**. Ο κωδικός δεν
αποθηκεύεται πουθενά — αυτό ακριβώς είναι που κάνει τον φάκελο άχρηστο σε όποιον
τον αντιγράψει, και ταυτόχρονα σημαίνει ότι ούτε εμείς μπορούμε να τον βρούμε.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
)

from .. import crypto
from .icons import icon
from .theme import CURRENT

#: Κάτω από αυτό, ένας σύγχρονος επιτιθέμενος με το ίδιο το αρχείο στα χέρια του
#: δοκιμάζει όλο τον χώρο. Το Argon2id ανεβάζει πολύ το κόστος ανά δοκιμή, αλλά
#: δεν σώζει κωδικό τεσσάρων χαρακτήρων.
MIN_LENGTH = 10

_NO_RECOVERY = (
    "Ο κωδικός δεν αποθηκεύεται πουθενά. Αν χαθεί, οι αποθηκευμένοι κωδικοί "
    "TAXISnet και τα API keys δεν ανακτώνται με κανέναν τρόπο και πρέπει να "
    "δοθούν ξανά (χειροκίνητα ή από Excel)."
)


def _password_field(placeholder: str) -> QLineEdit:
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setPlaceholderText(placeholder)
    field.setMinimumHeight(34)
    return field


def _title_row(text: str, colour: str) -> QHBoxLayout:
    row = QHBoxLayout()
    mark = QLabel()
    mark.setPixmap(icon("lock", colour, 26).pixmap(QSize(26, 26)))
    row.addWidget(mark)
    title = QLabel(text)
    title.setObjectName("h1")
    row.addWidget(title, 1)
    return row


class UnlockDialog(QDialog):
    """Ζητά τον κύριο κωδικό στην εκκίνηση."""

    def __init__(self, enckey_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._enckey_path = enckey_path
        self.setWindowTitle("Ξεκλείδωμα")
        self.setMinimumWidth(460)
        # Χωρίς αυτό, το X της γραμμής τίτλου κλείνει το παράθυρο σαν «άκυρο»
        # και η εφαρμογή προχωρά με κλειδωμένο φάκελο.
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.addLayout(_title_row("Προστατευμένος φάκελος δεδομένων", CURRENT.accent))

        intro = QLabel(
            "Δώστε τον κύριο κωδικό για να ανοίξει η εφαρμογή. Χωρίς αυτόν, τα "
            "διαπιστευτήρια των πελατών παραμένουν κρυπτογραφημένα."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        root.addWidget(intro)

        self.field = _password_field("Κύριος κωδικός")
        self.field.returnPressed.connect(self.accept)
        root.addWidget(self.field)

        show = QCheckBox("Εμφάνιση κωδικού")
        show.toggled.connect(
            lambda on: self.field.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        root.addWidget(show)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{CURRENT.bad};")
        self.error.hide()
        root.addWidget(self.error)

        buttons = QDialogButtonBox()
        buttons.addButton("Άνοιγμα", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("Έξοδος", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self) -> None:  # noqa: D102 - override
        password = self.field.text()
        if not password:
            return
        try:
            crypto.unlock(self._enckey_path, password)
        except crypto.WrongPassword:
            self.error.setText("Λάθος κωδικός. Δοκιμάστε ξανά.")
            self.error.show()
            self.field.selectAll()
            self.field.setFocus()
            return
        super().accept()


def ask_unlock(enckey_path: Path, parent=None) -> bool:
    """True αν ο φάκελος είναι ανοιχτός (ή δεν ήταν κλειδωμένος εξαρχής)."""
    if not crypto.is_protected(enckey_path):
        return True
    return UnlockDialog(enckey_path, parent).exec() == QDialog.DialogCode.Accepted


class PasswordDialog(QDialog):
    """Ορισμός, αλλαγή ή αφαίρεση του κύριου κωδικού."""

    def __init__(self, enckey_path: Path, parent=None) -> None:
        super().__init__(parent)
        self._enckey_path = enckey_path
        self._protected = crypto.is_protected(enckey_path)
        self.setWindowTitle("Κύριος κωδικός")
        self.setMinimumWidth(500)

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.addLayout(
            _title_row(
                "Αλλαγή κύριου κωδικού" if self._protected else "Ορισμός κύριου κωδικού",
                CURRENT.accent,
            )
        )

        intro = QLabel(
            "Με κύριο κωδικό, το κλειδί κρυπτογράφησης δεν υπάρχει σε "
            "αναγνώσιμη μορφή στον δίσκο: παράγεται από τον κωδικό κάθε φορά "
            "που ανοίγει η εφαρμογή. Όποιος αντιγράψει τον φάκελο δεδομένων "
            "χωρίς τον κωδικό δεν μπορεί να διαβάσει τίποτα."
            if not self._protected
            else "Η αλλαγή κωδικού δεν χρειάζεται να ξανακρυπτογραφήσει τη βάση: "
            "αλλάζει μόνο το περιτύλιγμα του κλειδιού."
        )
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        root.addWidget(intro)

        self.current = _password_field("Τρέχων κωδικός")
        if self._protected:
            root.addWidget(QLabel("Τρέχων κωδικός"))
            root.addWidget(self.current)

        root.addWidget(QLabel("Νέος κωδικός"))
        self.new = _password_field(f"Τουλάχιστον {MIN_LENGTH} χαρακτήρες")
        root.addWidget(self.new)

        self.repeat = _password_field("Επανάληψη νέου κωδικού")
        root.addWidget(self.repeat)

        show = QCheckBox("Εμφάνιση κωδικών")
        show.toggled.connect(self._set_echo)
        root.addWidget(show)

        warning = QLabel(f"⚠ {_NO_RECOVERY}")
        warning.setWordWrap(True)
        warning.setStyleSheet(f"color:{CURRENT.warn};")
        root.addWidget(warning)

        if self._protected:
            shared = QLabel(
                "Αν ο φάκελος δεδομένων είναι κοινόχρηστος (π.χ. δίκτυο γραφείου), ο κωδικός είναι "
                "κοινός για όλους: αλλάζοντάς τον εδώ, θα ζητηθεί ο νέος στην επόμενη εκκίνηση παντού."
            )
            shared.setWordWrap(True)
            shared.setObjectName("muted")
            root.addWidget(shared)

        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color:{CURRENT.bad};")
        self.error.hide()
        root.addWidget(self.error)

        buttons = QDialogButtonBox()
        buttons.addButton(
            "Αλλαγή κωδικού" if self._protected else "Ενεργοποίηση",
            QDialogButtonBox.ButtonRole.AcceptRole,
        )
        if self._protected:
            remove = buttons.addButton(
                "Αφαίρεση προστασίας", QDialogButtonBox.ButtonRole.DestructiveRole
            )
            remove.clicked.connect(self._remove)
        buttons.addButton("Άκυρο", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _set_echo(self, on: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        for field in (self.current, self.new, self.repeat):
            field.setEchoMode(mode)

    def _fail(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()

    def _remove(self) -> None:
        current = self.current.text()
        if not current:
            self._fail("Δώστε τον τρέχοντα κωδικό για να αφαιρεθεί η προστασία.")
            return
        confirm = QMessageBox.question(
            self,
            "Αφαίρεση προστασίας",
            "Το κλειδί θα ξαναγραφτεί σε αναγνώσιμη μορφή δίπλα στη βάση.\n\n"
            "Όποιος αποκτήσει αντίγραφο του φακέλου δεδομένων θα μπορεί να "
            "διαβάσει τα διαπιστευτήρια όλων των πελατών.\n\nΝα προχωρήσω;",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            crypto.remove_password(self._enckey_path, current)
        except crypto.WrongPassword:
            self._fail("Λάθος τρέχων κωδικός.")
            return
        QMessageBox.information(
            self, "Κύριος κωδικός", "Η προστασία με κωδικό αφαιρέθηκε."
        )
        super().accept()

    def accept(self) -> None:  # noqa: D102 - override
        new, repeat = self.new.text(), self.repeat.text()
        if len(new) < MIN_LENGTH:
            self._fail(f"Ο κωδικός θέλει τουλάχιστον {MIN_LENGTH} χαρακτήρες.")
            return
        if new != repeat:
            self._fail("Οι δύο κωδικοί δεν ταιριάζουν.")
            return
        try:
            crypto.set_password(
                self._enckey_path,
                new,
                current=self.current.text() if self._protected else None,
            )
        except crypto.WrongPassword:
            self._fail("Λάθος τρέχων κωδικός.")
            return
        QMessageBox.information(
            self,
            "Κύριος κωδικός",
            "Ο κύριος κωδικός ενεργοποιήθηκε.\n\n"
            "Θα ζητείται σε κάθε εκκίνηση της εφαρμογής.\n\n" + _NO_RECOVERY,
        )
        super().accept()


def manage(enckey_path: Path, parent=None) -> None:
    PasswordDialog(enckey_path, parent).exec()


def offer(enckey_path: Path, parent=None) -> None:
    """Προτείνει προστασία σε φάκελο που δεν έχει. Καλείται μετά την ξενάγηση."""
    if crypto.is_protected(enckey_path):
        return
    answer = QMessageBox.question(
        parent,
        "Προστασία με κύριο κωδικό",
        "Θα αποθηκεύσετε κωδικούς TAXISnet πελατών και API keys.\n\n"
        "Χωρίς κύριο κωδικό, το κλειδί κρυπτογράφησης βρίσκεται δίπλα στη βάση "
        "και όποιος αντιγράψει τον φάκελο μπορεί να τα διαβάσει.\n\n"
        "Θέλετε να ορίσετε κύριο κωδικό τώρα;",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.Yes,
    )
    if answer == QMessageBox.StandardButton.Yes:
        manage(enckey_path, parent)
