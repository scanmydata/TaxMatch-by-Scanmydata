"""Αξιολόγηση κινδύνου πελάτη (δέουσα επιμέλεια, ν. 4557/2018). Τρεις καρτέλες:
* KYC & φάκελος — ΠΕΠ, σκοπός σχέσης, πραγματικοί δικαιούχοι, κατάσταση φακέλου, έγγραφα ανά νομική μορφή.
* Συναλλαγές / προέλευση κεφαλαίων — ρεύματα συναλλαγών του πελάτη· κάθε γραμμή ΠΡΟΤΕΙΝΕΙ παράγοντες των Παραρτημάτων
  (όχι πόντους ανά γραμμή — βλ. `aml.model.factors_from_transactions`).
* Παράγοντες κινδύνου — Παραρτήματα Ι/ΙΙ + περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ».
Δεξιά: ζωντανό αποτέλεσμα και στα δύο μοντέλα. Η αποθήκευση ξαναϋπολογίζει στο `aml.store`."""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSplitter, QTableWidget,
    QTabWidget, QVBoxLayout, QWidget,
)

from .. import db as dbmod, settings_store
from ..aml import documents as aml_docs, export as aml_export, gemi_browser, kmpd_pdf, model, retrieval, sanctions, store
from ..aml.content import (
    ASSESSMENT_KINDS, CLIENT_KINDS, EXTRAORDINARY_TRIGGERS, FEE_PAYMENTS, FILE_STATUSES, KYC_ITEMS, MEASURES, TX_AMOUNTS,
    TX_ACTIVITIES, TX_CHANNELS, TX_COUNTRIES, TX_FREQUENCIES, TX_JUSTIFY_FROM, UBO_STATES, documents_for,
    required_documents,
)
from ..business_profiles import credentials as client_creds, service as clients
from .busy import BusyOverlay
from .theme import CURRENT
from .toast import toast
from .widgets import GrDateEdit, add_reveal
from .workers import run_task

_UBO_COLUMNS = (("name", "Ονοματεπώνυμο", None), ("afm", "ΑΦΜ", None), ("id_number", "Αρ. ταυτότητας", None),
                ("birth_date", "Γέννηση", None), ("nationality", "Υπηκοότητα/κατοικία", None),
                ("country_risk", "Χώρα (κίνδυνος)", TX_COUNTRIES), ("address", "Διεύθυνση", None),
                ("percent", "%", None), ("control", "Τρόπος ελέγχου", None),
                ("pep", "ΠΕΠ", tuple((k, v) for k, v in model.PEP_STATUS if k != "unknown")))
_TX_COLUMNS = (("amount", "Ποσό (ετήσιο)", TX_AMOUNTS), ("channel", "Μέσο πληρωμής", TX_CHANNELS),
               ("frequency", "Συχνότητα", TX_FREQUENCIES), ("activity", "Δραστηριότητα", TX_ACTIVITIES),
               ("country", "Χώρα", TX_COUNTRIES))


def category_colour(category: str) -> str:
    return {model.LOW: CURRENT.ok, model.MEDIUM: CURRENT.warn, model.HIGH: CURRENT.bad}.get(category, CURRENT.muted)


def _scroll(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    return area


class AmlAssessmentDialog(QDialog):
    def __init__(self, conn, afm: str, parent=None, *, kind: str = "") -> None:
        super().__init__(parent)
        self.conn = conn
        self.afm = afm
        self.saved_id: Optional[int] = None
        self.business = clients.get(conn, afm) or {"afm": afm, "name": "", "kads": [], "legal_form": ""}
        self.profile = store.get_profile(conn, afm)
        self.previous = store.latest(conn, afm)
        self.office_model = store.office_model(conn)
        self.suggestions = model.suggest(self.business)
        self._tx_factors: dict[str, str] = {}
        self._tx_overrides: dict[str, str] = {}
        self._tx_warnings: list[str] = []
        self._ready = False
        self._tasks: list = []

        self.setWindowTitle(f"Δέουσα επιμέλεια — {self.business['name'] or afm}")
        self.resize(1180, 780)
        root = QVBoxLayout(self)

        head = QHBoxLayout()
        title = QLabel(f"{self.business['name'] or 'Χωρίς επωνυμία'}  ·  ΑΦΜ {afm}"
                       + (f"  ·  {self.business['legal_form']}" if self.business.get("legal_form") else ""))
        title.setObjectName("h1")
        head.addWidget(title)
        head.addStretch()
        head.addWidget(QLabel("Είδος:"))
        self.kind = QComboBox()
        for key, label in ASSESSMENT_KINDS:
            self.kind.addItem(label, key)
        self.kind.setCurrentIndex(max(0, self.kind.findData(kind or ("periodic" if self.previous else "initial"))))
        self.kind.setToolTip("Έκτακτη όταν:\n• " + "\n• ".join(EXTRAORDINARY_TRIGGERS))
        head.addWidget(self.kind)
        head.addWidget(QLabel("Ημερομηνία:"))
        self.when = GrDateEdit(date.today())
        head.addWidget(self.when)
        root.addLayout(head)
        if self.previous:
            prev = QLabel(f"Προηγούμενη αξιολόγηση {self.previous['assessed_on']}: "
                          f"{model.CATEGORY_LABEL[self.previous['final_category']]} — οι επιλογές της φορτώθηκαν.")
            prev.setObjectName("muted")
            root.addWidget(prev)

        split = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(split, 1)
        self.left_tabs = QTabWidget()
        self.left_tabs.addTab(_scroll(self._kyc_panel()), "KYC & φάκελος")
        self.left_tabs.addTab(self._tx_panel(), "Συναλλαγές / προέλευση κεφαλαίων")
        self.left_tabs.addTab(_scroll(self._factors_panel()), "Παράγοντες κινδύνου")
        split.addWidget(self.left_tabs)
        split.addWidget(self._right_panel())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)

        buttons = QHBoxLayout()
        buttons.addStretch()
        cancel = QPushButton("Άκυρο")
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save_x = QPushButton("Αποθήκευση και εξαγωγή Excel…")
        save_x.clicked.connect(lambda: self._save(export=True))
        buttons.addWidget(save_x)
        save = QPushButton("Αποθήκευση αξιολόγησης")
        save.setObjectName("primary")
        save.clicked.connect(lambda: self._save(export=False))
        buttons.addWidget(save)
        root.addLayout(buttons)

        self.busy = BusyOverlay(self)
        self._load_initial()
        self._reload_files()
        self._show_last_screening()
        self._ready = True
        self._refresh_from_transactions()

    # ------------------------------------------------------------------ KYC & φάκελος
    def _kyc_panel(self) -> QWidget:
        body = QWidget()
        box = QVBoxLayout(body)
        kyc = QGroupBox("Γνωριμία με τον πελάτη (KYC) — άρθρο 13 παρ. 1")
        form = QFormLayout(kyc)
        self.client_kind = QComboBox()
        for key, label in CLIENT_KINDS:
            self.client_kind.addItem(label, key)
        self.client_kind.setToolTip("Καθορίζει τα έγγραφα που ζητούνται. Προτείνεται από τη νομική μορφή — διορθώστε το "
                                    "αν χρειάζεται. Η μεθοδολογία κινδύνου είναι ΜΙΑ για όλα τα είδη.")
        form.addRow("Είδος πελάτη", self.client_kind)
        self.file_status = QComboBox()
        for key, label in FILE_STATUSES:
            self.file_status.addItem(label, key)
        form.addRow("Κατάσταση φακέλου", self.file_status)
        self.pep = QComboBox()
        for key, label in model.PEP_STATUS:
            self.pep.addItem(label, key)
        self.pep.currentIndexChanged.connect(self._on_pep)
        form.addRow("Πολιτικώς εκτεθειμένο πρόσωπο", self.pep)
        screen_row = QHBoxLayout()
        self.sanctions_btn = QPushButton("Έλεγχος σε λίστες κυρώσεων ΕΕ")
        self.sanctions_btn.setToolTip("Πελάτης, νόμιμος εκπρόσωπος και πραγματικοί δικαιούχοι έναντι της Ενοποιημένης λίστας "
                                      "οικονομικών κυρώσεων της ΕΕ (περιλαμβάνει ΟΗΕ). Καταγράφεται στον φάκελο ως τεκμήριο.")
        self.sanctions_btn.clicked.connect(self._screen_sanctions)
        screen_row.addWidget(self.sanctions_btn)
        google = QPushButton("Google (ΠΕΠ / δυσμενή δημοσιεύματα)")
        google.setToolTip("Ανοίγει αναζήτηση Google στον browser με το όνομα του πελάτη/εκπροσώπου — μόνο το όνομα, όχι ΑΦΜ.")
        google.clicked.connect(self._google_check)
        screen_row.addWidget(google)
        screen_row.addStretch()
        form.addRow("", screen_row)
        self.sanctions_label = QLabel("")
        self.sanctions_label.setWordWrap(True)
        self.sanctions_label.setObjectName("muted")
        form.addRow("", self.sanctions_label)
        self.ubo_state = QComboBox()
        for key, label in UBO_STATES:
            self.ubo_state.addItem(label, key)
        self.ubo_state.currentIndexChanged.connect(self._on_ubo)
        form.addRow("Πραγματικοί δικαιούχοι", self.ubo_state)
        self.ubo_notes = QLineEdit()
        self.ubo_notes.setPlaceholderText("Ονόματα/ποσοστά (>25%) ή πώς χαρτογραφήθηκε η δομή")
        form.addRow("", self.ubo_notes)
        self.rel_start = QLineEdit()
        self.rel_start.setPlaceholderText("ηη/μμ/εεεε (προαιρετικό)")
        form.addRow("Έναρξη σχέσης", self.rel_start)
        self.purpose = QPlainTextEdit()
        self.purpose.setPlaceholderText("Σκοπός και φύση της σχέσης, και από πού προκύπτει (π.χ. συνέντευξη, "
                                        "καταστατικό, ΓΕΜΗ, προηγούμενος λογιστής)")
        self.purpose.setFixedHeight(56)
        form.addRow("Σκοπός / φύση", self.purpose)
        self.fee_payment = QComboBox()
        for key, label in FEE_PAYMENTS:
            self.fee_payment.addItem(label, key)
        self.fee_payment.currentIndexChanged.connect(self._refresh_from_transactions)
        form.addRow("Εξόφληση αμοιβής γραφείου", self.fee_payment)
        self.kyc_checks: dict[str, QCheckBox] = {}
        for key, text, ref in KYC_ITEMS:
            if key in ("purpose", "pep_check"):
                continue                            # καλύπτονται από τα πεδία ακριβώς από πάνω
            chk = QCheckBox(f"{text}" + (f"  ({ref})" if ref else ""))
            self.kyc_checks[key] = chk
            form.addRow("", chk)
        box.addWidget(kyc)

        docs = QGroupBox("Έγγραφα φακέλου (ΠΟΛ.1200/2018) — ανάλογα με το είδος πελάτη")
        dl = QVBoxLayout(docs)
        self.docs_box = QVBoxLayout()
        dl.addLayout(self.docs_box)
        self.doc_checks: dict[str, QCheckBox] = {}
        self._doc_state: dict[str, str] = dict(self.profile["docs"])      # κρατά ό,τι σημειώθηκε και σε άλλο είδος
        self.docs_label = QLabel("")
        dl.addWidget(self.docs_label)
        auto = QHBoxLayout()
        self.fetch_btn = QPushButton("Αυτόματη λήψη εγγράφων (Μητρώο ΑΑΔΕ · δήλωση εισοδήματος · ΚΜΠΔ)")
        self.fetch_btn.setToolTip("Κατεβάζει τα PDF από TAXISnet/ΚΜΠΔ με τους κωδικούς του πελάτη (και του νόμιμου "
                                  "εκπροσώπου για το ΚΜΠΔ) και τα σημειώνει ως παραληφθέντα. Μόνο ανάγνωση — "
                                  "καμία υποβολή.")
        self.fetch_btn.clicked.connect(self._fetch_documents)
        auto.addWidget(self.fetch_btn)
        auto.addStretch()
        dl.addLayout(auto)
        self.files_list = QListWidget()
        self.files_list.setMaximumHeight(110)
        self.files_list.itemActivated.connect(self._open_file)
        dl.addWidget(self.files_list)
        open_dir = QPushButton("Άνοιγμα φακέλου εγγράφων")
        open_dir.clicked.connect(self._open_folder)
        dl.addWidget(open_dir)
        box.addWidget(docs)
        self.client_kind.currentIndexChanged.connect(self._build_docs)

        self.rep_group = QGroupBox("Νόμιμος εκπρόσωπος")
        rform = QFormLayout(self.rep_group)
        self.rep_fields: dict[str, QLineEdit] = {}
        for key, label in (("name", "Ονοματεπώνυμο"), ("afm", "ΑΦΜ"), ("role", "Ιδιότητα"), ("id_number", "Αρ. ταυτότητας/διαβατηρίου"),
                           ("father_name", "Πατρώνυμο"), ("birth_date", "Ημ/νία γέννησης"), ("birth_place", "Τόπος γέννησης"),
                           ("nationality", "Υπηκοότητα"), ("address", "Διεύθυνση κατοικίας"), ("phone", "Τηλέφωνο"),
                           ("email", "Email")):
            field = QLineEdit(self.profile["rep"].get(key, ""))
            self.rep_fields[key] = field
            rform.addRow(label, field)
        self.rep_user = QLineEdit(retrieval.rep_user(self.conn, self.afm))
        self.rep_user.setPlaceholderText("Χρήστης TAXISnet του νόμιμου εκπροσώπου (για το ΚΜΠΔ)")
        self.rep_pass = QLineEdit()
        self.rep_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.rep_pass.setPlaceholderText("(κενό = μένει ο αποθηκευμένος)")
        add_reveal(self.rep_pass)
        self.rep_creds_label = QLabel("Κωδικοί TAXISnet εκπροσώπου")
        rform.addRow(self.rep_creds_label, self.rep_user)
        rform.addRow("", self.rep_pass)
        self.gemi_user = QLineEdit(gemi_browser.user_of(self.conn, self.afm))
        self.gemi_user.setPlaceholderText("Χρήστης Γ.Ε.ΜΗ. (businessportal)")
        self.gemi_pass = QLineEdit()
        self.gemi_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.gemi_pass.setPlaceholderText("(κενό = μένει ο αποθηκευμένος)")
        add_reveal(self.gemi_pass)
        rform.addRow("Κωδικοί Γ.Ε.ΜΗ.", self.gemi_user)
        gemi_row = QHBoxLayout()
        gemi_row.addWidget(self.gemi_pass, 1)
        self.gemi_btn = QPushButton("Σύνδεση στο Γ.Ε.ΜΗ. (browser)")
        self.gemi_btn.setToolTip("Ανοίγει τον Chrome/Edge του υπολογιστή ΣΥΝΔΕΔΕΜΕΝΟ στο businessportal (π.χ. για "
                                 "πιστοποιητικό εκπροσώπησης) — από εκεί συνεχίζετε εσείς.")
        self.gemi_btn.clicked.connect(self._open_gemi)
        gemi_row.addWidget(self.gemi_btn)
        rform.addRow("", gemi_row)
        box.addWidget(self.rep_group)

        self.ubo_group = QGroupBox("Πραγματικοί δικαιούχοι (>25% ή έλεγχος — άρθρο 3 παρ. 17)")
        ubox = QVBoxLayout(self.ubo_group)
        self.ubo_table = QTableWidget(0, len(_UBO_COLUMNS))
        self.ubo_table.setHorizontalHeaderLabels([c[1] for c in _UBO_COLUMNS])
        self.ubo_table.verticalHeader().setVisible(False)
        self.ubo_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.ubo_table.setMinimumHeight(150)
        ubox.addWidget(self.ubo_table)
        urow = QHBoxLayout()
        uadd = QPushButton("+ Δικαιούχος")
        uadd.clicked.connect(lambda: self._add_ubo_row({}))
        urow.addWidget(uadd)
        urem = QPushButton("Αφαίρεση επιλεγμένου")
        urem.clicked.connect(self._remove_ubo_row)
        urow.addWidget(urem)
        kimp = QPushButton("Εισαγωγή από PDF ΚΜΠΔ…")
        kimp.setToolTip("Διαβάζει την «Εκτύπωση δικαιούχων» του Κεντρικού Μητρώου Πραγματικών Δικαιούχων και "
                        "συμπληρώνει δικαιούχους (ποσοστό/ιδιότητες) και τα κενά στοιχεία του εκπροσώπου.")
        kimp.clicked.connect(self._import_kmpd_pdf)
        urow.addWidget(kimp)
        urow.addStretch()
        ubox.addLayout(urow)
        hint = QLabel("ΠΕΠ δικαιούχος ⇒ «ΥΠΕΡΙΣΧΥΕΙ» (άρθρο 18)· η χώρα του δικαιούχου μετρά στον άξονα Β — "
                      "μετρά ο πιο επιβαρυντικός δικαιούχος, όχι ο μέσος όρος.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        ubox.addWidget(hint)
        box.addWidget(self.ubo_group)
        self.client_kind.currentIndexChanged.connect(self._sync_person_groups)
        box.addStretch()
        return body

    # ------------------------------------------------------------------ Συναλλαγές
    def _tx_panel(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        intro = QLabel("Καταγράψτε τα κύρια ρεύματα συναλλαγών/εσόδων του πελάτη. Κάθε γραμμή προτείνει παράγοντες "
                       "των Παραρτημάτων (επιλέγονται αυτόματα στην καρτέλα «Παράγοντες κινδύνου») — δεν αθροίζονται "
                       "πόντοι ανά γραμμή, ώστε πολλές «ήπιες» γραμμές να μην κρύβουν μία επικίνδυνη.")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        box.addWidget(intro)
        self.tx_table = QTableWidget(0, len(_TX_COLUMNS) + 1)
        self.tx_table.setHorizontalHeaderLabels([c[1] for c in _TX_COLUMNS] + ["Αιτιολόγηση (>50.000 €)"])
        self.tx_table.verticalHeader().setVisible(False)
        self.tx_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tx_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        box.addWidget(self.tx_table, 1)
        row = QHBoxLayout()
        add = QPushButton("+ Προσθήκη γραμμής")
        add.clicked.connect(lambda: self._add_tx_row({}))
        row.addWidget(add)
        remove = QPushButton("Αφαίρεση επιλεγμένης")
        remove.clicked.connect(self._remove_tx_row)
        row.addWidget(remove)
        row.addStretch()
        box.addLayout(row)
        self.tx_derived = QLabel("")
        self.tx_derived.setWordWrap(True)
        box.addWidget(self.tx_derived)
        return page

    def _add_tx_row(self, tx: dict) -> None:
        r = self.tx_table.rowCount()
        self.tx_table.insertRow(r)
        for col, (field, _label, options) in enumerate(_TX_COLUMNS):
            combo = QComboBox()
            combo.addItem("—", "")
            for key, label in options:
                combo.addItem(label, key)
            combo.setCurrentIndex(max(0, combo.findData(tx.get(field, ""))))
            combo.currentIndexChanged.connect(self._refresh_from_transactions)
            self.tx_table.setCellWidget(r, col, combo)
        just = QLineEdit(tx.get("justification", ""))
        just.setPlaceholderText("σκοπός/τεκμηρίωση")
        just.textChanged.connect(self._refresh_from_transactions)
        self.tx_table.setCellWidget(r, len(_TX_COLUMNS), just)
        self._refresh_from_transactions()

    def _remove_tx_row(self) -> None:
        r = self.tx_table.currentRow()
        if r >= 0:
            self.tx_table.removeRow(r)
            self._refresh_from_transactions()

    def transactions(self) -> list[dict]:
        out = []
        for r in range(self.tx_table.rowCount()):
            tx = {field: self.tx_table.cellWidget(r, col).currentData() for col, (field, _l, _o) in enumerate(_TX_COLUMNS)}
            tx["justification"] = self.tx_table.cellWidget(r, len(_TX_COLUMNS)).text()
            out.append(tx)
        return store.clean_transactions(out)

    def _refresh_from_transactions(self, *_args) -> None:
        """Οι παράγοντες που προκύπτουν από τις συναλλαγές επιλέγονται αυτόματα· όσοι έπαψαν να προκύπτουν (π.χ.
        αφαιρέθηκε η γραμμή) αποεπιλέγονται — εκτός αν τους είχε επιλέξει ο χρήστης/η προηγούμενη αξιολόγηση."""
        if not self._ready:
            return
        factors, overrides, warnings = model.factors_from_transactions(self.transactions(), self.fee_payment.currentData())
        ubo_factors, ubo_overrides = model.factors_from_ubos(self.ubos())
        for key, why in ubo_factors.items():
            factors.setdefault(key, why)
        for key, why in ubo_overrides.items():
            overrides.setdefault(key, why)
        for key in set(self._tx_factors) - set(factors):
            if key not in self._manual_factors:
                self.factor_checks[key].setChecked(False)
        for key in set(self._tx_overrides) - set(overrides):
            if key not in self._manual_overrides and self.override_checks[key].isEnabled():
                self.override_checks[key].setChecked(False)
        self._tx_factors, self._tx_overrides, self._tx_warnings = factors, overrides, warnings
        for key in factors:
            self.factor_checks[key].setChecked(True)
        for key in overrides:
            self.override_checks[key].setChecked(True)
        lines = [f"• {model.FACTORS_BY_KEY[k].text} [{model.FACTORS_BY_KEY[k].ref}] — {why}" for k, why in factors.items()]
        lines += [f"• ΥΠΕΡΙΣΧΥΕΙ: {model.OVERRIDES_BY_KEY[k].text} — {why}" for k, why in overrides.items()]
        self.tx_derived.setText(("Προκύπτουν (από συναλλαγές και πραγματικούς δικαιούχους):\n" + "\n".join(lines)) if lines else
                                "Από τις συναλλαγές/τους δικαιούχους δεν προκύπτει κάποιος παράγοντας.")
        self._recalc()

    # ------------------------------------------------------------------ Παράγοντες
    def _factors_panel(self) -> QWidget:
        body = QWidget()
        box = QVBoxLayout(body)
        self.factor_checks: dict[str, QCheckBox] = {}
        for axis, label in model.AXES:
            group = QGroupBox(label)
            gbox = QVBoxLayout(group)
            gbox.setSpacing(2)
            for f in model.FACTORS:
                if f.axis != axis:
                    continue
                pts = f"Α {f.a:+d}" + ("" if f.b is None else f" · Β {f.b}")
                chk = QCheckBox(f"{f.text}   [{f.ref} · {pts}]")
                tip = [f.tip] if f.tip else []
                if f.key in self.suggestions:
                    tip.append(f"Πρόταση από το προφίλ: {self.suggestions[f.key]}")
                    chk.setText(chk.text() + "   ◆ πρόταση")
                chk.setToolTip("\n".join(tip))
                chk.toggled.connect(self._recalc)
                gbox.addWidget(chk)
                self.factor_checks[f.key] = chk
            box.addWidget(group)

        over = QGroupBox("Περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ» — μία αρκεί για ΥΨΗΛΟ κίνδυνο")
        obox = QVBoxLayout(over)
        self.override_checks: dict[str, QCheckBox] = {}
        for o in model.OVERRIDES:
            chk = QCheckBox(f"{o.text}   [{o.ref}]")
            chk.setToolTip(o.basis + (f"\nΤρέχων κατάλογος: {o.url}" if o.url else ""))
            chk.toggled.connect(self._recalc)
            obox.addWidget(chk)
            self.override_checks[o.key] = chk
        links = QLabel('<a href="https://finance.ec.europa.eu/financial-crime/anti-money-laundering-and-countering-'
                       'financing-terrorism-international-level_en">Κατάλογος ΕΕ τρίτων χωρών υψηλού κινδύνου</a> · '
                       '<a href="https://www.sanctionsmap.eu/">EU Sanctions Map</a> · '
                       '<a href="https://www.fatf-gafi.org/en/countries/black-and-grey-lists.html">Κατάλογοι FATF</a>')
        links.setOpenExternalLinks(True)                # ρητό κλικ του χρήστη — όπως το «Άνοιγμα συνδέσμου»
        links.setWordWrap(True)
        obox.addWidget(links)
        box.addWidget(over)
        box.addStretch()
        return body

    # ------------------------------------------------------------------ δεξιά: αποτέλεσμα + αιτιολόγηση
    def _right_panel(self) -> QWidget:
        panel = QWidget()
        box = QVBoxLayout(panel)
        self.result_title = QLabel("")
        self.result_title.setStyleSheet("font-size:20px; font-weight:800;")
        self.result_title.setWordWrap(True)
        box.addWidget(self.result_title)
        self.result_detail = QLabel("")
        self.result_detail.setWordWrap(True)
        self.result_detail.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.result_detail)
        self.result_notes = QLabel("")
        self.result_notes.setWordWrap(True)
        self.result_notes.setStyleSheet(f"color:{CURRENT.warn};")
        box.addWidget(self.result_notes)
        self.measures = QLabel("")
        self.measures.setWordWrap(True)
        self.measures.setObjectName("muted")
        box.addWidget(self.measures)

        form = QFormLayout()
        self.escalate = QComboBox()
        self.escalate.addItem("— όπως το μοντέλο —", "")
        self.escalate.addItem("Αύξηση σε ΜΕΤΡΙΟ", model.MEDIUM)
        self.escalate.addItem("Αύξηση σε ΥΨΗΛΟ", model.HIGH)
        self.escalate.setToolTip("Η κατάταξη μπορεί μόνο να ανέβει με ρητή αιτιολογία — ποτέ να κατέβει.")
        self.escalate.currentIndexChanged.connect(self._recalc)
        form.addRow("Κρίση αξιολογητή", self.escalate)
        self.escalation_note = QLineEdit()
        self.escalation_note.setPlaceholderText("Γιατί ανεβαίνει η κατάταξη (υποχρεωτικό αν αλλάξει)")
        form.addRow("Αιτιολογία αύξησης", self.escalation_note)
        self.justification = QPlainTextEdit()
        self.justification.setPlaceholderText("Αιτιολογία κατάταξης — και, αν συντρέχει παράγοντας του Παραρτήματος ΙΙ "
                                              "χωρίς υψηλή κατάταξη, γιατί")
        self.justification.setFixedHeight(90)
        form.addRow("Αιτιολογία", self.justification)
        self.assessor = QLineEdit(settings_store.get(self.conn, "aml_officer"))
        form.addRow("Αξιολογητής", self.assessor)
        self.approved_by = QLineEdit()
        self.approved_by.setPlaceholderText("Υποχρεωτικό για υψηλό κίνδυνο / ΠΕΠ")
        form.addRow("Έγκριση ανώτερου στελέχους", self.approved_by)
        box.addLayout(form)
        box.addStretch()
        return panel

    # ------------------------------------------------------------------ λογική
    def _load_initial(self) -> None:
        p = self.profile
        kind = store.effective_kind(p, self.business.get("legal_form", ""), self.business.get("activity_state", ""))
        self.client_kind.setCurrentIndex(max(0, self.client_kind.findData(kind)))
        self._build_docs()
        self.file_status.setCurrentIndex(max(0, self.file_status.findData(p["file_status"] or ("new" if not self.previous else ""))))
        self.pep.setCurrentIndex(max(0, self.pep.findData(p["pep_status"])))
        self.ubo_state.setCurrentIndex(max(0, self.ubo_state.findData(p["ubo_state"])))
        self.ubo_notes.setText(p["ubo_notes"])
        self.fee_payment.setCurrentIndex(max(0, self.fee_payment.findData(p["fee_payment"])))
        if p["relationship_start"]:
            try:
                self.rel_start.setText(date.fromisoformat(p["relationship_start"]).strftime("%d/%m/%Y"))
            except ValueError:
                pass
        self.purpose.setPlainText(p["purpose"])
        for key, chk in self.kyc_checks.items():
            chk.setChecked(key in p["kyc"])
            if key in p["kyc"]:
                chk.setToolTip(f"Ολοκληρώθηκε: {p['kyc'][key]}")
        chosen = set(self.previous["factors"]) if self.previous else set(self.suggestions)
        for key, chk in self.factor_checks.items():
            chk.setChecked(key in chosen)
        overrides = set(self.previous["overrides"]) if self.previous else set()
        for key, chk in self.override_checks.items():
            chk.setChecked(key in overrides)
        # Ό,τι επιλέχθηκε πριν από τις συναλλαγές θεωρείται «χειροκίνητο»: δεν το αποεπιλέγει η αφαίρεση γραμμής.
        self._manual_factors = set(chosen)
        self._manual_overrides = set(overrides)
        for tx in p["transactions"]:
            self._add_tx_row(tx)
        for u in p["ubos"]:
            self._add_ubo_row(u)
        self._sync_person_groups()
        if self.previous:
            self.approved_by.setText(self.previous["approved_by"])
        self._on_pep()
        self._on_ubo()

    # ------------------------------------------------------------------ εκπρόσωπος / δικαιούχοι
    def _sync_person_groups(self, *_args) -> None:
        legal = self._kind() in ("company", "partnership", "public", "trust")
        self.rep_group.setTitle("Νόμιμος εκπρόσωπος" if legal else "Στοιχεία ταυτότητας πελάτη (φυσικό πρόσωπο)")
        self.rep_fields["role"].setEnabled(legal)
        self.ubo_group.setVisible(legal)
        for w in (self.rep_user, self.rep_pass, self.rep_creds_label):
            w.setVisible(legal)

    def _add_ubo_row(self, u: dict) -> None:
        r = self.ubo_table.rowCount()
        self.ubo_table.insertRow(r)
        for col, (field, _label, options) in enumerate(_UBO_COLUMNS):
            if options:
                combo = QComboBox()
                combo.addItem("—", "")
                for key, label in options:
                    combo.addItem(label, key)
                combo.setCurrentIndex(max(0, combo.findData(u.get(field, ""))))
                combo.currentIndexChanged.connect(self._refresh_from_transactions)
                self.ubo_table.setCellWidget(r, col, combo)
            else:
                edit = QLineEdit(u.get(field, ""))
                self.ubo_table.setCellWidget(r, col, edit)
        self._refresh_from_transactions()

    def _remove_ubo_row(self) -> None:
        r = self.ubo_table.currentRow()
        if r >= 0:
            self.ubo_table.removeRow(r)
            self._refresh_from_transactions()

    def ubos(self) -> list[dict]:
        out = []
        for r in range(self.ubo_table.rowCount()):
            row = {}
            for col, (field, _l, options) in enumerate(_UBO_COLUMNS):
                w = self.ubo_table.cellWidget(r, col)
                row[field] = w.currentData() if options else w.text()
            out.append(row)
        return store.clean_ubos(out) if self._kind() in ("company", "partnership", "public", "trust") else []

    def rep(self) -> dict:
        return {k: f.text() for k, f in self.rep_fields.items()}

    def _apply_ubo_import(self, imp: dict) -> str:
        """Συγχωνεύει ό,τι διάβασε το ΚΜΠΔ στην ΤΡΕΧΟΥΣΑ κατάσταση του διαλόγου (που μπορεί να έχει μη αποθηκευμένες
        αλλαγές) — ΠΕΠ/ταυτότητα/χώρα που έγραψε ο χρήστης δεν αγγίζονται."""
        current = []
        for r in range(self.ubo_table.rowCount()):
            row = {}
            for col, (field, _l, options) in enumerate(_UBO_COLUMNS):
                w = self.ubo_table.cellWidget(r, col)
                row[field] = w.currentData() if options else w.text()
            current.append(row)
        merged, added, updated = kmpd_pdf.merge_ubos(current, imp.get("found", []))
        self.ubo_table.setRowCount(0)
        for u in merged:
            self._add_ubo_row(u)
        filled = 0
        for key, value in (imp.get("rep") or {}).items():
            if key in self.rep_fields and value and not self.rep_fields[key].text().strip():
                self.rep_fields[key].setText(value)
                filled += 1
        if filled and not self.rep_fields["role"].text().strip():
            self.rep_fields["role"].setText("Νόμιμος εκπρόσωπος (ΚΜΠΔ)")
        note = imp.get("note", "")
        if note and note not in self.ubo_notes.text():
            rest = re.sub(r"ΚΜΠΔ: αρ\. καταχώρισης[^)]*\)\.?", "", self.ubo_notes.text()).strip(" ·")
            self.ubo_notes.setText(" · ".join(x for x in (rest, note) if x))
        if imp.get("found") and not self.ubo_state.currentData():
            self.ubo_state.setCurrentIndex(max(0, self.ubo_state.findData("pending")))
        return (f"ΚΜΠΔ: {len(imp.get('found', []))} δικαιούχοι ({added} νέοι, {updated} ενημερώθηκαν)"
                + (f", {filled} στοιχεία εκπροσώπου" if filled else "") + ".")

    def _import_kmpd_pdf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "PDF «Εκτύπωση δικαιούχων» του ΚΜΠΔ", "", "PDF (*.pdf)")
        if not path:
            return
        try:
            data = Path(path).read_bytes()
        except OSError as exc:
            toast(self, f"Το αρχείο δεν διαβάστηκε: {exc}", "danger")
            return
        imp = retrieval.import_kmpd_owners(self.conn, self.afm, data)
        if not imp["ok"]:
            toast(self, f"Δεν έγινε εισαγωγή: {imp['reason']}.", "warn")
            return
        retrieval.save_file(self.conn, self.afm, "kmpd", data, self.business.get("name", ""), source="manual")
        self._doc_state.setdefault("ubo_registry", date.today().isoformat())
        if "ubo_registry" in self.doc_checks:
            self.doc_checks["ubo_registry"].setChecked(True)
        self._reload_files()
        toast(self, self._apply_ubo_import(imp), "ok", ms=7000)

    # ------------------------------------------------------------------ κυρώσεις / Google / Γ.Ε.ΜΗ.
    def _show_last_screening(self) -> None:
        last = sanctions.latest_screening(self.conn, self.afm)
        if not last:
            self.sanctions_label.setText("Δεν έχει γίνει έλεγχος σε λίστες κυρώσεων.")
            return
        when = last["screened_at"][:10]
        if last["result"] == "clear":
            self.sanctions_label.setText(f"Τελευταίος έλεγχος {when}: καμία ταύτιση ({len(last['subjects'])} πρόσωπα).")
        else:
            names = ", ".join(sorted({h["matched_name"] for h in last["hits"]}))[:160]
            self.sanctions_label.setText(f"⚠ Έλεγχος {when}: ΠΙΘΑΝΗ ταύτιση — {names}. Ελέγξτε ημ. γέννησης/ιθαγένεια· "
                                         "αν επιβεβαιωθεί, σημειώστε «ΥΠΕΡΙΣΧΥΕΙ — κυρώσεις».")

    def _screen_sanctions(self) -> None:
        # ό,τι γράφτηκε ΤΩΡΑ στον διάλογο (εκπρόσωπος/δικαιούχοι) πρέπει να ελεγχθεί — αποθήκευση πρώτα των προσώπων
        p = store.get_profile(self.conn, self.afm)
        store.save_profile(self.conn, self.afm, pep_status=p["pep_status"], relationship_start=p["relationship_start"],
                           relationship_end=p["relationship_end"], purpose=p["purpose"], kyc=p["kyc"], notes=p["notes"],
                           rep=self.rep(), ubos=self.ubos())
        afm = self.afm

        def work(progress):
            conn = dbmod.connect()           # δικό του thread — ποτέ self.conn
            try:
                idx = sanctions.refresh_index(progress=progress)
                progress("Σύγκριση ονομάτων…")
                return sanctions.screen_client(conn, afm, idx)
            finally:
                conn.close()

        def done(res):
            self.busy.stop()
            self.sanctions_btn.setEnabled(True)
            self._show_last_screening()
            if res["result"] == "clear":
                toast(self, f"Κυρώσεις ΕΕ: καμία ταύτιση για {len(res['subjects'])} πρόσωπα.", "ok")
            else:
                toast(self, f"Κυρώσεις ΕΕ: {len(res['hits'])} πιθανές ταυτίσεις — δείτε την έκθεση.", "danger", ms=9000)
                self._screening_pdf(res["id"])

        def failed(msg):
            self.busy.stop()
            self.sanctions_btn.setEnabled(True)
            toast(self, f"Ο έλεγχος κυρώσεων απέτυχε: {msg}", "danger")

        self.sanctions_btn.setEnabled(False)
        self.busy.start("Έλεγχος σε λίστες κυρώσεων…")
        self._tasks.append(run_task(self, work, on_progress=self.busy.start, on_done=done, on_error=failed))

    def _screening_pdf(self, screening_id: int) -> None:
        sc = next((x for x in sanctions.list_screenings(self.conn, self.afm) if x["id"] == screening_id), None)
        if not sc:
            return
        target = retrieval.client_dir(self.afm) / f"Έλεγχος κυρώσεων {sc['screened_at'][:10]} ({screening_id}).pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        aml_docs.write_pdf(sanctions.screening_html(self.conn, self.afm, sc), target, "Έλεγχος κυρώσεων")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))

    def _google_check(self) -> None:
        legal = self._kind() in ("company", "partnership", "public", "trust")
        name = ("" if legal else self.rep_fields["name"].text().strip()) or self.business.get("name", "")
        if name:
            QDesktopServices.openUrl(QUrl(sanctions.google_query_url(name)))

    def _save_gemi_credentials(self) -> None:
        if self.gemi_user.text().strip() or self.gemi_pass.text():
            gemi_browser.set_credentials(self.conn, self.afm, self.gemi_user.text(), self.gemi_pass.text())
            self.gemi_pass.clear()

    def _open_gemi(self) -> None:
        self._save_gemi_credentials()
        creds = gemi_browser.get_credentials(self.conn, self.afm)
        if not creds:
            toast(self, "Συμπληρώστε χρήστη και κωδικό Γ.Ε.ΜΗ. του πελάτη.", "warn")
            return

        def done(res):
            self.busy.stop()
            self.gemi_btn.setEnabled(True)
            if res["ok"]:
                toast(self, "Ο browser άνοιξε συνδεδεμένος στο Γ.Ε.ΜΗ. — συνεχίστε εκεί.", "ok")
            else:
                msg = gemi_browser.REASON_EL.get(res["reason"], res["reason"])
                toast(self, f"Γ.Ε.ΜΗ.: {msg} {res.get('message', '')}".strip(), "warn", ms=8000)

        def failed(msg):
            self.busy.stop()
            self.gemi_btn.setEnabled(True)
            toast(self, f"Γ.Ε.ΜΗ.: {msg}", "danger")

        self.gemi_btn.setEnabled(False)
        self.busy.start("Σύνδεση στο Γ.Ε.ΜΗ.…")
        self._tasks.append(run_task(self, lambda progress: gemi_browser.open_logged_in(*creds, progress=progress),
                                    on_progress=self.busy.start, on_done=done, on_error=failed))

    # ------------------------------------------------------------------ αυτόματη λήψη εγγράφων
    def _reload_files(self) -> None:
        self.files_list.clear()
        for f in retrieval.list_files(self.conn, self.afm):
            item = QListWidgetItem(f"{f['retrieved_at'][:10]}  ·  {f['filename']}")
            item.setData(Qt.ItemDataRole.UserRole, f["path"])
            item.setToolTip(f["path"])
            self.files_list.addItem(item)
        if not self.files_list.count():
            placeholder = QListWidgetItem("Δεν έχουν ανακτηθεί ακόμη έγγραφα.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.files_list.addItem(placeholder)

    def _open_file(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.ItemDataRole.UserRole)
        if path and Path(path).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
        elif path:
            toast(self, "Το αρχείο δεν βρέθηκε στον δίσκο (μετακινήθηκε ή διαγράφηκε).", "warn")

    def _open_folder(self) -> None:
        folder = retrieval.client_dir(self.afm)
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _save_rep_credentials(self) -> None:
        if self._kind() in ("company", "partnership", "public", "trust") and (self.rep_user.text().strip() or self.rep_pass.text()):
            retrieval.set_rep_credentials(self.conn, self.afm, self.rep_user.text(), self.rep_pass.text())
            self.rep_pass.clear()

    def _fetch_documents(self) -> None:
        legal = self._kind() in ("company", "partnership", "public", "trust")
        self._save_rep_credentials()
        if not client_creds.get(self.conn, self.afm) and not (legal and retrieval.get_rep_credentials(self.conn, self.afm)):
            toast(self, "Χρειάζονται κωδικοί TAXISnet του πελάτη (καρτέλα πελάτη) ή, για το ΚΜΠΔ, του νόμιμου εκπροσώπου.",
                  "warn")
            return
        afm = self.afm

        def work(progress):
            conn = dbmod.connect()           # δικό του thread — ποτέ self.conn (βλ. CLAUDE.md)
            try:
                return retrieval.retrieve_for_client(conn, afm, legal=legal, progress=progress)
            finally:
                conn.close()

        def done(result):
            self.busy.stop()
            self.fetch_btn.setEnabled(True)
            got = {f["doc_key"] for f in result["files"]}
            for key in got:
                self._doc_state.setdefault(key, date.today().isoformat())
                if key in self.doc_checks:
                    self.doc_checks[key].setChecked(True)
            self._reload_files()
            msg = f"Ανακτήθηκαν {len(result['files'])} έγγραφα."
            if result.get("ubo_import"):
                msg += " " + self._apply_ubo_import(result["ubo_import"])
            if result["errors"]:
                msg += " " + " · ".join(result["errors"][:3])
            toast(self, msg, "ok" if result["files"] and not result["errors"] else "warn", ms=9000)

        def failed(msg):
            self.busy.stop()
            self.fetch_btn.setEnabled(True)
            toast(self, f"Η λήψη απέτυχε: {msg}", "danger")

        self.fetch_btn.setEnabled(False)
        self.busy.start("Λήψη εγγράφων…")
        self._tasks.append(run_task(self, work, on_progress=self.busy.start, on_done=done, on_error=failed))

    def _kind(self) -> str:
        return self.client_kind.currentData()

    def _build_docs(self, *_args) -> None:
        for key, chk in self.doc_checks.items():
            if chk.isChecked():
                self._doc_state.setdefault(key, date.today().isoformat())
            else:
                self._doc_state.pop(key, None)
            chk.setParent(None)
            chk.deleteLater()
        self.doc_checks = {}
        legal_form = self.business.get("legal_form", "")
        required = set(required_documents(legal_form, self._kind()))
        for key, text in documents_for(legal_form, self._kind()):
            chk = QCheckBox(text + ("" if key in required else "  (αν απαιτείται)"))
            chk.setChecked(key in self._doc_state)
            if key in self._doc_state:
                chk.setToolTip(f"Παραλήφθηκε: {self._doc_state[key]}")
            chk.toggled.connect(self._update_docs_label)
            self.docs_box.addWidget(chk)
            self.doc_checks[key] = chk
        self._update_docs_label()

    def _update_docs_label(self) -> None:
        required = required_documents(self.business.get("legal_form", ""), self._kind())
        missing = [k for k in required if not self.doc_checks[k].isChecked()]
        self.docs_label.setText("Όλα τα υποχρεωτικά έγγραφα παραλήφθηκαν ✓" if not missing
                                else f"Εκκρεμούν {len(missing)} από {len(required)} υποχρεωτικά έγγραφα.")
        self.docs_label.setStyleSheet(f"color:{CURRENT.ok if not missing else CURRENT.warn};")

    def _on_pep(self) -> None:
        forced = model.overrides_from_profile(self.pep.currentData())
        chk = self.override_checks["o_pep"]
        if forced:
            chk.setChecked(True)
            chk.setEnabled(False)
            chk.setToolTip("Επιλεγμένο αυτόματα: ο πελάτης έχει δηλωθεί ΠΕΠ στην ενότητα KYC (άρθρο 18).")
        else:
            chk.setEnabled(True)
        self._recalc()

    def _on_ubo(self) -> None:
        if self.ubo_state.currentData() == "senior":
            self.factor_checks["a_ubo_unknown"].setChecked(True)
        self._recalc()

    def _selected(self) -> tuple[list[str], list[str]]:
        factors = [k for k, c in self.factor_checks.items() if c.isChecked()]
        overrides = [k for k, c in self.override_checks.items() if c.isChecked()]
        return factors, overrides

    def _recalc(self) -> None:
        if not hasattr(self, "result_title") or not hasattr(self, "override_checks"):
            return
        factors, overrides = self._selected()
        r = model.compute(factors, overrides)
        model_cat = r.category(self.office_model)
        final = model.final_category(model_cat, self.escalate.currentData() or "")
        colour = category_colour(final)
        self.result_title.setText(f"{model.CATEGORY_LABEL[final]} → {model.DD_SHORT[final]} δέουσα επιμέλεια")
        self.result_title.setStyleSheet(f"font-size:20px; font-weight:800; color:{colour};")
        lines = []
        for m, text in (("A", f"Μοντέλο Α: άθροισμα <b>{r.sum_a:+d}</b> → {model.CATEGORY_LABEL[r.cat_a]}"),
                        ("B", f"Μοντέλο Β: <b>{r.total_b}</b>/100 (Α {r.subs_b['A']} · Β {r.subs_b['B']} · "
                              f"Γ {r.subs_b['G']} · Δ {r.subs_b['D']}) → {model.CATEGORY_LABEL[r.cat_b]}")):
            lines.append(("▶ " if m == self.office_model else "&nbsp;&nbsp;&nbsp;") + text
                         + (" <i>(μοντέλο γραφείου)</i>" if m == self.office_model else ""))
        if overrides:
            lines.append(f"Συντρέχουν <b>{len(overrides)}</b> περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ».")
        if final != model_cat:
            lines.append(f"Κατάταξη μοντέλου {model.CATEGORY_LABEL[model_cat]} — αυξήθηκε από τον αξιολογητή.")
        lines.append(f"Παράγοντες: {r.n_annex_i} του Παρ. Ι · {r.n_annex_ii} του Παρ. ΙΙ.")
        self.result_detail.setText("<br>".join(lines))
        self.result_notes.setText("\n".join(f"• {n}" for n in r.notes + self._tx_warnings))
        self.measures.setText(MEASURES[final])

    def _kyc_dates(self) -> dict[str, str]:
        today = date.today().isoformat()
        out = {}
        for key, chk in self.kyc_checks.items():
            if chk.isChecked():
                out[key] = self.profile["kyc"].get(key) or today     # κρατά την ΑΡΧΙΚΗ ημερομηνία ολοκλήρωσης
        if self.pep.currentData() != "unknown":
            out["pep_check"] = self.profile["kyc"].get("pep_check") or today
        if self.purpose.toPlainText().strip():
            out["purpose"] = self.profile["kyc"].get("purpose") or today
        return out

    def _doc_dates(self) -> dict[str, str]:
        """Τα έγγραφα του τρέχοντος είδους πελάτη που είναι σημειωμένα (με την αρχική ημερομηνία παραλαβής)."""
        today = date.today().isoformat()
        return {k: self._doc_state.get(k) or today for k, chk in self.doc_checks.items() if chk.isChecked()}

    def _save(self, export: bool) -> None:
        factors, overrides = self._selected()
        final = model.final_category(model.compute(factors, overrides).category(self.office_model),
                                     self.escalate.currentData() or "")
        if final == model.HIGH and not self.approved_by.text().strip():
            if QMessageBox.question(self, "Έγκριση ανώτερου στελέχους",
                                    "Για πελάτη υψηλού κινδύνου/ΠΕΠ απαιτείται έγκριση ανώτερου διοικητικού στελέχους "
                                    "(άρθρα 16 και 18). Αποθήκευση χωρίς έγκριση;") != QMessageBox.StandardButton.Yes:
                return
        missing_just = [i for i, tx in enumerate(self.transactions(), 1)
                        if tx["amount"] in TX_JUSTIFY_FROM and not tx["justification"]]
        if missing_just:
            self.left_tabs.setCurrentIndex(1)
            toast(self, f"Γράψτε αιτιολόγηση σκοπού για τις μεγάλες συναλλαγές (γραμμή {', '.join(map(str, missing_just))}).",
                  "warn")
            return
        rel = self.rel_start.text().strip()
        rel_iso = ""
        if rel:
            try:
                d, m, y = (int(x) for x in rel.replace("-", "/").replace(".", "/").split("/"))
                rel_iso = date(y if y > 99 else 2000 + y, m, d).isoformat()
            except ValueError:
                toast(self, "Η ημερομηνία έναρξης σχέσης δεν είναι έγκυρη (ηη/μμ/εεεε).", "warn")
                return
        status = self.file_status.currentData()
        if status in ("", "new") and not store.missing_documents({"docs": self._doc_dates(), "client_kind": self._kind()},
                                                                self.business.get("legal_form", "")):
            status = "assessed"
        elif status in ("", "new"):
            status = "docs_pending"
        try:
            store.save_profile(self.conn, self.afm, pep_status=self.pep.currentData(), relationship_start=rel_iso,
                               relationship_end=self.profile.get("relationship_end", ""),
                               purpose=self.purpose.toPlainText(), kyc=self._kyc_dates(), notes=self.profile.get("notes", ""),
                               transactions=self.transactions(), file_status=status, docs=self._doc_dates(),
                               ubo_state=self.ubo_state.currentData(), ubo_notes=self.ubo_notes.text(),
                               fee_payment=self.fee_payment.currentData(), client_kind=self._kind(),
                               rep=self.rep(), ubos=self.ubos())
            self._save_rep_credentials()
            self._save_gemi_credentials()
            self.saved_id = store.save_assessment(
                self.conn, self.afm, factors=factors, overrides=overrides, kind=self.kind.currentData(),
                assessed_on=self.when.date().toPython(), escalate_to=self.escalate.currentData() or "",
                escalation_note=self.escalation_note.text(), justification=self.justification.toPlainText(),
                assessor=self.assessor.text(), approved_by=self.approved_by.text())
        except ValueError as exc:
            toast(self, str(exc), "warn")
            return
        if export:
            safe = "".join(ch for ch in (self.business["name"] or self.afm) if ch.isalnum() or ch in " -_")[:40].strip()
            path, _ = QFileDialog.getSaveFileName(self, "Φύλλο αξιολόγησης",
                                                  f"DE_{self.afm}_{safe}_{date.today():%Y%m%d}.xlsx", "Excel (*.xlsx)")
            if path:
                try:
                    aml_export.assessment_sheet(self.conn, self.saved_id, Path(path))
                except OSError as exc:
                    toast(self.parent() or self, f"Η αξιολόγηση αποθηκεύτηκε, αλλά η εξαγωγή απέτυχε: {exc}", "danger")
        self.accept()
