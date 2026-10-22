"""Καρτέλα πελάτη → «Αιτήματα ΑΑΔΕ»: «Τα Αιτήματά μου» (myAADE) με κατάσταση, απάντηση και τι άλλαξε από την τελευταία
φορά· «Άνοιγμα» φέρνει το πλήρες αίτημα και τα συνημμένα (υποβολής ΚΑΙ απάντησης). Λογική: `requests_aade.py` (port του
ScanMyData Tax Center). Μόνο ανάγνωση."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import db as dbmod
from .. import requests_aade as req
from .icons import icon
from .theme import CURRENT
from .toast import toast
from .workers import run_task

_COLS = ["", "Υποβολή", "Ενημέρωση", "Αρ. υπόθεσης", "Διαδικασία / θέμα", "Κατάσταση"]


class RequestsTab(QWidget):
    def __init__(self, conn, afm: str, window: QWidget, main: Any = None) -> None:
        super().__init__()
        self.conn, self.afm, self.window_, self.main = conn, afm, window, main
        self._tasks: list[Any] = []
        self._rows: list[dict[str, Any]] = []
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        refresh = QPushButton(icon("refresh", CURRENT.txt, 16), "  Ενημέρωση αιτημάτων")
        refresh.setObjectName("primary")
        refresh.setToolTip("Λίστα «Τα Αιτήματά μου» από το myAADE, με τους κωδικούς TAXISnet του πελάτη — μόνο ανάγνωση")
        refresh.clicked.connect(self._refresh)
        bar.addWidget(refresh)
        open_btn = QPushButton(icon("download", CURRENT.txt, 16), "  Άνοιγμα αιτήματος (με συνημμένα)")
        open_btn.clicked.connect(self._open)
        bar.addWidget(open_btn)
        bar.addStretch()
        root.addLayout(bar)
        self.info = QLabel("")
        self.info.setObjectName("muted")
        root.addWidget(self.info)
        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for col, width in enumerate((26, 120, 120, 110, 0, 200)):
            if width:
                self.table.setColumnWidth(col, width)
        self.table.itemSelectionChanged.connect(self._show)
        self.table.doubleClicked.connect(lambda *_: self._open())
        root.addWidget(self.table, 2)
        self.detail = QLabel("")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.TextFormat.RichText)
        self.detail.setObjectName("card")
        self.detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.detail, 1)
        self.files = QListWidget()
        self.files.setMaximumHeight(100)
        self.files.itemActivated.connect(self._open_file)
        root.addWidget(self.files)
        self.reload()

    def reload(self) -> None:
        self._rows = req.client_requests(self.conn, self.afm)
        sync = self.conn.execute("SELECT * FROM request_sync WHERE afm=?", (self.afm,)).fetchone()
        unseen = sum(1 for r in self._rows if not r["seen"])
        if sync and sync["status"] == "ok":
            self.info.setText(f"{len(self._rows)} αιτήματα · τελευταία ενημέρωση {sync['synced_at'][:16].replace('T', ' ')}"
                              + (f" · {unseen} με αλλαγές που δεν έχετε δει" if unseen else ""))
        elif sync:
            self.info.setText(f"Η τελευταία ενημέρωση απέτυχε: {sync['message']}")
        else:
            self.info.setText("Δεν έχει γίνει ενημέρωση — «Ενημέρωση αιτημάτων».")
        t = self.table
        t.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            mark = "●" if not r["seen"] else ("✓" if req.answered({"answer": r["answer"], "status": r["status"]}) else "")
            cells = [mark, req.date_text(r["submitted"]), req.date_text(r["updated"]), r["case_no"],
                     " — ".join(x for x in (r["process"], r["topic"]) if x) or "Αίτημα",
                     r["status"] + (" · απορρίφθηκε" if r["rejected"] else "")]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, i)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setForeground(QColor(CURRENT.accent if not r["seen"] else CURRENT.ok))
                    if r["change"] and not r["seen"]:
                        item.setToolTip(req.CHANGE_LABEL.get(r["change"], ""))
                if col == 5 and req.stage(r["status"]) == "waiting_client":
                    item.setForeground(QColor(CURRENT.warn))
                if not r["seen"]:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                t.setItem(i, col, item)
        if not self._rows:
            self.detail.setText("Κανένα αίτημα.")

    def _selected(self) -> dict[str, Any] | None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return self._rows[self.table.item(rows[0], 0).data(Qt.ItemDataRole.UserRole)] if rows else None

    def _show(self) -> None:
        r = self._selected()
        if not r:
            return
        e = html.escape
        parts = [f"<b>{e(r['process'] or r['topic'] or 'Αίτημα')}</b> · υπόθεση {e(r['case_no'])}",
                 f"<span style='color:{CURRENT.muted}'>{e(r['service'])}</span>" if r["service"] else ""]
        if r["change"] and not r["seen"]:
            parts.append(f"<b style='color:{CURRENT.accent}'>{e(req.CHANGE_LABEL.get(r['change'], ''))}</b>")
        if r["text"]:
            parts.append("<b>Αίτημα:</b><br>" + e(r["text"]).replace("\n", "<br>"))
        parts.append("<b>Απάντηση:</b><br>" + (e(r["answer"]).replace("\n", "<br>") if r["answer"] else
                                                f"<span style='color:{CURRENT.muted}'>— χωρίς κείμενο απάντησης —</span>"))
        self.detail.setText("<br>".join(p for p in parts if p))
        self._fill_files(r["files"])
        if not r["seen"]:
            req.mark_seen(self.conn, self.afm, r["id"])

    def _fill_files(self, files: list[dict[str, Any]]) -> None:
        self.files.clear()
        for f in files:
            label = f"{req.ORIGIN_LABEL.get(f.get('fileOrigin', ''), 'Αρχείο')}: {f['fileName']}"
            item = QListWidgetItem(label + ("" if f.get("path") else "  (δεν κατέβηκε — «Άνοιγμα αιτήματος»)"))
            item.setData(Qt.ItemDataRole.UserRole, f.get("path", ""))
            self.files.addItem(item)
        if not files:
            self.files.addItem("Χωρίς συνημμένα (ή δεν έχει ανοιχτεί ακόμη το αίτημα).")

    def _open_file(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def _refresh(self) -> None:
        if self.main is not None and hasattr(self.main, "run_requests_sync"):
            self.main.run_requests_sync([self.afm], window=self.window_, on_done=self.reload)
            return
        self._run(lambda conn: req.refresh(conn, self.afm), "Τα αιτήματα ενημερώθηκαν.")

    def _open(self) -> None:
        r = self._selected()
        if not r:
            toast(self.window_, "Επιλέξτε ένα αίτημα.", "warn")
            return

        def done_text(res):
            files = res.get("files") or []
            return f"Το αίτημα άνοιξε — {sum(1 for f in files if f.get('path'))}/{len(files)} συνημμένα κατέβηκαν."
        self._run(lambda conn: req.open_request(conn, self.afm, r["id"]), done_text)

    def _run(self, fn, ok_text) -> None:
        def work(_p):
            conn = dbmod.connect()                     # δικό του thread — ποτέ η σύνδεση του UI
            try:
                return fn(conn)
            finally:
                conn.close()

        def done(res):
            text = ok_text(res) if callable(ok_text) else ok_text
            toast(self.window_, text if res.get("ok") else res.get("reason", ""), "ok" if res.get("ok") else "danger")
            self.reload()
            self._show()

        toast(self.window_, "Σύνδεση στην ΑΑΔΕ…", "info")
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self.window_, m, "danger")))
