"""Καρτέλα πελάτη → «myDATA»: κωδικοί API, παραστατικά ανά χρήση/μήνα (με την επωνυμία του αντισυμβαλλόμενου, όπως το
Timologio Downloader), PDF / ZIP / εκτύπωση, και ό,τι προκύπτει για τις υποχρεώσεις: παρακρατούμενοι φόροι, VIES,
Intrastat, τέλος ανθεκτικότητας (8.2), ψηφιακό τέλος συναλλαγής, προειδοποίηση ΦΠΑ σε μη υπόχρεο. Λογική: `taxmatch/mydata/`.

Οι λήψεις τρέχουν στην ουρά εργασιών του κύριου παραθύρου (`MainWindow.run_mydata_sync`): με διακοπή, και αν κλείσει η
καρτέλα το μήνυμα προόδου περνά στο κύριο παράθυρο. Η προεπισκόπηση PDF στο πλάι αφαιρέθηκε (ρητό αίτημα χρήστη
2026-10-22) — «Άνοιγμα PDF» το ανοίγει στον προεπιλεγμένο προβολέα· διπλό κλικ = καρτέλα παραστατικού (ανάλυση + PDF)."""
from __future__ import annotations

import html
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices
import json

from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QScrollArea, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import db as dbmod
from .. import deadlines, obligations, office
from ..mydata import analysis, credentials as mydata_creds, labels, names, pdfs, store
from .icons import icon
from .table_kit import SortItem, TableKit
from .theme import CURRENT
from .toast import toast
from .widgets import fit_to_screen
from .workers import run_task

_COLS = ["Ημ/νία", "Τύπος", "Έσοδο/Έξοδο", "Χαρακτηρισμός", "Αντισυμβαλλόμενος", "ΑΦΜ", "Καθαρή", "ΦΠΑ", "Σύνολο", "Παρακρ.", "PDF"]
HISTORY_MONTHS = 24                     # για Intrastat χρειάζεται όλο το προηγούμενο έτος + το τρέχον


def _detail(d: dict[str, Any]) -> dict[str, Any]:
    try:
        return json.loads(d.get("detail_json") or "{}")
    except ValueError:
        return {}


def classification_lines(d: dict[str, Any]) -> list[str]:
    """Χαρακτηρισμοί του παραστατικού (σύνολα, αλλιώς ανά γραμμή) σε κείμενο — για hover και καρτέλα."""
    det = _detail(d)
    items = det.get("summary_classifications") or [c for line in det.get("lines", []) for c in line.get("classifications", [])]
    merged: dict[tuple, float] = {}
    for c in items:
        key = (c.get("kind"), c.get("category"), c.get("type"), c.get("vat_type"))
        merged[key] = round(merged.get(key, 0.0) + (c.get("amount") or 0.0), 2)
    out = []
    for (kind, cat, typ, vat_type), amount in merged.items():
        text = labels.classification({"category": cat, "type": typ})
        out.append(f"{'Έσοδο' if kind == 'income' else 'Έξοδο'}: {text}" + (f" · {vat_type}" if vat_type else "")
                   + f" · {office.money(amount)}")
    return out


class InvoiceCard(QDialog):
    """«Καρτέλα παραστατικού» (διπλό κλικ — ίδιο ύφος με την καρτέλα οφειλής, ρητό αίτημα 2026-10-29): στοιχεία,
    αντισυμβαλλόμενος, ποσά, χαρακτηρισμοί, γραμμές, τρόποι πληρωμής, και «Λήψη PDF» / «Άνοιγμα PDF» / πάροχος."""

    def __init__(self, tab: "MydataTab", d: dict[str, Any], other_name: str, other_vat: str) -> None:
        super().__init__(tab.window_)
        self.tab, self.d = tab, d
        self._tasks: list[Any] = []
        self.setWindowTitle("Καρτέλα παραστατικού myDATA")
        root = QVBoxLayout(self)
        det = _detail(d)
        income = analysis.is_income(d)
        title = QLabel(f"{labels.invoice_type(d['invoice_type'])} — {d['series']} {d['aa']}".strip())
        title.setObjectName("h2")
        title.setWordWrap(True)
        root.addWidget(title)
        grid = QGridLayout()
        facts = [("Ημερομηνία", d["issue_date"][8:10] + "/" + d["issue_date"][5:7] + "/" + d["issue_date"][:4]),
                 ("Κατεύθυνση", ("Λήψη από" if d["direction"] == "incoming" else "Έκδοση προς")
                  + f" {other_name or '—'} · ΑΦΜ {other_vat or '—'}"),
                 ("Έσοδο / Έξοδο", "Έσοδο" if income else "Έξοδο"),
                 ("Καθαρή αξία", office.money(d["net"])), ("ΦΠΑ", office.money(d["vat"])),
                 ("Σύνολο", office.money(d["total"]) + (f" ({det['currency']})" if det.get("currency") not in (None, "EUR") else ""))]
        if d["withheld"]:
            facts.append(("Παρακρατήσεις", office.money(d["withheld"])))
        if d.get("stamp"):
            facts.append(("Ψηφιακό τέλος", office.money(d["stamp"])))
        for key, label in (("total_fees", "Τέλη"), ("total_other_taxes", "Λοιποί φόροι"), ("total_deductions", "Κρατήσεις")):
            if det.get(key):
                facts.append((label, office.money(det[key])))
        address = det.get("counter_address") if d["direction"] == "outgoing" else det.get("issuer_address")
        if address:
            facts.append(("Διεύθυνση αντισυμβαλλόμενου", address))
        if det.get("payments"):
            facts.append(("Τρόπος πληρωμής", " · ".join(
                f"{labels.PAYMENT_METHODS.get(pm['type'], pm['type'])} {office.money(pm['amount'])}" for pm in det["payments"])))
        facts.append(("MARK", d["mark"]))
        if d["cancelled"]:
            facts.append(("Κατάσταση", "ΑΚΥΡΩΜΕΝΟ"))
        for row, (label, value) in enumerate(facts):
            k = QLabel(label + ":")
            k.setStyleSheet(f"color:{CURRENT.muted}")
            v = QLabel(f"<b>{html.escape(value)}</b>")
            v.setWordWrap(True)
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(k, row, 0)
            grid.addWidget(v, row, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)
        body = QWidget()
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        head = QLabel("Χαρακτηρισμοί")
        head.setObjectName("h2")
        lay.addWidget(head)
        cls = classification_lines(d)
        note = QLabel("<br>".join(html.escape(x) for x in cls) if cls else
                      ("Χωρίς χαρακτηρισμούς στο myDATA." if det else
                       "Δεν υπάρχουν λεπτομέρειες — ξανακάντε «Λήψη μήνα» για να κατέβει η ανάλυση του παραστατικού."))
        note.setWordWrap(True)
        note.setObjectName("card")
        lay.addWidget(note)
        if det.get("lines"):
            lhead = QLabel(f"Γραμμές ({len(det['lines'])})")
            lhead.setObjectName("h2")
            lay.addWidget(lhead)
            t = QTableWidget(len(det["lines"]), 5)
            t.setHorizontalHeaderLabels(["#", "Καθαρή", "ΦΠΑ", "Κατηγορία ΦΠΑ", "Χαρακτηρισμός"])
            t.verticalHeader().setVisible(False)
            t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            for i, line in enumerate(det["lines"]):
                cells = [line.get("n", ""), office.money(line.get("net", 0)), office.money(line.get("vat", 0)),
                         labels.VAT_CATEGORIES.get(line.get("vat_category", ""), line.get("vat_category", "")),
                         " | ".join(labels.classification(c) for c in line.get("classifications", []))]
                for col, text in enumerate(cells):
                    item = QTableWidgetItem(text)
                    item.setToolTip(text)
                    t.setItem(i, col, item)
            for col, width in enumerate((40, 100, 90, 120)):
                t.setColumnWidth(col, width)
            t.horizontalHeader().setStretchLastSection(True)
            t.setMinimumHeight(min(260, 40 + 30 * len(det["lines"])))
            lay.addWidget(t)
        lay.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(body)
        root.addWidget(scroll, 1)
        buttons = QHBoxLayout()
        self.fetch_btn = QPushButton(icon("download", CURRENT.txt, 16), "  Λήψη PDF")
        self.fetch_btn.setToolTip("Κατεβάζει το PDF από τον πάροχο του παραστατικού")
        self.fetch_btn.clicked.connect(self._fetch)
        buttons.addWidget(self.fetch_btn)
        self.open_btn = QPushButton(icon("pdf", CURRENT.accent, 16), "  Άνοιγμα PDF")
        self.open_btn.clicked.connect(self._open)
        buttons.addWidget(self.open_btn)
        self.provider_btn = QPushButton(icon("network", CURRENT.txt, 16), "  Στον πάροχο")
        self.provider_btn.setToolTip("Ανοίγει το παραστατικό στη σελίδα του παρόχου (π.χ. όταν είναι «μόνο online»)")
        self.provider_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(pdfs.openable_url(self.d["url"]))))
        buttons.addWidget(self.provider_btn)
        buttons.addStretch()
        close = QPushButton("Κλείσιμο")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.addWidget(close)
        root.addLayout(buttons)
        self._sync()
        fit_to_screen(self, 760, 640)

    def _sync(self) -> None:
        have = bool(self.d["pdf_path"]) and Path(self.d["pdf_path"]).exists()
        self.open_btn.setEnabled(have)
        self.fetch_btn.setEnabled(bool(self.d["url"]) and not have)
        self.provider_btn.setEnabled(bool(self.d["url"]))

    def _open(self) -> None:
        if self.d["pdf_path"] and Path(self.d["pdf_path"]).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.d["pdf_path"]))

    def _fetch(self) -> None:
        afm, mark, ym = self.tab.afm, self.d["mark"], self.d["issue_date"][:7]
        self.fetch_btn.setEnabled(False)

        def work(_p):
            conn = dbmod.connect()                      # δικό του thread — ποτέ η σύνδεση του UI
            try:
                pdfs.download_month(conn, afm, ym, marks=[mark])
                row = conn.execute("SELECT pdf_status, pdf_path FROM mydata_docs WHERE afm=? AND mark=?",
                                   (afm, mark)).fetchone()
                return dict(row) if row else {}
            finally:
                conn.close()

        def done(res: dict[str, Any]) -> None:
            self.d = {**self.d, **res}
            self._sync()
            if res.get("pdf_status") == "ok":
                toast(self, "Το PDF κατέβηκε.", "ok")
                self._open()
            elif res.get("pdf_status") == "viewer":
                toast(self, "Ο πάροχος δείχνει το παραστατικό μόνο online — «Στον πάροχο».", "warn")
            else:
                toast(self, "Η λήψη του PDF απέτυχε.", "danger")
            self.tab.reload()
        toast(self, "Λήψη PDF από τον πάροχο…", "info")
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self, m, "danger")))


def _months(n: int = 36) -> list[str]:
    today = date.today()
    return store.previous_months(f"{today.year}-{today.month:02d}", n)


def year_months(year: int) -> list[str]:
    """Οι μήνες της χρήσης ως σήμερα (νεότερος πρώτος)."""
    today = date.today()
    last = 12 if year < today.year else today.month
    return [f"{year}-{m:02d}" for m in range(last, 0, -1)]


class MydataTab(QWidget):
    def __init__(self, conn, afm: str, window: QWidget, main: Any = None) -> None:
        super().__init__()
        self.conn, self.afm, self.window_, self.main = conn, afm, window, main
        self._tasks: list = []
        self._docs: list[dict[str, Any]] = []
        root = QVBoxLayout(self)

        # Κλειδιά: ορατά μόνο όσο λείπουν· με αποθηκευμένα, popup «Κλειδιά myDATA…» (ρητό αίτημα 2026-11-04 — να
        # μεγαλώσει ο πίνακας παραστατικών).
        self.creds_widget = QWidget()
        creds = QHBoxLayout(self.creds_widget)
        creds.setContentsMargins(0, 0, 0, 0)
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
        root.addWidget(self.creds_widget)
        self.creds_status = QLabel("")
        self.creds_status.setObjectName("muted")
        root.addWidget(self.creds_status)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Χρήση:"))
        self.year = QComboBox()
        this_year = date.today().year
        for y in range(this_year, this_year - 6, -1):
            self.year.addItem(str(y), y)
        self.year.setCurrentIndex(max(self.year.findData(getattr(main, "fiscal_year", this_year)), 0))
        self.year.currentIndexChanged.connect(lambda _i: self._fill_months())
        bar.addWidget(self.year)
        bar.addWidget(QLabel("Μήνας:"))
        self.month = QComboBox()
        self.month.currentIndexChanged.connect(lambda _i: (self._sync_fetch_label(), self.reload()))
        bar.addWidget(self.month)
        self.fetch_btn = QPushButton(icon("download", CURRENT.txt, 16), "  Λήψη μήνα")
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.setToolTip("Παραστατικά του μήνα από το myDATA (λήψεις και εκδοθέντα) και τα PDF τους από τον πάροχο")
        self.fetch_btn.clicked.connect(lambda: self._fetch(self._period_months(), pdf=True))
        bar.addWidget(self.fetch_btn)
        self.year_btn = year_btn = QPushButton("  Λήψη χρήσης")
        year_btn.setToolTip("Όλοι οι μήνες της επιλεγμένης χρήσης με ΕΝΑ αίτημα ανά κατεύθυνση, και τα PDF τους από τον πάροχο "
                            "(όσα δεν έχουν ήδη κατέβει)")
        year_btn.clicked.connect(lambda: self._fetch(year_months(self.year.currentData()), pdf=True))
        bar.addWidget(year_btn)
        history = QPushButton("  Λήψη 24 μηνών")
        history.setToolTip("Μόνο στοιχεία των τελευταίων 24 μηνών (όσοι λείπουν + οι δύο πιο πρόσφατοι) — χρειάζονται για "
                           "τα κατώφλια Intrastat (προηγούμενο έτος) και το Ημερολόγιο")
        history.clicked.connect(lambda: self._fetch(_months(HISTORY_MONTHS), pdf=False, only_missing=True))
        bar.addWidget(history)
        bar.addStretch()
        self.summary_btn = QPushButton(icon("calendar", CURRENT.txt, 16), "  Υποχρεώσεις μήνα…")
        self.summary_btn.setToolTip("Παρακρατήσεις, τέλη, VIES, Intrastat και ΦΠΑ της περιόδου, με την κατάσταση υποβολής")
        self.summary_btn.clicked.connect(self._show_summary)
        bar.addWidget(self.summary_btn)
        self.keys_btn = QPushButton(icon("lock", CURRENT.txt, 16), "  Κλειδιά myDATA…")
        self.keys_btn.clicked.connect(self._show_keys)
        bar.addWidget(self.keys_btn)
        root.addLayout(bar)

        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setTextFormat(Qt.TextFormat.RichText)
        self.warning.setStyleSheet(f"QLabel {{ border:1px solid {CURRENT.bad}; border-left:3px solid {CURRENT.bad}; "
                                   f"border-radius:8px; padding:8px; }}")
        self.warning.hide()
        root.addWidget(self.warning)

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        self.summary.setTextFormat(Qt.TextFormat.RichText)
        self.summary.setObjectName("card")
        self.summary.linkActivated.connect(self._on_link)
        self.summary.hide()                            # εμφανίζεται στο popup «Υποχρεώσεις μήνα…»

        self.table = QTableWidget(0, len(_COLS))
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.kit = TableKit(self.table, list(zip(_COLS, (64, 48, 86, 120, 0, 90, 85, 85, 80, 85, 75))), "client_mydata",
                            prefs=getattr(main, "_prefs", None),
                            placeholder="Αναζήτηση παραστατικού (αντισυμβαλλόμενος, ΑΦΜ, τύπος, ποσό…)")
        root.addWidget(self.kit.bar)
        self.table.itemSelectionChanged.connect(self._selection_note)
        self.table.doubleClicked.connect(lambda *_: self._open_selected())
        root.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.preview_note = QLabel("")
        self.preview_note.setObjectName("muted")
        actions.addWidget(self.preview_note, 1)
        provider = QPushButton(icon("network", CURRENT.txt, 16), "  Μόνο online…")
        provider.setToolTip("Παραστατικά που ο πάροχος δείχνει μόνο online: ανοίγουν στον browser σας, τα αποθηκεύετε ως "
                            "PDF στον φάκελο λήψεων και η εφαρμογή τα αρχειοθετεί μόνη της (όπως το Timologio Downloader)")
        provider.clicked.connect(self._open_provider)
        actions.addWidget(provider)
        open_btn = QPushButton(icon("pdf", CURRENT.txt, 16), "  Άνοιγμα PDF")
        open_btn.clicked.connect(self._open_pdf_selected)
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
        self._fill_months(select_previous=True)
        self._resolve_names()

    def _resolve_names(self) -> None:
        """Επωνυμίες από το VIES για τους αντισυμβαλλόμενους του πελάτη που δεν έχουν ακόμη επίσημη (στο παρασκήνιο,
        ~1/δευτερόλεπτο)· ο πίνακας ξαναγεμίζει όταν τελειώσει."""
        n = len(names.pending(self.conn, self.afm))
        if not n:
            return
        self.preview_note.setText(f"Αναζήτηση {min(n, 60)} επωνυμιών στο VIES…")

        def work(_p):
            conn = dbmod.connect()
            try:
                return names.resolve_via_vies(conn, self.afm, limit=60)
            finally:
                conn.close()

        def done(found):
            try:
                self.reload()
                if found:
                    self.preview_note.setText(f"VIES: βρέθηκαν {found} επωνυμίες αντισυμβαλλομένων.")
            except RuntimeError:                             # η καρτέλα έκλεισε στο μεταξύ
                pass
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda _m: None))

    def _fill_months(self, select_previous: bool = False) -> None:
        """Μήνες της χρήσης + «Όλη η χρήση» (data = '2026': τα παραστατικά όλων των μηνών μαζί — ρητό αίτημα χρήστη)."""
        year = self.year.currentData()
        self.month.blockSignals(True)
        self.month.clear()
        self.month.addItem(f"Όλη η χρήση {year}", str(year))
        for ym in year_months(year):
            self.month.addItem(analysis.month_label(ym), ym)
        if select_previous and self.month.count() > 2:
            self.month.setCurrentIndex(2)                 # προηγούμενος μήνας: ο τελευταίος «κλειστός»
        elif select_previous and self.month.count() > 1:
            self.month.setCurrentIndex(1)                 # Ιανουάριος: μόνο ο τρέχων μήνας
        self.month.blockSignals(False)
        self._sync_fetch_label()
        self.reload()

    def _whole_year(self) -> bool:
        return len(self.month.currentData() or "") == 4

    def _period_months(self) -> list[str]:
        period = self.month.currentData() or ""
        return year_months(int(period)) if len(period) == 4 else [period]

    def _sync_fetch_label(self) -> None:
        self.fetch_btn.setText("  Λήψη χρήσης" if self._whole_year() else "  Λήψη μήνα")
        if hasattr(self, "year_btn"):
            self.year_btn.setVisible(not self._whole_year())    # αλλιώς δύο ίδια κουμπιά «Λήψη χρήσης»

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
        stored = bool(row and row["mydata_key"])
        if self.creds_widget.parent() is self:         # όχι όσο είναι μέσα στο popup
            self.creds_widget.setVisible(not stored)
            self.creds_status.setVisible(not stored or (row["check_status"] if row else "") == "invalid")
        self.keys_btn.setVisible(stored)
        self.keys_btn.setText("  Κλειδιά myDATA…" + (" ⚠" if row and row["check_status"] == "invalid" else ""))

    def _popup(self, title: str, widget: QWidget, width: int = 760) -> None:
        """Δείχνει ένα υπάρχον widget της καρτέλας σε popup και το επιστρέφει κρυμμένο στη θέση του."""
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QScrollArea
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(width, 460)
        lay = QVBoxLayout(dlg)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        holder = QWidget()
        inner = QVBoxLayout(holder)
        index = self.layout().indexOf(widget)
        inner.addWidget(widget)
        widget.show()
        inner.addStretch()
        scroll.setWidget(holder)
        lay.addWidget(scroll, 1)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(dlg.reject)
        lay.addWidget(box)
        dlg.exec()
        widget.setParent(self)
        if index >= 0:
            self.layout().insertWidget(index, widget)
        widget.hide()

    def _show_summary(self) -> None:
        self._popup(f"Υποχρεώσεις — {self.month.currentText() or self.year.currentText()}", self.summary)

    def _show_keys(self) -> None:
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        index = self.layout().indexOf(self.creds_widget)
        lay.addWidget(self.creds_widget)
        self.creds_widget.show()
        status_index = self.layout().indexOf(self.creds_status)
        lay.addWidget(self.creds_status)
        self.creds_status.show()
        self._popup("Κλειδιά myDATA", holder, 820)
        for w, i in ((self.creds_widget, index), (self.creds_status, status_index)):
            w.setParent(self)
            self.layout().insertWidget(i, w)
        self._reload_creds()

    # ------------------------------------------------------------------ λήψη (ουρά εργασιών του κύριου παραθύρου)
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
        n = len(months)
        what = analysis.month_label(months[0]) if n == 1 else f"{n} μήνες"
        title = f"λήψη myDATA ({what}{', με PDF' if pdf else ''}) — {self.window_.windowTitle() or self.afm}"
        if self.main is not None and hasattr(self.main, "run_mydata_sync"):
            self.main.run_mydata_sync([self.afm], months, window=self.window_, on_done=self._after_fetch, pdfs=pdf,
                                      title=title)
            return
        # Χωρίς κύριο παράθυρο (μόνο σε tests): απευθείας στο παρασκήνιο.

        def work(_p):
            conn = dbmod.connect()
            try:
                out = store.sync_range(conn, self.afm, months)
                if pdf and out["ok"]:
                    for ym in months:
                        pdfs.download_month(conn, self.afm, ym)
                return out
            finally:
                conn.close()
        self._tasks.append(run_task(self, work, on_done=lambda _o: self._after_fetch()))

    def _after_fetch(self) -> None:
        self.reload()
        self._resolve_names()

    # ------------------------------------------------------------------ προβολή
    def reload(self) -> None:
        self._reload_creds()
        ym = self.month.currentData()
        if not ym:
            return
        self._docs = store.month_docs(self.conn, self.afm, ym)
        party = names.name_map(self.conn)
        t = self.table
        self.kit.begin()
        t.setRowCount(len(self._docs))
        non_vat = self._non_vat()
        pdf_colour = {"ok": CURRENT.ok, "viewer": CURRENT.accent, "failed": CURRENT.bad, "no_url": CURRENT.muted,
                      "": CURRENT.muted}
        for i, d in enumerate(self._docs):
            incoming = d["direction"] == "incoming"
            income = analysis.is_income(d)
            other_name, other_vat = names.display_name(d, party, self.afm)
            det = _detail(d)
            top_cls = (det.get("summary_classifications") or [c for line in det.get("lines", []) for c in line.get("classifications", [])])
            cls_cell = ""
            if top_cls:
                first = top_cls[0]
                label_text = labels.classification_label(first)
                if len(top_cls) > 1:
                    cls_cell = f"{label_text} (+{len(top_cls)-1})"
                else:
                    cls_cell = label_text
            cells = [d["issue_date"][8:10] + "/" + d["issue_date"][5:7], labels.invoice_type(d["invoice_type"], short=True),
                     "Έσοδο" if income else "Έξοδο",
                     cls_cell or "—",
                     other_name or "—", other_vat or "—", office.money(d["net"]), office.money(d["vat"]),
                     office.money(d["total"]), office.money(d["withheld"]) if d["withheld"] else "",
                     pdfs.STATUS_LABEL.get(d["pdf_status"], "—")]
            keys = {0: d["issue_date"] + d["mark"], 3: cls_cell, 6: d["net"], 7: d["vat"], 8: d["total"], 9: d["withheld"] or 0.0}
            tip = (f"{'Λήψη από' if incoming else 'Έκδοση προς'} {other_name or '—'} · ΑΦΜ {other_vat or '—'}\n"
                   f"{d['series']} {d['aa']} · {d['issue_date']} · {labels.invoice_type(d['invoice_type'])} · MARK {d['mark']}\n"
                   f"Καθαρή {office.money(d['net'])} · ΦΠΑ {office.money(d['vat'])} · Σύνολο {office.money(d['total'])}"
                   + (f" · ψηφιακό τέλος {office.money(d['stamp'])}" if d.get("stamp") else "")
                   + "".join("\n" + c for c in classification_lines(d))
                   + "\n(διπλό κλικ: καρτέλα παραστατικού)")
            for col, text in enumerate(cells):
                item = SortItem(text, keys[col]) if col in keys else SortItem(text)
                item.setToolTip(tip)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, i)
                if col in (6, 7, 8, 9):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 2:                                   # έσοδα πράσινα, έξοδα κόκκινα
                    item.setForeground(QColor(CURRENT.ok if income else CURRENT.bad))
                if col == 3 and not cls_cell:
                    item.setForeground(QColor(CURRENT.muted))
                if col == 10:
                    item.setForeground(QColor(pdf_colour.get(d["pdf_status"] or "", CURRENT.muted)))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if col == 9 and d["withheld"] and incoming:
                    item.setForeground(QColor(CURRENT.warn))
                if d.get("vat_class") and non_vat:
                    item.setForeground(QColor(CURRENT.bad))
                if d["cancelled"]:
                    item.setForeground(QColor(CURRENT.muted))
                t.setItem(i, col, item)
        self.kit.end()
        self._render_warning()
        self._render_summary(ym)
        pending = self.summary.text().count(f"color:{CURRENT.warn}'>")
        self.summary_btn.setText(("  Υποχρεώσεις χρήσης…" if len(ym) == 4 else "  Υποχρεώσεις μήνα…")
                                 + (f" ({pending} εκκρεμούν)" if pending else ""))
        self.summary_btn.setStyleSheet(f"color:{CURRENT.warn}; font-weight:700;" if pending else "")
        self.preview_note.setText(f"{len(self._docs)} παραστατικά" if self._docs else "")

    def _non_vat(self) -> bool:
        row = self.conn.execute("SELECT vat_subject FROM businesses WHERE afm=?", (self.afm,)).fetchone()
        return bool(row) and row["vat_subject"] == 0

    def _render_warning(self) -> None:
        """Μη υπόχρεος ΦΠΑ με παραστατικά που φέρουν ΦΠΑ / χαρακτηρισμό ΦΠΑ — για όλη τη χρήση."""
        year = self.year.currentData()
        docs = analysis.vat_warnings(self.conn, self.afm, year)
        if not docs:
            self.warning.hide()
            return
        out = sum(1 for d in docs if d["direction"] == "outgoing")
        months = sorted({d["issue_date"][:7] for d in docs})
        self.warning.setText(
            f"<b style='color:{CURRENT.bad}'>⚠ Ο πελάτης ΔΕΝ είναι υπόχρεος ΦΠΑ, αλλά στο myDATA της χρήσης {year} "
            f"υπάρχουν {len(docs)} παραστατικά με ΦΠΑ ή χαρακτηρισμό ΦΠΑ</b> ({out} εκδοθέντα) — μήνες: "
            f"{', '.join(analysis.month_label(m) for m in months)}. Ελέγξτε την έκδοση/τον χαρακτηρισμό ή την υπαγωγή "
            "του πελάτη σε ΦΠΑ. Οι γραμμές εμφανίζονται με κόκκινο.")
        self.warning.show()

    def _row(self, title: str, body: str, state: str) -> str:
        """Μία γραμμή της σύνοψης: τίτλος σε σταθερή στήλη (πορτοκαλί = εκκρεμεί, ΠΡΑΣΙΝΟ με ✓ = ολοκληρώθηκε, γκρι = δεν
        αφορά) και περιεχόμενο δεξιά — ρητό αίτημα χρήστη 2026-10-22 για καλύτερη στοίχιση."""
        colour = {"done": CURRENT.ok, "open": CURRENT.warn}.get(state, CURRENT.muted)
        mark = "✓ " if state == "done" else ""
        return (f"<tr><td width='200' valign='top' style='padding:4px 12px 4px 0'><b style='color:{colour}'>{mark}{title}</b>"
                f"</td><td valign='top' style='padding:4px 0'>{body}</td></tr>")

    @staticmethod
    def _table(rows: list[str]) -> str:
        return "<table cellspacing='0' cellpadding='0' width='100%'>" + "".join(rows) + "</table>"

    def _render_summary(self, ym: str) -> None:
        if len(ym) == 4:
            self._render_year_summary(ym)
            return
        s = analysis.month_summary(self.conn, self.afm, ym)
        if not s["synced"]:
            self.summary.setText(f"<span style='color:{CURRENT.muted}'>Ο μήνας δεν έχει κατέβει από το myDATA — "
                                 "πατήστε «Λήψη μήνα».</span>")
            return
        business = self._business()
        rows = []
        if s["withholding"]:
            lines, states = [], []
            for w in s["withholding"]:
                status, done = self._status_html("withholding_fees", ym, business, w["amount"])
                states.append(done)
                lines.append(f"<b>{office.money(w['amount'])}</b> — {html.escape(w['label'])} ({w['n_docs']} παραστατικά, "
                             f"βάση {office.money(w['base'])}) → <b>{html.escape(w['declaration'])}</b> έως "
                             f"<b>{date.fromisoformat(w['due']).strftime('%d/%m/%Y')}</b> · {status}")
                cross = self._cross_check(w, ym, business)
                if cross:
                    lines[-1] += f"<br>&nbsp;&nbsp;↳ {cross}"
            rows.append(self._row("Παρακρατούμενοι φόροι", "<br>".join(lines), "done" if all(states) else "open"))
        else:
            rows.append(self._row("Παρακρατούμενοι φόροι", "κανένα παραστατικό λήψης με παρακράτηση", ""))
        fees = s["fees"]
        due = obligations.month_end_or_next(*map(int, _next_month(ym).split("-")))
        if fees["climate_docs"]:
            status, done = self._status_html("climate_fee", ym, business)
            rows.append(self._row("Τέλος ανθεκτικότητας",
                                  f"{fees['climate_docs']} στοιχεία 8.2 ({office.money(fees['climate'])}) → δήλωση απόδοσης "
                                  f"έως <b>{due.strftime('%d/%m/%Y')}</b> · {status}", "done" if done else "open"))
        if fees["digital_fee"]:
            status, done = self._status_html("digital_fee", ym, business)
            rows.append(self._row("Ψηφιακό τέλος συναλλαγής",
                                  f"{office.money(fees['digital_fee'])} στα έσοδα → δήλωση έως "
                                  f"<b>{due.strftime('%d/%m/%Y')}</b> · {status}", "done" if done else "open"))
        if fees.get("rent_fee"):
            rows.append(self._row("Ψηφιακό τέλος ενοικίων",
                                  f"{office.money(fees['rent_fee'])} σε παραστατικά ενοικίου — <b>δεν</b> αποδίδεται μηνιαία: "
                                  "δηλώνεται ετησίως με τη δήλωση φορολογίας εισοδήματος (Ε2)", ""))
        f = s["flows"]
        flows = (f"αποστολές αγαθών {office.money(f['goods_out'])} · παροχή υπηρεσιών {office.money(f['services_out'])} · "
                 f"αφίξεις αγαθών {office.money(f['goods_in'])} · λήψη υπηρεσιών {office.money(f['services_in'])}")
        services_only = (f["goods_in"] == 0 and f["goods_out"] == 0) and (f["services_in"] or f["services_out"])
        if s["vies"]:
            y, m = int(ym[:4]), int(ym[5:7])
            vies_due = obligations._due_vies(*((y + 1, 1) if m == 12 else (y, m + 1)))  # noqa: SLF001
            status, done = self._status_html("vies", ym, business)
            body = (f"{flows}<br><b>Ανακεφαλαιωτικός VIES</b> έως <b>{vies_due.strftime('%d/%m/%Y')}</b> · {status}"
                    + (" — μόνο υπηρεσίες, άρα όχι Intrastat" if services_only else ""))
            rows.append(self._row("Ενδοκοινοτικές (VIES)", body, "done" if done else "open"))
        else:
            rows.append(self._row("Ενδοκοινοτικές (VIES)", f"{flows}<br>VIES: όχι", ""))
        intr = s["intrastat"]
        intr_text = {True: "<b>ΝΑΙ</b>", False: "όχι", None: "άγνωστο — λείπουν μήνες (πατήστε «Λήψη 24 μηνών»)"}[intr["any"]]
        if intr["any"] and intr["reason"]:
            intr_text += f" ({html.escape(intr['reason'])})"
        rows.append(self._row("Intrastat", intr_text + (" · υποβάλλεται στο Τελωνείο — χωρίς αυτόματο έλεγχο"
                                                        if intr["any"] else ""), "open" if intr["any"] else ""))
        rows.append(self._vat_row(ym, business))
        self.summary.setText(self._table(rows))

    def _cross_check(self, w: dict[str, Any], ym: str, business: dict[str, Any]) -> str:
        """Διασταύρωση του PDF της δήλωσης (ΦΜΥ_ΕΠΙΧ 20% / ΕΡΓΟΛΑΒΩΝ 3%) με τα παραστατικά του μήνα — HTML ή ''."""
        from .. import withholding_check
        key = w.get("easycheck") or ""
        if key not in withholding_check.CATEGORY:
            return ""
        period = ((business.get("checks") or {}).get("periods") or {}).get((key, ym)) or {}
        pdf = (period.get("amounts") or {}).get("submission_pdf") or ""
        if not pdf or not Path(pdf).exists():
            return (f"<span style='color:{CURRENT.muted}'>διασταύρωση με τη δήλωση: χρειάζεται το PDF της (Έλεγχος "
                    "υποβολών «και λήψη PDF»)</span>") if period.get("status") == "submitted" else ""
        res = withholding_check.reconcile(self.conn, self.afm, ym, key, pdf)
        text = html.escape(withholding_check.summary_text(res, names.name_map(self.conn)))
        colour = CURRENT.ok if res["ok"] else (CURRENT.muted if res["ok"] is None else CURRENT.bad)
        return f"<span style='color:{colour}'>{text}</span>"

    def _vat_row(self, ym: str, business: dict[str, Any]) -> str:
        """ΦΠΑ: περίοδος (μηνιαία/τριμηνιαία — διακριτά), ενδεικτικό υπόλοιπο myDATA και η Φ2 της περιόδου από την ΑΑΔΕ."""
        if business.get("vat_subject") == 0:
            return self._row("ΦΠΑ", "μη υπόχρεος ΦΠΑ", "")
        period = obligations.vat_period(business)
        y, m = int(ym[:4]), int(ym[5:7])
        if period == "quarterly":
            q_end = ((m - 1) // 3 + 1) * 3
            months, rule, ref = [f"{y}-{k:02d}" for k in range(q_end - 2, q_end + 1)], "vat_quarterly", f"{y}-{q_end:02d}"
            title = f"ΦΠΑ τριμηνιαίος ({(m - 1) // 3 + 1}ο τρίμηνο)"
        else:
            months, rule, ref = [ym], "vat_monthly", ym
            title = "ΦΠΑ μηνιαίος" if period == "monthly" else "ΦΠΑ (περίοδος άγνωστη)"
        bal = analysis.vat_balance(self.conn, self.afm, months)
        status, done = self._status_html(rule, ref, business)
        body = (f"κατά το myDATA (ενδεικτικά): εκροές {office.money(bal['output'])} − εισροές {office.money(bal['input'])} = "
                f"<b>{office.money(bal['balance'])}</b><br>Φ2 ΑΑΔΕ: {status}")
        note = obligations.vat_option_note(business)
        if note:
            body += f"<br><span style='color:{CURRENT.accent}'>{html.escape(note)}</span>"
        return self._row(title, body, "done" if done else "open")

    def _render_year_summary(self, year: str) -> None:
        if not any(m.startswith(year) for m in store.synced_months(self.conn, self.afm).get(self.afm, set())):
            self.summary.setText(f"<span style='color:{CURRENT.muted}'>Η χρήση {year} δεν έχει κατέβει από το myDATA — "
                                 "πατήστε «Λήψη χρήσης».</span>")
            return
        t = analysis.year_totals(self.conn, self.afm, year)
        bal = analysis.vat_balance(self.conn, self.afm, year_months(int(year)))
        rows = [
            self._row("Έσοδα χρήσης", f"<b style='color:{CURRENT.ok}'>{office.money(t['income'])}</b> (καθαρή αξία)", ""),
            self._row("Έξοδα χρήσης", f"<b style='color:{CURRENT.bad}'>{office.money(t['expenses'])}</b> (καθαρή αξία)", ""),
            self._row("ΦΠΑ χρήσης", f"εκροές {office.money(bal['output'])} − εισροές {office.money(bal['input'])} = "
                                    f"<b>{office.money(bal['balance'])}</b> (ενδεικτικά)", ""),
            self._row("Παρακρατήσεις", office.money(t["withheld"]) + " σε παραστατικά λήψης", ""),
            self._row("", f"<span style='color:{CURRENT.muted}'>{t['n']} παραστατικά · για προθεσμίες και υποβολές "
                          "επιλέξτε μήνα</span>", ""),
        ]
        self.summary.setText(self._table(rows))

    def _business(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM businesses WHERE afm=?", (self.afm,)).fetchone()
        return {**(dict(row) if row else {}), **deadlines._checks_for(self.conn, self.afm)}  # noqa: SLF001

    def _status_html(self, rule_id: str, ym: str, business: dict[str, Any],
                     expected: float | None = None) -> tuple[str, bool]:
        """(κατάσταση υποβολής σε HTML, ολοκληρώθηκε;) — έλεγχος υποβολών ΑΑΔΕ/e-ΕΦΚΑ ή χειροκίνητο «ολοκληρώθηκε»,
        με σύνδεσμο σήμανσης."""
        sub = obligations.submission(rule_id, business, ym)
        status = sub["status"]
        link = (f" <a style='color:{CURRENT.accent}' href='done:{rule_id}:{ym}:{0 if status == 'done' else 1}'>"
                + ("αναίρεση" if status == "done" else "σήμανση ως ολοκληρωμένο") + "</a>")
        if not status:
            hint = "δεν υπάρχει ακόμη αυτόματος έλεγχος" if sub["manual"] else "δεν έχει ελεγχθεί (Προθεσμίες &amp; υποβολές)"
            return f"<span style='color:{CURRENT.muted}'>υποβολή: {hint}</span>{link}", False
        colour = {"submitted": CURRENT.ok, "done": CURRENT.ok, "not_submitted": CURRENT.bad}.get(status, CURRENT.warn)
        text = f"<b style='color:{colour}'>{html.escape(sub['label'])}</b>"
        amounts = obligations.amounts_text(sub["amounts"])
        if amounts:
            text += f" ({html.escape(amounts)})"
            due_amt = sub["amounts"].get("total_due")
            if expected is not None and due_amt is not None and abs(due_amt - expected) > 0.01:
                text += f" <b style='color:{CURRENT.bad}'>διαφορά με myDATA {office.money(due_amt - expected)}</b>"
        return text + (link if status in ("done", "") or sub["manual"] else ""), status in ("submitted", "done")

    def _on_link(self, href: str) -> None:
        if not href.startswith("done:"):
            return
        _tag, rule_id, ym, flag = href.split(":")
        deadlines.mark_done(self.conn, self.afm, rule_id, ym, flag == "1")
        toast(self.window_, "Σημειώθηκε ως ολοκληρωμένο." if flag == "1" else "Αναιρέθηκε η σήμανση.", "ok")
        self.reload()
        if self.main is not None and hasattr(self.main, "_render_calendar_from_db"):
            self.main._render_calendar_from_db()  # noqa: SLF001

    def _selected_docs(self) -> list[dict[str, Any]]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self._docs[self.table.item(r, 0).data(Qt.ItemDataRole.UserRole)] for r in rows]

    def _selection_note(self) -> None:
        docs = self._selected_docs()
        if not docs:
            return
        d = docs[0]
        if d["pdf_status"] == "ok" and d["pdf_path"] and Path(d["pdf_path"]).exists():
            self.preview_note.setText(Path(d["pdf_path"]).name + " — διπλό κλικ για άνοιγμα")
        else:
            self.preview_note.setText({"viewer": "Ο πάροχος δίνει μόνο online προβολή — «Άνοιγμα στον πάροχο».",
                                       "no_url": "Χωρίς σύνδεσμο παρόχου στο myDATA.",
                                       "failed": "Η λήψη του PDF απέτυχε — ξαναδοκιμάστε «Λήψη μήνα»."}.get(
                d["pdf_status"], "Δεν έχει κατέβει PDF — «Λήψη μήνα»."))

    def _open_selected(self) -> None:
        """Διπλό κλικ: καρτέλα παραστατικού (ανάλυση + PDF) — ρητό αίτημα 2026-10-29."""
        party = names.name_map(self.conn)
        for d in self._selected_docs()[:1]:
            other_name, other_vat = names.display_name(d, party, self.afm)
            InvoiceCard(self, d, other_name, other_vat).exec()

    def _open_pdf_selected(self) -> None:
        for d in self._selected_docs()[:1]:
            if d["pdf_path"] and Path(d["pdf_path"]).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(d["pdf_path"]))
            elif d["url"]:
                QDesktopServices.openUrl(QUrl(d["url"]))      # ρητό κλικ· capability URL, όχι το κλειδί

    def _open_provider(self) -> None:
        """Καθοδηγούμενη λήψη «μόνο online» (gui/online_only.py): τα επιλεγμένα, αλλιώς όλα τα «μόνο online» της
        προβολής που δεν έχουν ακόμη PDF."""
        from .online_only import OnlineOnlyDialog
        chosen = [d for d in self._selected_docs() if d["url"] and d["pdf_status"] != "ok"]
        if len(self._selected_docs()) <= 1:
            chosen = [d for d in self._docs if d["url"] and d["pdf_status"] in ("viewer", "failed")] or chosen
        if not chosen:
            toast(self.window_, "Δεν υπάρχουν παραστατικά «μόνο online» χωρίς PDF — πατήστε πρώτα «Λήψη μήνα» ώστε να "
                                "εντοπιστούν.", "info")
            return
        dlg = OnlineOnlyDialog(self.conn, self.afm, chosen, self)
        dlg.exec()
        if dlg.changed:
            self.reload()

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


def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"
