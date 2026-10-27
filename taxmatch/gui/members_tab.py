"""Καρτέλα εταιρείας → «Μέλη»: μέλη/εκπρόσωποι (Μητρώο ΑΑΔΕ ή Excel), ποιοι είναι ήδη πελάτες με κωδικούς, οι οφειλές
τους, και ενέργειες για τα επιλεγμένα: προσθήκη ως πελάτη, λήψη οφειλών, λήψη εντύπων (Μητρώο, δήλωση εισοδήματος) —
όλα με τους ΔΙΚΟΥΣ ΤΟΥΣ κωδικούς. Λογική: `members.py`."""
from __future__ import annotations

from datetime import date
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QVBoxLayout, QWidget,
)

from .. import db as dbmod
from .. import members, office
from ..business_profiles import service as businesses
from ..identifiers import normalize_afm
from .icons import icon
from .table_kit import SortItem, TableKit
from .theme import CURRENT
from .toast import toast
from .workers import run_task

_COLS = ["", "Χρήση", "ΑΦΜ", "Ονοματεπώνυμο / επωνυμία", "Ιδιότητα", "Ποσοστό", "Ατομική επιχείρηση",
         "Άλλες εταιρείες", "Πηγή", "Πελάτης", "Οφειλές"]
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
        bar.addWidget(QLabel("Χρήση"))
        self.year = QComboBox()
        self.year.currentIndexChanged.connect(self.reload)
        bar.addWidget(self.year)
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
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.kit = TableKit(self.table, list(zip(_COLS, (28, 75, 95, 0, 150, 70, 145, 210, 110, 145, 150))), "client_members",
                    filterable=range(1, len(_COLS)), placeholder="Αναζήτηση μέλους (χρήση, ΑΦΜ, όνομα, ιδιότητα…)")
        self.table.horizontalHeaderItem(6).setToolTip("Επιχείρηση του μέλους από την τελευταία διαθέσιμη ανάκτηση.")
        self.table.horizontalHeaderItem(7).setToolTip("Άλλες εταιρείες που έχουν ήδη αντληθεί και καταχωριστεί στο TaxMatch.")
        root.addWidget(self.kit.bar)
        self.table.doubleClicked.connect(lambda *_: self._open_client())
        root.addWidget(self.table, 1)
        acts = QHBoxLayout()
        as_client = QPushButton(icon("add_client", CURRENT.txt, 16), "  Προσθήκη ως πελάτες")
        as_client.setToolTip("Τα επιλεγμένα μέλη γίνονται πελάτες — για να έχουν δικούς τους κωδικούς, οφειλές, έντυπα")
        as_client.clicked.connect(self._as_clients)
        acts.addWidget(as_client)
        check_business = QPushButton(icon("refresh", CURRENT.txt, 16), "  Έλεγχος ατομικής / συμμετοχών")
        check_business.setToolTip("Ενημερώνει ατομική επιχείρηση και ΚΑΔ με τους προσωπικούς κωδικούς των μελών. "
                      "Οι άλλες εταιρείες προκύπτουν από τα ήδη αντλημένα ετήσια στοιχεία εταιρειών.")
        check_business.clicked.connect(self._check_member_businesses)
        acts.addWidget(check_business)
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
        self.remove_button = remove
        root.addLayout(acts)
        self.reload()

    def reload(self) -> None:
        selected_year = self.year.currentData() or date.today().year
        years = members.member_years(self.conn, self.afm)
        self.year.blockSignals(True)
        self.year.clear()
        for year in years or [date.today().year]:
            self.year.addItem(str(year), year)
        index = self.year.findData(selected_year)
        self.year.setCurrentIndex(index if index >= 0 else 0)
        self.year.blockSignals(False)
        selected_year = self.year.currentData() or date.today().year
        legal = members.legal_entity(self.conn, self.afm)
        self.hint.setText(
            ("Μέλη/εκπρόσωποι της εταιρείας. " if legal else
             "Τα μέλη αφορούν εταιρείες (όχι ατομικές επιχειρήσεις) — η νομική μορφή του πελάτη δεν είναι εταιρική. ")
            + "Πηγές: Μητρώο ΑΑΔΕ (με τους κωδικούς της εταιρείας — και αυτόματα με την ανάκτηση στοιχείων αν είναι "
              "ενεργή η ρύθμιση «Άντληση μελών εταιρειών») ή Excel (στήλες «ΑΦΜ εταιρείας» / «Μέλη»). Οφειλές και έντυπα "
              "μέλους αντλούνται με τους δικούς του κωδικούς — γι' αυτό πρέπει να είναι πελάτης. Κάθε χρήση κρατά "
              "ξεχωριστό στιγμιότυπο, ώστε οι αποχωρήσεις και προσθήκες να μην αλλάζουν παλιότερα έτη.")
        self._rows = members.company_members(self.conn, self.afm, selected_year)
        self.remove_button.setEnabled(selected_year == date.today().year)
        t = self.table
        self.kit.begin()
        t.setRowCount(len(self._rows))
        for i, m in enumerate(self._rows):
            debt_text = " · ".join(f"{k.upper() if k != 'aade' else 'ΑΑΔΕ'} {office.money(v['total'])}"
                                   for k, v in m["debts"].items() if v["status"] == "ok") or "—"
            client = ("ναι, με κωδικούς" if m["has_creds"] else "ναι, χωρίς κωδικούς") if m["is_client"] else "όχι"
            cells = ["", str(m["tax_year"]), m["member_afm"], m["name"] or "—", m["role"], m["share"],
                     m["own_business"], m["other_companies"], _SOURCE.get(m["source"], m["source"]), client, debt_text]
            for col, text in enumerate(cells):
                item = SortItem(text)
                if col == 0:
                    item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    item.setCheckState(Qt.CheckState.Unchecked)
                    item.setData(Qt.ItemDataRole.UserRole, i)
                if col == 9:
                    item.setForeground(QColor(CURRENT.ok if m["has_creds"] else (CURRENT.warn if m["is_client"]
                                                                                 else CURRENT.muted)))
                t.setItem(i, col, item)
        self.kit.end()

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
                    result = members.fetch_for_company(conn, self.afm)
                    if result["ok"]:
                        members.sync_member_clients(conn, self.afm)
                    return result
            finally:
                conn.close()

        def done(res):
            toast(self.window_, f"Βρέθηκαν {res['n']} ενεργά μέλη/εκπρόσωποι." if res["ok"] else res["reason"],
                  "ok" if res["ok"] else "danger")
            self.reload()
            if hasattr(self.window_, "refresh_member_shortcut"):
                self.window_.refresh_member_shortcut()
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
        if hasattr(self.window_, "refresh_member_shortcut"):
            self.window_.refresh_member_shortcut()

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

    def _check_member_businesses(self) -> None:
        afms = [m["member_afm"] for m in self._chosen() if m["has_creds"]]
        if not afms:
            toast(self.window_, "Για αυτόν τον έλεγχο χρειάζονται οι προσωπικοί κωδικοί TAXISnet των μελών.", "warn")
            return

        def work(progress):
            count = businesses.lookup_many(afms, on_each=lambda afm, n, _result: progress(
                f"Έλεγχος στοιχείων μέλους {n}/{len(afms)} — {afm}"))
            conn = dbmod.connect()
            try:
                members.sync_member_clients(conn, self.afm)
            finally:
                conn.close()
            return count

        def done(count):
            toast(self.window_, f"Ελέγχθηκαν τα στοιχεία {count} μελών με τους δικούς τους κωδικούς.", "ok")
            self.reload()
            if self.main is not None and hasattr(self.main, "reload_clients"):
                self.main.reload_clients()

        toast(self.window_, f"Έλεγχος ατομικής επιχείρησης/συμμετοχών για {len(afms)} μέλη…", "info")
        self._tasks.append(run_task(self, work, on_done=done,
                                    on_error=lambda message: toast(self.window_, message, "danger")))

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
        if hasattr(self.window_, "refresh_member_shortcut"):
            self.window_.refresh_member_shortcut()

    def _open_client(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if not rows or self.main is None or not hasattr(self.main, "open_client_detail"):
            return
        m = self._rows[self.table.item(rows[0], 0).data(Qt.ItemDataRole.UserRole)]
        if not m["is_client"]:
            members.add_as_client(self.conn, self.afm, m["member_afm"])
            self.reload()
            self.main.reload_clients()
        self.main.open_client_detail(m["member_afm"])
