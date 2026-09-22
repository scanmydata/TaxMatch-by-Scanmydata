"""Μικρά widgets που ξαναχρησιμοποιούνται σε πολλές σελίδες."""

from __future__ import annotations

import re
from datetime import date

from PySide6.QtCore import (
    Property,
    QDate,
    QDateTime,
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QTime,
    QTimer,
)
from PySide6.QtGui import QColor, QPainter, QValidator
from PySide6.QtWidgets import QCheckBox, QDateEdit, QHeaderView, QLineEdit, QTableWidget

from .theme import CURRENT


def add_reveal(field: QLineEdit) -> QLineEdit:
    """Βάζει «ματάκι» μέσα στο πεδίο, για να φαίνεται ο κωδικός με ένα κλικ.

    Ζει ΜΕΣΑ στο πεδίο (``addAction`` σε δεξιά θέση) και όχι ως χωριστό
    κουτάκι δίπλα: το κουτάκι είναι δεύτερο στοιχείο στη φόρμα και χάνεται,
    ενώ το ματάκι βρίσκεται εκεί που κοιτά ήδη το μάτι — πάνω στον κωδικό που
    δεν διαβάζεται.

    Επιστρέφει το ίδιο πεδίο, ώστε να γράφεται ``add_reveal(QLineEdit())``.
    """
    from .icons import icon

    field.setEchoMode(QLineEdit.EchoMode.Password)
    action = field.addAction(
        icon("eye", CURRENT.muted), QLineEdit.ActionPosition.TrailingPosition
    )
    action.setToolTip("Εμφάνιση κωδικού")

    def toggle() -> None:
        hidden = field.echoMode() == QLineEdit.EchoMode.Password
        field.setEchoMode(
            QLineEdit.EchoMode.Normal if hidden else QLineEdit.EchoMode.Password
        )
        action.setIcon(icon("eye_off" if hidden else "eye", CURRENT.muted))
        action.setToolTip("Απόκρυψη κωδικού" if hidden else "Εμφάνιση κωδικού")

    action.triggered.connect(toggle)
    #: Το βοήθημα το βρίσκουν τα τεστ και ο διακόπτης θέματος.
    field.reveal_action = action
    return field


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    return QColor(
        round(a.red() + (b.red() - a.red()) * t),
        round(a.green() + (b.green() - a.green()) * t),
        round(a.blue() + (b.blue() - a.blue()) * t),
    )


class ToggleSwitch(QCheckBox):
    """Διακόπτης on/off αντί για τετράγωνο κουτάκι.

    Ζωγραφίζεται εξ ολοκλήρου εδώ και όχι μέσω stylesheet: το Qt δεν έχει
    sub-control για «κάψουλα με μπίλια», οπότε ένα QSS θα κατέληγε σε εικόνες
    ανά θέμα και ανά κατάσταση.
    """

    _W, _H, _PAD = 40, 22, 3

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._knob = 1.0 if self.isChecked() else 0.0
        self._anim = QPropertyAnimation(self, b"knob", self)
        self._anim.setDuration(140)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    def checkStateSet(self) -> None:  # noqa: N802 (Qt API)
        """Η μπίλια ακολουθεί την κατάσταση — ακόμη και με μπλοκαρισμένα signals.

        Δεν ακούμε το `toggled`: η εκκίνηση κάνει `blockSignals(True)` για να
        θυμηθεί το αποθηκευμένο θέμα χωρίς να το ξαναεφαρμόσει, οπότε ο
        διακόπτης έδειχνε «κλειστό» ενώ το φωτεινό θέμα ήταν αναμμένο. Το
        checkStateSet είναι virtual, όχι signal, και καλείται πάντα.
        """
        super().checkStateSet()
        self._animate(self.isChecked())

    def nextCheckState(self) -> None:  # noqa: N802 (Qt API)
        """Το κλικ του χρήστη περνά από εδώ — όχι από το checkStateSet.

        Σε Qt 6.11 το πάτημα με το ποντίκι καλεί μόνο το nextCheckState (αλλάζει
        την κατάσταση και εκπέμπει το toggled), ενώ το checkStateSet καλείται
        μόνο μέσω setChecked. Χωρίς αυτή την υπέρβαση η μπίλια έμενε ακίνητη σε
        κάθε κλικ: ο διακόπτης άλλαζε κατάσταση αλλά έμοιαζε «νεκρός».
        """
        super().nextCheckState()
        self._animate(self.isChecked())

    def _animate(self, on: bool) -> None:
        target = 1.0 if on else 0.0
        if self._knob == target:
            return
        self._anim.stop()
        self._anim.setStartValue(self._knob)
        self._anim.setEndValue(target)
        self._anim.start()

    def _get_knob(self) -> float:
        return self._knob

    def _set_knob(self, value: float) -> None:
        self._knob = value
        self.update()

    knob = Property(float, _get_knob, _set_knob)

    def sizeHint(self) -> QSize:
        width = self._W + (8 + self.fontMetrics().horizontalAdvance(self.text())
                           if self.text() else 0)
        return QSize(width, max(self._H + 4, self.fontMetrics().height() + 6))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def hitButton(self, pos) -> bool:
        # Ολόκληρο το widget, ώστε να πιάνει και το κείμενο δίπλα στον διακόπτη.
        return self.rect().contains(pos)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        top = (self.height() - self._H) / 2
        track = QRectF(0, top, self._W, self._H)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(
            _blend(QColor(CURRENT.line), QColor(CURRENT.accent_deep), self._knob)
        )
        painter.drawRoundedRect(track, self._H / 2, self._H / 2)

        diameter = self._H - 2 * self._PAD
        travel = self._W - 2 * self._PAD - diameter
        painter.setBrush(
            _blend(QColor(CURRENT.muted), QColor(CURRENT.on_accent), self._knob)
        )
        painter.drawEllipse(
            QRectF(self._PAD + self._knob * travel, top + self._PAD, diameter, diameter)
        )

        if self.text():
            painter.setPen(QColor(CURRENT.txt if self.isEnabled() else CURRENT.muted))
            painter.drawText(
                self.rect().adjusted(self._W + 8, 0, 0, 0),
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                self.text(),
            )
        painter.end()


#: Χαλαρή μορφή ημερομηνίας: 1-2 ψηφία ημέρα, 1-2 μήνας, 2 ή 4 έτος, με
#: οποιονδήποτε από τους τρεις συνηθισμένους διαχωριστές.
_LOOSE_DATE = re.compile(r"^\s*(\d{1,2})\s*[/.\-]\s*(\d{1,2})\s*[/.\-]\s*(\d{2}|\d{4})\s*$")

#: Ό,τι θα *μπορούσε* να γίνει ημερομηνία αν συνεχίσει να πληκτρολογεί.
_DATE_PREFIX = re.compile(r"^[\d/.\-\s]*$")


def parse_gr_date(text: str) -> QDate | None:
    """«26/8/26» → 26/08/2026. Δεκτά και «26-8-2026», «26.08.26».

    Το QDateEdit από μόνο του δέχεται **μόνο** την ακριβή μορφή που δείχνει
    (ηη/μμ/εεεε): γράφοντας «26/8/26» ο χρήστης έπαιρνε ή σκουπίδια ή το πεδίο
    αρνιόταν το πάτημα. Στη χώρα που γράφει «26/8/26» στο χαρτί, αυτό είναι
    λάθος του προγράμματος, όχι του χρήστη.

    Το διψήφιο έτος διαβάζεται ως 20xx: η εφαρμογή αφορά παραστατικά myDATA και
    το myDATA ξεκίνησε το 2019 — «26» δεν σημαίνει ποτέ 1926.
    """
    match = _LOOSE_DATE.match(text or "")
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    if len(match.group(3)) == 2:
        year += 2000
    parsed = QDate(year, month, day)
    return parsed if parsed.isValid() else None


class GrDateEdit(QDateEdit):
    """Ημερομηνία με ημερολόγιο και ελληνική μορφή ηη/μμ/εεεε.

    Αντικατέστησε τα QLineEdit: το QDateEdit βάζει μόνο του τις καθέτους καθώς
    πληκτρολογεί ο χρήστης και δεν επιτρέπει να γραφτεί άκυρη ημερομηνία, οπότε
    το «31/02» δεν φτάνει ποτέ ως αίτημα στην ΑΑΔΕ.

    Δέχεται επιπλέον **συντομογραφίες**: «26/8/26», «26-8-2026», «26.08.26»
    διαβάζονται όλα ως 26/08/2026 και ξαναγράφονται στην πλήρη μορφή μόλις
    φύγει ο κέρσορας.
    """

    def __init__(self, initial: date | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setCalendarPopup(True)
        self.setDisplayFormat("dd/MM/yyyy")
        self.setFixedWidth(128)
        # Χωρίς όριο, το βελάκι «κάτω» ταξιδεύει στο 1752.
        self.setMinimumDate(QDate(2000, 1, 1))
        self.setMaximumDate(QDate.currentDate().addYears(1))
        self.setDate(QDate(initial) if initial else QDate.currentDate())
        # Χωρίς keyboard tracking, το πεδίο δεν προσπαθεί να «διορθώσει» την
        # ημερομηνία σε κάθε πληκτρολόγηση — ο χρήστης γράφει ολόκληρη την
        # ημέρα/μήνα/έτος και μετά επικυρώνεται, αντί να πηδά ο κέρσορας.
        self.setKeyboardTracking(False)
        # StrongFocus (όχι WheelFocus): το πεδίο δέχεται ρόδα ΜΟΝΟ αφού το
        # κλικάρει ο χρήστης. Αλλιώς, κάθε κύλιση της σελίδας που περνούσε πάνω
        # από το πεδίο άλλαζε σιωπηλά την ημερομηνία — το κλασικό «η ημερομηνία
        # αλλάζει μόνη της» που έκανε το datepicker να μοιάζει ασταθές.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def wheelEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if not self.hasFocus():
            # Δεν το καταναλώνουμε: αφήνουμε τη σελίδα από κάτω να κυλήσει.
            event.ignore()
            return
        super().wheelEvent(event)

    # --- χαλαρή πληκτρολόγηση ------------------------------------------
    def validate(self, text: str, pos: int):  # noqa: N802 (Qt API)
        """Αφήνει το «26/8/26» να γραφτεί.

        Το QDateEdit απορρίπτει κάθε πάτημα που δεν ταιριάζει ακριβώς στη μορφή
        που δείχνει — και ο μονοψήφιος μήνας δεν ταιριάζει ποτέ. Επιστρέφουμε
        **Intermediate** (όχι Acceptable) επίτηδες: έτσι το Qt καλεί το `fixup`
        όταν φύγει ο κέρσορας, και η ημερομηνία γράφεται μία φορά ολόκληρη αντί
        να αναδιατάσσεται κάτω από τα δάχτυλα του χρήστη σε κάθε πλήκτρο.
        """
        state, fixed, position = super().validate(text, pos)
        if state == QValidator.State.Acceptable:
            return state, fixed, position
        if parse_gr_date(text) is not None or _DATE_PREFIX.match(text or ""):
            return QValidator.State.Intermediate, text, pos
        return state, fixed, position

    def fixup(self, text: str) -> str:
        """Η συντομογραφία γίνεται πλήρης ημερομηνία μόλις τελειώσει η γραφή."""
        parsed = parse_gr_date(text)
        if parsed is not None:
            return parsed.toString("dd/MM/yyyy")
        return super().fixup(text)

    def commit_typed(self) -> None:
        """Διαβάζει ό,τι έγραψε ο χρήστης και το κάνει ημερομηνία.

        Το `fixup` του Qt **δεν** καλείται αξιόπιστα σε κάθε διαδρομή (το
        QAbstractSpinBox κρατά cache της τελευταίας επικύρωσης και σε κάποιες
        περιπτώσεις επαναφέρει σιωπηλά την προηγούμενη τιμή). Το αποτέλεσμα θα
        ήταν το χειρότερο δυνατό: η ημερομηνία που πληκτρολόγησε ο χρήστης να
        αγνοείται **χωρίς μήνυμα** και το πεδίο να δείχνει την παλιά. Οπότε το
        κάνουμε ρητά, εμείς, όταν φύγει ο κέρσορας ή πατηθεί Enter.
        """
        parsed = parse_gr_date(self.lineEdit().text())
        if parsed is not None and parsed != self.date():
            self.setDate(parsed)

    def focusOutEvent(self, event) -> None:  # noqa: N802 (Qt API)
        self.commit_typed()
        super().focusOutEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit_typed()
        super().keyPressEvent(event)

    def gr(self) -> str:
        return self.date().toString("dd/MM/yyyy")

    def set_gr(self, value: str) -> None:
        parsed = QDate.fromString(value, "dd/MM/yyyy")
        if not parsed.isValid():
            parsed = QDate.fromString(value, "yyyy-MM-dd")
        if parsed.isValid():
            self.setDate(parsed)


def persist_header(table: QTableWidget, prefs: QSettings, key: str):
    """Κάνει τις στήλες μετακινήσιμες και θυμάται πλάτη, σειρά και ταξινόμηση.

    Το κλειδί περιέχει το πλήθος στηλών: αν αργότερα προστεθεί στήλη, το παλιό
    state δεν ταιριάζει πια και το restoreState θα το απέρριπτε σιωπηλά,
    αφήνοντας τον χρήστη με «κολλημένα» πλάτη που δεν εξηγούνται.

    Επιστρέφει συνάρτηση που γράφει το state αμέσως, για όποιον χρειάζεται να
    το αποθηκεύσει συγχρονισμένα με κάτι άλλο.
    """
    header = table.horizontalHeader()
    header.setSectionsMovable(True)
    header.setFirstSectionMovable(False)  # το checkbox μένει πρώτο
    setting = f"header/{key}/{table.columnCount()}"

    state = prefs.value(setting)
    table._sort_chosen = bool(state) and header.restoreState(state)  # type: ignore[attr-defined]

    def save() -> None:
        prefs.setValue(setting, header.saveState())

    # Το sectionResized πυροδοτείται σε κάθε pixel του συρσίματος· χωρίς
    # debounce θα γράφαμε στο μητρώο δεκάδες φορές ανά κίνηση του ποντικιού.
    timer = QTimer(table)
    timer.setSingleShot(True)
    timer.setInterval(400)
    timer.timeout.connect(save)
    for signal in (header.sectionResized, header.sectionMoved,
                   header.sortIndicatorChanged):
        signal.connect(lambda *_: timer.start())

    def chosen(*_) -> None:
        table._sort_chosen = True  # type: ignore[attr-defined]

    header.sortIndicatorChanged.connect(chosen)
    return save


def resort(table: QTableWidget, default_column: int | None = None) -> None:
    """Ξαναταξινομεί έναν πίνακα αφού γεμίσει.

    Το setSortingEnabled(True) δεν αγγίζει όσες γραμμές υπάρχουν ήδη — ταξινομεί
    μόνο ό,τι μπει μετά. Χωρίς αυτό, η ταξινόμηση που διάλεξε ο χρήστης θα
    χανόταν σε κάθε ανανέωση του πίνακα.
    """
    header = table.horizontalHeader()
    if getattr(table, "_sort_chosen", False) and header.sortIndicatorSection() >= 0:
        table.sortItems(header.sortIndicatorSection(), header.sortIndicatorOrder())
    elif default_column is not None:
        table.sortItems(default_column, Qt.SortOrder.DescendingOrder)


class _FillColumn(QObject):
    """Δίνει σε μια στήλη ό,τι περισσεύει, χωρίς να την κλειδώνει.

    Το Stretch mode γεμίζει τον πίνακα αλλά κάνει τη στήλη **αδύνατη να συρθεί**
    — και είναι ακριβώς η στήλη (Επωνυμία / Αντισυμβαλλόμενος) που θέλει κανείς
    να φαρδύνει. Interactive + αυτό εδώ δίνει και τα δύο: γεμίζει μόνη της, μέχρι
    τη στιγμή που θα την πιάσει ο χρήστης· από εκεί και πέρα το πλάτος είναι
    δικό του και το θυμόμαστε.
    """

    def __init__(self, table: QTableWidget, column: int, prefs: QSettings, key: str,
                 save_state):
        super().__init__(table)
        self._table = table
        self._column = column
        self._prefs = prefs
        self._save_state = save_state
        self._setting = f"header/{key}/{table.columnCount()}/manual"
        self._manual = bool(prefs.value(self._setting, False, type=bool))
        self._guard = False

        header = table.horizontalHeader()
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.sectionResized.connect(self._on_resized)
        table.viewport().installEventFilter(self)

    def _on_resized(self, index: int, _old: int, _new: int) -> None:
        if index == self._column and not self._guard:
            # Ο χρήστης την έπιασε: από εδώ και πέρα δεν την ξαναπειράζουμε.
            self._manual = True
            self._prefs.setValue(self._setting, True)
            # Η σημαία και το πλάτος γράφονται μαζί. Το state αποθηκεύεται
            # κανονικά με καθυστέρηση 400ms· αν έκλεινε κανείς την εφαρμογή
            # ενδιάμεσα, θα θυμόμασταν «ο χρήστης διάλεξε πλάτος» χωρίς να
            # ξέρουμε ποιο — και η στήλη θα έμενε για πάντα στα 100 pixel.
            self._save_state()
        elif not self._manual:
            # Άλλη στήλη άλλαξε — μαζεύουμε ή δίνουμε τη διαφορά.
            self.fit()

    def eventFilter(self, watched, event) -> bool:
        if event.type() is QEvent.Type.Resize and not self._manual:
            self.fit()
        return False

    def fit(self) -> None:
        header = self._table.horizontalHeader()
        others = sum(
            header.sectionSize(i)
            for i in range(self._table.columnCount())
            if i != self._column and not header.isSectionHidden(i)
        )
        room = self._table.viewport().width() - others
        if room < 140 or abs(room - header.sectionSize(self._column)) <= 1:
            return
        self._guard = True
        header.resizeSection(self._column, room)
        self._guard = False


def setup_columns(
    table: QTableWidget, spec: list[tuple[str, int, str]], prefs: QSettings, key: str
) -> None:
    """Στήνει τις στήλες ενός πίνακα από το spec (επικεφαλίδα, πλάτος, tooltip).

    Πλάτος 0 σημαίνει «πάρε ό,τι περισσεύει». Όλες οι στήλες είναι Interactive:
    καμία δεν είναι κλειδωμένη, όλες σύρονται και αναδιατάσσονται.
    """
    header = table.horizontalHeader()
    fill = -1
    for column, (_, width, tip) in enumerate(spec):
        header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        if width:
            table.setColumnWidth(column, width)
        else:
            fill = column
        item = table.horizontalHeaderItem(column)
        if item and tip:
            item.setToolTip(tip)

    save = persist_header(table, prefs, key)
    if fill >= 0:
        # Μετά το restoreState: αλλιώς το φίλτρο θα υπολόγιζε το κενό με τα
        # αρχικά πλάτη και θα ξανάγραφε αμέσως το αποθηκευμένο.
        table._fill_column = _FillColumn(  # type: ignore[attr-defined]
            table, fill, prefs, key, save
        )
