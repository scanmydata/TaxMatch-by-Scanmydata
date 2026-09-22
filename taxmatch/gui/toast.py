"""Ειδοποιήσεις σαν «side flash message» (πλευρικό μήνυμα κάτω-δεξιά), αντί για native QMessageBox.

Ίδια ιδέα με το `toast()`/`.toasts-host` του παλιού web UI (`web/static/js/app.js`): στοίβα μηνυμάτων κάτω-δεξιά,
έγχρωμο περίγραμμα ανά επίπεδο (ok/warn/danger), αυτόματο κλείσιμο, ή με «×». Ζει ως παιδί ΤΟΥ ΠΑΡΑΘΥΡΟΥ όπου
συνέβη η ενέργεια (`MainWindow` ή οποιοδήποτε `QDialog`) — όχι ξεχωριστό top-level παράθυρο, ώστε να μένει πάντα
πάνω από το σωστό περιεχόμενο και να κλείνει μαζί του.

Κρατάμε τα ΕΠΙΒΕΒΑΙΩΤΙΚΑ (Ναι/Όχι πριν από διαγραφή) σε `QMessageBox.question` — δεν είναι ειδοποίηση αλλά
απόφαση που μπλοκάρει σκόπιμα. Ο,τιδήποτε άλλο (επιτυχία/αποτυχία μιας ενέργειας) γίνεται toast.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from .theme import CURRENT

_DEFAULT_MS = {"ok": 4200, "warn": 6000, "danger": 8000}


class _ToastItem(QFrame):
    def __init__(self, message: str, level: str) -> None:
        super().__init__()
        self.setObjectName("toastItem")
        colour = {"ok": CURRENT.ok, "warn": CURRENT.warn, "danger": CURRENT.bad}.get(level, CURRENT.accent)
        self.setStyleSheet(
            f"QFrame#toastItem {{ background:{CURRENT.panel}; border:1px solid {CURRENT.line}; "
            f"border-left:3px solid {colour}; border-radius:10px; }}"
        )
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 6, 10)
        label = QLabel(message)
        label.setWordWrap(True)
        label.setStyleSheet(f"color:{CURRENT.txt}; border:none; background:transparent;")
        row.addWidget(label, 1)
        self.close_btn = QPushButton("×")
        self.close_btn.setFlat(True)
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setStyleSheet(f"QPushButton {{ border:none; background:transparent; color:{CURRENT.muted}; "
                                     f"font-size:15px; }} QPushButton:hover {{ color:{CURRENT.txt}; }}")
        row.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)


class ToastHost(QWidget):
    """Μία στοίβα toasts, κάτω-δεξιά μέσα στο παράθυρο-γονέα."""

    _MARGIN = 18
    _WIDTH = 340

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self.setFixedWidth(self._WIDTH)
        self._box = QVBoxLayout(self)
        self._box.setContentsMargins(0, 0, 0, 0)
        self._box.setSpacing(8)
        self.hide()
        host.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt API)
        if watched is self._host and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._reposition()
        return False

    def _reposition(self) -> None:
        self.adjustSize()
        h = max(self.sizeHint().height(), 1)
        x = self._host.width() - self._WIDTH - self._MARGIN
        y = self._host.height() - h - self._MARGIN
        self.setGeometry(max(0, x), max(0, y), self._WIDTH, h)
        self.raise_()

    def add(self, message: str, level: str, ms: int) -> None:
        item = _ToastItem(message, level)
        self._box.addWidget(item)
        item.close_btn.clicked.connect(lambda: self._remove(item))
        self.show()
        self._reposition()
        if ms > 0:
            timer = QTimer(item)
            timer.setSingleShot(True)
            timer.timeout.connect(lambda: self._remove(item))
            timer.start(ms)
            item._timer = timer  # noqa: SLF001 - κρατά τον timer ζωντανό μαζί με το item

    def _remove(self, item: _ToastItem) -> None:
        if self._box.indexOf(item) < 0:
            return  # ήδη αφαιρέθηκε (π.χ. timer + κλικ στο «×» σχεδόν ταυτόχρονα)
        self._box.removeWidget(item)
        item.deleteLater()
        if self._box.count() == 0:
            self.hide()
        else:
            self._reposition()


def toast(window: QWidget, message: str, level: str = "ok", ms: int | None = None) -> None:
    """Δείχνει ένα πλευρικό μήνυμα πάνω στο `window` (`MainWindow` ή οποιοδήποτε `QDialog`).

    `level`: "ok" (πράσινο) | "warn" (πορτοκαλί) | "danger" (κόκκινο). `ms=0` το αφήνει μέχρι να κλείσει
    χειροκίνητα με το «×» — χρήσιμο για σφάλματα που θέλουμε σίγουρα να προσέξει ο χρήστης.
    """
    host = getattr(window, "_toast_host", None)
    if host is None:
        host = ToastHost(window)
        window._toast_host = host  # noqa: SLF001
    host.add(message, level, _DEFAULT_MS.get(level, 4200) if ms is None else ms)
