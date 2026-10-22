"""Ειδοποιήσεις σαν «side flash message» (πλευρικό μήνυμα πάνω-δεξιά), αντί για native QMessageBox.

Παρόμοια ιδέα με το `toast()`/`.toasts-host` του παλιού web UI (`web/static/js/app.js`, εκεί κάτω-δεξιά) — εδώ
πάνω-δεξιά, πιο κοντά στα κουμπιά ενεργειών (ρητό αίτημα χρήστη, 2026-09-22): στοίβα μηνυμάτων, έγχρωμο περίγραμμα
ανά επίπεδο (ok/warn/danger), αυτόματο κλείσιμο, ή με «×». Ζει ως παιδί ΤΟΥ ΠΑΡΑΘΥΡΟΥ όπου
συνέβη η ενέργεια (`MainWindow` ή οποιοδήποτε `QDialog`) — όχι ξεχωριστό top-level παράθυρο, ώστε να μένει πάντα
πάνω από το σωστό περιεχόμενο και να κλείνει μαζί του.

Κρατάμε τα ΕΠΙΒΕΒΑΙΩΤΙΚΑ (Ναι/Όχι πριν από διαγραφή) σε `QMessageBox.question` — δεν είναι ειδοποίηση αλλά
απόφαση που μπλοκάρει σκόπιμα. Ο,τιδήποτε άλλο (επιτυχία/αποτυχία μιας ενέργειας) γίνεται toast.

Μακριές εργασίες (π.χ. ανάκτηση στοιχείων για 160 πελάτες): `ProgressToast` — μόνιμο toast με μπάρα προόδου.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget

from .theme import CURRENT

_DEFAULT_MS = {"ok": 4200, "info": 4200, "warn": 6000, "danger": 5000}


class _ToastItem(QFrame):
    def __init__(self, message: str, level: str, progress: bool = False) -> None:
        super().__init__()
        self.setObjectName("toastItem")
        self.set_level(level)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 6, 10)
        col = QVBoxLayout()
        col.setSpacing(6)
        self.label = QLabel(message)
        self.label.setWordWrap(True)
        self.label.setStyleSheet(f"color:{CURRENT.txt}; border:none; background:transparent;")
        col.addWidget(self.label)
        self.bar: QProgressBar | None = None
        if progress:
            self.bar = QProgressBar()
            self.bar.setRange(0, 0)                      # «απροσδιόριστη» ώσπου να έρθει η πρώτη ενημέρωση
            self.bar.setFixedHeight(16)
            self.bar.setFormat("%p%")
            col.addWidget(self.bar)
        row.addLayout(col, 1)
        self.close_btn = QPushButton("×")
        self.close_btn.setFlat(True)
        self.close_btn.setFixedSize(20, 20)
        self.close_btn.setStyleSheet(f"QPushButton {{ border:none; background:transparent; color:{CURRENT.muted}; "
                                     f"font-size:15px; }} QPushButton:hover {{ color:{CURRENT.txt}; }}")
        row.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignTop)

    def set_level(self, level: str) -> None:
        colour = {"ok": CURRENT.ok, "warn": CURRENT.warn, "danger": CURRENT.bad}.get(level, CURRENT.accent)  # info = accent
        self.setStyleSheet(
            f"QFrame#toastItem {{ background:{CURRENT.panel}; border:1px solid {CURRENT.line}; "
            f"border-left:3px solid {colour}; border-radius:10px; }}"
        )


class ToastHost(QWidget):
    """Μία στοίβα toasts, πάνω-δεξιά μέσα στο παράθυρο-γονέα (ρητό αίτημα χρήστη, 2026-09-22 — πιο κοντά στο
    πάνω μενού/κουμπιά όπου γίνεται η ενέργεια, όχι κάτω-δεξιά όπως το παλιό web UI)."""

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
        # getattr: το φίλτρο μπορεί να κληθεί και κατά τη δημιουργία/καταστροφή, πριν οριστεί (ή αφού χαθεί) το `_host`
        if watched is getattr(self, "_host", None) and event.type() in (QEvent.Type.Resize, QEvent.Type.Move):
            self._reposition()
        return False

    def event(self, event) -> bool:  # noqa: N802 (Qt API)
        # Αλλαγή κειμένου (π.χ. πρόοδος) ή νέο/λιγότερα toasts ζητούν νέο layout: ξαναϋπολογίζουμε ΚΑΙ το δικό μας
        # ύψος — αλλιώς τα toasts στριμώχνονται στο παλιό ύψος.
        if event.type() == QEvent.Type.LayoutRequest:
            QTimer.singleShot(0, self._reposition)
        return super().event(event)

    def _reposition(self) -> None:
        if not self._box.count():
            return
        # Ύψος ΓΙΑ ΤΟ ΣΥΓΚΕΚΡΙΜΕΝΟ πλάτος: τα κείμενα αναδιπλώνονται και το sizeHint() υπολογίζεται για το «φυσικό»
        # πλάτος τους (μία μακριά γραμμή) — δίνει πολύ μικρότερο ύψος. Αυτό στρίμωχνε τα toasts (αναφορά χρήστη,
        # 2026-10-22· επαληθεύτηκε σε πραγματικό παράθυρο: 56px αντί για 137 με δύο toasts).
        self._box.activate()
        if self._box.hasHeightForWidth():
            h = self._box.totalHeightForWidth(self._WIDTH)
        else:
            h = self._box.totalSizeHint().height()
        x = self._host.width() - self._WIDTH - self._MARGIN
        y = self._MARGIN
        if self.geometry().getRect() != (max(0, x), y, self._WIDTH, max(h, 1)):
            self.setGeometry(max(0, x), y, self._WIDTH, max(h, 1))
        self.raise_()

    def add(self, message: str, level: str, ms: int, progress: bool = False) -> _ToastItem:
        item = _ToastItem(message, level, progress)
        self._box.addWidget(item)
        item.close_btn.clicked.connect(lambda: self._remove(item))
        self.show()
        self._reposition()
        if ms > 0:
            self.expire(item, ms)
        return item

    def expire(self, item: _ToastItem, ms: int) -> None:
        timer = QTimer(item)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._remove(item))
        timer.start(ms)
        item._timer = timer  # noqa: SLF001 - κρατά τον timer ζωντανό μαζί με το item

    def contains(self, item: _ToastItem) -> bool:
        return self._box.indexOf(item) >= 0

    def _remove(self, item: _ToastItem) -> None:
        if self._box.indexOf(item) < 0:
            return  # ήδη αφαιρέθηκε (π.χ. timer + κλικ στο «×» σχεδόν ταυτόχρονα)
        self._box.removeWidget(item)
        item.deleteLater()
        if self._box.count() == 0:
            self.hide()
        else:
            self._reposition()


def _host_for(window: QWidget) -> ToastHost:
    host = getattr(window, "_toast_host", None)
    if host is None:
        host = ToastHost(window)
        window._toast_host = host  # noqa: SLF001
    return host


def toast(window: QWidget, message: str, level: str = "ok", ms: int | None = None) -> None:
    """Δείχνει ένα πλευρικό μήνυμα πάνω στο `window` (`MainWindow` ή οποιοδήποτε `QDialog`).

    `level`: "info" (μπλε — «ξεκίνησε μια ενέργεια») | "ok" (πράσινο) | "warn" (πορτοκαλί) | "danger" (κόκκινο, εξαφανίζεται μόνο του μετά από 5"). Πάντα
    κλείνει και χειροκίνητα με το «×». `ms=0` το αφήνει μέχρι να κλείσει μόνο χειροκίνητα — δεν το χρησιμοποιούμε
    πια για σφάλματα (ο χρήστης θέλει να εξαφανίζονται μόνα τους), κρατιέται μόνο ως δυνατότητα.
    """
    _host_for(window).add(message, level, _DEFAULT_MS.get(level, 4200) if ms is None else ms)


class ProgressToast:
    """Μόνιμο toast με μπάρα προόδου: ποσοστό + σε ποιο στοιχείο βρίσκεται η εργασία (ρητό αίτημα χρήστη,
    2026-10-22, για την ανάκτηση στοιχείων). Μένει όσο τρέχει η εργασία· το `finish()` το κάνει κανονικό toast που
    κλείνει μόνο του. Το «×» απλώς το κρύβει — η εργασία συνεχίζει.

    `on_cancel`: κουμπί «Διακοπή» — η εργασία σταματά ΜΕΤΑ τον πελάτη που τρέχει (όχι στη μέση του). `move_to()`: το
    toast περνά σε άλλο παράθυρο (π.χ. από την καρτέλα πελάτη που κλείνει στο κύριο), με την ίδια πρόοδο."""

    def __init__(self, window: QWidget, message: str, on_cancel=None) -> None:
        self._window = window
        self._message, self._done, self._total = message, None, None
        self._on_cancel, self._cancelled = on_cancel, False
        self._host = _host_for(window)
        self._item = self._build()

    def _build(self) -> _ToastItem:
        item = self._host.add(self._message, "info", 0, progress=True)
        self.cancel_btn: QPushButton | None = None
        if self._on_cancel is not None:
            btn = QPushButton("Διακοπή μετά τον τρέχοντα πελάτη")
            btn.setToolTip("Ολοκληρώνεται ο πελάτης που τρέχει τώρα και η εργασία σταματά — ό,τι έχει γίνει κρατιέται")
            btn.clicked.connect(self.cancel)
            btn.setEnabled(not self._cancelled)
            if self._cancelled:
                btn.setText("Διακόπτεται…")
            item.layout().itemAt(0).layout().addWidget(btn)
            self.cancel_btn = btn
        if self._total and item.bar is not None:
            item.bar.setRange(0, self._total)
            item.bar.setValue(max(0, min(self._done or 0, self._total)))
        return item

    @property
    def window(self) -> QWidget:
        return self._window

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        if self._cancelled:
            return
        self._cancelled = True
        if self.cancel_btn is not None and self._alive():
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Διακόπτεται…")
        if self._on_cancel is not None:
            self._on_cancel()

    def _alive(self) -> bool:
        try:
            return self._host.contains(self._item)
        except RuntimeError:                             # το παράθυρο έκλεισε στο μεταξύ
            return False

    def move_to(self, window: QWidget) -> None:
        """Μεταφορά σε άλλο παράθυρο (στη σειρά, κάτω από όσα toasts έχει ήδη εκεί)."""
        if window is self._window:
            return
        if self._alive():
            self._host._remove(self._item)  # noqa: SLF001
        self._window, self._host = window, _host_for(window)
        self._item = self._build()

    def update(self, message: str, done: int | None = None, total: int | None = None) -> None:
        self._message = message
        if total:
            self._done, self._total = done, total
        if not self._alive():
            return
        self._item.label.setText(message)
        if total and self._item.bar is not None:
            self._item.bar.setRange(0, total)
            self._item.bar.setValue(max(0, min(done or 0, total)))

    def finish(self, message: str, level: str = "ok") -> None:
        if not self._alive():
            try:
                toast(self._window, message, level)
            except RuntimeError:
                pass
            return
        self._item.set_level(level)
        self._item.label.setText(message)
        if self._item.bar is not None:
            self._item.bar.hide()
        if self.cancel_btn is not None:
            self.cancel_btn.hide()
        self._host.expire(self._item, _DEFAULT_MS.get(level, 4200))
