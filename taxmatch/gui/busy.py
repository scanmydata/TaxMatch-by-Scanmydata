"""Επικάλυψη αναμονής.

Μπαίνει πάνω από ό,τι αργεί, ώστε ο χρήστης να μη νομίζει ότι κόλλησε το
πρόγραμμα και να μην πατάει κουμπιά που θα ξαναρχίσουν την ίδια δουλειά.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .theme import CURRENT


class BusyOverlay(QWidget):
    """Ημιδιάφανο πέπλο με περιστρεφόμενο δείκτη και κείμενο.

    Ζει ως παιδί του widget που καλύπτει: έτσι ακολουθεί μέγεθος και θέση χωρίς
    δικό μας κώδικα, και δεν μένει ποτέ ορφανό πάνω από άλλο παράθυρο.
    """

    #: Ρυθμός καρέ. 60 fps για ένα spinner είναι σπατάλη — το μάτι δεν το πιάνει.
    _INTERVAL_MS = 33

    def __init__(self, host: QWidget) -> None:
        super().__init__(host)
        self._host = host
        self._angle = 0
        self._text = ""
        self._timer = QTimer(self)
        self._timer.setInterval(self._INTERVAL_MS)
        self._timer.timeout.connect(self._spin)
        self.hide()
        host.installEventFilter(self)

    def _spin(self) -> None:
        self._angle = (self._angle + 12) % 360
        self.update()

    def start(self, text: str = "Παρακαλώ περιμένετε…") -> None:
        self._text = text
        self.setGeometry(self._host.rect())
        self.raise_()
        self.show()
        self._timer.start()
        # Χωρίς αυτό, το πέπλο εμφανίζεται μόνο όταν ησυχάσει το event loop —
        # δηλαδή αφού τελειώσει η δουλειά που ήρθε να καλύψει.
        self.repaint()

    def stop(self) -> None:
        self._timer.stop()
        self.hide()

    @property
    def running(self) -> bool:
        return self._timer.isActive()

    def eventFilter(self, watched, event) -> bool:
        if watched is self._host and event.type() is QEvent.Type.Resize:
            self.setGeometry(self._host.rect())
        return super().eventFilter(watched, event)

    # Το πέπλο τρώει τα κλικ: όσο τρέχει η δουλειά, τα κουμπιά από κάτω δεν
    # πρέπει να πατιούνται.
    def mousePressEvent(self, event) -> None:
        event.accept()

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        veil = QColor(CURRENT.bg)
        veil.setAlpha(205)
        painter.fillRect(self.rect(), veil)

        centre = self.rect().center()
        radius = 15
        box = QRectF(centre.x() - radius, centre.y() - radius - 14,
                     radius * 2, radius * 2)

        painter.setPen(QPen(QColor(CURRENT.line), 3))
        painter.drawEllipse(box)

        pen = QPen(QColor(CURRENT.accent), 3)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        # Το Qt μετρά σε 1/16 της μοίρας.
        painter.drawArc(box, -self._angle * 16, 100 * 16)

        if self._text:
            painter.setPen(QColor(CURRENT.txt))
            painter.drawText(
                self.rect().adjusted(20, 34, -20, 0),
                Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
                | Qt.TextFlag.TextWordWrap,
                self._text,
            )
        painter.end()
