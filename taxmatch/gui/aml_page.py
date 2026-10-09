"""Σελίδα «Δέουσα επιμέλεια» (ν. 4557/2018, όπως ισχύει μετά τον ν. 5313/2026):
* Κατάσταση πελατών — κατάταξη κινδύνου, επόμενη επανεξέταση, ΠΕΠ, KYC· αξιολόγηση/εξαγωγή.
* Αυτοδιάγνωση — τα 23 σημεία του ερωτηματολογίου ελέγχου της ΑΑΔΕ, με «τι κάνω» για κάθε ΟΧΙ.
* Φάκελος συμμόρφωσης — η πορεία των 15 βημάτων και τα έγγραφα του φακέλου.
* Υποθέσεις — εσωτερικές αναφορές ύποπτων συναλλαγών (→ απόφαση για την Αρχή) και εσωτερικές καταγγελίες
  (ν. 4990/2022), με προθεσμίες και «φάκελο υπόθεσης» σε PDF.
* Πρότυπα εγγράφων — Δήλωση/Συμφωνητικό/Αξιολόγηση × φυσικά/νομικά πρόσωπα σε Word, με ανέβασμα δικών σας.
* Μητρώα — εκπαίδευση, αναφορές/εξετάσεις ύποπτων συναλλαγών, απορρίψεις, καταγγελίες, εσωτερικοί έλεγχοι.
* Μεθοδολογία — ρυθμίσεις γραφείου (μοντέλο, διαστήματα, υπεύθυνος) και το νομικό πλαίσιο.
Όλα τοπικά (SQLite, πίνακες `aml_*`)· δίκτυο ΜΟΝΟ στην «Αυτόματη λήψη εγγράφων» (aml/retrieval.py, background thread)."""
from __future__ import annotations

import re
from datetime import date
from html import escape as html_escape
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from PySide6.QtCore import QFileSystemWatcher, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGridLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSpinBox, QTabWidget,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import config, settings_store
from ..aml import cases as cases_mod, content, documents as aml_docs, export as aml_export, model, store
from ..aml import retrieval, sanctions, templating
from .. import db as dbmod
from .workers import run_task
from .aml_case_dialog import AmlCaseDialog
from .aml_dialog import AmlAssessmentDialog, category_colour
from .icons import icon
from .theme import CURRENT
from .toast import toast
from .table_filter import TableColumnFilter
from .tour import Step, Tour
from .widgets import GrDateEdit, due_badge, resort, setup_columns

if TYPE_CHECKING:
    from .main_window import MainWindow

#: (επικεφαλίδα, πλάτος — 0 = ό,τι περισσεύει, tooltip) — βλ. widgets.setup_columns
_COL_SPEC = [("ΑΦΜ", 84, ""), ("Επωνυμία", 0, "Διπλό κλικ: αξιολόγηση / φάκελος πελάτη"), ("Κίνδυνος", 124, "Τελική κατάταξη"),
             ("Βαθμός", 58, "Βαθμολογία του μοντέλου του γραφείου (Α: άθροισμα · Β: /100)"),
             ("Επιμέλεια", 96, "Είδος δέουσας επιμέλειας"), ("ΠΕΠ", 90, "Πολιτικώς εκτεθειμένο πρόσωπο"),
             ("Αξιολόγηση", 88, "Ημερομηνία τελευταίας αξιολόγησης"), ("Επανεξέταση", 110, "Επόμενη επανεξέταση"),
             ("KYC", 44, "Βήματα γνωριμίας που ολοκληρώθηκαν"), ("Έγγραφα", 62, "Έγγραφα φακέλου που παραλήφθηκαν"),
             ("Κατάσταση φακέλου", 130, "")]
_COLS = [c[0] for c in _COL_SPEC]
_FILTER_COLS = (2, 4, 5, 10)                        # φίλτρο τύπου Excel: Κίνδυνος, Επιμέλεια, ΠΕΠ, Κατάσταση φακέλου
_FILTERS = (("all", "Όλοι οι πελάτες"), ("missing", "Χωρίς αξιολόγηση"), ("overdue", "Εκπρόθεσμη επανεξέταση"),
            ("due_soon", "Επανεξέταση σε 30 ημέρες"), ("high", "Υψηλός κίνδυνος"), ("pep", "ΠΕΠ"),
            ("docs", "Εκκρεμούν έγγραφα"), ("alerts", "Red flag / εκκρεμότητες φακέλου"),
            ("former", "Πρώην πελάτες (τήρηση 5ετίας)"))
_REG_COLS = ["Ημερομηνία", "Είδος", "Τίτλος", "ΑΦΜ", "Λεπτομέρειες", "Έκβαση / απόφαση"]


def _gr(iso: str) -> str:
    try:
        return date.fromisoformat(iso[:10]).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return ""


class AmlPage(QWidget):
    def __init__(self, main: "MainWindow") -> None:
        super().__init__()
        self.main = main
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        intro = QLabel(f"Δέουσα επιμέλεια πελατών — {content.LAW_VERSION}. Ανάλυση κινδύνου ανά πελάτη, φάκελος "
                       "συμμόρφωσης και μητρώα του γραφείου· όλα αποθηκεύονται τοπικά.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        root.addWidget(intro)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._clients_tab(), icon("clients", CURRENT.muted, 16), "Κατάσταση πελατών")
        self.tabs.addTab(self._self_check_tab(), icon("check", CURRENT.muted, 16), "Αυτοδιάγνωση (ΑΑΔΕ)")
        self.tabs.addTab(self._folder_tab(), icon("folder", CURRENT.muted, 16), "Φάκελος συμμόρφωσης")
        self.tabs.addTab(self._cases_tab(), icon("bell", CURRENT.muted, 16), "Υποθέσεις")
        self.tabs.addTab(self._registers_tab(), icon("edit", CURRENT.muted, 16), "Μητρώα")
        self.tabs.addTab(self._templates_tab(), icon("csv", CURRENT.muted, 16), "Πρότυπα εγγράφων")
        self.tabs.addTab(self._method_tab(), icon("info", CURRENT.muted, 16), "Μεθοδολογία & ρυθμίσεις")

    @property
    def conn(self):
        return self.main.conn

    def reload(self) -> None:
        self.reload_clients()
        self._load_office_items()
        self.reload_registers()
        self.reload_cases()
        self.reload_templates()
        self._load_settings()

    # ================================================================== Κατάσταση πελατών
    def _clients_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        kpis = QGridLayout()
        self._kpi: dict[str, QLabel] = {}
        for i, (key, label) in enumerate((("clients", "Πελάτες"), ("missing", "Χωρίς αξιολόγηση"),
                                          ("overdue", "Εκπρόθεσμες"), ("due_soon", "Σε 30 ημέρες"),
                                          ("high", "Υψηλός κίνδυνος"), ("pep", "ΠΕΠ"),
                                          ("docs_missing", "Εκκρεμούν έγγραφα"), ("alerts", "Red flags"))):
            card = QFrame()
            card.setObjectName("card")
            cbox = QVBoxLayout(card)
            value = QLabel("0")
            value.setObjectName("stat")
            cbox.addWidget(value)
            cap = QLabel(label)
            cap.setObjectName("statLabel")
            cbox.addWidget(cap)
            kpis.addWidget(card, 0, i)
            self._kpi[key] = value
        box.addLayout(kpis)

        top = QHBoxLayout()
        self.filter = QComboBox()
        for key, label in _FILTERS:
            self.filter.addItem(label, key)
        self.filter.currentIndexChanged.connect(self._apply_filter)
        top.addWidget(self.filter)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Αναζήτηση ΑΦΜ/επωνυμίας…")
        self.search.setMinimumWidth(240)
        self.search.textChanged.connect(self._apply_filter)
        top.addWidget(self.search, 1)
        help_btn = self.help_btn = QPushButton(icon("info", CURRENT.txt, 16), "  Ξενάγηση")
        help_btn.setToolTip("Σύντομη ξενάγηση στη Δέουσα επιμέλεια")
        help_btn.clicked.connect(self.start_tour)
        top.addWidget(help_btn)
        manual_btn = QPushButton(icon("pdf", CURRENT.txt, 16), "  Εγχειρίδιο")
        manual_btn.setToolTip("Άνοιγμα του εγχειριδίου (κεφάλαιο 8: Δέουσα επιμέλεια)")
        manual_btn.clicked.connect(self.main.on_manual)
        top.addWidget(manual_btn)
        box.addLayout(top)

        # Δεύτερη σειρά: ενέργειες για τον επιλεγμένο πελάτη. Χωριστά από τα φίλτρα — σε μία σειρά δεν χωρούσαν και τα
        # κουμπιά έπεφταν το ένα πάνω στο άλλο (αναφορά χρήστη, 2026-10-09).
        actions = QHBoxLayout()
        assess = self.assess_btn = QPushButton(icon("edit", CURRENT.txt, 16), "  Αξιολόγηση πελάτη")
        assess.setObjectName("primary")
        assess.setToolTip("Άνοιγμα του φακέλου του επιλεγμένου πελάτη για νέα αξιολόγηση")
        assess.clicked.connect(self._assess_selected)
        actions.addWidget(assess)
        docs_btn = self.docs_btn = QPushButton(icon("pdf", CURRENT.txt, 16), "  Έγγραφα πελάτη  ▾")
        docs_menu = QMenu(docs_btn)
        for label, kind in (("Δήλωση παροχής στοιχείων", "declaration"), ("Ερωτηματολόγιο γνωριμίας (KYC)", "questionnaire"),
                            ("Έκθεση αξιολόγησης κινδύνου", "assessment"), ("Ρήτρες σύμβασης ν. 4557/2018", "clauses")):
            docs_menu.addAction(label + " (PDF)", lambda k=kind: self._client_pdf(k))
        docs_menu.addSeparator()
        for doc_type, label in templating.DOC_TYPES:
            docs_menu.addAction(f"Word από πρότυπο γραφείου: {label}", lambda d=doc_type: self._client_word(d))
        docs_menu.addSeparator()
        docs_menu.addAction("Αυτόματη λήψη εγγράφων (Μητρώο ΑΑΔΕ · Ν/Ε1/Ε3 · ΚΜΠΔ)", self._fetch_selected_documents)
        docs_btn.setMenu(docs_menu)
        actions.addWidget(docs_btn)
        sheet = QPushButton(icon("excel", CURRENT.txt, 16), "  Φύλλο αξιολόγησης")
        sheet.setToolTip("Excel με την τελευταία αξιολόγηση του επιλεγμένου πελάτη")
        sheet.clicked.connect(self._export_selected_sheet)
        actions.addWidget(sheet)
        actions.addStretch()
        screen_all = QPushButton(icon("check", CURRENT.txt, 16), "  Έλεγχος κυρώσεων (όλοι)")
        screen_all.setToolTip("Έλεγχος όλων των πελατών στην Ενοποιημένη λίστα κυρώσεων της ΕΕ")
        screen_all.clicked.connect(self._screen_all_clients)
        actions.addWidget(screen_all)
        summary = QPushButton(icon("excel", CURRENT.txt, 16), "  Συγκεντρωτική (Excel)")
        summary.setToolTip("Συγκεντρωτική κατάσταση όλων των πελατών σε Excel")
        summary.clicked.connect(self._export_summary)
        actions.addWidget(summary)
        box.addLayout(actions)
        for layout in (top, actions):                       # κανένα κουμπί στενότερο από το κείμενό του
            for i in range(layout.count()):
                w = layout.itemAt(i).widget()
                if isinstance(w, QPushButton):
                    w.setMinimumWidth(w.sizeHint().width())

        self.table = QTableWidget(0, len(_COLS))
        self.table.setHorizontalHeaderLabels(_COLS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(self._assess_selected)
        # Όπως οι πίνακες Πελάτες/Νέα: ταξινόμηση με κλικ στην επικεφαλίδα, στήλες που σέρνονται (σειρά/πλάτος) και
        # θυμούνται τη διάταξη, φίλτρο τύπου Excel. apply_fn = η ΔΙΚΗ μας συνάρτηση που συνδυάζει όλα τα κριτήρια
        # (βλ. CLAUDE.md §Κανόνες για το TableColumnFilter).
        self._col_filter = TableColumnFilter(self.table, _FILTER_COLS, apply_fn=self._apply_filter)
        self._col_filter.filtersChanged.connect(self._apply_filter)
        self.table.setColumnWidth(1, 220)                    # αρχικό πλάτος αν το παράθυρο είναι στενό (η στήλη «γεμίζει» μόνη της)
        setup_columns(self.table, _COL_SPEC, self.main._prefs, "aml_clients")
        box.addWidget(self.table, 1)
        hint = QLabel("Διπλό κλικ: νέα αξιολόγηση (φορτώνει τις επιλογές της προηγούμενης). Οι επανεξετάσεις "
                      "εμφανίζονται και στο Ημερολόγιο. Η διαγραφή πελάτη ΔΕΝ σβήνει το ιστορικό δέουσας επιμέλειας "
                      "(τήρηση 5 ετών, άρθρο 30).")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        box.addWidget(hint)
        return page

    def reload_clients(self) -> None:
        self._rows = store.overview(self.conn)
        counts = store.summary_counts(self.conn)
        for key, lbl in self._kpi.items():
            lbl.setText(str(counts.get(key, 0)))
        self._kpi["overdue"].setStyleSheet(f"color:{CURRENT.bad};" if counts["overdue"] else "")
        self._kpi["missing"].setStyleSheet(f"color:{CURRENT.warn};" if counts["missing"] else "")
        self._kpi["docs_missing"].setStyleSheet(f"color:{CURRENT.warn};" if counts["docs_missing"] else "")
        self._kpi["alerts"].setStyleSheet(f"color:{CURRENT.bad};" if counts["alerts"] else "")
        t = self.table
        t.setSortingEnabled(False)
        t.setRowCount(len(self._rows))
        for i, r in enumerate(self._rows):
            a = r["assessment"]
            score = ""
            if a:
                score = f"{a['total_b']}/100" if a["model"] == "B" else f"{a['sum_a']:+d}"
            review = ""
            review_colour = None
            if a:
                badge = due_badge(r["next_review"])
                review = badge[0] if badge and r["status"] in ("overdue", "due_soon") else _gr(r["next_review"])
                review_colour = badge[1] if badge and r["status"] in ("overdue", "due_soon") else None
            name = r["name"] or "—"
            if not r["is_client"]:
                name += "  (πρώην πελάτης)"
            cells = [r["afm"], name, model.CATEGORY_LABEL.get(r["category"], "χωρίς αξιολόγηση"), score,
                     model.DD_SHORT.get(r["category"], ""), model.PEP_LABEL.get(r["pep_status"], ""),
                     _gr(a["assessed_on"]) if a else "", review, f"{r['kyc_done']}/{r['kyc_total']}",
                     f"{r['docs_done']}/{r['docs_total']}", content.FILE_STATUS_LABEL.get(r["file_status"], "")]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, r["afm"])
                if col == 2:
                    item.setForeground(QColor(category_colour(r["category"]) if r["category"] else CURRENT.warn))
                if col == 7 and review_colour:
                    item.setForeground(QColor(review_colour))
                if col == 9 and r["docs_done"] < r["docs_total"]:
                    item.setForeground(QColor(CURRENT.warn))
                if col == 10 and r["file_status"] in content.FILE_STATUS_ALERT:
                    item.setForeground(QColor(CURRENT.bad))
                if col == 5 and r["pep_status"] in ("domestic", "foreign", "family", "associate"):
                    item.setForeground(QColor(CURRENT.bad))
                if not r["is_client"]:
                    item.setForeground(QColor(CURRENT.muted))
                t.setItem(i, col, item)
        t.setSortingEnabled(True)
        resort(t)
        self._apply_filter()

    def _row_matches(self, r: dict, key: str) -> bool:
        if key == "former":
            return not r["is_client"]
        if not r["is_client"]:
            return False
        return {"all": True, "missing": r["status"] == "missing", "overdue": r["status"] == "overdue",
                "due_soon": r["status"] == "due_soon", "high": r["category"] == model.HIGH,
                "pep": r["pep_status"] in ("domestic", "foreign", "family", "associate"),
                "docs": r["docs_done"] < r["docs_total"],
                "alerts": r["file_status"] in content.FILE_STATUS_ALERT}.get(key, True)

    def _apply_filter(self) -> None:
        key = self.filter.currentData()
        needle = self.search.text().strip().lower()
        by_afm = {r["afm"]: r for r in getattr(self, "_rows", [])}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            r = by_afm.get(item.data(Qt.ItemDataRole.UserRole)) if item else None
            ok = bool(r) and self._row_matches(r, key)
            if ok and needle:
                ok = needle in r["afm"] or needle in (r["name"] or "").lower()
            if ok:
                ok = all(not allowed or self.table.item(row, c).text() in allowed
                         for c, allowed in self._col_filter.filters.items())
            self.table.setRowHidden(row, not ok)

    # ------------------------------------------------------------------ ξενάγηση σελίδας
    def tour_steps(self) -> list[Step]:
        first = lambda: self.tabs.setCurrentIndex(0)  # noqa: E731
        return [
            Step("Δέουσα επιμέλεια", "Εδώ οργανώνονται οι υποχρεώσεις του γραφείου ως υπόχρεης οντότητας (ν. 4557/2018): "
                 "ανάλυση κινδύνου κάθε πελάτη, φάκελος συμμόρφωσης, μητρώα και υποθέσεις.", lambda: self.tabs.tabBar(), first),
            Step("1. Εικόνα με μια ματιά", "Πόσοι πελάτες δεν έχουν αξιολόγηση, πόσες επανεξετάσεις έληξαν ή λήγουν σε 30 "
                 "ημέρες, υψηλός κίνδυνος, ΠΕΠ, έγγραφα που λείπουν.", lambda: self._kpi["missing"].parentWidget(), first),
            Step("2. Οι πελάτες", "Κλικ στην επικεφαλίδα ταξινομεί· οι στήλες σέρνονται για αλλαγή σειράς και πλάτους· το "
                 "χωνί φιλτράρει όπως στο Excel. Διπλό κλικ ανοίγει τον φάκελο του πελάτη.", lambda: self.table, first),
            Step("3. Αξιολόγηση πελάτη", "Ο φάκελος έχει τρεις καρτέλες: KYC (είδος πελάτη, ΠΕΠ, εκπρόσωπος, δικαιούχοι, "
                 "έγγραφα), Συναλλαγές/προέλευση κεφαλαίων και Παράγοντες κινδύνου — το αποτέλεσμα φαίνεται ζωντανά. Από "
                 "εκεί: αυτόματη λήψη εγγράφων (Μητρώο ΑΑΔΕ, δήλωση εισοδήματος, ΚΜΠΔ), έλεγχος κυρώσεων, σύνδεση Γ.Ε.ΜΗ.",
                 lambda: self.assess_btn, first),
            Step("4. Έγγραφα πελάτη", "Δήλωση παροχής στοιχείων, Ερωτηματολόγιο KYC, Έκθεση αξιολόγησης και Ρήτρες σύμβασης "
                 "σε PDF — ή Word από τα πρότυπα του γραφείου σας.", lambda: self.docs_btn, first),
            Step("5. Πρότυπα εγγράφων", "Τα έξι πρότυπα Word (Δήλωση, Συμφωνητικό, Αξιολόγηση × φυσικά/νομικά). "
                 "«Επεξεργασία στο Word» ανοίγει το πρότυπο· μόλις το αποθηκεύσετε, η εφαρμογή κρατά τη νέα εκδοχή.",
                 lambda: self.tpl_table, lambda: self.tabs.setCurrentIndex(5)),
            Step("6. Γραφείο", "Αυτοδιάγνωση με τα 23 σημεία της ΑΑΔΕ, φάκελος συμμόρφωσης, υποθέσεις (αναφορές ύποπτων "
                 "συναλλαγών στην Αρχή, καταγγελίες ν. 4990/2022), μητρώα και μεθοδολογία (Μοντέλο Α/Β).",
                 lambda: self.tabs.tabBar(), first),
        ]

    def start_tour(self) -> None:
        old = getattr(self, "_tour", None)
        if old is not None:
            old.deleteLater()
        self._tour = Tour(self.main, self.tour_steps())
        self._tour.start()
        self._tour.setFocus()

    def _selected_afm(self) -> Optional[str]:
        row = self.table.currentRow()
        if row < 0 or self.table.isRowHidden(row):
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _assess_selected(self, *_args) -> None:
        afm = self._selected_afm()
        if not afm:
            toast(self.main, "Επιλέξτε πρώτα έναν πελάτη από τη λίστα.", "warn")
            return
        self.open_assessment(afm)

    def open_assessment(self, afm: str) -> bool:
        r = next((x for x in getattr(self, "_rows", []) if x["afm"] == afm), None)
        if r is not None and not r["is_client"]:
            toast(self.main, "Πρώην πελάτης: το ιστορικό τηρείται, αλλά δεν γίνεται νέα αξιολόγηση.", "warn")
            return False
        dlg = AmlAssessmentDialog(self.conn, afm, self.main)
        if dlg.exec() and dlg.saved_id:
            a = store.get_assessment(self.conn, dlg.saved_id)
            toast(self.main, f"Αποθηκεύτηκε: {model.CATEGORY_LABEL[a['final_category']]} — επόμενη επανεξέταση "
                             f"{_gr(a['next_review'])}.", "ok")
            self.main.after_aml_change()
            return True
        return False

    def _export_selected_sheet(self) -> None:
        afm = self._selected_afm()
        a = store.latest(self.conn, afm) if afm else None
        if not a:
            toast(self.main, "Ο επιλεγμένος πελάτης δεν έχει ακόμη αξιολόγηση.", "warn")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Φύλλο αξιολόγησης", f"DE_{afm}_{a['assessed_on']}.xlsx",
                                              "Excel (*.xlsx)")
        if path:
            try:
                aml_export.assessment_sheet(self.conn, a["id"], Path(path))
                toast(self.main, "Το φύλλο αξιολόγησης εξήχθη — υπογράφεται και μπαίνει στον φάκελο του πελάτη.", "ok")
            except OSError as exc:
                toast(self.main, f"Η εξαγωγή απέτυχε: {exc}", "danger")

    def _client_pdf(self, kind: str) -> None:
        afm = self._selected_afm()
        if not afm:
            toast(self.main, "Επιλέξτε πρώτα έναν πελάτη από τη λίστα.", "warn")
            return
        titles = {"declaration": "Δήλωση παροχής στοιχείων", "questionnaire": "Ερωτηματολόγιο KYC",
                  "assessment": "Έκθεση αξιολόγησης κινδύνου", "clauses": "Ρήτρες σύμβασης ν. 4557"}
        if kind == "assessment":
            a = store.latest(self.conn, afm)
            if not a:
                toast(self.main, "Ο πελάτης δεν έχει ακόμη αξιολόγηση.", "warn")
                return
            html_text = aml_docs.assessment_html(self.conn, a["id"])
        else:
            html_text = {"declaration": aml_docs.declaration_html, "questionnaire": aml_docs.questionnaire_html,
                         "clauses": aml_docs.engagement_clauses_html}[kind](self.conn, afm)
        slug = {"declaration": "Dilosi", "questionnaire": "Erotimatologio", "assessment": "Ekthesi", "clauses": "Rhtres"}[kind]
        path, _ = QFileDialog.getSaveFileName(self, titles[kind], f"{slug}_{afm}_{date.today():%Y%m%d}.pdf", "PDF (*.pdf)")
        if not path:
            return
        try:
            aml_docs.write_pdf(html_text, Path(path), titles[kind])
        except OSError as exc:
            toast(self.main, f"Η δημιουργία του PDF απέτυχε: {exc}", "danger")
            return
        toast(self.main, f"«{titles[kind]}» δημιουργήθηκε.", "ok")

    def _fetch_selected_documents(self) -> None:
        afm = self._selected_afm()
        if not afm:
            toast(self.main, "Επιλέξτε πρώτα έναν πελάτη από τη λίστα.", "warn")
            return

        def work(progress):
            conn = dbmod.connect()           # δικό του thread — ποτέ self.conn (βλ. CLAUDE.md)
            try:
                return retrieval.retrieve_for_client(conn, afm, progress=progress)
            finally:
                conn.close()

        def done(result):
            self.reload_clients()
            msg = f"{afm}: ανακτήθηκαν {len(result['files'])} έγγραφα."
            if result["errors"]:
                msg += " " + " · ".join(result["errors"][:3])
            toast(self.main, msg, "ok" if result["files"] and not result["errors"] else "warn", ms=9000)

        toast(self.main, f"Ξεκίνησε η λήψη εγγράφων για {afm}…", "info")
        self.main._tasks.append(run_task(self.main, work, on_done=done,
                                         on_error=lambda m: toast(self.main, f"Η λήψη απέτυχε: {m}", "danger")))

    def _screen_all_clients(self) -> None:
        """Μαζικός έλεγχος (όπως η «Λίστα προσώπων με ονομαστικές κυρώσεις σε Ε.Ε.» του taxis): πελάτης, εκπρόσωπος,
        δικαιούχοι — κάθε πελάτης καταγράφεται χωριστά στο `aml_screenings`."""

        def work(progress):
            conn = dbmod.connect()           # δικό του thread
            try:
                idx = sanctions.refresh_index(progress=progress)
                afms = [r["afm"] for r in conn.execute("SELECT afm FROM businesses ORDER BY name")]
                flagged = []
                for i, afm in enumerate(afms, 1):
                    progress(f"Έλεγχος κυρώσεων {i}/{len(afms)}…")
                    if sanctions.screen_client(conn, afm, idx)["result"] != "clear":
                        flagged.append(afm)
                return len(afms), flagged
            finally:
                conn.close()

        def done(result):
            total, flagged = result
            if flagged:
                toast(self.main, f"Κυρώσεις ΕΕ: πιθανή ταύτιση σε {len(flagged)} από {total} πελάτες ({', '.join(flagged[:5])}"
                      f"{'…' if len(flagged) > 5 else ''}) — ανοίξτε την αξιολόγησή τους.", "danger", ms=12000)
            else:
                toast(self.main, f"Κυρώσεις ΕΕ: καμία ταύτιση σε {total} πελάτες.", "ok")

        toast(self.main, "Ξεκίνησε ο έλεγχος κυρώσεων για όλους τους πελάτες…", "info")
        self.main._tasks.append(run_task(self.main, work, on_done=done,
                                         on_error=lambda m: toast(self.main, f"Ο έλεγχος απέτυχε: {m}", "danger")))

    def _client_word(self, doc_type: str) -> None:
        afm = self._selected_afm()
        if not afm:
            toast(self.main, "Επιλέξτε πρώτα έναν πελάτη από τη λίστα.", "warn")
            return
        data, unknown, slot = templating.generate(self.conn, doc_type, afm)
        slug = {"declaration": "Dilosi", "agreement": "Symfonitiko", "assessment": "Axiologisi"}[doc_type]
        path, _ = QFileDialog.getSaveFileName(self, templating.DOC_TYPE_LABEL[doc_type],
                                              f"{slug}_{afm}_{date.today():%Y%m%d}.docx", "Word (*.docx)")
        if not path:
            return
        try:
            Path(path).write_bytes(data)
        except OSError as exc:
            toast(self.main, f"Η αποθήκευση απέτυχε: {exc}", "danger")
            return
        msg = f"Δημιουργήθηκε από το πρότυπο «{templating.slot_label(slot)}»."
        if unknown:
            msg += f" Άγνωστα πεδία (έμειναν κενά): {', '.join(unknown[:8])}{'…' if len(unknown) > 8 else ''}."
        toast(self.main, msg, "warn" if unknown else "ok", ms=9000 if unknown else None)

    # ================================================================== Πρότυπα εγγράφων
    def _templates_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        info = QLabel("Έξι πρότυπα Word: Δήλωση παροχής στοιχείων, Ιδιωτικό συμφωνητικό και Αξιολόγηση κινδύνου, χωριστά για "
                      "φυσικά και νομικά πρόσωπα. «Επεξεργασία στο Word» (ή διπλό κλικ) ανοίγει το πρότυπο· γράψτε το κείμενό "
                      "σας με πεδία όπως ${client_name} ή {{client_afm}} και πατήστε Αποθήκευση στο Word — η εφαρμογή κρατά "
                      "αυτόματα τη νέα εκδοχή. Μπορείτε και να ανεβάσετε έτοιμο .docx (π.χ. το συμφωνητικό σας, ή πρότυπο "
                      "του taxis.com.gr με πεδία ${meponimia_etairias}, ${mtitlos1}…).")
        info.setWordWrap(True)
        info.setObjectName("muted")
        box.addWidget(info)
        self.tpl_table = QTableWidget(len(templating.SLOTS), 3)
        self.tpl_table.setHorizontalHeaderLabels(["Πρότυπο", "Πηγή", "Ενημέρωση"])
        self.tpl_table.verticalHeader().setVisible(False)
        self.tpl_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tpl_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.tpl_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tpl_table.setColumnWidth(0, 360)
        self.tpl_table.setColumnWidth(1, 300)
        self.tpl_table.horizontalHeader().setStretchLastSection(True)
        self.tpl_table.setFixedHeight(232)
        box.addWidget(self.tpl_table)
        row = QHBoxLayout()
        for label, handler in (("Επεξεργασία στο Word", self._edit_template), ("Δοκιμή με πελάτη…", self._preview_template),
                               ("Ανέβασμα δικού σας (.docx)…", self._upload_template),
                               ("Αποθήκευση αντιγράφου…", self._download_template),
                               ("Επαναφορά προεπιλογής", self._reset_template)):
            b = QPushButton(label)
            if handler == self._edit_template:
                b.setObjectName("primary")
                self.tpl_edit_btn = b
            b.clicked.connect(handler)
            b.setMinimumWidth(b.sizeHint().width())
            row.addWidget(b)
        row.addStretch()
        box.addLayout(row)
        self.tpl_table.doubleClicked.connect(self._edit_template)
        # Επεξεργασία: το πρότυπο γράφεται σε αρχείο εργασίας και παρακολουθείται ο ΦΑΚΕΛΟΣ (το Word αποθηκεύει με
        # προσωρινό αρχείο + μετονομασία, οπότε η παρακολούθηση του ίδιου του αρχείου χάνεται στην πρώτη αποθήκευση).
        self._tpl_editing: dict[str, tuple[str, bytes]] = {}          # διαδρομή -> (θέση, τελευταίο γνωστό περιεχόμενο)
        self._tpl_watcher = QFileSystemWatcher(self)
        self._tpl_timer = QTimer(self)
        self._tpl_timer.setSingleShot(True)
        self._tpl_timer.setInterval(1200)
        self._tpl_timer.timeout.connect(self._sync_edited_templates)
        self._tpl_watcher.directoryChanged.connect(lambda _p: self._tpl_timer.start())
        self._tpl_watcher.fileChanged.connect(lambda _p: self._tpl_timer.start())
        fields = QGroupBox("Διαθέσιμα πεδία")
        fl = QVBoxLayout(fields)
        text = QLabel("<table cellpadding=2>" + "".join(
            f"<tr><td><code>${{{html_escape(k)}}}</code></td><td>{html_escape(v)}</td></tr>" for k, v in templating.FIELD_HELP)
            + "</table>")
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setWordWrap(True)
        text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(text)
        fl.addWidget(scroll)
        box.addWidget(fields, 1)
        return page

    def reload_templates(self) -> None:
        self._sync_edited_templates()
        info = templating.template_info(self.conn)
        for i, slot in enumerate(templating.SLOTS):
            custom = info.get(slot)
            cells = [templating.slot_label(slot), f"Του γραφείου: {custom['filename']}" if custom else "Προεπιλογή εφαρμογής",
                     _gr(custom["uploaded_at"]) if custom else ""]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, slot)
                if col == 1 and custom:
                    item.setForeground(QColor(CURRENT.ok))
                self.tpl_table.setItem(i, col, item)

    def _selected_slot(self) -> Optional[str]:
        row = self.tpl_table.currentRow()
        if row < 0:
            toast(self.main, "Επιλέξτε πρώτα ένα πρότυπο από τη λίστα.", "warn")
            return None
        return self.tpl_table.item(row, 0).data(Qt.ItemDataRole.UserRole)

    def _template_work_dir(self) -> Path:
        folder = config.data_dir() / "aml_templates"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def _edit_template(self, *_args) -> None:
        slot = self._selected_slot()
        if not slot:
            return
        data, _name, _custom = templating.get_template(self.conn, slot)
        label = re.sub(r'[\\/:*?"<>|]+', " ", templating.slot_label(slot))
        path = self._template_work_dir() / f"{label}.docx"
        try:
            if not path.exists() or path.read_bytes() != data:
                path.write_bytes(data)
        except OSError:
            pass                                    # ήδη ανοιχτό στο Word: απλώς φέρ' το μπροστά
        self._tpl_editing[str(path)] = (slot, data)
        folder = str(path.parent)
        if folder not in self._tpl_watcher.directories():
            self._tpl_watcher.addPath(folder)
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            toast(self.main, "Δεν βρέθηκε πρόγραμμα για αρχεία .docx (Word/LibreOffice).", "danger")
            return
        toast(self.main, "Το πρότυπο άνοιξε για επεξεργασία. Πατήστε Αποθήκευση στο Word — η νέα εκδοχή κρατιέται αυτόματα.",
              "ok", ms=8000)

    def _sync_edited_templates(self) -> None:
        """Ό,τι αποθηκεύτηκε στο Word από την τελευταία φορά γίνεται το πρότυπο του γραφείου."""
        saved = []
        for path, (slot, known) in list(self._tpl_editing.items()):
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue                            # κλειδωμένο/στη μέση αποθήκευσης — θα ξαναδοκιμαστεί στην επόμενη αλλαγή
            if not data or data == known:
                continue
            try:
                templating.save_template(self.conn, slot, Path(path).name, data)
            except ValueError:
                continue                            # μισογραμμένο αρχείο
            self._tpl_editing[path] = (slot, data)
            saved.append(templating.slot_label(slot))
        if saved:
            self.reload_templates()
            toast(self.main, "Αποθηκεύτηκε το πρότυπο: " + ", ".join(saved), "ok")

    def _preview_template(self) -> None:
        """Γεμίζει το επιλεγμένο πρότυπο με τα στοιχεία του επιλεγμένου πελάτη και το ανοίγει — για έλεγχο των πεδίων."""
        slot = self._selected_slot()
        if not slot:
            return
        self._sync_edited_templates()
        afm = self._selected_afm() or next((r["afm"] for r in getattr(self, "_rows", []) if r["is_client"]), None)
        if not afm:
            toast(self.main, "Χρειάζεται τουλάχιστον ένας πελάτης για τη δοκιμή.", "warn")
            return
        data, _name, _custom = templating.get_template(self.conn, slot)
        out, unknown = templating.render(data, templating.build_fields(self.conn, afm))
        path = self._template_work_dir() / f"Δοκιμή - {afm}.docx"
        try:
            path.write_bytes(out)
        except OSError:
            toast(self.main, "Κλείστε πρώτα την προηγούμενη δοκιμή στο Word.", "warn")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        if unknown:
            toast(self.main, "Πεδία χωρίς τιμή: " + ", ".join(sorted(unknown)[:8]), "warn", ms=9000)

    def _download_template(self) -> None:
        slot = self._selected_slot()
        if not slot:
            return
        data, name, _custom = templating.get_template(self.conn, slot)
        path, _ = QFileDialog.getSaveFileName(self, "Λήψη προτύπου", name, "Word (*.docx)")
        if path:
            Path(path).write_bytes(data)
            toast(self.main, "Το πρότυπο αποθηκεύτηκε — ανοίξτε το στο Word για να το προσαρμόσετε.", "ok")

    def _upload_template(self) -> None:
        slot = self._selected_slot()
        if not slot:
            return
        path, _ = QFileDialog.getOpenFileName(self, f"Πρότυπο: {templating.slot_label(slot)}", "", "Word (*.docx)")
        if not path:
            return
        try:
            data = Path(path).read_bytes()
            templating.save_template(self.conn, slot, Path(path).name, data)
            _out, unknown = templating.render(data, {})
        except (OSError, ValueError) as exc:
            toast(self.main, str(exc), "danger")
            return
        known = {k for k in unknown if k in templating.TAXIS_ALIASES}
        foreign = sorted(set(unknown) - known - {k for k, _ in templating.FIELD_HELP})
        self.reload_templates()
        toast(self.main, "Το πρότυπο αποθηκεύτηκε." + (f" Πεδία που δεν αναγνωρίζονται: {', '.join(foreign[:8])}" if foreign else ""),
              "warn" if foreign else "ok", ms=9000 if foreign else None)

    def _reset_template(self) -> None:
        slot = self._selected_slot()
        if not slot:
            return
        if QMessageBox.question(self, "Επαναφορά", f"Επαναφορά του «{templating.slot_label(slot)}» στο προεπιλεγμένο πρότυπο;") \
                != QMessageBox.StandardButton.Yes:
            return
        templating.reset_template(self.conn, slot)
        self.reload_templates()

    # ================================================================== Υποθέσεις
    def _cases_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        info = QLabel("Δύο ξεχωριστές διαδικασίες: (α) ύποπτη συναλλαγή/δραστηριότητα ΠΕΛΑΤΗ → εσωτερική αναφορά στον "
                      "υπεύθυνο συμμόρφωσης και γραπτή απόφαση αν θα αναφερθεί στην Αρχή Καταπολέμησης της Νομιμοποίησης "
                      "Εσόδων (άρθρο 22)· (β) καταγγελία παράβασης από το ΙΔΙΟ το γραφείο (ν. 4990/2022).")
        info.setObjectName("muted")
        info.setWordWrap(True)
        box.addWidget(info)
        top = QHBoxLayout()
        new_s = QPushButton("+ Ύποπτη συναλλαγή πελάτη")
        new_s.setObjectName("primary")
        new_s.clicked.connect(lambda: self._open_case(kind="suspicion"))
        top.addWidget(new_s)
        new_w = QPushButton("+ Εσωτερική καταγγελία")
        new_w.clicked.connect(lambda: self._open_case(kind="whistle"))
        top.addWidget(new_w)
        top.addStretch()
        self.case_filter = QComboBox()
        self.case_filter.addItem("Όλες οι υποθέσεις", "")
        for key, label in content.CASE_KINDS:
            self.case_filter.addItem(label.split(" (")[0], key)
        self.case_filter.currentIndexChanged.connect(self.reload_cases)
        top.addWidget(self.case_filter)
        case_pdf = QPushButton(icon("pdf", CURRENT.txt, 16), "  Φάκελος υπόθεσης (PDF)")
        case_pdf.clicked.connect(self._case_pdf)
        top.addWidget(case_pdf)
        box.addLayout(top)
        self.case_table = QTableWidget(0, 8)
        self.case_table.setHorizontalHeaderLabels(["#", "Είδος", "Παραλαβή", "Πελάτης", "Περιγραφή", "Κατάσταση",
                                                   "Απόφαση / αποτέλεσμα", "Εκκρεμότητα"])
        self.case_table.verticalHeader().setVisible(False)
        self.case_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.case_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.case_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.case_table.setAlternatingRowColors(True)
        for col, width in enumerate((40, 150, 85, 190, 260, 120, 200)):
            self.case_table.setColumnWidth(col, width)
        self.case_table.horizontalHeader().setStretchLastSection(True)
        self.case_table.doubleClicked.connect(lambda *_: self._open_case())
        box.addWidget(self.case_table, 1)
        return page

    def reload_cases(self) -> None:
        rows = cases_mod.list_cases(self.conn, self.case_filter.currentData() or "")
        attention = {c["id"]: c["reason"] for c in cases_mod.attention(self.conn)}
        self.case_table.setRowCount(len(rows))
        for i, c in enumerate(rows):
            cells = [str(c["id"]), "Ύποπτη συναλλαγή" if c["kind"] == "suspicion" else "Καταγγελία",
                     _gr(c["received_on"]), f"{c['client_name']} ({c['afm']})" if c["afm"] else "—",
                     c["description"][:120], content.CASE_STATUS_LABEL.get(c["status"], ""), cases_mod.outcome_label(c),
                     attention.get(c["id"], "")]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, c["id"])
                if col == 7 and text:
                    item.setForeground(QColor(CURRENT.bad))
                if col == 1 and c["kind"] == "suspicion":
                    item.setForeground(QColor(CURRENT.warn))
                item.setToolTip(text)
                self.case_table.setItem(i, col, item)

    def _selected_case(self) -> Optional[int]:
        row = self.case_table.currentRow()
        item = self.case_table.item(row, 0) if row >= 0 else None
        return int(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def _open_case(self, kind: str = "") -> None:
        case_id = None if kind else self._selected_case()
        if not kind and case_id is None:
            return
        existing = cases_mod.get(self.conn, case_id) if case_id else None
        dlg = AmlCaseDialog(self.conn, self.main, kind=existing["kind"] if existing else kind, case_id=case_id,
                            afm=(self._selected_afm() or "") if kind == "suspicion" else "")
        if dlg.exec() and dlg.saved_id:
            toast(self.main, f"Η υπόθεση #{dlg.saved_id} αποθηκεύτηκε.", "ok")
            self.reload_cases()
            self.main.after_aml_change()

    def _case_pdf(self) -> None:
        case_id = self._selected_case()
        if case_id is None:
            toast(self.main, "Επιλέξτε πρώτα μια υπόθεση.", "warn")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Φάκελος υπόθεσης", f"Ypothesi_{case_id}_{date.today():%Y%m%d}.pdf",
                                              "PDF (*.pdf)")
        if not path:
            return
        try:
            aml_docs.write_pdf(aml_docs.case_file_html(self.conn, case_id), Path(path), f"Υπόθεση #{case_id}")
        except OSError as exc:
            toast(self.main, f"Η δημιουργία του PDF απέτυχε: {exc}", "danger")
            return
        toast(self.main, "Ο φάκελος της υπόθεσης δημιουργήθηκε (εμπιστευτικό).", "ok")

    def _export_summary(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Συγκεντρωτική κατάσταση κατάταξης",
                                              f"Katastasi_DE_{date.today():%Y%m%d}.xlsx", "Excel (*.xlsx)")
        if path:
            try:
                aml_export.summary_sheet(self.conn, Path(path))
                toast(self.main, "Η συγκεντρωτική κατάσταση εξήχθη.", "ok")
            except OSError as exc:
                toast(self.main, f"Η εξαγωγή απέτυχε: {exc}", "danger")

    # ================================================================== Αυτοδιάγνωση
    def _self_check_tab(self) -> QWidget:
        outer = QWidget()
        obox = QVBoxLayout(outer)
        self.q_progress = QLabel("")
        self.q_progress.setStyleSheet("font-weight:700;")
        obox.addWidget(self.q_progress)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        box = QVBoxLayout(body)
        self._q_groups: dict[int, QButtonGroup] = {}
        self._q_fix: dict[int, QLabel] = {}
        group_box: Optional[QVBoxLayout] = None
        current = ""
        for q in content.QUESTIONS:
            if q.group != current:
                current = q.group
                gb = QGroupBox(current)
                group_box = QVBoxLayout(gb)
                box.addWidget(gb)
            row = QWidget()
            rbox = QVBoxLayout(row)
            rbox.setContentsMargins(0, 2, 0, 6)
            text = QLabel(f"<b>{q.n}.</b> {q.text} <span style='color:{CURRENT.muted}'>({q.ref})</span>")
            text.setWordWrap(True)
            rbox.addWidget(text)
            btns = QHBoxLayout()
            grp = QButtonGroup(row)
            for state, label in (("yes", "ΝΑΙ"), ("no", "ΟΧΙ")) + ((("na", "Δεν συντρέχει"),) if q.allow_na else ()):
                rb = QRadioButton(label)
                rb.setProperty("state", state)
                grp.addButton(rb)
                btns.addWidget(rb)
            btns.addStretch()
            rbox.addLayout(btns)
            grp.buttonToggled.connect(lambda btn, checked, n=q.n: checked and self._on_answer(n, btn.property("state")))
            fix = QLabel(f"→ {q.fix}" + (f"  (Βήμα {q.step})" if q.step else ""))
            fix.setWordWrap(True)
            fix.setStyleSheet(f"color:{CURRENT.warn};")
            fix.hide()
            rbox.addWidget(fix)
            self._q_groups[q.n] = grp
            self._q_fix[q.n] = fix
            group_box.addWidget(row)
        box.addStretch()
        scroll.setWidget(body)
        obox.addWidget(scroll, 1)
        return outer

    def _on_answer(self, n: int, state: str) -> None:
        if getattr(self, "_loading", False):
            return
        store.set_office_item(self.conn, f"q{n:02d}", state)
        self._q_fix[n].setVisible(state == "no")
        self._update_q_progress()

    def _update_q_progress(self) -> None:
        items = store.office_items(self.conn)
        answered = [q for q in content.QUESTIONS if items.get(f"q{q.n:02d}", {}).get("state") in ("yes", "no", "na")]
        no = [q for q in answered if items[f"q{q.n:02d}"]["state"] == "no"]
        text = f"{len(answered)} από {len(content.QUESTIONS)} ερωτήματα απαντημένα"
        if no:
            text += f" · {len(no)} με ΟΧΙ χρειάζονται ενέργειες"
        elif len(answered) == len(content.QUESTIONS):
            text += " · όλα καλύπτονται ✓"
        self.q_progress.setText(text)
        self.q_progress.setStyleSheet(f"font-weight:700; color:{CURRENT.warn if no else CURRENT.ok if answered else CURRENT.txt};")

    # ================================================================== Φάκελος συμμόρφωσης
    def _folder_tab(self) -> QWidget:
        outer = QWidget()
        obox = QVBoxLayout(outer)
        self.folder_progress = QLabel("")
        self.folder_progress.setStyleSheet("font-weight:700;")
        obox.addWidget(self.folder_progress)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        box = QHBoxLayout(body)

        steps_col = QVBoxLayout()
        self._step_checks: dict[str, QCheckBox] = {}
        current = ""
        gl: Optional[QVBoxLayout] = None
        for s in content.STEPS:
            if s.stage != current:
                current = s.stage
                gb = QGroupBox(f"Στάδιο: {current}")
                gl = QVBoxLayout(gb)
                steps_col.addWidget(gb)
            chk = QCheckBox(f"{s.n}. {s.title}")
            chk.setToolTip(f"Παραδοτέο: {s.output}" + (f"\nΕρωτήματα ΑΑΔΕ: {s.questions}" if s.questions else ""))
            chk.toggled.connect(lambda on, key=f"step{s.n:02d}": self._on_folder_item(key, on))
            gl.addWidget(chk)
            out = QLabel(s.output + (f"  · ερ. {s.questions}" if s.questions else ""))
            out.setObjectName("muted")
            out.setWordWrap(True)
            out.setContentsMargins(24, 0, 0, 4)
            gl.addWidget(out)
            self._step_checks[f"step{s.n:02d}"] = chk
        steps_col.addStretch()
        box.addLayout(steps_col, 1)

        docs = QGroupBox("Έγγραφα του φακέλου συμμόρφωσης")
        dl = QVBoxLayout(docs)
        self._doc_checks: dict[str, QCheckBox] = {}
        for key, text in content.DOCUMENTS:
            chk = QCheckBox(text)
            chk.toggled.connect(lambda on, k=key: self._on_folder_item(k, on))
            dl.addWidget(chk)
            self._doc_checks[key] = chk
        cycle = QLabel("Ετήσιος κύκλος: κάθε επανεξέταση πελάτη τροφοδοτεί νέα βαθμολόγηση και νέα έκδοση της "
                       "συγκεντρωτικής κατάστασης (άρθρο 35 παρ. 2, άρθρο 13 παρ. 7).")
        cycle.setWordWrap(True)
        cycle.setObjectName("muted")
        dl.addWidget(cycle)
        dl.addStretch()
        box.addWidget(docs, 1)
        scroll.setWidget(body)
        obox.addWidget(scroll, 1)
        return outer

    def _on_folder_item(self, key: str, on: bool) -> None:
        if getattr(self, "_loading", False):
            return
        store.set_office_item(self.conn, key, "done" if on else "")
        self._update_folder_progress()

    def _update_folder_progress(self) -> None:
        steps = sum(1 for c in self._step_checks.values() if c.isChecked())
        docs = sum(1 for c in self._doc_checks.values() if c.isChecked())
        self.folder_progress.setText(f"{steps} από {len(self._step_checks)} βήματα · {docs} από "
                                     f"{len(self._doc_checks)} έγγραφα του φακέλου")

    def _load_office_items(self) -> None:
        items = store.office_items(self.conn)
        self._loading = True
        try:
            for n, grp in self._q_groups.items():
                state = items.get(f"q{n:02d}", {}).get("state", "")
                grp.setExclusive(False)
                for b in grp.buttons():
                    b.setChecked(b.property("state") == state)
                grp.setExclusive(True)
                self._q_fix[n].setVisible(state == "no")
            for key, chk in {**self._step_checks, **self._doc_checks}.items():
                chk.setChecked(items.get(key, {}).get("state") == "done")
        finally:
            self._loading = False
        self._update_q_progress()
        self._update_folder_progress()

    # ================================================================== Μητρώα
    def _registers_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        form_box = QGroupBox("Νέα εγγραφή")
        form = QGridLayout(form_box)
        self.reg_kind = QComboBox()
        for key, label in content.REGISTER_KINDS:
            self.reg_kind.addItem(label, key)
        self.reg_date = GrDateEdit(date.today())
        self.reg_title = QLineEdit()
        self.reg_title.setPlaceholderText("Τίτλος (π.χ. «Σεμινάριο AML», «Ασυνήθιστη κατάθεση μετρητών»)")
        self.reg_afm = QLineEdit()
        self.reg_afm.setPlaceholderText("ΑΦΜ πελάτη (προαιρετικό)")
        self.reg_afm.setMaxLength(9)
        self.reg_details = QLineEdit()
        self.reg_details.setPlaceholderText("Λεπτομέρειες (συμμετέχοντες, ώρες, περιστατικό…)")
        self.reg_outcome = QLineEdit()
        self.reg_outcome.setPlaceholderText("Έκβαση/απόφαση — π.χ. «αναφέρθηκε στην Αρχή» ή «δεν αναφέρθηκε, γιατί…»")
        form.addWidget(QLabel("Είδος"), 0, 0)
        form.addWidget(self.reg_kind, 0, 1)
        form.addWidget(QLabel("Ημερομηνία"), 0, 2)
        form.addWidget(self.reg_date, 0, 3)
        form.addWidget(self.reg_afm, 0, 4)
        form.addWidget(self.reg_title, 1, 0, 1, 5)
        form.addWidget(self.reg_details, 2, 0, 1, 5)
        form.addWidget(self.reg_outcome, 3, 0, 1, 4)
        add = QPushButton("Καταχώριση")
        add.setObjectName("primary")
        add.clicked.connect(self._add_register)
        form.addWidget(add, 3, 4)
        box.addWidget(form_box)
        tip = QLabel("Κρατήστε ΚΑΙ τα περιστατικά που εξετάσατε αλλά δεν αναφέρατε, με αιτιολογία (άρθρο 30 παρ. 1γ). "
                     "Ποτέ μην ενημερώνετε τον πελάτη για αναφορά ή έρευνα (άρθρο 27).")
        tip.setObjectName("muted")
        tip.setWordWrap(True)
        box.addWidget(tip)

        top = QHBoxLayout()
        self.reg_filter = QComboBox()
        self.reg_filter.addItem("Όλα τα μητρώα", "")
        for key, label in content.REGISTER_KINDS:
            self.reg_filter.addItem(label, key)
        self.reg_filter.currentIndexChanged.connect(self.reload_registers)
        top.addWidget(self.reg_filter)
        top.addStretch()
        delete = QPushButton(icon("delete", CURRENT.bad, 16), "  Διαγραφή επιλεγμένης")
        delete.setObjectName("danger")
        delete.clicked.connect(self._delete_register)
        top.addWidget(delete)
        box.addLayout(top)
        self.reg_table = QTableWidget(0, len(_REG_COLS))
        self.reg_table.setHorizontalHeaderLabels(_REG_COLS)
        self.reg_table.verticalHeader().setVisible(False)
        self.reg_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.reg_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.reg_table.setAlternatingRowColors(True)
        self.reg_table.horizontalHeader().setStretchLastSection(True)
        for col, width in enumerate((90, 190, 260, 90, 260)):
            self.reg_table.setColumnWidth(col, width)
        box.addWidget(self.reg_table, 1)
        return page

    def reload_registers(self) -> None:
        rows = store.list_register(self.conn, self.reg_filter.currentData() or "")
        self.reg_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            for col, text in enumerate((_gr(r["event_date"]), content.REGISTER_LABEL.get(r["kind"], r["kind"]), r["title"],
                                        r["afm"], r["details"], r["outcome"])):
                item = QTableWidgetItem(text)
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, r["id"])
                item.setToolTip(text)
                self.reg_table.setItem(i, col, item)

    def _add_register(self) -> None:
        title = self.reg_title.text().strip()
        if not title:
            toast(self.main, "Γράψτε έναν τίτλο για την εγγραφή.", "warn")
            return
        store.add_register(self.conn, self.reg_kind.currentData(), self.reg_date.date().toPython().isoformat(), title,
                           afm=self.reg_afm.text().strip(), details=self.reg_details.text(),
                           outcome=self.reg_outcome.text())
        for field in (self.reg_title, self.reg_afm, self.reg_details, self.reg_outcome):
            field.clear()
        self.reload_registers()
        toast(self.main, "Η εγγραφή καταχωρίστηκε.", "ok")

    def _delete_register(self) -> None:
        row = self.reg_table.currentRow()
        if row < 0:
            return
        entry_id = self.reg_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        if QMessageBox.question(self, "Διαγραφή εγγραφής",
                                "Διαγραφή της εγγραφής από το μητρώο; Τα μητρώα είναι αποδεικτικά για τον έλεγχο — "
                                "διαγράψτε μόνο λανθασμένες καταχωρίσεις.") != QMessageBox.StandardButton.Yes:
            return
        store.delete_register(self.conn, int(entry_id))
        self.reload_registers()

    # ================================================================== Μεθοδολογία & ρυθμίσεις
    def _method_tab(self) -> QWidget:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        body = QWidget()
        box = QVBoxLayout(body)

        cfg = QGroupBox("Ρυθμίσεις γραφείου — μία μεθοδολογία για ΟΛΟΥΣ τους πελάτες")
        form = QFormLayout(cfg)
        self.set_model = QComboBox()
        self.set_model.addItem("Μοντέλο Α — αφετηρία 0, Π.Ι αφαιρεί / Π.ΙΙ προσθέτει (ΥΨΗΛΟΣ ≥ +6)", "A")
        self.set_model.addItem("Μοντέλο Β — 4 άξονες × 0–25 = 0–100 (ΥΨΗΛΟΣ ≥ 46)", "B")
        form.addRow("Μοντέλο βαθμολόγησης", self.set_model)
        self.set_review: dict[str, QSpinBox] = {}
        for cat in model.CATEGORIES:
            spin = QSpinBox()
            spin.setRange(1, 60)
            spin.setSuffix(" μήνες")
            self.set_review[cat] = spin
            form.addRow(f"Επανεξέταση — {model.CATEGORY_LABEL[cat].lower()} κίνδυνος", spin)
        self.set_officer = QLineEdit()
        self.set_officer.setPlaceholderText("Ονοματεπώνυμο υπευθύνου συμμόρφωσης (άρθρο 35)")
        form.addRow("Υπεύθυνος συμμόρφωσης", self.set_officer)
        self.set_office_info = QPlainTextEdit()
        self.set_office_info.setPlaceholderText("Έως 4 γραμμές για την κεφαλίδα των εγγράφων: επωνυμία · διεύθυνση · "
                                                "δραστηριότητα · ΑΦΜ/ΔΟΥ/τηλέφωνο")
        self.set_office_info.setFixedHeight(80)
        form.addRow("Στοιχεία γραφείου", self.set_office_info)
        self.set_office_city = QLineEdit()
        self.set_office_city.setPlaceholderText("π.χ. Αθήνα")
        form.addRow("Τόπος υπογραφής", self.set_office_city)
        save = QPushButton("Αποθήκευση ρυθμίσεων")
        save.setObjectName("primary")
        save.clicked.connect(self._save_settings)
        form.addRow("", save)
        note = QLabel("Τα διαστήματα επανεξέτασης δεν προβλέπονται από τον νόμο· οι προεπιλογές (24/12/12 μήνες) "
                      "ακολουθούν το ενημερωτικό υλικό της ΑΑΔΕ. Κάθε αλλαγή μεθοδολογίας τεκμηριώνεται γραπτώς, "
                      "εγκρίνεται και καταγράφεται στο μητρώο «Εσωτερικός έλεγχος / επανεξέταση πολιτικής».")
        note.setObjectName("muted")
        note.setWordWrap(True)
        form.addRow(note)
        box.addWidget(cfg)

        law = QGroupBox("Νομικό πλαίσιο")
        lbox = QVBoxLayout(law)
        for title, text in content.LEGAL_FRAMEWORK:
            lbl = QLabel(f"<b>{title}.</b> {text}")
            lbl.setWordWrap(True)
            lbox.addWidget(lbl)
        box.addWidget(law)

        scale = QGroupBox("Κλίμακα κατάταξης και μέτρα")
        sbox = QVBoxLayout(scale)
        for cat, rule in ((model.LOW, "Α: ≤ 0 · Β: ≤ 15 — ΚΑΙ ≥1 τεκμηριωμένος παράγοντας Π.Ι, κανένας Π.ΙΙ"),
                          (model.MEDIUM, "Α: +1 έως +5 · Β: 16–45 — ή χαμηλό άθροισμα χωρίς τις προϋποθέσεις του χαμηλού"),
                          (model.HIGH, "Α: ≥ +6 · Β: ≥ 46 — ή έστω μία περίπτωση «ΥΠΕΡΙΣΧΥΕΙ»")):
            lbl = QLabel(f"<b style='color:{category_colour(cat)}'>{model.CATEGORY_LABEL[cat]}</b> ({rule}) → "
                         f"{model.DD_LABEL[cat]}.<br><span style='color:{CURRENT.muted}'>{content.MEASURES[cat]}</span>")
            lbl.setWordWrap(True)
            sbox.addWidget(lbl)
        trig = QLabel("<b>Έκτακτη επανεξέταση όταν:</b> " + "· ".join(content.EXTRAORDINARY_TRIGGERS) + ".")
        trig.setWordWrap(True)
        sbox.addWidget(trig)
        box.addWidget(scale)

        factors = QGroupBox("Παράγοντες του μοντέλου (βαθμοί = επιλογή οργάνωσης, όχι του νόμου)")
        fbox = QVBoxLayout(factors)
        rows = []
        for axis, label in model.AXES:
            rows.append(f"<tr><td colspan=4><b>{label}</b></td></tr>")
            for f in model.FACTORS:
                if f.axis == axis:
                    rows.append(f"<tr><td>{f.text}</td><td>{f.ref}</td><td align=right>{f.a:+d}</td>"
                                f"<td align=right>{'—' if f.b is None else f.b}</td></tr>")
        rows.append("<tr><td colspan=4><b>Περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ»</b></td></tr>")
        for o in model.OVERRIDES:
            rows.append(f"<tr><td>{o.text}<br><i>{o.basis}</i></td><td>{o.ref}</td><td colspan=2>ΥΨΗΛΟΣ</td></tr>")
        table = QLabel("<table cellspacing=0 cellpadding=3 width='100%'><tr><th align=left>Παράγοντας</th>"
                       "<th align=left>Διάταξη</th><th>Α</th><th>Β</th></tr>" + "".join(rows) + "</table>")
        table.setWordWrap(True)
        table.setTextFormat(Qt.TextFormat.RichText)
        fbox.addWidget(table)
        box.addWidget(factors)
        box.addStretch()
        scroll.setWidget(body)
        return scroll

    def _load_settings(self) -> None:
        self.set_model.setCurrentIndex(max(0, self.set_model.findData(store.office_model(self.conn))))
        for cat, spin in self.set_review.items():
            spin.setValue(store.review_months(self.conn, cat))
        self.set_officer.setText(settings_store.get(self.conn, "aml_officer"))
        self.set_office_info.setPlainText(settings_store.get(self.conn, "aml_office_info"))
        self.set_office_city.setText(settings_store.get(self.conn, "aml_office_city"))

    def _save_settings(self) -> None:
        old_model = store.office_model(self.conn)
        settings_store.set_value(self.conn, "aml_model", self.set_model.currentData())
        for cat, spin in self.set_review.items():
            settings_store.set_value(self.conn, f"aml_review_{cat}", str(spin.value()))
        settings_store.set_value(self.conn, "aml_officer", self.set_officer.text().strip())
        settings_store.set_value(self.conn, "aml_office_info",
                                 "\n".join(ln.strip() for ln in self.set_office_info.toPlainText().splitlines() if ln.strip())[:600])
        settings_store.set_value(self.conn, "aml_office_city", self.set_office_city.text().strip())
        if old_model != self.set_model.currentData():
            store.add_register(self.conn, "audit", date.today().isoformat(),
                               f"Αλλαγή μοντέλου βαθμολόγησης {old_model} → {self.set_model.currentData()}",
                               details="Αυτόματη καταγραφή από την εφαρμογή· συμπληρώστε αιτιολογία/έγκριση στη γραπτή πολιτική.")
            self.reload_registers()
        toast(self.main, "Οι ρυθμίσεις δέουσας επιμέλειας αποθηκεύτηκαν. Ισχύουν για τις ΝΕΕΣ αξιολογήσεις.", "ok")
