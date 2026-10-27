"""«Έλεγχοι & ενημέρωση»: ΕΝΑ κουμπί στους Πελάτες αντί για πέντε (ρητό αίτημα χρήστη 2026-10-23 — εξοικονόμηση χώρου)·
ο χρήστης τσεκάρει ποιους ελέγχους θέλει. Η «Ανανέωση στοιχείων» προτείνεται (τσεκαρισμένη) όταν σε κάποιον από τους
πελάτες λείπουν βασικά στοιχεία που χρειάζεται η εφαρμογή για το είδος του (`service.missing_basics`)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

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

    def __init__(self, parent, scope_text: str, incomplete: dict[str, list[str]], names: dict[str, str]) -> None:
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

    def _run(self) -> None:
        self.chosen = [k for k, _t, _tip in CHECKS if self.boxes[k].isChecked()]
        self._prefs.setValue("checks/submissions_pdf", "1" if self.pdf_box.isChecked() else "0")
        for key, box in self.boxes.items():
            if key != "lookup":                          # η ανανέωση προτείνεται κάθε φορά από τα ελλιπή στοιχεία
                self._prefs.setValue(f"checks/{key}", "1" if box.isChecked() else "0")
        self.accept()
