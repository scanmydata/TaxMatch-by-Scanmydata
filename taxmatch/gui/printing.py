"""(Port από το Timologio Downloader, gui/printing.py.) Μαζική εκτύπωση των PDF των επιλεγμένων παραστατικών, με προεπισκόπηση.

Ο λογιστής θέλει συχνά να τυπώσει με τη μία όλα τα τιμολόγια που μόλις κατέβασε
(π.χ. για τον φάκελο ενός πελάτη). Ανοίγουμε τη **native προεπισκόπηση**
(``QPrintPreviewDialog``): ο χρήστης βλέπει όλες τις σελίδες και έχει έτοιμη τη
γραμμή εργαλείων του Qt — ζουμ, προσαρμογή σελίδας, πλοήγηση, διάταξη σελίδων
και το κουμπί εκτύπωσης — με τα εικονίδια και τα hints της. Μία εργασία, χωρίς
να ανοίγει ένα-ένα τα αρχεία.

Γιατί render-σε-εικόνα: το Qt δεν τυπώνει PDF κατευθείαν. Το ``QPdfDocument``
στοιχειοθετεί κάθε σελίδα σε εικόνα, την οποία ζωγραφίζουμε στον ``QPrinter``. Η
προεπισκόπηση ξαναζητά ζωγράφισμα σε κάθε zoom/σελιδοποίηση, οπότε κρατάμε
**cache** των εικόνων ανά σελίδα ώστε να μένει responsive.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEvent, QLocale, QObject, QRect, QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPrintSupport import QPrinter, QPrintPreviewDialog, QPrintPreviewWidget
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QLineEdit,
    QToolBar,
    QWidgetAction,
    QWidget,
)

from .icons import icon
from .theme import CURRENT

log = logging.getLogger(__name__)

#: Ελληνικά tooltips ΚΑΤΑ ΣΕΙΡΑ των εργαλείων της native γραμμής του Qt. Ταιριάζουμε
#: κατά ΘΕΣΗ και όχι κατά ``text()``: το QPrintPreviewDialog φτιάχνει τα κουμπιά
#: **χωρίς κείμενο** (μόνο εικονίδιο), οπότε το παλιό ταίριασμα με «Print»/«Zoom in»
#: δεν έβρισκε ποτέ τίποτα — γι' αυτό δεν εμφανίζονταν ούτε tooltips ούτε τα δικά
#: μας εικονίδια. Η σειρά είναι σταθερή σε όλες τις εκδόσεις του Qt (15 ενέργειες).
_TOOLBAR_TIPS = [
    "Προσαρμογή στο πλάτος",          # 0
    "Προσαρμογή σελίδας",             # 1
    "Σμίκρυνση",                      # 2
    "Μεγέθυνση",                      # 3
    "Κατακόρυφος προσανατολισμός",    # 4
    "Οριζόντιος προσανατολισμός",     # 5
    "Πρώτη σελίδα",                   # 6
    "Προηγούμενη σελίδα",             # 7
    "Επόμενη σελίδα",                 # 8
    "Τελευταία σελίδα",               # 9
    "Μία σελίδα",                     # 10
    "Αντικριστές σελίδες",            # 11
    "Επισκόπηση όλων των σελίδων",    # 12
    "Διαμόρφωση σελίδας",             # 13
    "Εκτύπωση",                       # 14
]

#: Δικά μας SVG εικονίδια για όσα κουμπιά έβγαιναν **κενά** στο πακεταρισμένο
#: build (τα εικονίδιά τους έρχονται από πόρους του Qt που το PyInstaller δεν
#: πάντα περιλαμβάνει): ζουμ, πλοήγηση σελίδων και εκτύπωση. Κλειδί = θέση στη
#: γραμμή. Η πλοήγηση (πρώτη/προηγ./επόμ./τελευταία) έβγαινε αόρατη γιατί τα
#: native βελάκια του Qt δεν πακετάρονταν — γι' αυτό «δεν φαίνονταν» τα κουμπιά
#: επόμενης/προηγούμενης σελίδας.
_TOOLBAR_ICONS = {
    2: "zoom_out", 3: "zoom_in",
    6: "nav_first", 7: "nav_prev", 8: "nav_next", 9: "nav_last",
    14: "printer",
}


def _fix_toolbar_combos(dialog: QPrintPreviewDialog) -> None:
    """Κάνει ορατά το dropdown του ζουμ και το πεδίο αριθμού σελίδας.

    Δύο ξεχωριστά προβλήματα, και τα δύο άφηναν ένα **άδειο κουτί**:

    * Χρώμα: στο σκούρο θέμα το popup του combo έβγαινε με κείμενο στο χρώμα του
      φόντου. Δίνουμε ρητά χρώματα κειμένου/φόντου, και στη λίστα του popup.
    * Ύψος: το γενικό stylesheet της εφαρμογής δίνει στα πεδία `padding: 6px 9px`.
      Μέσα στη χαμηλή γραμμή εργαλείων της προεπισκόπησης, αυτό το padding
      έτρωγε το κείμενο — το «100,0%» φαινόταν κομμένο στη μέση ή καθόλου, κι
      έτσι το πεδίο του ζουμ έμοιαζε κενό. Το μικραίνουμε **μόνο εδώ**.

    Το combo δεν έχει ετικέτα πουθενά: χωρίς tooltip κανείς δεν μπορεί να
    μαντέψει ότι είναι το ζουμ της προεπισκόπησης.
    """
    field_qss = (
        f"color:{CURRENT.txt}; background:{CURRENT.panel};"
        f"padding:1px 4px; border-radius:6px;"
        f"selection-background-color:{CURRENT.accent}; selection-color:{CURRENT.on_accent};"
    )
    for combo in dialog.findChildren(QComboBox):
        combo.setStyleSheet(
            f"QComboBox {{ {field_qss} }}"
            f"QComboBox QAbstractItemView {{ {field_qss} }}"
            f"QComboBox QAbstractItemView::item {{ color:{CURRENT.txt};"
            f" padding:2px 6px; }}"
        )
        combo.setToolTip("Μεγέθυνση της προεπισκόπησης")
        if combo.lineEdit() is not None:
            combo.lineEdit().setToolTip("Μεγέθυνση της προεπισκόπησης")
    for edit in dialog.findChildren(QLineEdit):
        edit.setStyleSheet(f"QLineEdit {{ {field_qss} }}")
        if not edit.toolTip():
            edit.setToolTip("Αριθμός σελίδας")


def _fix_toolbar_icons(dialog: QPrintPreviewDialog) -> None:
    for toolbar in dialog.findChildren(QToolBar):
        # Μόνο οι ενέργειες-κουμπιά, με τη σειρά τους: πετάμε τους διαχωριστές
        # και τα widget (το combo του ζουμ, το πεδίο αριθμού σελίδας).
        actions = [
            a for a in toolbar.actions()
            if not a.isSeparator() and not isinstance(a, QWidgetAction)
        ]
        for i, action in enumerate(actions):
            if i < len(_TOOLBAR_TIPS):
                tip = _TOOLBAR_TIPS[i]
                # Και τα δύο: το QToolButton δείχνει το toolTip στο hover, αλλά
                # κάποια στυλ διαβάζουν το statusTip — τα ορίζουμε μαζί.
                action.setToolTip(tip)
                action.setStatusTip(tip)
            name = _TOOLBAR_ICONS.get(i)
            if name:
                action.setIcon(icon(name, CURRENT.txt))
    _fix_toolbar_combos(dialog)


def _parse_zoom(text: str) -> float | None:
    """«400,0%» / «125%» / «80.5» → 4.0 / 1.25 / 0.805, αλλιώς None."""
    raw = (text or "").replace("%", "").replace(" ", "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        return None
    return max(1.0, min(1000.0, value)) / 100.0


class _WheelZoom(QObject):
    """Ctrl + ροδέλα = μεγέθυνση/σμίκρυνση, όπως σε κάθε viewer PDF."""

    def __init__(self, preview: QPrintPreviewWidget, after: Callable[[], None]) -> None:
        super().__init__(preview)
        self._preview = preview
        self._after = after

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if (
            event.type() == QEvent.Type.Wheel
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            delta = event.angleDelta().y()
            if delta > 0:
                self._preview.zoomIn()
            elif delta < 0:
                self._preview.zoomOut()
            self._after()
            return True
        return False


def _fix_zoom(dialog: QPrintPreviewDialog) -> None:
    """Κάνει να δουλεύει το ζουμ: έτοιμες τιμές του dropdown και Ctrl+ροδέλα.

    * **Dropdown.** Το Qt γράφει τις τιμές με τη μορφή της γλώσσας («400,0%»)
      αλλά τις ξαναδιαβάζει με τελεία (``toFloat``). Με ελληνική υποδιαστολή η
      ανάγνωση αποτύγχανε σιωπηλά και η επιλογή δεν έκανε τίποτα. Διαβάζουμε
      εμείς την τιμή, με κόμμα ή τελεία.
    * **Ctrl+ροδέλα.** Η προεπισκόπηση του Qt δεν το υποστηρίζει καθόλου.
    """
    preview = dialog.findChild(QPrintPreviewWidget)
    combos = dialog.findChildren(QComboBox)
    if preview is None or not combos:
        return
    combo = combos[0]
    # Οι δύο «Προσαρμογή» είναι οι πρώτες checkable ενέργειες της γραμμής.
    fits = [
        a for tb in dialog.findChildren(QToolBar) for a in tb.actions()
        if a.isCheckable() and not isinstance(a, QWidgetAction)
    ][:2]

    def show_factor() -> None:
        combo.blockSignals(True)
        combo.setEditText(QLocale().toString(preview.zoomFactor() * 100, "f", 1) + "%")
        combo.blockSignals(False)

    def apply(text: str) -> None:
        factor = _parse_zoom(text)
        if factor is None:
            return
        for action in fits:
            action.setChecked(False)
        preview.setZoomMode(QPrintPreviewWidget.ZoomMode.CustomZoom)
        preview.setZoomFactor(factor)
        show_factor()

    combo.textActivated.connect(apply)
    if combo.lineEdit() is not None:
        combo.lineEdit().returnPressed.connect(lambda: apply(combo.currentText()))

    def after_wheel() -> None:
        for action in fits:
            action.setChecked(False)
        show_factor()

    wheel = _WheelZoom(preview, after_wheel)
    preview.installEventFilter(wheel)
    for child in preview.findChildren(QWidget):
        child.installEventFilter(wheel)


def _wire_print_action(dialog: QPrintPreviewDialog, on_print: Callable[[], None]) -> None:
    """Συνδέει το κουμπί «Εκτύπωση» της γραμμής εργαλείων με το ``on_print``.

    Έτσι, μόλις ο χρήστης στείλει τα παραστατικά στον εκτυπωτή, καταγράφεται η
    ημερομηνία εκτύπωσης (στήλη «Εκτυπώθηκε»). Η εκτύπωση είναι η τελευταία
    ενέργεια (θέση 14) της σταθερής γραμμής του Qt."""
    for toolbar in dialog.findChildren(QToolBar):
        actions = [
            a for a in toolbar.actions()
            if not a.isSeparator() and not isinstance(a, QWidgetAction)
        ]
        if len(actions) > 14:
            actions[14].triggered.connect(lambda *_: on_print())
            return


#: Ανάλυση απόδοσης της σελίδας σε εικόνα. 200 DPI διαβάζεται άνετα και κρατά
#: μια σελίδα A4 γύρω στα ~15MP αντί για ~35MP στα 300 DPI.
RENDER_DPI = 200


def _load(doc: QPdfDocument, path: Path) -> bool:
    """Φορτώνει τοπικό PDF (σύγχρονο) και λέει αν είναι έτοιμο για απόδοση."""
    try:
        doc.load(str(path))
    except Exception:  # noqa: BLE001
        return False
    return doc.status() == QPdfDocument.Status.Ready and doc.pageCount() > 0


def print_pdfs(
    paths: list[Path],
    parent: QWidget | None = None,
    on_print: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Ανοίγει προεπισκόπηση εκτύπωσης για όλα τα PDF. Επιστρέφει ``(έτοιμα,
    απέτυχαν)`` — «έτοιμα» = παραστατικά που μπήκαν στην προεπισκόπηση.

    Η ίδια η εκτύπωση γίνεται από τη γραμμή εργαλείων της προεπισκόπησης. Το
    προαιρετικό ``on_print`` καλείται όταν ο χρήστης πατήσει «Εκτύπωση» εκεί.
    """
    paths = [p for p in paths if p.exists()]
    if not paths:
        return 0, 0

    # Φορτώνουμε μία φορά κάθε έγγραφο· τα κρατάμε ζωντανά όσο ζει η
    # προεπισκόπηση ώστε να αποδίδουμε σελίδες on-demand.
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
    docs: list[QPdfDocument] = []
    pages: list[tuple[QPdfDocument, int]] = []
    failed = 0
    try:
        for path in paths:
            doc = QPdfDocument(parent)
            if _load(doc, path):
                docs.append(doc)
                pages.extend((doc, i) for i in range(doc.pageCount()))
            else:
                failed += 1
                log.warning("Το PDF δεν φορτώθηκε για εκτύπωση: %s", path)
    finally:
        QApplication.restoreOverrideCursor()

    if not pages:
        return 0, failed

    cache: dict[tuple[int, int], QImage] = {}

    def render(printer: QPrinter) -> None:
        # Ο δείκτης αναμονής μπαίνει/βγαίνει ΜΕΣΑ στο render (ισοσκελισμένο), ώστε
        # να μη μένει ποτέ κολλημένος: παλιά τον βάζαμε γύρω από το exec() του
        # modal, οπότε ο κέρσορας έδειχνε «loading» σε όλη τη διάρκεια της
        # προεπισκόπησης και η γραμμή εργαλείων έμοιαζε παγωμένη.
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        painter = QPainter()
        try:
            if not painter.begin(printer):
                return
            first = True
            for doc, page in pages:
                if not first:
                    printer.newPage()
                first = False
                _draw_page(painter, printer, doc, page, cache)
        finally:
            if painter.isActive():
                painter.end()
            QApplication.restoreOverrideCursor()

    printer = QPrinter(QPrinter.PrinterMode.HighResolution)

    # Native προεπισκόπηση του Qt: φέρνει έτοιμη τη δική της γραμμή εργαλείων με
    # τα εικονίδια και τα hints της (ζουμ, πλάτος/σελίδα, πλοήγηση, διάταξη,
    # ρύθμιση σελίδας και το κουμπί εκτύπωσης). Το μόνο που φτιάχνουμε εμείς
    # είναι τι ζωγραφίζεται (render) και ο δείκτης αναμονής.
    dialog = QPrintPreviewDialog(printer, parent)
    dialog.setWindowTitle("Προεπισκόπηση εκτύπωσης")
    dialog.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
    # Τα κουμπιά «Εκτύπωση / Ζουμ +/−» έδειχναν κενά περιγράμματα: τους δίνουμε
    # δικά μας εικονίδια. Το υπόλοιπο preview μένει αυτούσιο (native).
    _fix_toolbar_icons(dialog)
    _fix_zoom(dialog)
    if on_print is not None:
        _wire_print_action(dialog, on_print)
    dialog.paintRequested.connect(render)
    if parent is not None:
        dialog.resize(parent.size())
    try:
        dialog.exec()
    finally:
        for doc in docs:
            doc.close()
    return len(docs), failed


def _draw_page(
    painter: QPainter,
    printer: QPrinter,
    doc: QPdfDocument,
    page: int,
    cache: dict[tuple[int, int], QImage],
) -> None:
    """Αποδίδει (με cache) μία σελίδα σε εικόνα και τη ζωγραφίζει κεντραρισμένη.

    Διατηρεί τις αναλογίες: ένα A4 τιμολόγιο δεν πρέπει να «τεντωθεί» στο πλάτος
    ενός φακέλου εκτυπωτή με άλλη αναλογία.
    """
    key = (id(doc), page)
    image = cache.get(key)
    if image is None:
        pt = doc.pagePointSize(page)  # σε points (1/72 ίντσας)
        w = max(1, round(pt.width() / 72.0 * RENDER_DPI))
        h = max(1, round(pt.height() / 72.0 * RENDER_DPI))
        image = doc.render(page, QSize(w, h))
        cache[key] = image
    if image.isNull():
        return

    target = painter.viewport()  # εκτυπώσιμη περιοχή σε pixels συσκευής
    scaled = image.size().scaled(target.size(), Qt.AspectRatioMode.KeepAspectRatio)
    x = target.x() + (target.width() - scaled.width()) // 2
    y = target.y() + (target.height() - scaled.height()) // 2
    painter.drawImage(QRect(x, y, scaled.width(), scaled.height()), image)
