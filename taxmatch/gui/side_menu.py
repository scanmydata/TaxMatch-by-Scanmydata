"""Πλαϊνό μενού με λογότυπο, εικονίδια και κείμενο.

Ό,τι δεν είναι η καθημερινή δουλειά ζει εδώ, ώστε η κύρια οθόνη να μένει καθαρή.
Το μενού μαζεύεται σε μια λωρίδα εικονιδίων όταν ο χρήστης θέλει τον χώρο.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QUrl, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from PySide6.QtGui import QDesktopServices, QPixmap

from .. import __version__ as APP_VERSION
from .icons import icon, logo_pixmap
from .theme import CURRENT
from .widgets import ToggleSwitch

# Ελάχιστο και μέγιστο πλάτος του ανοιχτού μενού. Το πραγματικό πλάτος
# υπολογίζεται στο `_fit_width()` από τις ΠΡΑΓΜΑΤΙΚΕΣ διαστάσεις των ετικετών:
# με καρφωμένο 226 και λίγο μεγαλύτερη γραμματοσειρά συστήματος, μισή ντουζίνα
# ετικέτες («Αντίγραφο ασφαλείας», «Αρχείο καταγραφής», …) κόβονταν στη μέση.
WIDE_MIN, WIDE_MAX, NARROW = 226, 320, 58
WIDE = WIDE_MIN


#: Εικονίδιο ανά ενέργεια, όπου το όνομα της ενέργειας δεν είναι και όνομα εικονιδίου.
_ICONS = {
    "dashboard": "stats",
    "news": "csv",
    "export": "download",
    "run": "refresh",
    "logfile": "csv",
    "password": "lock",
}


class MenuButton(QPushButton):
    def __init__(self, name: str, text: str, tip: str = "") -> None:
        super().__init__(text)
        self._name = name
        self._icon = _ICONS.get(name, name)
        self._label = text
        self._tip = tip
        self._active = False
        self.setObjectName("menuButton")
        self.setIconSize(QSize(18, 18))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.help_text = tip
        if tip:
            self.setToolTip(tip)
        self.restyle()

    def restyle(self) -> None:
        """Ξαναβάφει το εικονίδιο — το SVG είναι μονόχρωμο, οπότε πρέπει να
        ξαναφτιαχτεί όταν αλλάξει θέμα."""
        self.setIcon(icon(self._icon, CURRENT.accent if self._active else CURRENT.muted))

    def set_active(self, active: bool) -> None:
        self._active = active
        self.setProperty("active", active)
        self.restyle()
        # Το Qt δεν ξαναδιαβάζει το stylesheet μόνο του σε αλλαγή property.
        self.style().unpolish(self)
        self.style().polish(self)

    def set_collapsed(self, collapsed: bool) -> None:
        """Μαζεμένο: μόνο εικονίδιο, με το κείμενο να επιβιώνει ως tooltip.

        Χωρίς αυτό η λωρίδα θα ήταν εικονίδια χωρίς όνομα — αναγνωρίσιμα μόνο
        από όποιον ξέρει ήδη το πρόγραμμα.
        """
        self.setText("" if collapsed else self._label)
        full = f"{self._label} — {self._tip}" if self._tip else self._label
        self.help_text = full if collapsed else self._tip
        self.setToolTip(self.help_text)
        self.setProperty("help_text", self.help_text)


#: Ο ιστότοπος του γραφείου — το σήμα κάτω αριστερά οδηγεί εκεί.
BRAND_URL = "https://scanmydata.gr"


def _brand_path(name: str):
    """Το λογότυπο του γραφείου, από το bundle ή από τον φάκελο του έργου."""
    import sys
    from pathlib import Path

    roots = []
    base = getattr(sys, "_MEIPASS", "")
    if base:
        roots.append(Path(base) / "taxmatch" / "gui" / "assets")
    roots.append(Path(__file__).resolve().parent / "assets")
    for root in roots:
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


class SideMenu(QWidget):
    """Εκπέμπει το όνομα της ενέργειας· δεν ξέρει τι κάνει η καθεμιά."""

    triggered = Signal(str)
    tooltips_toggled = Signal(bool)
    theme_toggled = Signal(bool)  # True = φωτεινό
    collapsed_changed = Signal(bool)
    #: Κλικ στον αριθμό έκδοσης. Είναι η πρώτη πληροφορία που κοιτά όποιος
    #: αναρωτιέται «τρέχω την τελευταία;» — άρα είναι και το φυσικό σημείο για
    #: να ρωτήσει. Ο έλεγχος τον κάνει το παράθυρο, ένας και μοναδικός.
    version_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sideMenu")
        self._wide = WIDE_MIN
        self.setFixedWidth(self._wide)
        self._collapsed = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)
        self._layout = layout

        layout.addWidget(self._header())
        layout.addSpacing(12)

        self._buttons: dict[str, MenuButton] = {}
        self._sections: list[QLabel] = []

        self._dl_panel = self._build_menu()
        # Σε χαμηλή οθόνη (ή με ανοιγμένη τη ΒΟΗΘΕΙΑ) το μενού δεν χωρά και το Qt
        # έκοβε τα τελευταία στοιχεία χωρίς κανένα σημάδι. Με κύλιση, ό,τι
        # περισσεύει παραμένει προσβάσιμο.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        holder = QWidget()
        holder_box = QVBoxLayout(holder)
        holder_box.setContentsMargins(0, 0, 0, 0)
        holder_box.setSpacing(0)
        holder_box.addWidget(self._dl_panel)
        holder_box.addStretch(1)
        scroll.setWidget(holder)
        layout.addWidget(scroll, 1)

        layout.addStretch()
        self._settings_label = self._separator("ΡΥΘΜΙΣΕΙΣ")
        layout.addWidget(self._settings_label)

        self.chk_light = ToggleSwitch("Φωτεινό θέμα")
        self.chk_light.setToolTip("Εναλλαγή ανάμεσα σε σκούρο και φωτεινό")
        self.chk_light.toggled.connect(self.theme_toggled.emit)
        layout.addWidget(self.chk_light)

        self.chk_tooltips = ToggleSwitch("Βοηθητικά μηνύματα")
        self.chk_tooltips.setChecked(True)
        self.chk_tooltips.setToolTip(
            "Εμφάνιση επεξηγήσεων όταν αφήνετε τον δείκτη πάνω από ένα κουμπί"
        )
        self.chk_tooltips.toggled.connect(self.tooltips_toggled.emit)
        layout.addWidget(self.chk_tooltips)

        layout.addSpacing(8)
        layout.addWidget(self._footer())

        self._fit_width()

    def _fit_width(self) -> None:
        """Πλάτος όσο χρειάζεται η μακρύτερη ετικέτα — και των δύο μενού.

        Με σταθερό πλάτος, όποιος έχει λίγο μεγαλύτερη γραμματοσειρά συστήματος
        έβλεπε κομμένα τα «Αντίγραφο ασφαλείας», «Αρχείο καταγραφής» κ.λπ. Το
        μετράμε αντί να το μαντεύουμε, με όριο ώστε να μη φάει την οθόνη.
        """
        widest = max(
            (b.sizeHint().width() for b in self._buttons.values()),
            default=WIDE_MIN,
        )
        margins = self._layout.contentsMargins()
        needed = widest + margins.left() + margins.right()
        self._wide = max(WIDE_MIN, min(WIDE_MAX, needed))
        if not self._collapsed:
            self.setFixedWidth(self._wide)

    # --------------------------------------------------- τα δύο μενού
    def _panel(self) -> tuple[QWidget, QVBoxLayout]:
        panel = QWidget()
        box = QVBoxLayout(panel)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)
        return panel, box

    def _build_menu(self) -> QWidget:
        """Το μενού του TaxMatch — ίδια διάταξη ενοτήτων με το Timologio Downloader."""
        panel, box = self._panel()

        self._add(box, "add_client", "Νέος πελάτης",
                  "Προσθήκη πελάτη — με ΑΦΜ (η επωνυμία έρχεται αυτόματα) ή από Excel")
        box.addSpacing(6)

        self._pages = ("dashboard", "clients", "calendar", "news", "settings", "schedule")
        for name, text, tip in [
            ("dashboard", "Αρχική", "Σημερινό ενημερωτικό: ποιον αφορά τι"),
            ("clients", "Πελάτες", "Η λίστα των πελατών σας"),
            ("calendar", "Ημερολόγιο", "Προθεσμίες υποχρεώσεων: ΦΠΑ, VIES, Intrastat, ΑΠΔ…"),
            ("news", "Νέα & Matches", "Όλα τα άρθρα και ποιους πελάτες αφορούν"),
        ]:
            self._add(box, name, text, tip)

        box.addSpacing(10)
        box.addWidget(self._separator("ΔΕΔΟΜΕΝΑ"))
        for name, text, tip in [
            ("import", "Εισαγωγή από Excel", "Μαζική εισαγωγή πελατών (και κωδικών TAXISnet) από Excel"),
            ("export", "Εξαγωγή πελατών", "Λίστα πελατών σε CSV (χωρίς κωδικούς)"),
        ]:
            self._add(box, name, text, tip)

        box.addSpacing(10)
        box.addWidget(self._separator("ΑΥΤΟΜΑΤΑ"))
        self._add(box, "run", "Έλεγχος τώρα", "Λήψη νέων, ανάλυση και αντιστοίχιση με πελάτες αυτή τη στιγμή")
        self._add(box, "schedule", "Χρονοπρογραμματισμός",
                  "Καθημερινός έλεγχος στο παρασκήνιο (Windows Task Scheduler)")

        box.addSpacing(10)
        box.addWidget(self._separator("ΑΣΦΑΛΕΙΑ"))
        self._add(box, "password", "Κύριος κωδικός", "Προστασία του φακέλου δεδομένων με κωδικό")

        box.addSpacing(10)
        box.addWidget(self._separator("ΣΥΣΤΗΜΑ"))
        self._add(box, "settings", "Ρυθμίσεις", "Κλειδιά API, μοντέλα LLM, κωδικοί γραφείου, πηγές ειδήσεων")

        box.addSpacing(10)
        box.addWidget(self._separator("ΒΟΗΘΕΙΑ"))
        for name, text, tip in [
            ("tour", "Ξενάγηση", "Σύντομη περιήγηση στις λειτουργίες της εφαρμογής"),
            ("manual", "Εγχειρίδιο PDF", "Άνοιγμα του πλήρους εγχειριδίου χρήσης"),
            ("logfile", "Αρχείο καταγραφής", "Άνοιγμα του αρχείου με το αναλυτικό ιστορικό"),
        ]:
            self._add(box, name, text, tip)
        return panel

    # ------------------------------------------------------------------ UI
    def _header(self) -> QWidget:
        holder = QWidget()
        # Όταν το μενού δεν χωρά σε ύψος, το Qt συμπιέζει ό,τι μπορεί. Η
        # κεφαλίδα συρρικνωνόταν στα 31px ενώ το λογότυπο είναι 38, οπότε του
        # κοβόταν το κάτω μέρος. Το λογότυπο δεν είναι διαπραγματεύσιμο.
        holder.setMinimumHeight(38)
        row = QHBoxLayout(holder)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)

        self.logo = QLabel()
        self.logo.setPixmap(logo_pixmap(38))
        self.logo.setFixedSize(38, 38)
        self.logo.setScaledContents(True)
        row.addWidget(self.logo)

        self._title_box = QWidget()
        text = QVBoxLayout(self._title_box)
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(0)
        # Κρατιούνται ως πεδία: το `set_mode` τα αλλάζει όταν ο χρήστης περνά
        # στο e-Τιμολόγιο, ώστε να ξέρει πάντα σε ποια εφαρμογή βρίσκεται.
        self._title = QLabel("TaxMatch")
        self._title.setObjectName("menuTitle")
        self._subtitle = QLabel("Φορολογικά νέα")
        self._subtitle.setObjectName("menuSubtitle")
        text.addWidget(self._title)
        text.addWidget(self._subtitle)
        row.addWidget(self._title_box)
        row.addStretch()

        self.btn_toggle = QPushButton()
        self.btn_toggle.setObjectName("menuToggle")
        self.btn_toggle.setIcon(icon("menu", CURRENT.muted))
        self.btn_toggle.setIconSize(QSize(18, 18))
        self.btn_toggle.setFixedSize(30, 30)
        self.btn_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle.setToolTip("Σύμπτυξη/ανάπτυξη του μενού")
        self.btn_toggle.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        row.addWidget(self.btn_toggle)
        return holder

    def _footer(self) -> QWidget:
        """Το σήμα του γραφείου, κάτω αριστερά — και η έκδοση, διακριτικά.

        Το εικονίδιο της εφαρμογής έφυγε από εδώ: κάθεται ήδη στην κορυφή του
        μενού, και δίπλα στο σήμα του γραφείου διαβαζόταν σαν δεύτερο λογότυπο.
        """
        holder = QWidget()
        holder.setMinimumHeight(26)
        row = QHBoxLayout(holder)
        row.setContentsMargins(4, 0, 0, 0)
        row.setSpacing(8)

        # Σήμα του γραφείου — το ίδιο που δείχνει και η web εφαρμογή, ώστε οι
        # δύο μισές να μοιάζουν ένα προϊόν. Ανοίγει το scanmydata.gr.
        self.brand = QLabel()
        self.brand.setObjectName("brandMark")
        self.brand.setScaledContents(True)
        # Η αναλογία του αρχείου είναι 360x176 (εικονίδιο + λεκτικός τύπος,
        # χωρίς το σύνθημα). Ένα «λογικό» 110x22 το έλιωνε.
        self.brand.setFixedSize(53, 26)
        self.brand.setCursor(Qt.CursorShape.PointingHandCursor)
        self.brand.setToolTip(f"{BRAND_URL} — άνοιγμα στον browser")
        self.brand.mousePressEvent = self._open_brand_site
        row.addWidget(self.brand)
        self._paint_brand()

        self.version = QLabel(f"έκδοση {APP_VERSION}")
        self.version.setObjectName("menuVersion")
        self.version.setCursor(Qt.CursorShape.PointingHandCursor)
        self.version.setToolTip("Έλεγχος για νεότερη έκδοση")
        self.version.mousePressEvent = self._version_pressed
        row.addWidget(self.version)
        row.addStretch()
        return holder

    def _version_pressed(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.version_clicked.emit()

    def _open_brand_site(self, event) -> None:
        """Το σήμα είναι σύνδεσμος. Ανοίγει στον ΚΑΝΟΝΙΚΟ browser, όχι μέσα
        στην εφαρμογή: είναι ιστότοπος, όχι οθόνη του προγράμματος."""
        if event.button() == Qt.MouseButton.LeftButton:
            QDesktopServices.openUrl(QUrl(BRAND_URL))

    def _paint_brand(self) -> None:
        """Διαλέγει την εκδοχή του λογοτύπου που ταιριάζει στο θέμα.

        Το φωτεινό λογότυπο πάνω σε σκούρο μενού χάνεται — και αντίστροφα.
        """
        brand = getattr(self, "brand", None)
        if brand is None:
            return
        light_theme = getattr(CURRENT, "name", "dark") == "light"
        name = "scanmydata-light.png" if light_theme else "scanmydata-dark.png"
        path = _brand_path(name)
        if path is None:
            brand.hide()
            return
        pix = QPixmap(str(path))
        if pix.isNull():
            brand.hide()
            return
        brand.setPixmap(pix)
        brand.show()

    def _add(self, layout: QVBoxLayout, name: str, text: str, tip: str) -> None:
        button = MenuButton(name, text, tip)
        button.clicked.connect(lambda _=False, n=name: self.triggered.emit(n))
        layout.addWidget(button)
        self._buttons[name] = button

    def _separator(self, text: str) -> QWidget:
        holder = QWidget()
        box = QVBoxLayout(holder)
        box.setContentsMargins(6, 4, 0, 2)
        box.setSpacing(3)
        label = QLabel(text)
        label.setObjectName("menuSection")
        box.addWidget(label)
        self._sections.append(label)
        line = QFrame()
        line.setObjectName("line")
        box.addWidget(line)
        return holder

    # ------------------------------------------------------------ σύμπτυξη
    @property
    def collapsed(self) -> bool:
        return self._collapsed

    def set_collapsed(self, collapsed: bool, *, animate: bool = True) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        for button in self._buttons.values():
            button.set_collapsed(collapsed)
        for label in self._sections:
            label.setVisible(not collapsed)
        self._title_box.setVisible(not collapsed)
        self.version.setVisible(not collapsed)
        # Στη λωρίδα δεν χωρούν λογότυπο (38px) και ☰ (30px) μαζί. Φεύγει το
        # λογότυπο: χωρίς το ☰ δεν υπάρχει τρόπος να ξανανοίξει το μενού — και
        # το λογότυπο μένει ούτως ή άλλως στο κάτω μέρος.
        self.logo.setVisible(not collapsed)
        self.chk_light.setText("" if collapsed else "Φωτεινό θέμα")
        self.chk_tooltips.setText("" if collapsed else "Βοηθητικά μηνύματα")
        self._layout.setContentsMargins(*((8, 10, 8, 10) if collapsed
                                          else (10, 10, 10, 10)))

        target = NARROW if collapsed else self._wide
        if not animate:
            self.setFixedWidth(target)
        else:
            # Το πλάτος εκκίνησης διαβάζεται ΠΡΙΝ ξεκλειδώσουμε το maximumWidth:
            # αν το διαβάζαμε μετά, στο άνοιγμα το layout είχε ήδη επεκταθεί στο
            # WIDE, οπότε start == end και η κίνηση δεν φαινόταν — το μενού
            # «πεταγόταν» ανοιχτό ενώ το κλείσιμο κινούνταν ομαλά. Κρατάμε το
            # maximumWidth στο σημείο εκκίνησης ώστε να μην πηδήξει, και μετά το
            # κινούμε: έτσι άνοιγμα και κλείσιμο έχουν ακριβώς το ίδιο εφέ.
            start = self.width()
            self.setMinimumWidth(0)
            self.setMaximumWidth(start)
            anim = QPropertyAnimation(self, b"maximumWidth", self)
            anim.setDuration(160)
            anim.setStartValue(start)
            anim.setEndValue(target)
            anim.setEasingCurve(QEasingCurve.Type.OutCubic)
            anim.finished.connect(lambda: self.setFixedWidth(target))
            anim.start(QPropertyAnimation.DeletionPolicy.DeleteWhenStopped)
        self.collapsed_changed.emit(collapsed)

    # ------------------------------------------------------------------ API
    def restyle(self) -> None:
        """Μετά από αλλαγή θέματος: τα εικονίδια είναι bitmaps σε συγκεκριμένο
        χρώμα και δεν αλλάζουν μόνα τους από το stylesheet."""
        for button in self._buttons.values():
            button.restyle()
        self.btn_toggle.setIcon(icon("menu", CURRENT.muted))
        self.logo.setPixmap(logo_pixmap(38))
        self._paint_brand()
        self.chk_light.update()
        self.chk_tooltips.update()

    def set_active(self, name: str) -> None:
        """Σημαδεύει πού βρίσκεται ο χρήστης (μόνο οι σελίδες — τα υπόλοιπα κουμπιά είναι ενέργειες)."""
        for key, button in self._buttons.items():
            if key in self._pages:
                button.set_active(key == name)

    def set_enabled_action(self, name: str, enabled: bool) -> None:
        if name in self._buttons:
            self._buttons[name].setEnabled(enabled)

    def button(self, name: str) -> MenuButton | None:
        return self._buttons.get(name)
