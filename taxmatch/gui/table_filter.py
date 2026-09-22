"""Γρήγορο φίλτρο ανά στήλη (Excel-style) για τους πίνακες.

Τρία κομμάτια:

* ``FilterHeader`` — κεφαλίδα που δείχνει ένα «χωνί» στη στήλη **όταν περνάς από
  πάνω** (ή όταν η στήλη έχει ενεργό φίλτρο, οπότε μένει χρωματισμένο). Κλικ στο
  χωνί ανοίγει το φίλτρο· κλικ αλλού ταξινομεί, όπως πάντα. Το χωνί ζωγραφίζεται
  σε «καθαρή» ζώνη ώστε να μην μπλέκει με το κείμενο ή το βελάκι ταξινόμησης.
* ``ColumnFilterPopup`` / ``open_filter_popup`` — ένα **ενσωματωμένο** αναδυόμενο
  (όχι ξεχωριστό παράθυρο) με αναζήτηση, «(Όλα)» και λίστα τιμών με κουτάκια.
* ``TableColumnFilter`` — έτοιμος «ελεγκτής» που κουμπώνει σε οποιονδήποτε
  ``QTableWidget`` και φιλτράρει κρύβοντας γραμμές βάσει του κειμένου των κελιών.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QObject, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
)

from .icons import icon
from .theme import CURRENT

#: Πλάτος ζώνης χωνιού και χώρος που κρατάμε δεξιά για το βελάκι ταξινόμησης.
_HOT = 20
_ARROW = 14
_GLYPH = 13


class FilterHeader(QHeaderView):
    """Οριζόντια κεφαλίδα με χωνί φίλτρου (σε hover ή όταν είναι ενεργό)."""

    filterClicked = Signal(int)

    def __init__(self, filterable: Iterable[int], parent=None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self._filterable = set(filterable)
        self._active: set[int] = set()
        self._hover = -1
        self.setSectionsClickable(True)
        self.setHighlightSections(True)
        self.setMouseTracking(True)

    def set_active(self, columns: set[int]) -> None:
        """Ποιες στήλες έχουν ενεργό φίλτρο (το χωνί μένει χρωματισμένο)."""
        columns = set(columns)
        if columns != self._active:
            self._active = columns
            self.viewport().update()

    # --------------------------------------------------------- hover
    def _set_hover(self, idx: int) -> None:
        if idx != self._hover:
            self._hover = idx
            self.viewport().update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        self._set_hover(self.logicalIndexAt(event.position().toPoint()))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._set_hover(-1)
        super().leaveEvent(event)

    # --------------------------------------------------------- paint
    def _funnel_rect(self, rect: QRect) -> QRect:
        y = rect.y() + (rect.height() - _GLYPH) // 2
        x = rect.right() - _ARROW - _HOT + (_HOT - _GLYPH) // 2
        return QRect(x, y, _GLYPH, _GLYPH)

    def paintSection(self, painter, rect, logical_index) -> None:  # noqa: N802
        super().paintSection(painter, rect, logical_index)
        if logical_index not in self._filterable:
            return
        active = logical_index in self._active
        # Το χωνί φαίνεται μόνο όταν χρειάζεται: σε hover ή όταν η στήλη είναι ήδη
        # φιλτραρισμένη. Έτσι οι υπόλοιπες κεφαλίδες μένουν καθαρές, χωρίς να
        # «μπλέκει» το εικονίδιο με το κείμενο.
        if not active and logical_index != self._hover:
            return
        painter.save()
        # Καθαρή ζώνη κάτω από το χωνί ώστε να μην περνά από πίσω κείμενο.
        zone = QRect(rect.right() - _ARROW - _HOT, rect.top() + 1, _HOT,
                     rect.height() - 2)
        painter.fillRect(zone, QColor(CURRENT.bg))
        color = CURRENT.accent if active else CURRENT.muted
        pixmap = icon("filter", color, _GLYPH).pixmap(QSize(_GLYPH, _GLYPH))
        painter.drawPixmap(self._funnel_rect(rect), pixmap)
        painter.restore()

    # --------------------------------------------------------- click
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            idx = self.logicalIndexAt(event.position().toPoint())
            if idx in self._filterable:
                right = self.sectionViewportPosition(idx) + self.sectionSize(idx)
                x = event.position().toPoint().x()
                if right - _ARROW - _HOT <= x < right - _ARROW:
                    self.filterClicked.emit(idx)
                    return  # δεν προωθείται: δεν ταξινομεί
        super().mousePressEvent(event)


class ColumnFilterPopup(QFrame):
    """Ενσωματωμένο αναδυόμενο φίλτρο (όχι ξεχωριστό παράθυρο).

    Εφαρμόζει «ζωντανά»: κάθε τσεκάρισμα ενημερώνει αμέσως τον πίνακα. Κλείνει με
    κλικ έξω από το πλαίσιο (``Qt.Popup``) ή με το κουμπί «Κλείσιμο».
    """

    def __init__(self, title, values, selected, on_apply, parent=None) -> None:
        super().__init__(parent, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setObjectName("filterPopup")
        self.setStyleSheet(
            f"QFrame#filterPopup {{ background:{CURRENT.panel}; "
            f"border:1px solid {CURRENT.accent}; border-radius:9px; }}"
        )
        self._on_apply = on_apply
        self._loading = True
        self._build(title, values, selected)
        self._loading = False

    def _build(self, title, values, selected) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(7)
        self.setMinimumWidth(230)

        heading = QLabel(f"Φίλτρο: {title}")
        heading.setStyleSheet(f"color:{CURRENT.accent}; font-weight:700;")
        root.addWidget(heading)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Αναζήτηση τιμής…")
        self.search.textChanged.connect(self._apply_search)
        root.addWidget(self.search)

        self.list = QListWidget()
        self.list.setMaximumHeight(260)
        all_checked = not selected or selected >= set(values)
        all_item = QListWidgetItem("(Όλα)")
        all_item.setFlags(all_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        all_item.setCheckState(
            Qt.CheckState.Checked if all_checked else Qt.CheckState.PartiallyChecked
        )
        self.list.addItem(all_item)
        for value in values:
            item = QListWidgetItem(value or "—")
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if (all_checked or value in selected)
                else Qt.CheckState.Unchecked
            )
            self.list.addItem(item)
        self.list.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.list, 1)

        close = QPushButton("Κλείσιμο")
        close.clicked.connect(self.close)
        root.addWidget(close)

    def _value_items(self) -> list[QListWidgetItem]:
        return [self.list.item(i) for i in range(1, self.list.count())]

    def _apply_search(self, text: str) -> None:
        needle = text.strip().lower()
        for item in self._value_items():
            item.setHidden(bool(needle) and needle not in item.text().lower())

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        self.list.blockSignals(True)
        if item is self.list.item(0):  # «(Όλα)»
            state = item.checkState()
            if state != Qt.CheckState.PartiallyChecked:
                for row in self._value_items():
                    if not row.isHidden():
                        row.setCheckState(state)
        else:
            self._sync_all()
        self.list.blockSignals(False)
        self._on_apply(self.selected_values())

    def _sync_all(self) -> None:
        items = self._value_items()
        checked = sum(it.checkState() == Qt.CheckState.Checked for it in items)
        all_item = self.list.item(0)
        if checked == 0:
            all_item.setCheckState(Qt.CheckState.Unchecked)
        elif checked == len(items):
            all_item.setCheckState(Qt.CheckState.Checked)
        else:
            all_item.setCheckState(Qt.CheckState.PartiallyChecked)

    def selected_values(self) -> set[str]:
        return {
            it.data(Qt.ItemDataRole.UserRole)
            for it in self._value_items()
            if it.checkState() == Qt.CheckState.Checked
        }


def open_filter_popup(header: FilterHeader, col: int, title, values, selected,
                      on_apply) -> None:
    """Ανοίγει το ενσωματωμένο φίλτρο ακριβώς κάτω από την κεφαλίδα της στήλης.

    Το άνοιγμα αναβάλλεται (``singleShot(0)``) ώστε να ολοκληρωθεί πρώτα το κλικ
    που το κάλεσε — αλλιώς ένα ``Qt.Popup`` θα έκλεινε ακαριαία από το release.
    """
    x = header.sectionViewportPosition(col)
    top_left = header.mapToGlobal(header.rect().topLeft())
    popup = ColumnFilterPopup(title, values, set(selected), on_apply, header)
    popup.adjustSize()
    popup.move(top_left.x() + x, top_left.y() + header.height())
    QTimer.singleShot(0, popup.show)


class TableColumnFilter(QObject):
    """Γρήγορο φίλτρο στηλών για οποιονδήποτε ``QTableWidget``.

    Φιλτράρει κρύβοντας γραμμές (``setRowHidden``) βάσει του **κειμένου** των
    κελιών — δεν χρειάζεται να ξέρει το μοντέλο δεδομένων του πίνακα. Ξανα-
    εφαρμόζεται μόνο του μετά από ταξινόμηση ή ξαναγέμισμα του πίνακα.
    """

    #: Εκπέμπεται όταν αλλάζει το σύνολο των ενεργών φίλτρων στήλης.
    filtersChanged = Signal()

    def __init__(self, table: QTableWidget, filterable: Iterable[int]) -> None:
        super().__init__(table)
        self.table = table
        self.filters: dict[int, set[str]] = {}
        self._pending = False
        header = FilterHeader(filterable, table)
        header.filterClicked.connect(self._open)
        table.setHorizontalHeader(header)
        self._header = header
        header.sortIndicatorChanged.connect(lambda *_: self._schedule())
        model = table.model()
        model.rowsInserted.connect(lambda *_: self._schedule())
        model.modelReset.connect(lambda *_: self._schedule())
        model.layoutChanged.connect(lambda *_: self._schedule())

    def _text(self, row: int, col: int) -> str:
        item = self.table.item(row, col)
        return item.text() if item is not None else ""

    def _distinct(self, col: int) -> list[str]:
        return sorted({self._text(r, col) for r in range(self.table.rowCount())})

    def _open(self, col: int) -> None:
        values = self._distinct(col)
        selected = self.filters.get(col) or set(values)
        label = self.table.horizontalHeaderItem(col)
        title = label.text() if label else "Στήλη"

        def apply(chosen: set[str]) -> None:
            if not chosen or chosen == set(values):
                self.filters.pop(col, None)
            else:
                self.filters[col] = chosen
            self.apply()
            self.filtersChanged.emit()

        open_filter_popup(self._header, col, title or "Στήλη", values, selected, apply)

    def clear(self) -> None:
        """Καθαρίζει όλα τα φίλτρα στήλης και ξαναδείχνει τις γραμμές."""
        if self.filters:
            self.filters.clear()
            self.apply()
            self.filtersChanged.emit()

    def has_filters(self) -> bool:
        return any(self.filters.values())

    def _schedule(self) -> None:
        # Συγχώνευση: πολλά σήματα (ανά γραμμή στο γέμισμα) -> μία εφαρμογή.
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self._run)

    def _run(self) -> None:
        self._pending = False
        self.apply()

    def apply(self) -> None:
        for row in range(self.table.rowCount()):
            hide = any(
                allowed and self._text(row, col) not in allowed
                for col, allowed in self.filters.items()
            )
            self.table.setRowHidden(row, hide)
        self._header.set_active({c for c, v in self.filters.items() if v})
