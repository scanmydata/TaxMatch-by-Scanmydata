"""Παράμετροι γραφείου, τιμοκατάλογος υπηρεσιών (Ρυθμίσεις) και αμοιβές πελάτη (καρτέλα πελάτη → «Αμοιβές»).

Λογική/αποθήκευση: `taxmatch.office`. Εδώ μόνο τα widgets — επεξεργασία μέσα στον πίνακα (όνομα/ποσό/περιοδικότητα)
και ΕΝΑ κουμπί αποθήκευσης ανά ενότητα, ώστε ο χρήστης να διορθώνει πολλές γραμμές μαζί.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMenu, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import office
from .icons import icon
from .table_kit import interactive
from .theme import CURRENT
from .toast import toast
from .widgets import GrDateEdit


def _amount_box(value: float = 0.0) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(0, 1_000_000)
    box.setDecimals(2)
    box.setSuffix(" €")
    box.setGroupSeparatorShown(True)
    box.setButtonSymbols(QDoubleSpinBox.ButtonSymbols.NoButtons)
    box.setValue(float(value or 0))
    return box


def _period_box(value: str = "monthly") -> QComboBox:
    box = QComboBox()
    for key, label in office.PERIODS:
        box.addItem(label, key)
    box.setCurrentIndex(max(0, box.findData(value)))
    return box


class OfficeSettingsBox(QGroupBox):
    """Ρυθμίσεις → «Γραφείο»: τα στοιχεία που μπαίνουν στην κεφαλίδα εγγράφων, στα συμφωνητικά και στις αμοιβές."""

    def __init__(self, conn: sqlite3.Connection, window: QWidget) -> None:
        super().__init__("Γραφείο — παράμετροι")
        self.conn, self.window_ = conn, window
        form = QFormLayout(self)
        hint = QLabel("Χρησιμοποιούνται στην κεφαλίδα των εγγράφων (Δέουσα επιμέλεια), στο ιδιωτικό συμφωνητικό και "
                      "στις αμοιβές πελατών. Αν έχετε γράψει δική σας κεφαλίδα στη Δέουσα επιμέλεια → Μεθοδολογία, "
                      "υπερισχύει εκείνη.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        form.addRow(hint)
        self.fields: dict[str, QLineEdit] = {}
        for f in office.OFFICE_FIELDS:
            edit = QLineEdit()
            edit.setPlaceholderText(f.placeholder)
            self.fields[f.key] = edit
            form.addRow(f.label, edit)
        save = QPushButton("Αποθήκευση στοιχείων γραφείου")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        form.addRow("", save)
        self.reload()

    def reload(self) -> None:
        values = office.get_office(self.conn)
        for key, edit in self.fields.items():
            edit.setText(values.get(key, ""))

    def save(self) -> None:
        rate = self.fields["office_vat_rate"].text().strip().replace(",", ".")
        if rate:
            try:
                float(rate)
            except ValueError:
                toast(self.window_, "Ο συντελεστής ΦΠΑ πρέπει να είναι αριθμός (π.χ. 24).", "warn")
                return
        office.save_office(self.conn, {k: e.text() for k, e in self.fields.items()})
        toast(self.window_, "Τα στοιχεία του γραφείου αποθηκεύτηκαν.", "ok")


class ServicesBox(QGroupBox):
    """Ρυθμίσεις → «Τιμοκατάλογος υπηρεσιών»: όνομα, περιοδικότητα, προτεινόμενη αμοιβή (προ ΦΠΑ)."""

    def __init__(self, conn: sqlite3.Connection, window: QWidget) -> None:
        super().__init__("Τιμοκατάλογος υπηρεσιών")
        self.conn, self.window_ = conn, window
        box = QVBoxLayout(self)
        hint = QLabel("Οι υπηρεσίες του γραφείου με την προτεινόμενη αμοιβή τους (προ ΦΠΑ). Στην καρτέλα κάθε πελάτη → "
                      "«Αμοιβές» τις προσθέτετε με ένα κλικ και αλλάζετε το ποσό όπου χρειάζεται.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        box.addWidget(hint)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Υπηρεσία", "Περιοδικότητα", "Αμοιβή"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)   # χώρος για combo/ποσό/ημερομηνία μέσα στο κελί
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        interactive(self.table)                                 # στήλες που σύρονται (πίνακας καταχώρισης: χωρίς ταξινόμηση)
        self.table.setColumnWidth(0, 320)
        self.table.setColumnWidth(1, 140)
        self.table.setColumnWidth(2, 130)
        self.table.setMinimumHeight(220)
        box.addWidget(self.table)
        row = QHBoxLayout()
        add = QPushButton("+ Νέα υπηρεσία")
        add.clicked.connect(lambda: self._add_row())
        row.addWidget(add)
        remove = QPushButton(icon("delete", CURRENT.bad, 16), "  Αφαίρεση")
        remove.clicked.connect(self._remove_row)
        row.addWidget(remove)
        suggested = QPushButton("Προτεινόμενες υπηρεσίες")
        suggested.setToolTip("Γεμίζει άδειο τιμοκατάλογο με συνηθισμένες υπηρεσίες λογιστικού γραφείου (ποσό 0)")
        suggested.clicked.connect(self._seed)
        row.addWidget(suggested)
        row.addStretch()
        save = QPushButton("Αποθήκευση τιμοκαταλόγου")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        row.addWidget(save)
        box.addLayout(row)
        self._removed: list[int] = []
        self.reload()

    def _add_row(self, service: Optional[dict] = None) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        name = QTableWidgetItem((service or {}).get("name", ""))
        name.setData(Qt.ItemDataRole.UserRole, (service or {}).get("id"))
        self.table.setItem(r, 0, name)
        self.table.setCellWidget(r, 1, _period_box((service or {}).get("period", "monthly")))
        self.table.setCellWidget(r, 2, _amount_box((service or {}).get("default_amount", 0)))
        if service is None:
            self.table.setCurrentCell(r, 0)
            self.table.editItem(name)

    def _remove_row(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        sid = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        if sid:
            self._removed.append(int(sid))
        self.table.removeRow(r)

    def _seed(self) -> None:
        if self.table.rowCount():
            toast(self.window_, "Ο τιμοκατάλογος έχει ήδη υπηρεσίες — οι προτεινόμενες μπαίνουν μόνο σε άδειο.", "warn")
            return
        n = office.seed_services(self.conn)
        self.reload()
        toast(self.window_, f"Προστέθηκαν {n} προτεινόμενες υπηρεσίες — ορίστε τα ποσά και πατήστε Αποθήκευση.", "ok")

    def reload(self) -> None:
        self._removed = []
        self.table.setRowCount(0)
        for s in office.list_services(self.conn):
            self._add_row(s)

    def save(self) -> None:
        for sid in self._removed:
            office.delete_service(self.conn, sid)
        saved = 0
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            name = item.text().strip() if item else ""
            if not name:
                continue
            sid = office.save_service(self.conn, name, self.table.cellWidget(r, 2).value(),
                                      self.table.cellWidget(r, 1).currentData(),
                                      service_id=item.data(Qt.ItemDataRole.UserRole))
            item.setData(Qt.ItemDataRole.UserRole, sid)
            saved += 1
        self._removed = []
        toast(self.window_, f"Ο τιμοκατάλογος αποθηκεύτηκε ({saved} υπηρεσίες).", "ok")


class ClientFeesWidget(QWidget):
    """Καρτέλα πελάτη → «Αμοιβές»: ό,τι έχει συμφωνηθεί με τον πελάτη, με σύνολα μηνιαία/ετήσια και με ΦΠΑ."""

    _COLS = ["Υπηρεσία", "Περιοδικότητα", "Ποσό (προ ΦΠΑ)", "Από", "Έως", "Σημειώσεις"]

    def __init__(self, conn: sqlite3.Connection, afm: str, window: QWidget,
                 on_change: Optional[Callable[[], None]] = None) -> None:
        super().__init__()
        self.conn, self.afm, self.window_, self.on_change = conn, afm, window, on_change
        box = QVBoxLayout(self)
        hint = QLabel("Οι αμοιβές που έχουν συμφωνηθεί με τον πελάτη (προ ΦΠΑ). Γεμίζουν και το ιδιωτικό συμφωνητικό "
                      "της Δέουσας επιμέλειας· η μηνιαία αμοιβή φαίνεται και στη λίστα πελατών.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        box.addWidget(hint)
        self.table = QTableWidget(0, len(self._COLS))
        self.table.setHorizontalHeaderLabels(self._COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)   # χώρος για combo/ποσό/ημερομηνία μέσα στο κελί
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        interactive(self.table)                                 # στήλες που σύρονται (πίνακας καταχώρισης: χωρίς ταξινόμηση)
        for col, width in ((0, 260), (1, 120), (2, 130), (3, 118), (4, 118), (5, 170)):
            self.table.setColumnWidth(col, width)
        box.addWidget(self.table, 1)
        self.totals = QLabel("")
        self.totals.setObjectName("h2")
        box.addWidget(self.totals)
        row = QHBoxLayout()
        self.from_catalog = QPushButton("+ Από τον τιμοκατάλογο  ▾")
        self.from_catalog.setObjectName("primary")
        self.from_catalog.clicked.connect(self._catalog_menu)
        row.addWidget(self.from_catalog)
        add = QPushButton("+ Άλλη αμοιβή")
        add.clicked.connect(lambda: self._add_row())
        row.addWidget(add)
        remove = QPushButton(icon("delete", CURRENT.bad, 16), "  Αφαίρεση")
        remove.clicked.connect(self._remove_row)
        row.addWidget(remove)
        row.addStretch()
        save = QPushButton("Αποθήκευση αμοιβών")
        save.setObjectName("primary")
        save.clicked.connect(self.save)
        row.addWidget(save)
        box.addLayout(row)
        self._removed: list[int] = []
        self.reload()

    def _catalog_menu(self) -> None:
        menu = QMenu(self)
        services = office.list_services(self.conn, active_only=True)
        if not services:
            act = menu.addAction("Ο τιμοκατάλογος είναι άδειος — ορίστε τον στις Ρυθμίσεις → Τιμοκατάλογος υπηρεσιών")
            act.setEnabled(False)
        for s in services:
            label = f"{s['name']} — {office.money(s['default_amount'])} ({office.PERIOD_LABEL[s['period']].lower()})"
            menu.addAction(label, lambda s=s: self._add_row({"description": s["name"], "amount": s["default_amount"],
                                                             "period": s["period"], "service_id": s["id"]}))
        menu.exec(self.from_catalog.mapToGlobal(self.from_catalog.rect().bottomLeft()))

    def _add_row(self, fee: Optional[dict] = None) -> None:
        fee = fee or {}
        r = self.table.rowCount()
        self.table.insertRow(r)
        desc = QTableWidgetItem(fee.get("description", ""))
        desc.setData(Qt.ItemDataRole.UserRole, fee.get("id"))
        desc.setData(Qt.ItemDataRole.UserRole + 1, fee.get("service_id"))
        self.table.setItem(r, 0, desc)
        period = _period_box(fee.get("period", "monthly"))
        period.currentIndexChanged.connect(self._update_totals)
        self.table.setCellWidget(r, 1, period)
        amount = _amount_box(fee.get("amount", 0))
        amount.valueChanged.connect(self._update_totals)
        self.table.setCellWidget(r, 2, amount)
        for col, key in ((3, "start_date"), (4, "end_date")):
            edit = GrDateEdit()
            edit.setSpecialValueText(" ")                # κενή ημερομηνία = χωρίς όριο
            edit.setMinimumDate(edit.minimumDate())
            if fee.get(key):
                from datetime import date
                edit.setDate(date.fromisoformat(fee[key]))
            else:
                edit.setDate(edit.minimumDate())
            self.table.setCellWidget(r, col, edit)
        self.table.setItem(r, 5, QTableWidgetItem(fee.get("notes", "")))
        if not fee.get("description"):
            self.table.setCurrentCell(r, 0)
            self.table.editItem(desc)
        self._update_totals()

    def _remove_row(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        fid = self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)
        if fid:
            self._removed.append(int(fid))
        self.table.removeRow(r)
        self._update_totals()

    def _date(self, r: int, col: int) -> str:
        edit = self.table.cellWidget(r, col)
        return "" if edit.date() == edit.minimumDate() else edit.date().toString("yyyy-MM-dd")

    def _update_totals(self, *_args) -> None:
        monthly = sum(office.per_month(self.table.cellWidget(r, 2).value(), self.table.cellWidget(r, 1).currentData())
                      for r in range(self.table.rowCount()) if self.table.cellWidget(r, 2))
        once = sum(self.table.cellWidget(r, 2).value() for r in range(self.table.rowCount())
                   if self.table.cellWidget(r, 1) and self.table.cellWidget(r, 1).currentData() == "once")
        rate = office.vat_rate(self.conn)
        text = (f"Μηνιαία: {office.money(monthly)} · Ετήσια: {office.money(monthly * 12)} · "
                f"με ΦΠΑ {rate:g}%: {office.money(monthly * (1 + rate / 100))}/μήνα")
        if once:
            text += f" · Εφάπαξ: {office.money(once)}"
        self.totals.setText(text)

    def reload(self) -> None:
        self._removed = []
        self.table.setRowCount(0)
        for fee in office.list_fees(self.conn, self.afm):
            self._add_row(fee)
        self._update_totals()

    def save(self) -> None:
        for fid in self._removed:
            office.delete_fee(self.conn, fid)
        saved = 0
        for r in range(self.table.rowCount()):
            item = self.table.item(r, 0)
            desc = item.text().strip() if item else ""
            if not desc:
                continue
            notes = self.table.item(r, 5).text() if self.table.item(r, 5) else ""
            fid = office.save_fee(self.conn, self.afm, desc, self.table.cellWidget(r, 2).value(),
                                  self.table.cellWidget(r, 1).currentData(), fee_id=item.data(Qt.ItemDataRole.UserRole),
                                  service_id=item.data(Qt.ItemDataRole.UserRole + 1),
                                  start_date=self._date(r, 3), end_date=self._date(r, 4), notes=notes)
            item.setData(Qt.ItemDataRole.UserRole, fid)
            saved += 1
        self._removed = []
        toast(self.window_, f"Οι αμοιβές αποθηκεύτηκαν ({saved}).", "ok")
        if self.on_change:
            self.on_change()
