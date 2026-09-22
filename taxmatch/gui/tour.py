"""Ξενάγηση για νέους χρήστες.

Σκουραίνει το παράθυρο, φωτίζει ένα στοιχείο τη φορά και εξηγεί τι κάνει. Ζει
ως παιδί του κύριου παραθύρου και όχι ως ξεχωριστό popup: έτσι ακολουθεί το
παράθυρο όταν μετακινείται και δεν χάνεται πίσω του.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QEvent, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .theme import CURRENT

_MARGIN = 10
_CARD_W = 320


@dataclass
class Step:
    """Ένα βήμα: τι δείχνουμε, τι λέμε, και τι πρέπει να γίνει πριν φανεί."""

    title: str
    text: str
    target: Callable[[], QWidget | None] = field(default=lambda: None)
    before: Callable[[], None] | None = None


class Tour(QWidget):
    #: True αν ο χρήστης έφτασε ως το τέλος, False αν παρέλειψε ή πάτησε Escape.
    #: Η διάκριση μετράει: μόνο μετά από ολοκληρωμένη ξενάγηση σβήνουμε τα
    #: δεδομένα επίδειξης — αλλιώς όποιος την παρέλειπε θα έμενε με άδεια οθόνη
    #: χωρίς να έχει δει τι κάνει η εφαρμογή.
    finished = Signal(bool)

    def __init__(self, host: QWidget, steps: list[Step]) -> None:
        super().__init__(host)
        self._host = host
        self._steps = steps
        self._index = 0
        self._hole = QRect()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._build_card()
        host.installEventFilter(self)

    def _build_card(self) -> None:
        self.card = QFrame(self)
        self.card.setObjectName("tourCard")
        self.card.setFixedWidth(_CARD_W)
        box = QVBoxLayout(self.card)
        box.setContentsMargins(16, 14, 16, 12)
        box.setSpacing(7)

        self.step_label = QLabel()
        self.step_label.setObjectName("tourStep")
        box.addWidget(self.step_label)

        # Το πλάτος καρφώνεται στις ίδιες τις ετικέτες και όχι μόνο στην κάρτα:
        # ένα QLabel με wordWrap δεν ξέρει το ύψος του αν δεν ξέρει το πλάτος
        # του, οπότε το adjustSize() έκοβε την τελευταία γραμμή του κειμένου.
        inner = _CARD_W - 32

        self.title_label = QLabel()
        self.title_label.setObjectName("tourTitle")
        self.title_label.setWordWrap(True)
        self.title_label.setFixedWidth(inner)
        box.addWidget(self.title_label)

        self.text_label = QLabel()
        self.text_label.setWordWrap(True)
        self.text_label.setFixedWidth(inner)
        box.addWidget(self.text_label)
        box.addSpacing(4)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.btn_skip = QPushButton("Παράλειψη")
        self.btn_skip.setToolTip("Κλείσιμο της ξενάγησης")
        # Ρητό lambda: το clicked στέλνει το «checked» ως πρώτο όρισμα, που θα
        # κατέληγε κατά λάθος στο `completed`.
        self.btn_skip.clicked.connect(lambda: self.stop(completed=False))
        buttons.addWidget(self.btn_skip)
        buttons.addStretch()

        self.btn_prev = QPushButton("Πίσω")
        self.btn_prev.clicked.connect(lambda: self.go(self._index - 1))
        buttons.addWidget(self.btn_prev)

        self.btn_next = QPushButton("Επόμενο")
        self.btn_next.setObjectName("primary")
        self.btn_next.clicked.connect(lambda: self.go(self._index + 1))
        buttons.addWidget(self.btn_next)
        box.addLayout(buttons)

    # ------------------------------------------------------------------ ροή
    def start(self) -> None:
        self.setGeometry(self._host.rect())
        self.show()
        self.raise_()
        self.go(0)

    def stop(self, completed: bool = False) -> None:
        self.hide()
        self.finished.emit(completed)

    def go(self, index: int) -> None:
        if index < 0:
            return
        if index >= len(self._steps):
            self.stop(completed=True)
            return
        self._index = index
        step = self._steps[index]
        if step.before:
            step.before()

        self.step_label.setText(f"Βήμα {index + 1} από {len(self._steps)}")
        self.title_label.setText(step.title)
        self.text_label.setText(step.text)
        self.btn_prev.setEnabled(index > 0)
        self.btn_next.setText("Τέλος" if index == len(self._steps) - 1 else "Επόμενο")

        self._relayout()

    # ------------------------------------------------------------- γεωμετρία
    def _target_rect(self) -> QRect:
        widget = self._steps[self._index].target()
        if widget is None or not widget.isVisible():
            return QRect()
        top_left = widget.mapTo(self._host, QPoint(0, 0))
        return QRect(top_left, widget.size()).adjusted(-6, -6, 6, 6)

    def _relayout(self) -> None:
        self.setGeometry(self._host.rect())
        self._hole = self._target_rect()
        # activate() πριν το sizeHint: αλλιώς το layout δίνει το ύψος του
        # προηγούμενου βήματος, που έχει άλλο μήκος κειμένου.
        self.card.layout().activate()
        self.card.resize(self.card.sizeHint())
        self.card.move(self._card_position())
        self.update()

    def _card_position(self) -> QPoint:
        """Δίπλα στο φωτισμένο στοιχείο — και αν δεν χωράει, από την άλλη.

        Χωρίς αυτόν τον έλεγχο, η κάρτα για ένα κουμπί στη δεξιά άκρη θα έβγαινε
        έξω από το παράθυρο και θα κοβόταν.
        """
        size = self.card.size()
        if self._hole.isNull():
            return QRect(self.rect()).center() - QPoint(size.width() // 2,
                                                        size.height() // 2)

        x = self._hole.right() + _MARGIN
        if x + size.width() > self.width() - _MARGIN:
            x = self._hole.left() - size.width() - _MARGIN
        if x < _MARGIN:
            x = max(_MARGIN, min(self._hole.left(),
                                 self.width() - size.width() - _MARGIN))
        y = self._hole.top()
        if y + size.height() > self.height() - _MARGIN:
            y = self.height() - size.height() - _MARGIN
        return QPoint(max(_MARGIN, x), max(_MARGIN, y))

    def eventFilter(self, watched, event) -> bool:
        if watched is self._host and event.type() in (
            QEvent.Type.Resize, QEvent.Type.Move
        ) and self.isVisible():
            self._relayout()
        return super().eventFilter(watched, event)

    # ------------------------------------------------------------- ζωγραφική
    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        veil = QPainterPath()
        veil.addRect(self.rect())
        if not self._hole.isNull():
            cut = QPainterPath()
            cut.addRoundedRect(self._hole, 10, 10)
            veil = veil.subtracted(cut)
        painter.fillPath(veil, QColor(0, 0, 0, 155))

        if not self._hole.isNull():
            pen = painter.pen()
            pen.setColor(QColor(CURRENT.accent))
            pen.setWidth(2)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(self._hole, 10, 10)
        painter.end()

    def mousePressEvent(self, event) -> None:
        # Κλικ έξω από την κάρτα δεν πρέπει να πατήσει το κουμπί από κάτω: η
        # ξενάγηση εξηγεί, δεν εκτελεί.
        event.accept()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.stop()
        elif event.key() in (Qt.Key.Key_Right, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.go(self._index + 1)
        elif event.key() == Qt.Key.Key_Left:
            self.go(self._index - 1)
        else:
            super().keyPressEvent(event)
