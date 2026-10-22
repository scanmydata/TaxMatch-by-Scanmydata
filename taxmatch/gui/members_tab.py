"""Καρτέλα εταιρείας → «Μέλη»: μέλη/εκπρόσωποι (Μητρώο ΑΑΔΕ ή Excel), ποιοι είναι ήδη πελάτες με κωδικούς, οι οφειλές
τους, και ενέργειες για τα επιλεγμένα: προσθήκη ως πελάτη, λήψη οφειλών, λήψη εντύπων (Μητρώο, δήλωση εισοδήματος) —
όλα με τους ΔΙΚΟΥΣ ΤΟΥΣ κωδικούς. Λογική: `members.py`."""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import db as dbmod
from .. import members, office
from ..identifiers import normalize_afm
from .icons import icon
from .theme import CURRENT
from .toast import toast
from .workers import run_task

_COLS = ["", "ΑΦΜ", "Ονοματεπώνυμο / επωνυμία", "Ιδιότητα", "Ποσοστό", "Πηγή", "Πελάτης", "Οφειλές"]
_SOURCE = {"aade": "Μητρώο ΑΑΔΕ", "excel": "Excel", "manual": "Χειροκίνητα"}


class MembersTab(QWidget):
    def __init__(self, conn, afm: str, window: QWidget, main: Any = None) -> None:
        super().__init__()
        self.conn, self.afm, self.window_, self.main = conn, afm, window, main
        self._tasks: list[Any] = []
        self._rows: list[dict[str, Any]] = []
        root = QVBoxLayout(self)
        self.hint = QLabel("")
        self.hint.setWordWrap(True)
        self.hint.setObjectName("muted")
        root.addWidget(self.hint)
        bar = QHBoxLayout()
        fetch = QPushButton(icon("download", CURRENT.txt, 16), "  Άντληση μελών (ΑΑΔΕ)")
        fetch.setToolTip("Μέλη και εκπρόσωποι από το Μητρώο ΑΑΔΕ, με τους κωδικούς TAXISnet της εταιρείας — μόνο ενεργά")
        fetch.clicked.connect(self._fetch)
        bar.addWidget(fetch)
        self.manual = QLineEdit()
        self.manual.setPlaceholderText("ΑΦΜ μέλους")
        self.manual.setFixedWidth(130)
        bar.addWidget(self.manual)
        add_manual = QPushButton("Προσθήκη")
        add_manual.clicked.connect(self._add_manual)
        bar.addWidget(add_manual)
        bar.addStretch()
        root.addLayout(bar)
        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col, width in enumerate((28, 95, 0, 190, 70, 125, 150, 160)):
            if width:
                self.table.setColumnWidth(col, width)
        self.table.doubleClicked.connect(lambda *_: self._open_client())
        root.addWidget(self.table, 1)
        acts = QHBoxLayout()
        as_client = QPushButton(icon("add_client", CURRENT.txt, 16), "  Προσθήκη ως πελάτες")
        as_client.setToolTip("Τα επιλεγμένα μέλη γίνονται πελάτες — για να έχουν δικούς τους κωδικούς, οφειλές, έντυπα")
        as_client.clicked.connect(self._as_clients)
        acts.addWidget(as_client)
        debts_btn = QPushButton(icon("refresh", CURRENT.txt, 16), "  Οφειλές μελών")
        debts_btn.setToolTip("ΑΑΔΕ / ΚΕΑΟ / ΕΦΚΑ για τα επιλεγμένα (ή όλα) μέλη που είναι πελάτες με κωδικούς TAXISnet")
        debts_btn.clicked.connect(lambda: self._debts(False))
        acts.addWidget(debts_btn)
        docs_debts = QPushButton("  Έντυπα οφειλών μελών")
        docs_debts.clicked.connect(lambda: self._debts(True))
        acts.addWidget(docs_debts)
        docs = QPushButton(icon("pdf", CURRENT.txt, 16), "  Έντυπα μελών (Μητρώο, Ε1)")
        docs.setToolTip("Εκτύπωση Μητρώου ΑΑΔΕ και δήλωση φορολογίας εισοδήματος κάθε μέλους — στον φάκελο Δέουσας "
                        "επιμέλειας του μέλους")
        docs.clicked.connect(self._documents)
        acts.addWidget(docs)
        acts.addStretch()
        remove = QPushButton("Αφαίρεση")
        remove.clicked.connect(self._remove)
        acts.addWidget(remove)
        root.addLayout(acts)
        self.reload()

    def reload(self) -> None:
        legal = members.legal_entity(self.conn, self.afm)
        self.hint.setText(
            ("Μέλη/εκπρόσωποι της εταιρείας. " if legal else
             "Τα μέλη αφορούν εταιρείες (όχι ατομικές επιχειρήσεις) — η νομική μορφή του πελάτη δεν είναι εταιρική. ")
            + "Πηγές: Μητρώο ΑΑΔΕ (με τους κωδικούς της εταιρείας — και αυτόματα με την ανάκτηση στοιχείων αν είναι "
              "ενεργή η ρύθμιση «Άντληση μελών εταιρειών») ή Excel (στήλες «ΑΦΜ εταιρείας» / «Μέλη»). Οφειλές και έντυπα "
              "μέλους αντλούνται με τους δικούς του κωδικούς — γι' αυτό πρέπει να είναι πελάτης.")
        self._rows = members.company_members(self.conn, self.afm)
        t = self.table
        t.setRowCount(len(self._rows))
        for i, m in enumerate(self._rows):
            debt_text = " · ".join(f"{k.upper() if k != 'aade' else 'ΑΑΔΕ'} {office.money(v['total'])}"
                                   for k, v in m["debts"].items() if v["status"] == "ok") or "—"
            client = ("ναι, με κωδικούς" if m["has_creds"] else "ναι, χωρίς κωδικούς") if m["is_client"] else "όχι"
            cells = ["", m["member_afm"], m["name"] or "—", m["role"], m["share"], _SOURCE.get(m["source"], m["source"]),
                     client, debt_text]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    item.setData(Qt.ItemDataRole.UserRole, i)
                if col == 6:
                    item.setForeground(QColor(CURRENT.ok if m["has_creds"] else (CURRENT.warn if m["is_client"]
                                                                                 else CURRENT.muted)))
                t.setItem(i, col, item)

    def _chosen(self) -> list[dict[str, Any]]:
        checked = [self._rows[self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)] for r in range(self.table.rowCount())
                   if self.table.item(r, 0).checkState() == Qt.CheckState.Checked]
        if checked:
            return checked
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self._rows[self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)] for r in rows] or list(self._rows)

    def _fetch(self) -> None:
        def work(_p):
            conn = dbmod.connect()
            try:
                return members.fetch_for_company(conn, self.afm)
            finally:
                conn.close()

        def done(res):
            toast(self.window_, f"Βρέθηκαν {res['n']} ενεργά μέλη/εκπρόσωποι." if res["ok"] else res["reason"],
                  "ok" if res["ok"] else "danger")
            self.reload()
        toast(self.window_, "Άντληση μελών από το Μητρώο ΑΑΔΕ…", "info")
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self.window_, m, "danger")))

    def _add_manual(self) -> None:
        afm = normalize_afm(self.manual.text())
        if not afm or afm == self.afm:
            toast(self.window_, "Δώστε έγκυρο ΑΦΜ μέλους.", "warn")
            return
        members.store(self.conn, self.afm, [{"afm": afm}], "manual")
        self.manual.clear()
        self.reload()

    def _as_clients(self) -> None:
        chosen = [m for m in self._chosen() if not m["is_client"]]
        if not chosen:
            toast(self.window_, "Τα επιλεγμένα μέλη είναι ήδη πελάτες.", "info")
            return
        n = sum(1 for m in chosen if members.add_as_client(self.conn, self.afm, m["member_afm"]))
        toast(self.window_, f"{n} μέλη προστέθηκαν ως πελάτες — ορίστε τους κωδικούς τους (καρτέλα τους ή Excel).", "ok")
        if self.main is not None and hasattr(self.main, "reload_clients"):
            self.main.reload_clients()
        self.reload()

    def _ready(self) -> list[str]:
        ready = [m["member_afm"] for m in self._chosen() if m["has_creds"]]
        if not ready:
            toast(self.window_, "Κανένα (επιλεγμένο) μέλος δεν είναι πελάτης με κωδικούς TAXISnet.", "warn")
        return ready

    def _debts(self, want_pdf: bool) -> None:
        afms = self._ready()
        if afms and self.main is not None and hasattr(self.main, "run_debt_sync"):
            self.main.run_debt_sync(afms, window=self.window_, on_done=self.reload, want_pdf=want_pdf)

    def _documents(self) -> None:
        afms = self._ready()
        if not afms:
            return
        from ..aml import retrieval

        def work(progress):
            conn = dbmod.connect()
            try:
                out = {"files": 0, "errors": []}
                for a in afms:
                    progress(f"Έντυπα μέλους {a}…")
                    res = retrieval.retrieve_for_client(conn, a, targets=("registry", "income"))
                    out["files"] += len(res["files"])
                    out["errors"] += [f"{a}: {e}" for e in res["errors"]]
                return out
            finally:
                conn.close()

        def done(res):
            toast(self.window_, f"Κατέβηκαν {res['files']} έντυπα μελών (φάκελος Δέουσας επιμέλειας κάθε μέλους)."
                  + (f" Σφάλματα: {'; '.join(res['errors'][:3])}" if res["errors"] else ""),
                  "warn" if res["errors"] else "ok")
        toast(self.window_, f"Λήψη εντύπων για {len(afms)} μέλη…", "info")
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self.window_, m, "danger")))

    def _remove(self) -> None:
        checked = [r for r in range(self.table.rowCount()) if self.table.item(r, 0).checkState() == Qt.CheckState.Checked]
        rows = checked or sorted({i.row() for i in self.table.selectedItems()})
        if not rows:
            toast(self.window_, "Επιλέξτε τα μέλη που θα αφαιρεθούν.", "warn")
            return
        for r in rows:
            members.remove(self.conn, self.afm, self._rows[self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)]["member_afm"])
        self.reload()

    def _open_client(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if not rows or self.main is None or not hasattr(self.main, "open_client_detail"):
            return
        m = self._rows[self.table.item(rows[0], 0).data(Qt.ItemDataRole.UserRole)]
        if m["is_client"]:
            self.main.open_client_detail(m["member_afm"])
