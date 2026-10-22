"""Από πού βγαίνει το Excel με τους πελάτες/κωδικούς — το «i» δίπλα στο «Εισαγωγή από Excel…».

Ίδιο παράθυρο με το `excel_help.py` του timologio downloader (ρητό αίτημα χρήστη, 2026-10-22): ο λογιστής δεν
πληκτρολογεί 160 κωδικούς, τους εξάγει από το πρόγραμμα που ήδη χρησιμοποιεί — αλλά οι διαδρομές δεν είναι
προφανείς, οπότε ζουν δίπλα στο ίδιο το κουμπί. Οι δύο εξαγωγές αντιστοιχούν στις δύο μορφές που αναγνωρίζει το
`business_profiles.import_excel` (μπλοκ «Κωδικοί Υπηρεσιών μέσω Internet» / πλατύς πίνακας «Κωδικοί Υπόχρεων»).
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..business_profiles import import_excel
from .icons import icon
from .theme import CURRENT
from .toast import toast


@dataclass(frozen=True)
class Source:
    program: str
    steps: list[str]
    produces: str
    url: str


#: Οι πηγές, όπως τις τεκμηριώνει η ίδια η Epsilon Net.
SOURCES: list[Source] = [
    Source(
        program="Hyper / Extra Λογιστική",
        steps=[
            "Παράμετροι → Εταιρεία → <b>Εκτύπωση Κωδικών Υπηρεσιών μέσω Internet</b>",
            "Η εκτύπωση είναι διεταιρική: περιλαμβάνει τους κωδικούς σύνδεσης για κάθε εταιρεία.",
            "Στην προεπισκόπηση, πατήστε <b>Εξαγωγή</b> και επιλέξτε <b>Excel</b>· αποθηκεύστε το αρχείο .xlsx.",
        ],
        produces="«Κωδικοί Υπηρεσιών μέσω Internet» (μπλοκ ανά υπόχρεο)",
        url="https://kb.epsilonnet.gr/ld/dietairiki-ektyposi-kodikon-syndesis/",
    ),
    Source(
        program="TaxSystem",
        steps=[
            "Κεντρικό Μενού → Εκτυπώσεις → <b>Κωδικοί e-Υπηρεσιών</b>",
            "Πατήστε <b>Επιλογή Όλων</b>",
            "Πατήστε <b>Εξαγωγή σε Excel</b>",
            "Δώστε όνομα αρχείου και θέση αποθήκευσης και πατήστε <b>Save</b>",
        ],
        produces="«Κωδικοί Υπόχρεων» (πλατύς πίνακας)",
        url="https://kb.epsilonnet.gr/taxsystem/pos-mporo-na-kano-exagogi-kodikon-mazika-gia-toys-ypochreoys/",
    ),
]

#: Τι διαβάζεται από το αρχείο — το ίδιο κείμενο και στο εγχειρίδιο.
READS = ("ΑΦΜ, επωνυμία (και μικρό όνομα για φυσικά πρόσωπα), ΚΑΔ, <b>κωδικοί TAXISnet</b> (ανάκτηση στοιχείων "
         "από το Μητρώο ΑΑΔΕ, έλεγχος υποβολών), <b>κωδικοί Γ.Ε.ΜΗ.</b> (σύνδεση στο businessportal από τη Δέουσα "
         "επιμέλεια) και <b>myDATA</b> (όνομα χρήστη + κλειδί «Api myData» — παραστατικά, PDF, παρακρατήσεις, VIES/Intrastat). Επίσης η στήλη <b>«Μισθοδοσία»</b> (εργοδότης → ΑΠΔ/Ε4) και το αν υπάρχουν στοιχεία "
         "<b>Intrastat</b> — μόνο ως ένδειξη για το Ημερολόγιο, όχι οι κωδικοί.")
IGNORES = ("Οι υπόλοιποι κωδικοί του αρχείου (ΕΦΚΑ/ΙΚΑ, ΟΑΕΔ, Κτηματολόγιο, Intrastat, e-timologio, το «Συνθηματικό myData» "
           "της ιστοσελίδας…) δεν φορτώνονται "
           "ποτέ. Όσοι διαβάζονται αποθηκεύονται κρυπτογραφημένοι και δεν εμφανίζονται ξανά.")


def as_html() -> str:
    """Οι οδηγίες ως HTML — το ίδιο κείμενο μπαίνει και στο εγχειρίδιο."""
    blocks: list[str] = []
    for source in SOURCES:
        steps = "".join(f"<li>{step}</li>" for step in source.steps)
        blocks.append(
            f'<p style="margin-bottom:2px;"><b style="color:{CURRENT.accent};">{source.program}</b></p>'
            f"<ol style='margin-top:0;'>{steps}</ol>"
            f'<p style="color:{CURRENT.muted}; margin-top:0;">Δίνει: {source.produces} &middot; '
            f'<a href="{source.url}">οδηγίες με εικόνες</a></p>'
        )
    return "".join(blocks)


class ExcelHelpDialog(QDialog):
    """Μικρό παράθυρο με τις διαδρομές εξαγωγής ανά πρόγραμμα + κενό πρότυπο για όσους δεν έχουν τέτοιο πρόγραμμα."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Εξαγωγή πελατών και κωδικών σε Excel")
        self.setMinimumWidth(540)
        root = QVBoxLayout(self)
        root.setSpacing(10)

        header = QHBoxLayout()
        mark = QLabel()
        mark.setPixmap(icon("excel", CURRENT.ok, 26).pixmap(QSize(26, 26)))
        header.addWidget(mark)
        title = QLabel("Εξαγωγή κωδικών σε Excel")
        title.setObjectName("h1")
        header.addWidget(title, 1)
        root.addLayout(header)

        intro = QLabel("Η εφαρμογή αναγνωρίζει μόνη της και τις δύο μορφές. Βγάλτε το αρχείο από το πρόγραμμά σας και "
                       "μετά πατήστε «Εισαγωγή από Excel…».")
        intro.setWordWrap(True)
        intro.setObjectName("muted")
        root.addWidget(intro)

        body = QLabel(as_html())
        body.setWordWrap(True)
        body.setTextFormat(Qt.TextFormat.RichText)
        # Χωρίς αυτά τα δύο ο σύνδεσμος δείχνει σαν σύνδεσμος αλλά δεν ανοίγει.
        body.setOpenExternalLinks(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        root.addWidget(body)

        reads = QLabel(f"<b>Τι διαβάζεται:</b> {READS}")
        reads.setWordWrap(True)
        reads.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(reads)
        note = QLabel(IGNORES)
        note.setWordWrap(True)
        note.setStyleSheet(f"color:{CURRENT.muted};")
        root.addWidget(note)

        buttons = QDialogButtonBox()
        template = QPushButton(icon("excel", CURRENT.txt, 16), "  Κενό πρότυπο Excel…")
        template.setToolTip("Για όσους δεν έχουν πρόγραμμα λογιστικής: ΑΦΜ · Επωνυμία · ΚΑΔ · κωδικοί TAXISnet")
        template.clicked.connect(self._save_template)
        buttons.addButton(template, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton("Κλείσιμο", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        root.addWidget(buttons)

    def _save_template(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Κενό πρότυπο Excel", "pelates.xlsx", "Excel (*.xlsx)")
        if not path:
            return
        try:
            with open(path, "wb") as fh:
                fh.write(import_excel.template_xlsx())
        except OSError as exc:
            toast(self, f"Δεν αποθηκεύτηκε: {exc}", "danger")
            return
        toast(self, "Το πρότυπο αποθηκεύτηκε.", "ok")


def show(parent=None) -> None:
    ExcelHelpDialog(parent).exec()


def help_button(parent) -> QPushButton:
    """Το στρογγυλό «i» δίπλα σε κάθε κουμπί «Εισαγωγή από Excel»."""
    btn = QPushButton()
    btn.setIcon(icon("info", CURRENT.accent))
    btn.setFixedSize(30, 30)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setToolTip("Πώς εξάγω πελάτες και κωδικούς σε Excel από Hyper/Extra ή TaxSystem")
    btn.clicked.connect(lambda: show(parent))
    return btn
