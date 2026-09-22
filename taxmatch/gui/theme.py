"""Θέματα εμφάνισης — σκούρο και φωτεινό.

Το σκούρο κρατά την παλέτα του το αδελφό εργαλείο, ώστε τα δύο εργαλεία να
μοιάζουν. Το φωτεινό είναι ο ίδιος σκελετός με αντεστραμμένες τιμές.

Τα χρώματα εκτίθενται ως module-level ονόματα (ACCENT, OK, BAD…) γιατί τα
χρησιμοποιεί κώδικας που βάφει κελιά πινάκων στη στιγμή. Το `apply_theme()` τα
ξαναγράφει, οπότε όποιος διαβάζει `theme.OK` παίρνει πάντα το τρέχον θέμα.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Palette:
    name: str
    bg: str
    panel: str
    panel_alt: str
    chip: str
    line: str
    txt: str
    muted: str
    accent: str
    accent_deep: str
    on_accent: str
    ok: str
    bad: str
    warn: str
    menu_bg: str
    log_bg: str
    log_txt: str
    tile_hover: str


DARK = Palette(
    name="dark",
    bg="#0b1220",
    panel="#131f33",
    panel_alt="#16233a",
    chip="#0b2942",
    line="#2b3b54",
    txt="#e6edf6",
    muted="#93a4bd",
    accent="#38bdf8",
    accent_deep="#0ea5e9",
    on_accent="#04222f",
    ok="#22c55e",
    bad="#ef4444",
    warn="#f59e0b",
    menu_bg="#0a111e",
    log_bg="#08101c",
    log_txt="#c7d5e8",
    tile_hover="#182742",
)

LIGHT = Palette(
    name="light",
    bg="#f4f7fb",
    panel="#ffffff",
    panel_alt="#f0f4f9",
    chip="#e2effa",
    line="#d3dde9",
    txt="#0d2340",
    muted="#5f7285",
    accent="#0e7fbf",
    accent_deep="#0ea5e9",
    on_accent="#ffffff",
    # Πιο σκούρα από το σκούρο θέμα: το #22c55e σε λευκό φόντο δεν διαβάζεται.
    ok="#15803d",
    bad="#c81e1e",
    warn="#b45309",
    menu_bg="#e9eff7",
    log_bg="#ffffff",
    log_txt="#22364f",
    tile_hover="#eaf4fd",
)

RADIUS = "14px"


class _LivePalette:
    """Ζωντανή όψη της τρέχουσας παλέτας.

    Υπάρχει για έναν λόγο: τα modules γράφουν `from .theme import CURRENT`, που
    δένει το ίδιο το *αντικείμενο* τη στιγμή του import. Αν το `apply_theme`
    ξανάδενε απλώς το όνομα `CURRENT`, το `theme.CURRENT` θα γινόταν LIGHT ενώ
    το `side_menu.CURRENT` θα έμενε DARK για πάντα — και το φωτεινό θέμα θα
    ζωγράφιζε εικονίδια με `#93a4bd` πάνω σε ανοιχτό φόντο, δηλαδή αόρατα.

    Έτσι το αντικείμενο μένει το ίδιο και αλλάζει από μέσα· όποιος κρατά
    αναφορά, βλέπει πάντα το τρέχον θέμα.
    """

    __slots__ = ("_palette",)

    def __init__(self, palette: Palette) -> None:
        object.__setattr__(self, "_palette", palette)

    def _swap(self, palette: Palette) -> None:
        object.__setattr__(self, "_palette", palette)

    def __getattr__(self, field: str) -> str:
        # Το _palette βρίσκεται από το slot descriptor, οπότε δεν αναδρομεί.
        return getattr(object.__getattribute__(self, "_palette"), field)

    def __repr__(self) -> str:
        return f"<CURRENT {self._palette.name}>"


#: Το τρέχον θέμα. Το `apply_theme` αλλάζει το περιεχόμενό του, ποτέ το όνομα.
CURRENT = _LivePalette(DARK)

# Συμβατά ονόματα για κώδικα που βάφει κελιά.
BG = CURRENT.bg
PANEL = CURRENT.panel
CHIP = CURRENT.chip
LINE = CURRENT.line
TXT = CURRENT.txt
MUTED = CURRENT.muted
ACCENT = CURRENT.accent
ACCENT_DEEP = CURRENT.accent_deep
OK = CURRENT.ok
BAD = CURRENT.bad
WARN = CURRENT.warn


def _refresh_names(p: Palette) -> None:
    """Ενημερώνει το CURRENT και τα συμβατά ονόματα.

    Προσοχή: τα BG/PANEL/OK/… είναι απλά strings και **ξαναδένονται**. Όποιος τα
    κάνει `from .theme import OK` κρατά την παλιά τιμή. Χρησιμοποιήστε
    `CURRENT.ok`, που είναι ζωντανό.
    """
    global BG, PANEL, CHIP, LINE, TXT, MUTED, ACCENT, ACCENT_DEEP, OK, BAD, WARN
    CURRENT._swap(p)
    BG, PANEL, CHIP, LINE = p.bg, p.panel, p.chip, p.line
    TXT, MUTED = p.txt, p.muted
    ACCENT, ACCENT_DEEP = p.accent, p.accent_deep
    OK, BAD, WARN = p.ok, p.bad, p.warn


def build(p: Palette) -> str:
    # Το ✓ γεννιέται εδώ γιατί το χρώμα του εξαρτάται από το θέμα. Η εισαγωγή
    # είναι τοπική: το icons.py χρειάζεται QGuiApplication, ενώ αυτό το module
    # φορτώνεται και από κώδικα χωρίς GUI (π.χ. tests του CLI).
    from .icons import arrow_image, calendar_image, indicator_image

    check = indicator_image(p.on_accent)
    arrow = arrow_image(p.muted)
    cal = calendar_image(p.accent)
    return f"""
QWidget {{ background: {p.bg}; color: {p.txt}; font-size: 13px; }}
QMainWindow, QDialog {{ background: {p.bg}; }}

QLabel {{ background: transparent; }}
QLabel#h1 {{ font-size: 19px; font-weight: 600; color: {p.txt}; }}
QLabel#muted {{ color: {p.muted}; }}
QLabel#stat {{ font-size: 22px; font-weight: 700; color: {p.accent}; }}
QLabel#statLabel {{ color: {p.muted}; font-size: 11px; }}
QLabel#hint {{ color: {p.accent}; font-size: 11px; }}

QFrame#card {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: {RADIUS};
}}
QFrame#line {{ background: {p.line}; max-height: 1px; border: none; }}
QFrame#banner {{
    background: {p.chip};
    border: 1px solid {p.accent};
    border-radius: 9px;
}}

QPushButton {{
    background: {p.chip};
    color: {p.txt};
    border: 1px solid {p.line};
    border-radius: 9px;
    padding: 7px 14px;
    font-weight: 600;
}}
QPushButton:hover  {{ border-color: {p.accent}; color: {p.accent}; }}
QPushButton:pressed {{ background: {p.line}; }}
QPushButton:disabled {{ color: {p.muted}; border-color: {p.line}; background: {p.panel}; }}

QPushButton#primary {{
    background: {p.accent_deep};
    border: 1px solid {p.accent};
    color: {p.on_accent};
}}
QPushButton#primary:hover {{ background: {p.accent}; }}
QPushButton#primary:disabled {{
    background: {p.panel}; color: {p.muted}; border-color: {p.line};
}}

QPushButton#danger {{ border-color: {p.bad}; color: {p.bad}; }}
QPushButton#danger:hover {{ background: {p.bad}; color: {p.on_accent}; }}

QLineEdit, QComboBox, QDateEdit, QSpinBox {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: 9px;
    padding: 6px 9px;
    color: {p.txt};
    selection-background-color: {p.accent_deep};
    selection-color: {p.on_accent};
}}
QLineEdit:focus, QComboBox:focus, QDateEdit:focus {{ border-color: {p.accent}; }}
QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled {{ color: {p.muted}; }}
QComboBox::drop-down, QDateEdit::drop-down {{
    border: none; width: 20px;
    subcontrol-origin: padding; subcontrol-position: center right;
}}
QComboBox::down-arrow {{
    image: url("{arrow}"); width: 12px; height: 12px;
}}
/* Ημερολόγιο, όχι βελάκι: το πεδίο ανοίγει ημερολόγιο και το βελάκι «κάτω»
   υπόσχεται λίστα. Στο χρώμα του τόνου, ώστε να διαβάζεται ως κουμπί. */
QDateEdit::drop-down {{ width: 24px; }}
QDateEdit::down-arrow {{
    image: url("{cal}"); width: 14px; height: 14px;
}}
QComboBox QAbstractItemView {{
    background: {p.panel};
    /* Το `color` είναι απαραίτητο: χωρίς αυτό η λίστα του popup παίρνει το
       προεπιλεγμένο (σκούρο) χρώμα κειμένου της πλατφόρμας πάνω στο σκούρο
       panel, και ο χρήστης βλέπει ένα άδειο κουτί. */
    color: {p.txt};
    border: 1px solid {p.line};
    selection-background-color: {p.chip};
    selection-color: {p.txt};
    outline: none;
}}
/* Ίδιο θέμα στο ίδιο το item view του popup, που σε κάποια στυλ είναι
   ξεχωριστό widget και δεν κληρονομεί το παραπάνω. */
QComboBox QListView {{ background: {p.panel}; color: {p.txt}; }}
QComboBox QAbstractItemView::item {{ color: {p.txt}; }}
QComboBox QAbstractItemView::item:selected {{
    background: {p.chip}; color: {p.txt};
}}
QCalendarWidget QWidget {{ alternate-background-color: {p.panel_alt}; }}
QCalendarWidget QAbstractItemView:enabled {{
    background: {p.panel}; color: {p.txt};
    selection-background-color: {p.accent_deep};
    selection-color: {p.on_accent};
}}
QCalendarWidget QToolButton {{ color: {p.txt}; background: transparent; }}
QCalendarWidget QMenu {{ background: {p.panel}; color: {p.txt}; }}

QCheckBox {{ spacing: 7px; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 2px solid {p.muted};
    border-radius: 4px;
    background: {p.panel};
}}
QCheckBox::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked {{
    background: {p.accent_deep};
    border-color: {p.accent};
    image: url({check});
}}
QCheckBox::indicator:disabled {{ background: {p.panel_alt}; border-color: {p.line}; }}

QTableWidget {{
    background: {p.panel};
    alternate-background-color: {p.panel_alt};
    border: 1px solid {p.line};
    border-radius: {RADIUS};
    gridline-color: {p.line};
    selection-background-color: {p.chip};
    selection-color: {p.txt};
    outline: none;
}}
QTableWidget::item {{ padding: 5px 7px; border: none; }}
QTableWidget::item:selected {{ background: {p.chip}; }}
QTableWidget::indicator {{
    width: 16px; height: 16px;
    border: 2px solid {p.muted};
    border-radius: 4px;
    background: {p.panel};
}}
QTableWidget::indicator:hover {{ border-color: {p.accent}; }}
QTableWidget::indicator:checked {{
    background: {p.accent_deep};
    border-color: {p.accent};
    image: url({check});
}}
QTableWidget::indicator:disabled {{ background: {p.panel_alt}; border-color: {p.line}; }}

/* Οι λίστες με κουτάκια (π.χ. «Πελάτες» στον Χρονοπρογραμματισμό) δεν είχαν
   ΚΑΝΕΝΑΝ κανόνα: το κουτάκι έμενε στο προεπιλεγμένο του Qt, ένα αχνό
   περίγραμμα που πάνω στο σκοτεινό θέμα μόλις που διακρινόταν. */
QListWidget::indicator {{
    width: 16px; height: 16px;
    border: 2px solid {p.muted};
    border-radius: 4px;
    background: {p.panel};
}}
QListWidget::indicator:hover {{ border-color: {p.accent}; }}
QListWidget::indicator:checked {{
    background: {p.accent_deep};
    border-color: {p.accent};
    image: url({check});
}}
QListWidget::indicator:disabled {{ background: {p.panel_alt}; border-color: {p.line}; }}
QListWidget::item {{ padding: 4px 2px; }}
/* Οι λίστες με πλαίσιο, όπως οι πίνακες: χωρίς αυτό η λίστα «Πελάτες» δεν
   ξεχώριζε καθόλου από το κουτί γύρω της στο σκοτεινό θέμα. */
QListWidget#scheduleClients {{
    background: {p.panel};
    alternate-background-color: {p.panel_alt};
    border: 1px solid {p.line};
    border-radius: 9px;
    padding: 4px;
    outline: none;
}}
/* Τα κουτιά (π.χ. «Πότε / Πελάτες / Κατάσταση» στον Χρονοπρογραμματισμό)
   έπαιρναν το native περίγραμμα των Windows: γκρι σε σχεδόν μαύρο, αόρατο στο
   σκοτεινό θέμα. Ρητό περίγραμμα και τίτλος στο χρώμα του τόνου. */
QGroupBox {{
    border: 1px solid {p.line};
    border-radius: 11px;
    margin-top: 12px;
    padding-top: 8px;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: {p.accent};
    font-weight: 700;
}}
QHeaderView::section {{
    background: {p.bg};
    color: {p.muted};
    border: none;
    border-bottom: 1px solid {p.line};
    padding: 7px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background: {p.bg}; border: none; }}

QPlainTextEdit {{
    background: {p.log_bg};
    border: 1px solid {p.line};
    border-radius: {RADIUS};
    padding: 7px;
    font-family: Consolas, "Cascadia Mono", monospace;
    font-size: 12px;
    color: {p.log_txt};
}}

QProgressBar {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: 7px;
    height: 15px;
    text-align: center;
    color: {p.txt};
}}
QProgressBar::chunk {{ background: {p.accent_deep}; border-radius: 6px; }}

QScrollBar:vertical {{
    background: transparent; width: 10px; margin: 2px 2px 2px 0; border: none;
}}
QScrollBar::handle:vertical {{ background: {p.line}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {p.muted}; }}
QScrollBar:horizontal {{
    background: transparent; height: 10px; margin: 0 2px 2px 2px; border: none;
}}
QScrollBar::handle:horizontal {{ background: {p.line}; border-radius: 4px; min-width: 30px; }}
QScrollBar::handle:horizontal:hover {{ background: {p.muted}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; border: none; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QSplitter::handle {{ background: transparent; }}
QSplitter::handle:horizontal {{ width: 7px; }}
QSplitter::handle:vertical {{ height: 7px; }}
QSplitter::handle:hover {{ background: {p.accent_deep}; }}
QSplitter::handle:pressed {{ background: {p.accent}; }}

QStatusBar {{ background: {p.bg}; color: {p.muted}; border-top: 1px solid {p.line}; }}
QToolTip {{
    background: {p.panel};
    color: {p.txt};
    border: 1px solid {p.accent};
    border-radius: 7px;
    padding: 5px;
}}
QMenu {{ background: {p.panel}; border: 1px solid {p.line}; padding: 4px; }}
QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 6px; }}
QMenu::item:selected {{ background: {p.chip}; color: {p.accent}; }}
QMenu::separator {{ height: 1px; background: {p.line}; margin: 4px 8px; }}

/* ---------- Πλαϊνό μενού ---------- */
QWidget#sideMenu {{ background: {p.menu_bg}; border-right: 1px solid {p.line}; }}
QLabel#menuTitle {{ font-size: 17px; font-weight: 800; color: {p.accent}; }}
QLabel#menuSubtitle {{ font-size: 10px; color: {p.muted}; }}
QLabel#menuSection {{
    font-size: 10px; font-weight: 700; color: {p.muted}; letter-spacing: 1px;
}}
QLabel#menuVersion {{ font-size: 12px; color: {p.muted}; padding: 2px 4px; border-radius: 6px; }}
QLabel#menuVersion:hover {{ color: {p.accent}; background: {p.tile_hover}; }}

QPushButton#menuButton {{
    background: transparent; border: none; border-radius: 8px;
    /* 6px και όχι 8: με 14 κουμπιά, τα 2px ανά κουμπί είναι 28px συνολικά —
       αρκετά ώστε το μενού να χωρά χωρίς να συμπιέζεται η κεφαλίδα. */
    padding: 6px 10px; text-align: left; font-weight: 600; color: {p.muted};
}}
QPushButton#menuButton:hover {{ background: {p.panel}; color: {p.txt}; }}
QPushButton#menuButton[active="true"] {{ background: {p.chip}; color: {p.accent}; }}
QPushButton#menuButton:disabled {{ color: {p.line}; }}
QPushButton#menuToggle {{
    background: transparent; border: none; border-radius: 8px; padding: 6px;
}}
QPushButton#menuToggle:hover {{ background: {p.panel}; }}

/* ---------- Ξενάγηση ---------- */
QFrame#tourCard {{
    background: {p.panel};
    border: 2px solid {p.accent};
    border-radius: {RADIUS};
}}
QLabel#tourTitle {{ font-size: 15px; font-weight: 700; color: {p.accent}; }}
QLabel#tourStep {{ font-size: 11px; color: {p.muted}; }}

/* ---------- Πλακίδια ---------- */
QPushButton#tile {{
    background: {p.panel};
    border: 1px solid {p.line};
    border-radius: 11px;
    text-align: left;
    padding: 0;
}}
QPushButton#tile:hover {{ border-color: {p.accent}; background: {p.tile_hover}; }}
QPushButton#tile:pressed {{ background: {p.chip}; }}

QPushButton#linkButton {{
    background: transparent; border: none; padding: 2px 0; text-align: left;
}}
QPushButton#linkButton:hover {{ text-decoration: underline; }}

QPushButton#rowButton {{
    background: transparent; border: 1px solid {p.line}; border-radius: 6px; padding: 0;
}}
QPushButton#rowButton:hover {{ border-color: {p.accent}; background: {p.chip}; }}
QPushButton#rowButton:disabled {{ border-color: transparent; }}
"""


def apply_theme(app, name: str) -> Palette:
    """Εφαρμόζει θέμα σε ολόκληρη την εφαρμογή και ενημερώνει τα χρώματα."""
    palette = LIGHT if name == "light" else DARK
    _refresh_names(palette)
    app.setStyleSheet(build(palette))
    return palette


def _colorref(hex_color: str) -> int:
    """#RRGGBB -> COLORREF (0x00BBGGRR). Τα Windows θέλουν ΑΝΑΠΟΔΑ bytes."""
    value = hex_color.lstrip("#")
    r, g, b = int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)
    return (b << 16) | (g << 8) | r


def paint_title_bar(window, dark: bool) -> bool:
    """Βάφει τη γραμμή τίτλου (minimize/close) στα χρώματα ΤΟΥ ΘΕΜΑΤΟΣ.

    Η γραμμή τίτλου ανήκει στα Windows, όχι στο Qt: κανένα stylesheet δεν τη
    φτάνει, γι' αυτό έμενε λευκή πάνω από σκούρα εφαρμογή.

    Δύο βήματα, και το δεύτερο είναι που λείπει συνήθως:

    * **σκούρη λειτουργία** (attribute 20, Windows 10 20H1+) — δίνει τη γκρίζα
      γραμμή του συστήματος, `#202020`. Καλύτερη από λευκή, αλλά ΔΕΝ είναι το
      χρώμα της εφαρμογής: πάνω από το ναυτικό μπλε του μενού φαίνεται σαν
      ξένο κομμάτι κολλημένο στην κορυφή.
    * **ρητά χρώματα** (34/35/36, Windows 11 22000+) — φόντο, κείμενο και
      περίγραμμα παίρνουν ΤΙΣ ΤΙΜΕΣ ΤΗΣ ΠΑΛΕΤΑΣ, οπότε το παράθυρο διαβάζεται
      σαν ένα πράγμα.

    Σε παλιότερα Windows τα 34/35/36 γυρίζουν σφάλμα και αγνοούνται σιωπηλά —
    μένει η σκούρη λειτουργία, που είναι ήδη σωστή. Επιστρέφει αν εφαρμόστηκε
    οτιδήποτε.
    """
    if os.name != "nt":
        return False
    palette = DARK if dark else LIGHT
    done = False
    try:
        import ctypes

        handle = ctypes.c_void_p(int(window.winId()))
        dwm = ctypes.windll.dwmapi

        def send(attribute: int, value) -> bool:
            return dwm.DwmSetWindowAttribute(
                handle, ctypes.c_int(attribute), ctypes.byref(value), ctypes.sizeof(value)
            ) == 0

        # 20 από το build 19041· 19 στα πρώτα insider builds. Δοκιμάζουμε και τα
        # δύο: το λάθος attribute απλώς γυρίζει σφάλμα, δεν χαλάει το παράθυρο.
        mode = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):
            if send(attribute, mode):
                done = True
                break

        for attribute, color in (
            (35, palette.menu_bg),   # DWMWA_CAPTION_COLOR — ίδιο με το πλαϊνό μενού
            (36, palette.txt),       # DWMWA_TEXT_COLOR
            (34, palette.line),      # DWMWA_BORDER_COLOR
        ):
            if send(attribute, ctypes.c_uint(_colorref(color))):
                done = True
    except (OSError, AttributeError, ValueError):
        # Χωρίς dwmapi (Wine, παλιά Windows) η εφαρμογή δουλεύει μια χαρά με
        # λευκή γραμμή τίτλου — δεν είναι λόγος να μην ανοίξει.
        pass
    return done


def install_title_bar_painter(app) -> object:
    """Βάφει τη γραμμή τίτλου **κάθε** παραθύρου, όχι μόνο του κεντρικού.

    Το `paint_title_bar` καλούνταν στο `showEvent` του κύριου παραθύρου. Κάθε
    διάλογος όμως είναι δικό του παράθυρο με δική του γραμμή τίτλου, οπότε η
    προεπισκόπηση εκτύπωσης, τα μηνύματα και οι φόρμες άνοιγαν με **λευκή**
    μπάρα πάνω από σκούρα εφαρμογή — σαν ξένο κομμάτι κολλημένο στην κορυφή.

    Στήνεται ως φίλτρο συμβάντων στην εφαρμογή: ό,τι παράθυρο εμφανιστεί,
    βάφεται. Δεν χρειάζεται να το θυμάται κανείς σε κάθε νέο διάλογο.

    Κρατήστε την επιστροφή ζωντανή — αλλιώς τον μαζεύει ο garbage collector και
    το φίλτρο παύει σιωπηλά.
    """
    from PySide6.QtCore import QEvent, QObject

    class Painter(QObject):
        def eventFilter(self, watched, event) -> bool:  # noqa: N802 (Qt API)
            if event.type() == QEvent.Type.Show:
                try:
                    if watched.isWindow():
                        paint_title_bar(watched, CURRENT.name != "light")
                except (RuntimeError, AttributeError):
                    # Widget που μόλις καταστράφηκε, ή αντικείμενο χωρίς
                    # isWindow (το φίλτρο βλέπει ΚΑΘΕ QObject της εφαρμογής).
                    pass
            return False

    painter = Painter()
    app.installEventFilter(painter)
    app._title_bar_painter = painter  # noqa: SLF001
    return painter


def repaint_title_bars(dark: bool) -> None:
    """Ξαναβάφει ό,τι είναι ήδη ανοιχτό, μετά από αλλαγή θέματος."""
    from PySide6.QtWidgets import QApplication

    for window in QApplication.topLevelWidgets():
        if window.isWindow():
            paint_title_bar(window, dark)


def money(value: float) -> str:
    """Ελληνική μορφή: 1.234,56"""
    return f"{value:,.2f}".replace(",", "~").replace(".", ",").replace("~", ".")
