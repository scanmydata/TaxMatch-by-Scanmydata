"""Καρτέλα πελάτη → «Αιτήματα ΑΑΔΕ»: «Τα Αιτήματά μου» (myAADE) με κατάσταση, απάντηση και τι άλλαξε από την τελευταία
φορά· διπλό κλικ = «καρτέλα αιτήματος» (όπως η καρτέλα οφειλής) με το πλήρες αίτημα, την απάντηση και τα συνημμένα
χωριστά ανά προέλευση (υποβολής / απάντησης ΑΑΔΕ). Λογική: `requests_aade.py` (port του ScanMyData Tax Center).
Μόνο ανάγνωση."""
from __future__ import annotations

import html
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QTableWidget, QVBoxLayout,
    QWidget,
)

from .. import db as dbmod
from .. import requests_aade as req
from .icons import icon
from .table_kit import SortItem, TableKit
from .theme import CURRENT
from .toast import toast
from .widgets import fit_to_screen
from .workers import run_task

_COLS = ["", "Υποβολή", "Ενημέρωση", "Αρ. υπόθεσης", "Διαδικασία / θέμα", "Κατάσταση", "Συνημμένα"]
# Σειρά ομάδων συνημμένων στην καρτέλα αιτήματος (fileOrigin → τίτλος ενότητας)
_FILE_GROUPS = [("attachedFile", "Συνημμένα της αίτησης (υποβλήθηκαν)"),
                ("internalFile", "Συνημμένα της απάντησης ΑΑΔΕ"),
                ("commFile", "Συνημμένα αλληλογραφίας"),
                ("certificateFile", "Εκπροσώπηση")]


def run_open_request(owner: QWidget, window: QWidget, afm: str, request_id: str,
                     on_done: Callable[[dict[str, Any]], None]) -> Any:
    """`requests_aade.open_request` σε background (δική του σύνδεση sqlite) — το πλήρες αίτημα και τα συνημμένα."""
    def work(_p):
        conn = dbmod.connect()                         # δικό του thread — ποτέ η σύνδεση του UI
        try:
            return req.open_request(conn, afm, request_id)
        finally:
            conn.close()
    return run_task(owner, work, on_done=on_done, on_error=lambda m: toast(window, m, "danger"))


class RequestCard(QDialog):
    """«Καρτέλα αιτήματος»: στοιχεία, κείμενο αιτήματος και απάντησης, συνημμένα ανά προέλευση (κλικ = άνοιγμα).
    Αν τα συνημμένα δεν έχουν κατέβει ακόμη, τα φέρνει μόνη της (μία φορά — μετά μένουν στον φάκελο)."""

    def __init__(self, tab: "RequestsTab", r: dict[str, Any]) -> None:
        super().__init__(tab.window_)
        self.tab, self.r = tab, r
        self._tasks: list[Any] = []
        self.setWindowTitle("Καρτέλα αιτήματος ΑΑΔΕ")
        root = QVBoxLayout(self)
        title = QLabel(r["process"] or r["topic"] or "Αίτημα")
        title.setObjectName("h2")
        title.setWordWrap(True)
        root.addWidget(title)
        if req.stage(r["status"]) == "waiting_client":
            warn = QLabel("⚠ Σε εκκρεμότητα από τον πελάτη — η ΑΑΔΕ περιμένει ενέργεια (συμπλήρωση / απάντηση στο myAADE).")
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color:{CURRENT.warn}; font-weight:600;")
            root.addWidget(warn)
        grid = QGridLayout()
        facts = [("Αρ. υπόθεσης", r["case_no"] or "—"), ("Υπηρεσία", r["service"] or "—"),
                 ("Υποβολή", req.date_text(r["submitted"]) or "—"), ("Τελευταία ενημέρωση", req.date_text(r["updated"]) or "—"),
                 ("Κατάσταση", r["status"] + (" · απορρίφθηκε" if r["rejected"] else ""))]
        if r["topic"] and r["process"]:
            facts.insert(0, ("Θέμα", r["topic"]))
        for row, (label, value) in enumerate(facts):
            k = QLabel(label + ":")
            k.setStyleSheet(f"color:{CURRENT.muted}")
            v = QLabel(f"<b>{html.escape(value)}</b>")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if label == "Κατάσταση" and req.stage(r["status"]) == "waiting_client":
                v.setStyleSheet(f"color:{CURRENT.warn}")
            grid.addWidget(k, row, 0)
            grid.addWidget(v, row, 1)
        grid.setColumnStretch(1, 1)
        grid.setVerticalSpacing(6)
        root.addLayout(grid)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        for head, text in (("Αίτημα", r["text"]), ("Απάντηση ΑΑΔΕ", r["answer"])):
            h = QLabel(head)
            h.setObjectName("h2")
            lay.addWidget(h)
            t = QLabel(html.escape(text).replace("\n", "<br>") if text else
                       f"<span style='color:{CURRENT.muted}'>— χωρίς κείμενο —</span>")
            t.setObjectName("card")
            t.setWordWrap(True)
            t.setTextFormat(Qt.TextFormat.RichText)
            t.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lay.addWidget(t)
        self.files_box = QVBoxLayout()
        lay.addLayout(self.files_box)
        lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        self.fetch_btn = QPushButton(icon("download", CURRENT.txt, 16), "  Λήψη αιτήματος && συνημμένων")
        self.fetch_btn.setToolTip("Φέρνει από το myAADE το πλήρες αίτημα και τα συνημμένα (υποβολής και απάντησης)")
        self.fetch_btn.clicked.connect(self.fetch)
        buttons.addWidget(self.fetch_btn)
        buttons.addStretch()
        close = QPushButton("Κλείσιμο")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self._fill_files(r["files"])
        fit_to_screen(self, 760, 640)
        if not r.get("opened") or any(not f.get("path") for f in r["files"]):
            self.fetch()                               # συνημμένα που δεν έχουν κατέβει → αυτόματα

    def _fill_files(self, files: list[dict[str, Any]]) -> None:
        while self.files_box.count():
            item = self.files_box.takeAt(0)
            if item.widget() is not None:
                item.widget().hide()
                item.widget().deleteLater()
        groups = dict(_FILE_GROUPS)
        known = [o for o, _t in _FILE_GROUPS]
        for origin in known + sorted({f.get("fileOrigin", "") for f in files} - set(known)):
            group = [f for f in files if f.get("fileOrigin", "") == origin]
            if not group:
                continue
            h = QLabel(f"{groups.get(origin, req.ORIGIN_LABEL.get(origin, 'Συνημμένα'))} ({len(group)})")
            h.setObjectName("h2")
            self.files_box.addWidget(h)
            for f in group:
                path = f.get("path", "")
                b = QPushButton(icon("pdf" if f["fileName"].lower().endswith(".pdf") else "download", CURRENT.accent, 16),
                                "  " + f["fileName"])
                b.setStyleSheet("text-align:left; padding:6px 10px;")
                if path and Path(path).exists():
                    b.setToolTip("Άνοιγμα: " + path)
                    b.clicked.connect(lambda _c=False, p=path: QDesktopServices.openUrl(QUrl.fromLocalFile(p)))
                else:
                    b.setEnabled(False)
                    b.setToolTip(f.get("error") or "Δεν έχει κατέβει ακόμη")
                self.files_box.addWidget(b)
        if not files:
            note = QLabel("Χωρίς συνημμένα." if self.r.get("opened") else "Τα συνημμένα φορτώνουν…")
            note.setObjectName("muted")
            self.files_box.addWidget(note)

    def fetch(self) -> None:
        self.fetch_btn.setEnabled(False)
        self.fetch_btn.setText("  Λήψη από την ΑΑΔΕ…")

        def done(res: dict[str, Any]) -> None:
            self.fetch_btn.setEnabled(True)
            self.fetch_btn.setText("  Λήψη αιτήματος && συνημμένων")
            if not res.get("ok"):
                toast(self, res.get("reason", "Αποτυχία λήψης."), "danger")
                return
            self.r = {**self.r, "opened": True, "files": res.get("files") or []}
            self._fill_files(self.r["files"])
            self.tab.reload()
        self._tasks.append(run_open_request(self, self, self.tab.afm, self.r["id"], done))


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
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.kit = TableKit(self.table, list(zip(_COLS, (26, 120, 120, 110, 0, 200, 90))), "client_requests2",
                            placeholder="Αναζήτηση αιτήματος (αριθμός, θέμα, κατάσταση…)")
        root.addWidget(self.kit.bar)
        self.table.doubleClicked.connect(lambda *_: self._open())
        root.addWidget(self.table, 1)
        hint = QLabel("Διπλό κλικ σε αίτημα: καρτέλα με το αίτημα, την απάντηση και τα συνημμένα.")
        hint.setObjectName("muted")
        root.addWidget(hint)
        self.reload()

    def reload(self) -> None:
        self._rows = req.client_requests(self.conn, self.afm)
        sync = self.conn.execute("SELECT * FROM request_sync WHERE afm=?", (self.afm,)).fetchone()
        unseen = sum(1 for r in self._rows if not r["seen"])
        waiting = sum(1 for r in self._rows if req.stage(r["status"]) == "waiting_client")
        if sync and sync["status"] == "ok":
            self.info.setText(f"{len(self._rows)} αιτήματα · τελευταία ενημέρωση {sync['synced_at'][:16].replace('T', ' ')}"
                              + (f" · {unseen} με αλλαγές που δεν έχετε δει" if unseen else "")
                              + (f" · ⚠ {waiting} σε εκκρεμότητα από τον πελάτη" if waiting else ""))
        elif sync:
            self.info.setText(f"Η τελευταία ενημέρωση απέτυχε: {sync['message']}")
        else:
            self.info.setText("Δεν έχει γίνει ενημέρωση — «Ενημέρωση αιτημάτων».")
        t = self.table
        self.kit.begin()
        t.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            mark = "●" if not r["seen"] else ("✓" if req.answered({"answer": r["answer"], "status": r["status"]}) else "")
            n_files = len(r["files"])
            cells = [mark, req.date_text(r["submitted"]), req.date_text(r["updated"]), r["case_no"],
                     " — ".join(x for x in (r["process"], r["topic"]) if x) or "Αίτημα",
                     r["status"] + (" · απορρίφθηκε" if r["rejected"] else ""),
                     f"📎 {n_files}" if n_files else ("—" if r["opened"] else "")]
            for col, text in enumerate(cells):
                item = SortItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, i)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setForeground(QColor(CURRENT.accent if not r["seen"] else CURRENT.ok))
                    if r["change"] and not r["seen"]:
                        item.setToolTip(req.CHANGE_LABEL.get(r["change"], ""))
                if col == 5 and req.stage(r["status"]) == "waiting_client":
                    item.setForeground(QColor(CURRENT.warn))
                    item.setToolTip(req.CHANGE_LABEL["waiting_client"])
                if col == 6:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setToolTip(", ".join(f["fileName"] for f in r["files"]) if n_files
                                    else "Χωρίς συνημμένα" if r["opened"] else "Διπλό κλικ για λήψη")
                if not r["seen"]:
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                t.setItem(i, col, item)
        self.kit.end()

    def _selected(self) -> Optional[dict[str, Any]]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return self._rows[self.table.item(rows[0], 0).data(Qt.ItemDataRole.UserRole)] if rows else None

    def _refresh(self) -> None:
        if self.main is not None and hasattr(self.main, "run_requests_sync"):
            self.main.run_requests_sync([self.afm], window=self.window_, on_done=self.reload)
            return

        def done(res):
            toast(self.window_, "Τα αιτήματα ενημερώθηκαν." if res.get("ok") else res.get("reason", ""),
                  "ok" if res.get("ok") else "danger")
            self.reload()

        def work(_p):
            conn = dbmod.connect()
            try:
                return req.refresh(conn, self.afm)
            finally:
                conn.close()
        toast(self.window_, "Σύνδεση στην ΑΑΔΕ…", "info")
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self.window_, m, "danger")))

    def _open(self) -> None:
        r = self._selected()
        if not r:
            toast(self.window_, "Επιλέξτε ένα αίτημα.", "warn")
            return
        if not r["seen"]:
            req.mark_seen(self.conn, self.afm, r["id"])
        card = RequestCard(self, r)
        card.exec()
        self.reload()
