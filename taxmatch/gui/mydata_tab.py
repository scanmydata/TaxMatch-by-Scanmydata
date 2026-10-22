"""Καρτέλα πελάτη → «myDATA»: κωδικοί API, παραστατικά ανά χρήση/μήνα (με την επωνυμία του αντισυμβαλλόμενου, όπως το
Timologio Downloader), PDF / ZIP / εκτύπωση, και ό,τι προκύπτει για τις υποχρεώσεις: παρακρατούμενοι φόροι, VIES,
Intrastat, τέλος ανθεκτικότητας (8.2), ψηφιακό τέλος συναλλαγής, προειδοποίηση ΦΠΑ σε μη υπόχρεο. Λογική: `taxmatch/mydata/`.

Οι λήψεις τρέχουν στην ουρά εργασιών του κύριου παραθύρου (`MainWindow.run_mydata_sync`): με διακοπή, και αν κλείσει η
καρτέλα το μήνυμα προόδου περνά στο κύριο παράθυρο. Η προεπισκόπηση PDF στο πλάι αφαιρέθηκε (ρητό αίτημα χρήστη
2026-10-22) — διπλό κλικ / «Άνοιγμα PDF» το ανοίγει στον προεπιλεγμένο προβολέα."""
from __future__ import annotations

import html
from datetime import date
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFileDialog, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import db as dbmod
from .. import deadlines, obligations, office
from ..mydata import analysis, credentials as mydata_creds, names, pdfs, store
from .icons import icon
from .theme import CURRENT
from .toast import toast
from .workers import run_task

_COLS = ["Ημ/νία", "Τύπος", "Λ/Ε", "Αντισυμβαλλόμενος", "ΑΦΜ", "Καθαρή", "ΦΠΑ", "Σύνολο", "Παρακρ.", "PDF"]
HISTORY_MONTHS = 24                     # για Intrastat χρειάζεται όλο το προηγούμενο έτος + το τρέχον


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
        bar.addWidget(QLabel("Χρήση:"))
        self.year = QComboBox()
        this_year = date.today().year
        for y in range(this_year, this_year - 6, -1):
            self.year.addItem(str(y), y)
        self.year.currentIndexChanged.connect(lambda _i: self._fill_months())
        bar.addWidget(self.year)
        bar.addWidget(QLabel("Μήνας:"))
        self.month = QComboBox()
        self.month.currentIndexChanged.connect(lambda _i: self.reload())
        bar.addWidget(self.month)
        self.fetch_btn = QPushButton(icon("download", CURRENT.txt, 16), "  Λήψη μήνα")
        self.fetch_btn.setObjectName("primary")
        self.fetch_btn.setToolTip("Παραστατικά του μήνα από το myDATA (λήψεις και εκδοθέντα) και τα PDF τους από τον πάροχο")
        self.fetch_btn.clicked.connect(lambda: self._fetch([self.month.currentData()], pdf=True))
        bar.addWidget(self.fetch_btn)
        year_btn = QPushButton("  Λήψη χρήσης")
        year_btn.setToolTip("Όλοι οι μήνες της επιλεγμένης χρήσης — μόνο στοιχεία, χωρίς PDF, με ΕΝΑ αίτημα ανά κατεύθυνση")
        year_btn.clicked.connect(lambda: self._fetch(year_months(self.year.currentData()), pdf=False))
        bar.addWidget(year_btn)
        history = QPushButton("  Λήψη 24 μηνών")
        history.setToolTip("Μόνο στοιχεία των τελευταίων 24 μηνών (όσοι λείπουν + οι δύο πιο πρόσφατοι) — χρειάζονται για "
                           "τα κατώφλια Intrastat (προηγούμενο έτος) και το Ημερολόγιο")
        history.clicked.connect(lambda: self._fetch(_months(HISTORY_MONTHS), pdf=False, only_missing=True))
        bar.addWidget(history)
        bar.addStretch()
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
        root.addWidget(self.summary)

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
        for col, width in enumerate((64, 48, 40, 0, 90, 90, 80, 90, 80, 90)):
            if width:
                self.table.setColumnWidth(col, width)
        self.table.itemSelectionChanged.connect(self._selection_note)
        self.table.doubleClicked.connect(lambda *_: self._open_selected())
        root.addWidget(self.table, 1)

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
        self.month.blockSignals(True)
        self.month.clear()
        for ym in year_months(self.year.currentData()):
            self.month.addItem(analysis.month_label(ym), ym)
        if select_previous and self.month.count() > 1:
            self.month.setCurrentIndex(1)                 # προηγούμενος μήνας: ο τελευταίος «κλειστός»
        self.month.blockSignals(False)
        if self.month.count() == 0:                       # Ιανουάριος: η τρέχουσα χρήση έχει μόνο τον τρέχοντα μήνα
            self.month.addItem(analysis.month_label(_months(1)[0]), _months(1)[0])
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
        t.setSortingEnabled(False)
        t.setRowCount(len(self._docs))
        for i, d in enumerate(self._docs):
            incoming = d["direction"] == "incoming"
            other_name, other_vat = names.display_name(d, party, self.afm)
            cells = [d["issue_date"][8:10] + "/" + d["issue_date"][5:7], d["invoice_type"], "Λ" if incoming else "Ε",
                     other_name or "—", other_vat or "—", office.money(d["net"]), office.money(d["vat"]),
                     office.money(d["total"]), office.money(d["withheld"]) if d["withheld"] else "",
                     pdfs.STATUS_LABEL.get(d["pdf_status"], "")]
            tip = (f"{'Λήψη από' if incoming else 'Έκδοση προς'} {other_name or '—'} · ΑΦΜ {other_vat or '—'}\n"
                   f"{d['series']} {d['aa']} · {d['issue_date']} · τύπος {d['invoice_type']} · MARK {d['mark']}\n"
                   f"Καθαρή {office.money(d['net'])} · ΦΠΑ {office.money(d['vat'])} · Σύνολο {office.money(d['total'])}"
                   + (f" · ψηφιακό τέλος {office.money(d['stamp'])}" if d.get("stamp") else ""))
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(tip)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, i)
                if col in (5, 6, 7, 8):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 8 and d["withheld"] and incoming:
                    item.setForeground(QColor(CURRENT.warn))
                if d.get("vat_class") and self._non_vat():
                    item.setForeground(QColor(CURRENT.bad))
                if d["cancelled"]:
                    item.setForeground(QColor(CURRENT.muted))
                t.setItem(i, col, item)
        t.setSortingEnabled(True)
        self._render_warning()
        self._render_summary(ym)
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

    def _render_summary(self, ym: str) -> None:
        s = analysis.month_summary(self.conn, self.afm, ym)
        if not s["synced"]:
            self.summary.setText(f"<span style='color:{CURRENT.muted}'>Ο μήνας δεν έχει κατέβει από το myDATA — "
                                 "πατήστε «Λήψη μήνα».</span>")
            return
        business = self._business()
        parts = []
        if s["withholding"]:
            rows = "".join(
                f"<li><b>{office.money(w['amount'])}</b> — {html.escape(w['label'])} ({w['n_docs']} παραστατικά, "
                f"βάση {office.money(w['base'])}) → <b>{html.escape(w['declaration'])}</b> έως "
                f"<b>{date.fromisoformat(w['due']).strftime('%d/%m/%Y')}</b>"
                f"{self._status_html('withholding_fees', ym, business, w['amount'])}</li>" for w in s["withholding"])
            parts.append(f"<b style='color:{CURRENT.warn}'>Παρακρατούμενοι φόροι προς απόδοση</b><ul>{rows}</ul>")
        else:
            parts.append("Παρακρατούμενοι φόροι: κανένα παραστατικό λήψης με παρακράτηση.")
        fees = s["fees"]
        due = date.fromisoformat(obligations.month_end_or_next(*map(int, _next_month(ym).split("-"))).isoformat())
        if fees["climate_docs"]:
            parts.append(f"<b style='color:{CURRENT.warn}'>Τέλος ανθεκτικότητας</b>: {fees['climate_docs']} στοιχεία 8.2 "
                         f"({office.money(fees['climate'])}) → δήλωση απόδοσης έως <b>{due.strftime('%d/%m/%Y')}</b>"
                         f"{self._status_html('climate_fee', ym, business)}")
        if fees["digital_fee"]:
            parts.append(f"<b style='color:{CURRENT.warn}'>Ψηφιακό τέλος συναλλαγής</b> στα έσοδα: "
                         f"{office.money(fees['digital_fee'])} → δήλωση έως <b>{due.strftime('%d/%m/%Y')}</b>"
                         f"{self._status_html('digital_fee', ym, business)}")
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
        bal = analysis.vat_balance(self.conn, self.afm, [ym])
        if bal["output"] or bal["input"]:
            parts.append(f"<span style='color:{CURRENT.muted}'>ΦΠΑ μήνα κατά το myDATA (ενδεικτικά): εκροές "
                         f"{office.money(bal['output'])} − εισροές {office.money(bal['input'])} = "
                         f"{office.money(bal['balance'])}{self._vat_compare(ym, business)}</span>")
        self.summary.setText("<br>".join(parts))

    def _business(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM businesses WHERE afm=?", (self.afm,)).fetchone()
        return {**(dict(row) if row else {}), **deadlines._checks_for(self.conn, self.afm)}  # noqa: SLF001

    def _status_html(self, rule_id: str, ym: str, business: dict[str, Any], expected: float | None = None) -> str:
        """Κατάσταση υποβολής (έλεγχος υποβολών ΑΑΔΕ ή χειροκίνητο «ολοκληρώθηκε») + σύνδεσμος σήμανσης."""
        sub = obligations.submission(rule_id, business, ym)
        status = sub["status"]
        link = (f" <a style='color:{CURRENT.accent}' href='done:{rule_id}:{ym}:{0 if status == 'done' else 1}'>"
                + (
                    "αναίρεση" if status == "done" else "σήμανση ως ολοκληρωμένο") + "</a>")
        if not status:
            hint = "δεν υπάρχει ακόμη αυτόματος έλεγχος" if sub["manual"] else "δεν έχει ελεγχθεί (Προθεσμίες &amp; υποβολές)"
            return f" · <span style='color:{CURRENT.muted}'>υποβολή: {hint}</span>{link}"
        colour = {"submitted": CURRENT.ok, "done": CURRENT.ok, "not_submitted": CURRENT.bad}.get(status, CURRENT.warn)
        text = f" · <b style='color:{colour}'>{html.escape(sub['label'])}</b>"
        amounts = obligations.amounts_text(sub["amounts"])
        if amounts:
            text += f" ({html.escape(amounts)})"
            due_amt = sub["amounts"].get("total_due")
            if expected is not None and due_amt is not None and abs(due_amt - expected) > 0.01:
                text += (f" <b style='color:{CURRENT.bad}'>διαφορά με myDATA {office.money(due_amt - expected)}</b>")
        return text + (link if status in ("done", "") or sub["manual"] else "")

    def _vat_compare(self, ym: str, business: dict[str, Any]) -> str:
        sub = obligations.submission("vat_monthly", business, ym)
        if not sub["amounts"]:
            return ""
        return " · Φ2 ΑΑΔΕ: " + html.escape(obligations.amounts_text(sub["amounts"]))

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
        for d in self._selected_docs()[:1]:
            if d["pdf_path"] and Path(d["pdf_path"]).exists():
                QDesktopServices.openUrl(QUrl.fromLocalFile(d["pdf_path"]))
            elif d["url"]:
                QDesktopServices.openUrl(QUrl(d["url"]))      # ρητό κλικ· capability URL, όχι το κλειδί

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


def _next_month(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"
