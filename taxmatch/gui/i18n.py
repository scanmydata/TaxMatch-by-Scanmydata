"""Ελληνικά για ό,τι γράφει το ίδιο το Qt.

Το Qt **δεν** διαθέτει ελληνική μετάφραση: στον κατάλογο `translations` του
PySide6 υπάρχουν 30+ γλώσσες, αλλά `qtbase_el.qm` δεν υπάρχει. Αποτέλεσμα: τα
κουμπιά των τυπικών παραθύρων («Yes», «Cancel», «OK»), το μενού δεξιού κλικ σε
πεδία κειμένου και τα ονόματα μηνών έβγαιναν **αγγλικά** μέσα σε κατά τα άλλα
ελληνική εφαρμογή.

Δεν φτιάχνουμε αρχείο .qm (θα ήθελε lrelease στο build και θα έμπαινε ακόμη ένα
βήμα στη συσκευασία). Αντί γι' αυτό κάνουμε override το `translate()` ενός
QTranslator: το Qt ρωτά από εκεί κάθε δικό του κείμενο, οπότε ένα λεξικό αρκεί.

Τα source strings είναι ακριβώς όπως τα ζητά το Qt — μαζί με τα `&` των
συντομεύσεων, αλλιώς δεν ταιριάζουν.
"""

from __future__ import annotations

from PySide6.QtCore import QLocale, QTranslator

#: Τα contexts του Qt που μας αφορούν. Περιορίζουμε τη μετάφραση σε αυτά ώστε
#: να μην πειράξουμε κατά λάθος κείμενο της ίδιας της εφαρμογής.
_CONTEXTS = frozenset({
    "QPlatformTheme",       # κουμπιά τυπικών παραθύρων (Yes/No/OK/Cancel…)
    "QDialogButtonBox",
    "QMessageBox",
    "QLineEdit",            # μενού δεξιού κλικ σε πεδίο κειμένου
    "QWidgetTextControl",
    "QTextControl",
    "QAbstractSpinBox",
    "QShortcut",
    "QFileDialog",
    "QComboBox",
})

#: source -> ελληνικά. Τα «&» δηλώνουν το πλήκτρο συντόμευσης.
_STRINGS: dict[str, str] = {
    # ---- κουμπιά τυπικών παραθύρων
    "OK": "Εντάξει",
    "&OK": "&Εντάξει",
    "Cancel": "Άκυρο",
    "&Cancel": "Ά&κυρο",
    "&Yes": "&Ναι",
    "Yes": "Ναι",
    "&No": "Ό&χι",
    "No": "Όχι",
    "Yes to &All": "Ναι σε ό&λα",
    "N&o to All": "Όχι σε όλ&α",
    "Save": "Αποθήκευση",
    "&Save": "&Αποθήκευση",
    "Save All": "Αποθήκευση όλων",
    "Open": "Άνοιγμα",
    "&Open": "Ά&νοιγμα",
    "Close": "Κλείσιμο",
    "&Close": "&Κλείσιμο",
    "Discard": "Απόρριψη",
    "Apply": "Εφαρμογή",
    "Reset": "Επαναφορά",
    "Restore Defaults": "Επαναφορά προεπιλογών",
    "Help": "Βοήθεια",
    "Abort": "Ματαίωση",
    "Retry": "Επανάληψη",
    "Ignore": "Παράβλεψη",
    "Don't Save": "Να μην αποθηκευτεί",
    # ---- μενού δεξιού κλικ σε πεδία κειμένου
    "&Undo": "Α&ναίρεση",
    "&Redo": "Επανά&ληψη",
    "Cu&t": "Απο&κοπή",
    "&Copy": "Αν&τιγραφή",
    "&Paste": "Επι&κόλληση",
    "Delete": "Διαγραφή",
    "Select All": "Επιλογή όλων",
    "&Select All": "Επιλογή ό&λων",
    "Copy &Link Location": "Αντιγραφή &διεύθυνσης συνδέσμου",
    # ---- διάφορα
    "&Step up": "Βήμα &πάνω",
    "Step &down": "Βήμα &κάτω",
    "Press": "Πάτημα",
    "Show Menu": "Εμφάνιση μενού",
    "What's This?": "Τι είναι αυτό;",
}


class GreekTranslator(QTranslator):
    """Επιστρέφει ελληνικά για τα ενσωματωμένα κείμενα του Qt."""

    def translate(
        self,
        context: str,
        source: str,
        disambiguation: str | None = None,
        n: int = -1,
    ) -> str | None:
        # ⚠️ `None`, ΟΧΙ `""`. Το PySide μετατρέπει το `""` σε μη-null κενό
        # QString, και το Qt το διαβάζει ως «η μετάφραση ΕΙΝΑΙ το κενό»: κάθε
        # δικό του κείμενο εκτός λεξικού έσβηνε. Έτσι το ζουμ της προεπισκόπησης
        # εκτύπωσης («%1%») έβγαινε άδειο κουτί, με άδεια λίστα. Μόνο το `None`
        # γίνεται null QString, που σημαίνει «δεν έχω μετάφραση».
        if context in _CONTEXTS:
            return _STRINGS.get(source)
        return None


def install(app) -> GreekTranslator:
    """Ελληνικά παντού: κείμενα του Qt, ημερομηνίες, αριθμοί.

    Το QLocale είναι εξίσου σημαντικό με τη μετάφραση: από αυτό παίρνει το
    ημερολόγιο τα ονόματα μηνών/ημερών. Χωρίς αυτό, το popup του QDateEdit
    έγραφε «January» μέσα σε ελληνική οθόνη.

    Επιστρέφει τον translator ώστε να κρατηθεί ζωντανός από τον καλούντα — αν
    τον μαζέψει ο garbage collector, το Qt μένει ξανά χωρίς μετάφραση.
    """
    greek = QLocale(QLocale.Language.Greek, QLocale.Country.Greece)
    QLocale.setDefault(greek)
    translator = GreekTranslator()
    app.installTranslator(translator)
    return translator
