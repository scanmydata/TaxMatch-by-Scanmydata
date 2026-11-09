"""«Έλεγχοι & ενημέρωση»: ΕΝΑ κουμπί στους Πελάτες αντί για πέντε (ρητό αίτημα χρήστη 2026-10-23 — εξοικονόμηση χώρου)·
ο χρήστης τσεκάρει ποιους ελέγχους θέλει. Η «Ανανέωση στοιχείων» προτείνεται (τσεκαρισμένη) όταν σε κάποιον από τους
πελάτες λείπουν βασικά στοιχεία που χρειάζεται η εφαρμογή για το είδος του (`service.missing_basics`)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from .table_kit import ui_prefs
from .theme import CURRENT

#: (κλειδί, τίτλος, επεξήγηση) — με τη σειρά που εκτελούνται
CHECKS = [
    ("lookup", "Ανανέωση στοιχείων", "Μητρώο ΑΑΔΕ → ΓΕΜΗ → VIES: ΚΑΔ, βιβλία, ΦΠΑ, επικοινωνία, μέλη εταιρειών"),
    ("submissions", "Έλεγχος υποβολών", "Υποβλήθηκαν οι δηλώσεις της χρήσης (Φ2 με ποσά, Φ4/Φ5, ΦΜΥ, ΑΠΔ…); Μόνο ανάγνωση"),
    ("mydata", "Λήψη myDATA", "Παραστατικά των 3 τελευταίων μηνών (χωρίς PDF): παρακρατήσεις, VIES, Intrastat, τέλη"),
    ("debts", "Λήψη οφειλών", "ΑΑΔΕ (εκτός/σε ρύθμιση), ΚΕΑΟ ανά φορέα, εισφορές e-ΕΦΚΑ — μόνο στοιχεία"),
    ("requests", "Αιτήματα ΑΑΔΕ", "«Τα Αιτήματά μου»: τι απαντήθηκε, τι περιμένει ενέργεια του πελάτη"),
]


class ChecksDialog(QDialog):
    """`chosen` μετά το exec(): τα κλειδιά που τσεκαρίστηκαν (με τη σειρά του `CHECKS`)."""

    def __init__(self, parent, scope_text: str, incomplete: dict[str, list[str]], names: dict[str, str],
                 missing_ika: Optional[list[str]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Έλεγχοι & ενημέρωση")
        self.setMinimumWidth(560)
        self.chosen: list[str] = []
        self._prefs = ui_prefs()
        root = QVBoxLayout(self)
        head = QLabel(f"<b>{scope_text}</b> — επιλέξτε τι θα γίνει (σε σειρά, στο παρασκήνιο, μόνο ανάγνωση):")
        head.setTextFormat(Qt.TextFormat.RichText)
        head.setWordWrap(True)
        root.addWidget(head)
        self.boxes: dict[str, QCheckBox] = {}
        for key, title, tip in CHECKS:
            box = QCheckBox(title)
            box.setToolTip(tip)
            remembered = self._prefs.value(f"checks/{key}", "1" if key == "submissions" else "0")
            box.setChecked(str(remembered) == "1")
            root.addWidget(box)
            note = QLabel(tip)
            note.setObjectName("muted")
            note.setWordWrap(True)
            note.setContentsMargins(26, 0, 0, 4)
            root.addWidget(note)
            self.boxes[key] = box
            if key == "submissions":
                # PDF των υποβληθέντων δηλώσεων (ρητό αίτημα 2026-10-27) — επιλογή, γιατί κοστίζει χρόνο
                self.pdf_box = QCheckBox("και λήψη PDF των υποβληθεισών δηλώσεων (Φ2, Φ4/Φ5, ΦΜΥ, Intrastat…)")
                self.pdf_box.setToolTip("Αποθηκεύονται στον φάκελο δεδομένων → submissions\\<ΑΦΜ>\\<χρήση>· ανοίγουν από "
                                        "την καρτέλα πελάτη → Προθεσμίες & υποβολές. Μία φορά ανά δήλωση.")
                self.pdf_box.setChecked(str(self._prefs.value("checks/submissions_pdf", "0")) == "1")
                self.pdf_box.setContentsMargins(26, 0, 0, 4)
                self.pdf_box.setStyleSheet("margin-left:26px;")
                box.toggled.connect(self.pdf_box.setEnabled)
                self.pdf_box.setEnabled(box.isChecked())
                root.addWidget(self.pdf_box)

                # Prompt / υπόμνηση για κωδικούς ΙΚΑ εργοδότη στον έλεγχο ΑΠΔ e-ΕΦΚΑ / ΤΕΚΑ
                self.ika_note = QLabel("💡 <b>ΑΠΔ (e-ΕΦΚΑ & ΤΕΚΑ):</b> Απαιτούνται κωδικοί ΙΚΑ εργοδότη (στην καρτέλα e-ΕΦΚΑ του πελάτη). "
                                       "Σε πελάτες χωρίς κωδικούς ΙΚΑ ο έλεγχος ΑΠΔ παραλείπεται για αποφυγή άσκοπης αναμονής.")
                self.ika_note.setWordWrap(True)
                self.ika_note.setContentsMargins(26, 0, 0, 6)
                self.ika_note.setStyleSheet(f"margin-left:26px; color:{CURRENT.accent_deep}; font-size:11.5px;")
                box.toggled.connect(self.ika_note.setVisible)
                self.ika_note.setVisible(box.isChecked())
                root.addWidget(self.ika_note)
            elif key == "mydata":
                # Διάστημα λήψης myDATA
                range_layout = QHBoxLayout()
                range_layout.setContentsMargins(26, 0, 0, 4)
                range_lbl = QLabel("Διάστημα:")
                range_lbl.setStyleSheet(f"color:{CURRENT.muted}; font-size:11.5px;")
                self.mydata_range_combo = QComboBox()
                ranges = [
                    ("Τελευταίοι 3 μήνες (προεπιλογή)", "3m"),
                    ("Τρέχον έτος", "current_year"),
                    ("Προηγούμενο έτος", "prev_year"),
                    ("Τελευταίοι 6 μήνες", "6m"),
                    ("Τελευταίοι 12 μήνες", "12m"),
                    ("Όλα (24 μήνες)", "24m"),
                ]
                for r_label, r_code in ranges:
                    self.mydata_range_combo.addItem(r_label, r_code)
                rem_range = str(self._prefs.value("checks/mydata_range", "3m"))
                idx = self.mydata_range_combo.findData(rem_range)
                if idx >= 0:
                    self.mydata_range_combo.setCurrentIndex(idx)
                range_layout.addWidget(range_lbl)
                range_layout.addWidget(self.mydata_range_combo)
                range_layout.addStretch()
                root.addLayout(range_layout)

                # PDF παραστατικών εσόδων (εκδοθέντα)
                self.mydata_pdf_income_box = QCheckBox("και λήψη PDF παραστατικών εσόδων (εκδοθέντα)")
                self.mydata_pdf_income_box.setToolTip("Κατεβάζει τα PDF των εκδοθέντων τιμολογίων στον φάκελο invoices")
                self.mydata_pdf_income_box.setChecked(str(self._prefs.value("checks/mydata_pdf_income", "0")) == "1")
                self.mydata_pdf_income_box.setContentsMargins(26, 0, 0, 2)
                self.mydata_pdf_income_box.setStyleSheet("margin-left:26px;")
                root.addWidget(self.mydata_pdf_income_box)

                # PDF παραστατικών εξόδων (ληφθέντα)
                self.mydata_pdf_expenses_box = QCheckBox("και λήψη PDF παραστατικών εξόδων (ληφθέντα)")
                self.mydata_pdf_expenses_box.setToolTip("Κατεβάζει τα PDF των ληφθέντων τιμολογίων στον φάκελο invoices")
                self.mydata_pdf_expenses_box.setChecked(str(self._prefs.value("checks/mydata_pdf_expenses", "0")) == "1")
                self.mydata_pdf_expenses_box.setContentsMargins(26, 0, 0, 2)
                self.mydata_pdf_expenses_box.setStyleSheet("margin-left:26px;")
                root.addWidget(self.mydata_pdf_expenses_box)

                # Μόνο αχαρακτήριστα (τα υπόλοιπα σε αναμονή)
                self.mydata_unclassified_box = QCheckBox("λήψη μόνο των αχαρακτήριστων εξόδων/εσόδων (τα υπόλοιπα σε αναμονή)")
                self.mydata_unclassified_box.setToolTip("Κατεβάζει PDF ΜΟΝΟ για τα αχαρακτήριστα παραστατικά (έξοδα/έσοδα) — τα υπόλοιπα παραμένουν σε αναμονή για πολύ πιο γρήγορη διαδικασία")
                self.mydata_unclassified_box.setChecked(str(self._prefs.value("checks/mydata_unclassified", "0")) == "1")
                self.mydata_unclassified_box.setContentsMargins(26, 0, 0, 4)
                self.mydata_unclassified_box.setStyleSheet("margin-left:26px;")
                root.addWidget(self.mydata_unclassified_box)

                def sync_mydata(on: bool) -> None:
                    range_lbl.setEnabled(on)
                    self.mydata_range_combo.setEnabled(on)
                    self.mydata_pdf_income_box.setEnabled(on)
                    self.mydata_pdf_expenses_box.setEnabled(on)
                    self.mydata_unclassified_box.setEnabled(on)

                box.toggled.connect(sync_mydata)
                sync_mydata(box.isChecked())
        if incomplete:
            # ελλιπή βασικά στοιχεία → προτείνεται ανανέωση, με το ποιοι και τι λείπει
            self.boxes["lookup"].setChecked(True)
            sample = "; ".join(f"{names.get(a, a)}: {', '.join(m)}" for a, m in list(incomplete.items())[:4])
            more = len(incomplete) - 4
            warn = QLabel(f"⚠ {len(incomplete)} {'πελάτης δεν έχει' if len(incomplete) == 1 else 'πελάτες δεν έχουν'} "
                          f"όλα τα βασικά στοιχεία — προτείνεται «Ανανέωση στοιχείων». {sample}"
                          + (f" και άλλοι {more}." if more > 0 else "."))
            warn.setWordWrap(True)
            warn.setStyleSheet(f"color:{CURRENT.warn};")
            root.addWidget(warn)
        if missing_ika:
            sample_ika = "; ".join(names.get(a, a) for a in missing_ika[:4])
            more_ika = len(missing_ika) - 4
            warn_ika = QLabel(f"ℹ {len(missing_ika)} {'εργοδότης δεν έχει' if len(missing_ika) == 1 else 'εργοδότες δεν έχουν'} "
                              f"κωδικούς ΙΚΑ ({sample_ika}{f' και άλλοι {more_ika}' if more_ika > 0 else ''}) — "
                              f"για τον έλεγχο ΑΠΔ e-ΕΦΚΑ/ΤΕΚΑ πρέπει να υπάρχουν κωδικοί ΙΚΑ.")
            warn_ika.setWordWrap(True)
            warn_ika.setStyleSheet(f"color:{CURRENT.accent_deep};")
            root.addWidget(warn_ika)
        row = QHBoxLayout()
        row.addStretch()
        run = QPushButton("Εκτέλεση")
        run.setObjectName("primary")
        run.setDefault(True)
        run.clicked.connect(self._run)
        cancel = QPushButton("Άκυρο")
        cancel.clicked.connect(self.reject)
        row.addWidget(run)
        row.addWidget(cancel)
        root.addLayout(row)
        for box in self.boxes.values():
            box.toggled.connect(lambda _on: run.setEnabled(any(b.isChecked() for b in self.boxes.values())))
        run.setEnabled(any(b.isChecked() for b in self.boxes.values()))

    @property
    def want_pdf(self) -> bool:
        return self.pdf_box.isChecked() and self.boxes["submissions"].isChecked()

    @property
    def mydata_range(self) -> str:
        if hasattr(self, "mydata_range_combo"):
            return self.mydata_range_combo.currentData() or "3m"
        return "3m"

    @property
    def mydata_unclassified_only(self) -> bool:
        return bool(self.boxes.get("mydata") and self.boxes["mydata"].isChecked() and hasattr(self, "mydata_unclassified_box") and self.mydata_unclassified_box.isChecked())

    @property
    def mydata_pdf_directions(self) -> list[str]:
        dirs: list[str] = []
        if self.boxes.get("mydata") and self.boxes["mydata"].isChecked():
            if hasattr(self, "mydata_pdf_income_box") and self.mydata_pdf_income_box.isChecked():
                dirs.append("outgoing")
            if hasattr(self, "mydata_pdf_expenses_box") and self.mydata_pdf_expenses_box.isChecked():
                dirs.append("incoming")
        return dirs

    def _run(self) -> None:
        self.chosen = [k for k, _t, _tip in CHECKS if self.boxes[k].isChecked()]
        self._prefs.setValue("checks/submissions_pdf", "1" if self.pdf_box.isChecked() else "0")
        if hasattr(self, "mydata_range_combo"):
            self._prefs.setValue("checks/mydata_range", self.mydata_range_combo.currentData() or "3m")
        if hasattr(self, "mydata_pdf_income_box"):
            self._prefs.setValue("checks/mydata_pdf_income", "1" if self.mydata_pdf_income_box.isChecked() else "0")
        if hasattr(self, "mydata_pdf_expenses_box"):
            self._prefs.setValue("checks/mydata_pdf_expenses", "1" if self.mydata_pdf_expenses_box.isChecked() else "0")
        if hasattr(self, "mydata_unclassified_box"):
            self._prefs.setValue("checks/mydata_unclassified", "1" if self.mydata_unclassified_box.isChecked() else "0")
        for key, box in self.boxes.items():
            if key != "lookup":                          # η ανανέωση προτείνεται κάθε φορά από τα ελλιπή στοιχεία
                self._prefs.setValue(f"checks/{key}", "1" if box.isChecked() else "0")
        self.accept()
