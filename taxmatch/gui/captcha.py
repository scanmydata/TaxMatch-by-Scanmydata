"""Κωδικός ασφαλείας (captcha) από τον χρήστη για ελέγχους που τρέχουν στο ΠΑΡΑΣΚΗΝΙΟ (π.χ. ΑΚΕΕΔ ΕΔΟΕΑΠ —
`advertising.akeed_login`): το νήμα του ελέγχου καλεί `CaptchaBridge.ask(εικόνα)`, που στέλνει σήμα στο UI thread,
δείχνει διάλογο με την εικόνα και περιμένει την απάντηση. Ένα captcha τη φορά (οι έλεγχοι τρέχουν παράλληλα)·
«Παράλειψη» ή 5′ χωρίς απάντηση = κενό (ο έλεγχος σημειώνεται «χρειάζεται captcha», όχι σφάλμα).

Αν υπάρχει το ddddocr, το captcha λύνεται αυτόματα και ο διάλογος δεν ανοίγει (βλ. advertising.solve_captcha)."""
from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget


class CaptchaDialog(QDialog):
    def __init__(self, parent: Optional[QWidget], image: bytes, title: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Κωδικός ασφαλείας")
        lay = QVBoxLayout(self)
        head = QLabel(f"<b>{title}</b><br>Πληκτρολογήστε τους χαρακτήρες της εικόνας (πεζά/αριθμοί).")
        head.setWordWrap(True)
        lay.addWidget(head)
        pic = QLabel()
        pix = QPixmap()
        pix.loadFromData(image)
        if not pix.isNull():
            pic.setPixmap(pix.scaled(pix.width() * 2, pix.height() * 2, Qt.AspectRatioMode.KeepAspectRatio,
                                     Qt.TransformationMode.SmoothTransformation))
        pic.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(pic)
        self.code = QLineEdit()
        self.code.setPlaceholderText("π.χ. 4k7wq")
        self.code.returnPressed.connect(self.accept)
        lay.addWidget(self.code)
        row = QHBoxLayout()
        skip = QPushButton("Παράλειψη")
        skip.clicked.connect(self.reject)
        row.addWidget(skip)
        row.addStretch()
        ok = QPushButton("Συνέχεια")
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        lay.addLayout(row)


class CaptchaBridge(QObject):
    """Ζει στο UI thread. `ask()` καλείται από ΟΠΟΙΟΔΗΠΟΤΕ νήμα και μπλοκάρει ώσπου να απαντήσει ο χρήστης."""
    _request = Signal(bytes, str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._parent = parent
        self._lock = threading.Lock()                 # ένα captcha τη φορά
        self._done = threading.Event()
        self._answer = ""
        self._request.connect(self._show, Qt.ConnectionType.QueuedConnection)

    def ask(self, image: bytes, title: str = "Εφαρμογή ΕΔΟΕΑΠ (ΑΚΕΕΔ)", timeout: float = 300.0) -> str:
        with self._lock:
            self._done.clear()
            self._answer = ""
            self._request.emit(image, title)
            self._done.wait(timeout)
            return self._answer

    @Slot(bytes, str)
    def _show(self, image: bytes, title: str) -> None:
        dlg = CaptchaDialog(self._parent, image, title)
        try:
            self._answer = dlg.code.text().strip().lower() if dlg.exec() == QDialog.DialogCode.Accepted else ""
        finally:
            self._done.set()
