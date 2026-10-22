"""Καρτέλα πελάτη → «myDATA»: κωδικοί API, παραστατικά ανά μήνα με επιτόπια προεπισκόπηση PDF, ZIP, εκτύπωση,
και ό,τι προκύπτει για τις υποχρεώσεις (παρακρατούμενοι φόροι, VIES, Intrastat). Λογική: `taxmatch/mydata/`."""
from __future__ import annotations

import html
from datetime import date
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import db as dbmod
from .. import office
from ..mydata import analysis, credentials as mydata_creds, pdfs, store
from .icons import icon
from .theme import CURRENT
from .toast import ProgressToast, toast
from .workers import run_task

#: Λίγες στήλες: ο πίνακας μοιράζεται το πλάτος με την προεπισκόπηση PDF (ΑΦΜ/ΦΠΑ στο tooltip της γραμμής).
_COLS = ["Ημ/νία", "Τύπος", "Λ/Ε", "Αντισυμβαλλόμενος", "Καθαρή", "Σύνολο", "Παρακρ.", "PDF"]
HISTORY_MONTHS = 24                     # για Intrastat χρειάζεται όλο το προηγούμενο έτος + το τρέχον


def _months(n: int = 36) -> list[str]:
    today = date.today()
    return store.previous_months(f"{today.year}-{today.month:02d}", n)


class MydataTab(QWidget):
    def __init__(self, conn, afm: str, window: QWidget) -> None:
        super().__init__()
        self.conn, self.afm, self.window_ = conn, afm, window
        self._tasks: list = []
        self._docs: list[dict[str, Any]] = []
        root = QVBoxLayout(self)

        creds = QHBoxLayout()
        self.user = QLineEdit()
        self.user.setPlaceholderText("Όνομα χρήστη myDATA")
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("Κλειδί API (Ocp-Apim-Subscription-Key)")
        creds.addWidget(QLabel("myDATA REST API:"))
        creds.addWidget(self.user, 1)
        creds.addWidget(self.key, 2)
        save = QPushButton("Αποθήκευση")
        save.clicked.connect(self._save_creds)
        creds.addWidget(save)
        test = QPushButton(icon("network", CURRENT.txt, 16), "  Έλεγχος")
        test.setToolTip("Μικρό αίτημα στο myDATA (σύνολα εξόδων) — επιβεβαιώνει ότι οι κωδικοί γίνονται δεκτοί")
        test.clicked.connect(self._check_creds)
        creds.addWidget(test)
        root.addLayout(creds)
        self.creds_status = QLabel("")
        self.creds_status.setObjectName("muted")
        root.addWidget(self.creds_status)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Μήνας:"))
        self.month = QComboBox()
        for ym in _months():
            self.month.addItem(analysis.month_label(ym), ym)
        self.month.setCurrentIndex(1)                     # προηγούμενος μήνας: ο τελευταίος «κλειστός»
        self.month.currentIndexChanged.connect(lambda _i: self.reload())
        bar.addWidget(self.month)
        self.fetch_btn = QPushButton(icon("download", CURRENT.txt, 16), "  Λήψη μήνα")
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.setToolTip("Παραστατικά του μήνα από το myDATA (λήψεις και εκδοθέντα) και τα PDF τους από τον πάροχο")
        self.fetch_btn.clicked.connect(lambda: self._fetch([self.month.currentData()], pdf=True))
        bar.addWidget(self.fetch_btn)
        history = QPushButton("  Λήψη 24 μηνών")
        history.setToolTip("Μόνο στοιχεία (χωρίς PDF) των τελευταίων 24 μηνών — χρειάζονται για τα κατώφλια Intrastat "
                           "(προηγούμενο έτος) και για το Ημερολόγιο (VIES / παρακράτηση ανά μήνα)")
        history.clicked.connect(lambda: self._fetch(_months(HISTORY_MONTHS), pdf=False, only_missing=True))
        bar.addWidget(history)
        bar.addStretch()
        root.addLayout(bar)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setObjectName("card")
        root.addWidget(self.summary)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSortingEnabled(True)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col, width in enumerate((64, 44, 44, 0, 86, 86, 76, 84)):
            if width:
                self.table.setColumnWidth(col, width)
        self.table.itemSelectionChanged.connect(self._preview_selected)
        self.table.doubleClicked.connect(lambda *_: self._open_selected())
        split.addWidget(self.table)
        self.pdf_doc = QPdfDocument(self)
        self.pdf_view = QPdfView(self)
        self.pdf_view.setDocument(self.pdf_doc)
        self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
        self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
        self.pdf_view.setMinimumWidth(320)
        split.addWidget(self.pdf_view)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([640, 420])
        self.table.setMinimumWidth(420)
        root.addWidget(split, 1)

        actions = QHBoxLayout()
        self.preview_note = QLabel("")
        self.preview_note.setObjectName("muted")
        actions.addWidget(self.preview_note, 1)
        provider = QPushButton("Άνοιγμα στον πάροχο")
        provider.setToolTip("Για παραστατικά «μόνο online» (ο πάροχος δεν δίνει αρχείο PDF)")
        provider.clicked.connect(self._open_provider)
        actions.addWidget(provider)
        open_btn = QPushButton(icon("pdf", CURRENT.txt, 16), "  Άνοιγμα PDF")
        open_btn.clicked.connect(self._open_selected)
        actions.addWidget(open_btn)
        zip_btn = QPushButton(icon("folder", CURRENT.txt, 16), "  Εξαγωγή ZIP")
        zip_btn.setToolTip("Τα PDF του μήνα (ή μόνο των επιλεγμένων) σε ένα ZIP, χωρισμένα σε «Λήψεις» / «Εκδοθέντα»")
        zip_btn.clicked.connect(self._export_zip)
        actions.addWidget(zip_btn)
        print_btn = QPushButton(icon("printer", CURRENT.txt, 16), "  Εκτύπωση")
        print_btn.setToolTip("Προεπισκόπηση εκτύπωσης όλων των PDF του μήνα (ή των επιλεγμένων)")
        print_btn.clicked.connect(self._print)
        actions.addWidget(print_btn)
        root.addLayout(actions)
        self.reload()

    # ------------------------------------------------------------------ κωδικοί
    def _save_creds(self) -> None:
        if not mydata_creds.set_(self.conn, self.afm, self.user.text(), self.key.text()):
            toast(self.window_, "Δεν δόθηκαν κωδικοί myDATA.", "warn")
            return
        self.key.clear()
        toast(self.window_, "Οι κωδικοί myDATA αποθηκεύτηκαν (κρυπτογραφημένοι).", "ok")
        self._reload_creds()

    def _check_creds(self) -> None:
        def work(_p):
            conn = dbmod.connect()             # δικό του thread — ποτέ η σύνδεση του UI
            try:
                return store.check_credentials(conn, self.afm)
            finally:
                conn.close()

        def done(result):
            ok, msg = result
            toast(self.window_, msg, "ok" if ok else "danger")
            self._reload_creds()

        toast(self.window_, "Ξεκίνησε ο έλεγχος κωδικών myDATA…", "info")
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self.window_, m, "danger")))

    def _reload_creds(self) -> None:
        self.user.setText(mydata_creds.user_of(self.conn, self.afm))
        row = self.conn.execute("SELECT mydata_key, check_status FROM mydata_credentials WHERE afm=?", (self.afm,)).fetchone()
        has_key = bool(row and row["mydata_key"])
        self.key.setPlaceholderText("•••••••• (αφήστε κενό για να μείνει ως έχει)" if has_key
                                    else "Κλειδί API (Ocp-Apim-Subscription-Key)")
        status = {"ok": "Οι κωδικοί έγιναν δεκτοί από το myDATA.", "invalid": "Το myDATA ΑΠΕΡΡΙΨΕ τους κωδικούς."}.get(
            row["check_status"] if row else "", "Δεν έχουν ελεγχθεί." if has_key else
            "Χωρίς κωδικούς myDATA — ορίστε τους εδώ ή εισάγετέ τους από Excel (στήλη «Api myData»).")
        self.creds_status.setText(status)

    # ------------------------------------------------------------------ λήψη
    def _fetch(self, months: list[str], pdf: bool, only_missing: bool = False) -> None:
        if not mydata_creds.get(self.conn, self.afm):
            toast(self.window_, "Χρειάζονται οι κωδικοί myDATA του πελάτη.", "warn")
            return
        if only_missing:
            have = store.synced_months(self.conn, self.afm).get(self.afm, set())
            current = _months(2)                                  # τρέχων και προηγούμενος ξαναζητούνται πάντα
            months = [m for m in months if m not in have or m in current]
            if not months:
                toast(self.window_, "Όλοι οι μήνες έχουν ήδη κατέβει.", "ok")
                return
        total = len(months)
        bar = ProgressToast(self.window_, f"Ξεκίνησε η λήψη myDATA ({total} {'μήνας' if total == 1 else 'μήνες'})…")
        state = {"done": 0}

        def work(progress):
            conn = dbmod.connect()
            try:
                found = 0
                for i, ym in enumerate(months, 1):
                    state["done"] = i - 1
                    progress(f"myDATA: {analysis.month_label(ym)} ({i}/{total})")
                    out = store.sync_month(conn, self.afm, ym)
                    if not out["ok"]:
                        raise RuntimeError(out["reason"])
                    found += out["n"]
                    if pdf:
                        progress(f"PDF παραστατικών {analysis.month_label(ym)}…")
                        pdfs.download_month(conn, self.afm, ym,
                                            on_each=lambda k, n: progress(f"PDF {k}/{n} — {analysis.month_label(ym)}"))
                state["done"] = total
                return found
            finally:
                conn.close()

        def done(found):
            bar.finish(f"Η λήψη myDATA ολοκληρώθηκε — {found} παραστατικά.", "ok")
            self.reload()

        def failed(m):
            bar.finish(f"Η λήψη myDATA απέτυχε: {m}", "danger")
            self.reload()

        self._tasks.append(run_task(self, work, on_progress=lambda m: bar.update(m, state["done"], total),
                                    on_done=done, on_error=failed))

    # ------------------------------------------------------------------ προβολή
    def reload(self) -> None:
        self._reload_creds()
        ym = self.month.currentData()
        self._docs = store.month_docs(self.conn, self.afm, ym)
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(self._docs))
        for i, d in enumerate(self._docs):
            incoming = d["direction"] == "incoming"
            other = pdfs.other_party(d, self.afm)
            cells = [d["issue_date"][8:10] + "/" + d["issue_date"][5:7], d["invoice_type"], "Λ" if incoming else "Ε",
                     other[0] or "—", office.money(d["net"]), office.money(d["total"]),
                     office.money(d["withheld"]) if d["withheld"] else "", pdfs.STATUS_LABEL.get(d["pdf_status"], "")]
            tip = (f"{'Λήψη από' if incoming else 'Έκδοση προς'} {other[0]} · ΑΦΜ {other[1] or '—'}\n"
                   f"{d['series']} {d['aa']} · {d['issue_date']} · τύπος {d['invoice_type']} · MARK {d['mark']}\n"
                   f"Καθαρή {office.money(d['net'])} · ΦΠΑ {office.money(d['vat'])} · Σύνολο {office.money(d['total'])}")
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(tip)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, i)
                if col in (4, 5, 6):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 6 and d["withheld"] and incoming:
                    item.setForeground(QColor(CURRENT.warn))
                if d["cancelled"]:
                    item.setForeground(QColor(CURRENT.muted))
                t.setItem(i, col, item)
        t.setSortingEnabled(True)
        self._render_summary(ym)
        self.pdf_doc.close()
        self.preview_note.setText(f"{len(self._docs)} παραστατικά" if self._docs else "")

    def _render_summary(self, ym: str) -> None:
        s = analysis.month_summary(self.conn, self.afm, ym)
        if not s["synced"]:
            self.summary.setText(f"<span style='color:{CURRENT.muted}'>Ο μήνας δεν έχει κατέβει από το myDATA — "
                                 "πατήστε «Λήψη μήνα».</span>")
            return
        parts = []
        if s["withholding"]:
            rows = "".join(
                f"<li><b>{office.money(w['amount'])}</b> — {html.escape(w['label'])} ({w['n_docs']} παραστατικά, "
                f"βάση {office.money(w['base'])}) → <b>{html.escape(w['declaration'])}</b> έως "
                f"<b>{date.fromisoformat(w['due']).strftime('%d/%m/%Y')}</b>{self._filed(w, ym)}</li>" for w in s["withholding"])
            parts.append(f"<b style='color:{CURRENT.warn}'>Παρακρατούμενοι φόροι προς απόδοση</b><ul>{rows}</ul>")
        else:
            parts.append("Παρακρατούμενοι φόροι: κανένα παραστατικό λήψης με παρακράτηση.")
        f = s["flows"]
        flows = (f"αποστολές αγαθών {office.money(f['goods_out'])} · παροχή υπηρεσιών {office.money(f['services_out'])} · "
                 f"αφίξεις αγαθών {office.money(f['goods_in'])} · λήψη υπηρεσιών {office.money(f['services_in'])}")
        vies = "<b>ΝΑΙ</b>" if s["vies"] else "όχι"
        intr = s["intrastat"]
        intr_text = {True: "<b>ΝΑΙ</b>", False: "όχι", None: "άγνωστο — λείπουν μήνες (πατήστε «Λήψη 24 μηνών»)"}[intr["any"]]
        if intr["any"] and intr["reason"]:
            intr_text += f" ({html.escape(intr['reason'])})"
        services_only = (f["goods_in"] == 0 and f["goods_out"] == 0) and (f["services_in"] or f["services_out"])
        parts.append(f"Ενδοκοινοτικές: {flows}<br>VIES: {vies}{' — μόνο υπηρεσίες, άρα όχι Intrastat' if services_only else ''}"
                     f" · Intrastat: {intr_text}")
        self.summary.setText("<br>".join(parts))

    def _filed(self, w: dict[str, Any], ym: str) -> str:
        """Κατάσταση υποβολής της δήλωσης από τον «Έλεγχο υποβολών» (EasyCheck): ΦΜΥ_ΕΠΙΧ/ΕΡΓΟΛΑΒΩΝ ανά μήνα."""
        if not w.get("easycheck"):
            return ""
        row = self.conn.execute("SELECT status FROM obligation_checks WHERE afm=? AND year=? AND obligation=? AND period=?",
                                (self.afm, int(ym[:4]), w["easycheck"], f"{int(ym[5:7])}ος Μήνας")).fetchone()
        if not row:
            return f" · <span style='color:{CURRENT.muted}'>υποβολή: δεν έχει ελεγχθεί (Προθεσμίες &amp; υποβολές)</span>"
        colour = {"submitted": CURRENT.ok, "not_submitted": CURRENT.bad}.get(row["status"], CURRENT.warn)
        from ..easycheck import STATUS_LABEL
        return f" · <b style='color:{colour}'>ΑΑΔΕ: {STATUS_LABEL.get(row['status'], row['status'])}</b>"

    def _selected_docs(self) -> list[dict[str, Any]]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self._docs[self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)] for r in rows]

    def _preview_selected(self) -> None:
        docs = self._selected_docs()
        if not docs:
            return
        d = docs[0]
        if d["pdf_status"] == "ok" and d["pdf_path"] and Path(d["pdf_path"]).exists():
            self.pdf_doc.load(d["pdf_path"])
            self.preview_note.setText(Path(d["pdf_path"]).name)
        else:
            self.pdf_doc.close()
            self.preview_note.setText({"viewer": "Ο πάροχος δίνει μόνο online προβολή — «Άνοιγμα στον πάροχο».",
                                       "no_url": "Χωρίς σύνδεσμο παρόχου στο myDATA.",
                                       "failed": "Η λήψη του PDF απέτυχε — ξαναδοκιμάστε «Λήψη μήνα»."}.get(
                d["pdf_status"], "Δεν έχει κατέβει PDF — «Λήψη μήνα»."))

    def _open_selected(self) -> None:
        for d in self._selected_docs()[:1]:
            if d["pdf_path"] and Path(d["pdf_path"]).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(d["pdf_path"]))

    def _open_provider(self) -> None:
        for d in self._selected_docs()[:1]:
            if d["url"]:
                QDesktopServices.openUrl(QUrl(d["url"]))      # ρητό κλικ του χρήστη· capability URL, όχι το κλειδί

    def _export_zip(self) -> None:
        ym = self.month.currentData()
        marks = [d["mark"] for d in self._selected_docs()] if len(self._selected_docs()) > 1 else None
        data = pdfs.zip_month(self.conn, self.afm, ym, marks)
        if len(data) <= 22:                                   # άδειο zip
            toast(self.window_, "Δεν υπάρχουν PDF για εξαγωγή — πατήστε πρώτα «Λήψη μήνα».", "warn")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Εξαγωγή ZIP", f"{self.afm}_{ym}.zip", "ZIP (*.zip)")
        if not path:
            return
        try:
            Path(path).write_bytes(data)
        except OSError as exc:
            toast(self.window_, f"Δεν αποθηκεύτηκε: {exc}", "danger")
            return
        toast(self.window_, "Το ZIP αποθηκεύτηκε.", "ok")

    def _print(self) -> None:
        from .printing import print_pdfs
        docs = self._selected_docs() if len(self._selected_docs()) > 1 else self._docs
        paths = [Path(d["pdf_path"]) for d in docs if d["pdf_status"] == "ok" and d["pdf_path"]]
        ready, failed = print_pdfs(paths, self.window_) if paths else (0, 0)
        if not ready:
            toast(self.window_, "Δεν υπάρχουν PDF για εκτύπωση — πατήστε πρώτα «Λήψη μήνα».", "warn")
        elif failed:
            toast(self.window_, f"{failed} PDF δεν άνοιξαν για εκτύπωση.", "warn")
