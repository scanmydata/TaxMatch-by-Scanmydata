"""Κύριο παράθυρο — native (PySide6), χωρίς Flask/webview. Οι σελίδες μιλούν απευθείας στα ίδια modules που
χρησιμοποιούσε το web UI (business_profiles.service, matching.engine, deadlines, pipeline, notices, …) — καμία
HTTP κλήση, κανένα token. Ό,τι αγγίζει δίκτυο/LLM/ΑΑΔΕ τρέχει σε background thread (`workers.run_task`)."""
from __future__ import annotations

import calendar as pycal
import html
import json
import logging
import os
import sqlite3
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from PySide6.QtCore import QEvent, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTableWidget, QToolButton,
    QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import APP_TITLE, __version__, config, crypto, deadlines, easycheck, efka, logs, notices as notices_mod, obligations, office, pipeline, scheduler_win, settings_store
from .. import backup as backup_mod
from .. import fee_portals, handoff
from .. import intrastat as intrastat_mod
from .. import fiscal
from .. import members as members_mod
from ..aml import cases as aml_cases, store as aml_store
from .. import db as dbmod
from ..business_profiles import credentials as client_creds, import_excel, lookup_aade, service as clients, vies
from ..extraction import llm_extract
from ..http import make_session
from ..identifiers import is_valid_afm, kad_digits, normalize_afm
from ..ingestion import sources, taxheaven_calendar
from ..matching import engine, ml
from . import i18n
from .aml_page import AmlPage
from .busy import BusyOverlay
from .client_detail_dialog import ClientDetailDialog
from .client_dialog import ClientDialog
from .email_settings import EmailSettingsBox
from .import_dialog import ImportPreviewDialog
from .job_queue import JobQueue
from .icons import dot_icon, icon, logo_pixmap
from .manual import ensure_manual
from .news_dialog import NewsDialog
from .side_menu import SideMenu
from .table_kit import SortItem, TableKit, sort_key
from .table_filter import TableColumnFilter, install_cell_menu
from .theme import CURRENT, apply_theme, paint_title_bar
from .toast import ProgressToast, toast
from .tour import Step, Tour
from .office_widgets import OfficeSettingsBox, ServicesBox
from .tray import Tray
from . import unlock
from .widgets import ToggleSwitch, due_badge, kind_colour, resort, setup_columns
from .workers import run_task

log = logging.getLogger(__name__)

_W_CHK, _W_STATUS = 0, 6           # πίνακας «Εργασίες / Υπόχρεοι» του Ημερολογίου


class _WorkCard(QFrame):
    """Κάρτα εργασίας του Ημερολογίου (Ανά εργασία → Εργασίες): κείμενο που αναδιπλώνεται, κλικ = υπόχρεοι."""
    clicked = Signal()

    def __init__(self, text: str, colour: str) -> None:
        super().__init__()
        self.setObjectName("workCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum)
        self.setStyleSheet(f"QFrame#workCard {{ background:{CURRENT.panel}; border:1px solid {CURRENT.line}; "
                           f"border-radius:10px; }} QFrame#workCard:hover {{ border:1px solid {colour}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setStyleSheet(f"color:{colour}; border:none; background:transparent;")
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        label.setMinimumHeight(60)
        lay.addWidget(label)

    def mouseReleaseEvent(self, event) -> None:          # noqa: N802 — Qt API
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)

_CLIENT_COLS = [
    ("", 30, "Επιλέξτε για μαζικές ενέργειες"), ("ΑΦΜ", 90, ""), ("Επωνυμία", 0, "Διπλό κλικ για την καρτέλα"),
    ("Είδος", 130, ""), ("Κατάσταση", 90, ""), ("Κύριος ΚΑΔ", 110, ""), ("Βιβλία", 55, ""),
    ("ΦΠΑ", 95, "Περίοδος ΦΠΑ: μηνιαίος / τριμηνιαίος (Μητρώο ΑΑΔΕ, περίοδοι Φ2 ή κατηγορία βιβλίων) · «Όχι» = μη υπόχρεος"),
    ("Στοιχεία", 80, "Πληρότητα ανάκτησης"), ("TAXISnet", 80, ""), ("Matches", 65, ""),
    ("Αμοιβή/μήνα", 100, "Μηνιαία επαναλαμβανόμενη αμοιβή, προ ΦΠΑ (καρτέλα πελάτη → Αμοιβές)"),
    ("Υποβολές", 90, "Κατάσταση υποβολών του τρέχοντος έτους από την ΑΑΔΕ (✓ υποβλήθηκαν · ✗ δεν υποβλήθηκαν)"),
]
#: Φίλτρο «Εμφάνιση» της σελίδας Πελατών (όπως το «Ποιους πελάτες να δείχνει» του timologio): (ετικέτα, σημαία)
_CLIENT_SHOW = [("Όλοι οι πελάτες", ""), ("Με κωδικούς TAXISnet", "creds"), ("Χωρίς κωδικούς TAXISnet", "nocreds"),
                ("Με ελλιπή στοιχεία", "incomplete"), ("Με υποβολές που λείπουν", "missing"),
                ("Εταιρείες (νομικά πρόσωπα)", "company"), ("Ατομικές επιχειρήσεις", "sole"),
                ("Ιδιώτες", "private"), ("Με διακοπή εργασιών", "ceased"), ("Μέλη εταιρειών", "member"),
                ("Πιθανοί υπόχρεοι τέλους παρεπιδημούντων", "parepid")]
_C_CHK, _C_AFM, _C_NAME, _C_KIND, _C_STATE, _C_KAD, _C_BOOKS, _C_VAT, _C_LOOKUP, _C_CREDS, _C_MATCHES, _C_FEE, _C_CHECKS = range(13)

_NEWS_COLS = [("Πηγή", 130, ""), ("Ημερομηνία", 90, ""), ("Τίτλος", 0, "Διπλό κλικ για προεπισκόπηση"),
             ("Κατάσταση", 110, ""), ("Πελάτες", 60, "")]

TOUR_VERSION = 4                                       # 2: νέα σελίδα «Δέουσα επιμέλεια» · 3: βήματα του φακέλου ΔΕ · 4: λίστα πελατών/ημερολόγιο

_WEEKDAY_HEADS = ("Δε", "Τρ", "Τε", "Πε", "Πα", "Σα", "Κυ")
_GENERAL_NEWS_TOPICS = {"ΕΡΓΑΤΙΚΑ", "ΑΣΦΑΛΙΣΤΙΚΑ", "ΕΠΙΧΟΡΗΓΗΣΕΙΣ", "ΑΛΛΟ"}


_CELL_CHIPS = 3  # πόσα events δείχνονται πλήρη μέσα στο κελί πριν το «+N ακόμη» — όπως στο παλιό .cal .ev του web UI


class _NumItem(QTableWidgetItem):
    """Κελί που ταξινομείται αριθμητικά («1.200,00 €» μετά το «90,00 €», όχι αλφαβητικά)."""

    def __init__(self, text: str, value: float) -> None:
        super().__init__(text)
        self._value = value

    def __lt__(self, other) -> bool:  # noqa: D105
        return self._value < getattr(other, "_value", 0.0)


class _ClientItem(QTableWidgetItem):
    """Κελί του πίνακα πελατών: τα ΜΕΛΗ μιας εταιρείας μένουν ΠΑΝΤΑ ακριβώς κάτω από αυτήν, όποια ταξινόμηση κι αν
    επιλεγεί (ρητό αίτημα χρήστη 2026-10-23 — «dropdown κάτω από την εταιρεία»). Η σύγκριση γίνεται πρώτα με την τιμή
    της ΕΤΑΙΡΕΙΑΣ στη στήλη (`group_key`, ίδια για εταιρεία και μέλη της), μετά: εταιρεία πρώτα, μέλη μετά. Στη
    φθίνουσα ταξινόμηση η Qt καλεί `r < l` — γι' αυτό η σειρά μέσα στην ομάδα αντιστρέφεται εδώ, ώστε να μένει ίδια."""

    def __init__(self, text: str = "", key: Any = None) -> None:
        super().__init__(text)
        self.key = key if key is not None else sort_key(text)
        self.group_key: Any = self.key
        self.group_afm = ""
        self.child = False

    def __lt__(self, other) -> bool:  # noqa: D105
        if not isinstance(other, _ClientItem):
            return super().__lt__(other)
        mine, theirs = (self.group_key, self.group_afm), (other.group_key, other.group_afm)
        if mine != theirs:
            try:
                return mine < theirs
            except TypeError:
                return str(mine) < str(theirs)
        a, b = (self.child, self.key), (other.child, other.key)
        table = self.tableWidget()
        descending = table is not None and table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        try:
            return (b < a) if descending else (a < b)
        except TypeError:
            return (str(b) < str(a)) if descending else (str(a) < str(b))


class _DayCell(QFrame):
    """Ένα κελί ημέρας στο grid του μηνιαίου ημερολογίου: αριθμός + μέχρι `_CELL_CHIPS` πραγματικά events
    (μικρά chips, χρωματισμένα κατά είδος — νέα/κανόνας/ημερολόγιο), όπως έδειχνε το παλιό web UI (`.cal .ev`)
    αντί για ένα ξερό πλήθος."""

    clicked = Signal()

    def __init__(self, day: int, events: list[dict], *, in_month: bool, is_today: bool) -> None:
        super().__init__()
        self._clickable = in_month
        self.setMinimumSize(0, 78)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor if in_month else Qt.CursorShape.ArrowCursor)

        bg = CURRENT.panel if in_month else "transparent"
        border = CURRENT.line if in_month else "transparent"
        ring = f"2px solid {CURRENT.accent_deep}" if is_today else f"1px solid {border}"
        # Hover (ρητό αίτημα 2026-10-23 — «καλύτερο effect»): ανοιχτότερο φόντο + έντονο περίγραμμα σε ΟΛΟ το κελί.
        # Με objectName: το «QFrame:hover» χωρίς αυτό έπιανε και τα παιδιά (QLabel είναι QFrame) — κάθε chip άναβε μόνο του.
        self.setObjectName("dayCell")
        hover = (f"QFrame#dayCell:hover {{ background:{CURRENT.tile_hover}; border:2px solid {CURRENT.accent}; }}"
                 if in_month else "")
        self.setStyleSheet(f"QFrame#dayCell {{ background:{bg}; border:{ring}; border-radius:8px; }} {hover}")

        box = QVBoxLayout(self)
        box.setContentsMargins(6, 4, 6, 4)
        box.setSpacing(2)
        num = QLabel(str(day))
        num.setStyleSheet(
            f"background:transparent; border:none; font-weight:700; "
            f"color:{CURRENT.txt if in_month else CURRENT.muted};"
        )
        num.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        box.addWidget(num)

        for ev in events[:_CELL_CHIPS]:
            n = ev.get("obligated") or 0
            # κόκκινη κουκκίδα: υπάρχουν πελάτες που είναι ΣΙΓΟΥΡΑ υπόχρεοι σε αυτή την προθεσμία
            chip = QLabel((f"<span style='color:{CURRENT.bad}'>●</span> " if n else "") + html.escape(ev["title"]))
            chip.setTextFormat(Qt.TextFormat.RichText)
            chip.setToolTip(ev["title"] + (f"\n● {n} {'υπόχρεος πελάτης' if n == 1 else 'υπόχρεοι πελάτες'}" if n else ""))
            # Ignored: ο τίτλος ΔΕΝ επιβάλλει πλάτος στη στήλη. Πριν, ένας μακρύς τίτλος «φούσκωνε» τη στήλη της ημέρας
            # του και οι στήλες χωρίς γεγονότα (π.χ. Τετάρτη/Κυριακή) στρίμωχναν (αναφορά χρήστη 2026-10-22).
            chip.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            chip.setStyleSheet(
                f"background:{CURRENT.panel_alt}; color:{CURRENT.txt}; font-size:10px; "
                f"border:none; border-left:3px solid {kind_colour(ev['kind'])}; border-radius:3px; padding:1px 4px;"
            )
            box.addWidget(chip)
        extra = len(events) - _CELL_CHIPS
        if extra > 0:
            more = QLabel(f"+{extra} ακόμη")
            more.setStyleSheet(f"background:transparent; border:none; color:{CURRENT.muted}; font-size:10px;")
            more.setToolTip("\n".join(ev["title"] for ev in events[_CELL_CHIPS:]))
            more.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            box.addWidget(more)
        box.addStretch()

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt API)
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


def _section_header(text: str, icon_name: str) -> QWidget:
    """Τίτλος ενότητας με μικρό εικονίδιο μπροστά — όπως τα `<h2>{{ icon(...) }} …` του παλιού web UI."""
    holder = QWidget()
    row = QHBoxLayout(holder)
    row.setContentsMargins(0, 0, 0, 0)
    row.setSpacing(6)
    pic = QLabel()
    pic.setPixmap(icon(icon_name, CURRENT.muted, 14).pixmap(14, 14))
    row.addWidget(pic)
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    row.addWidget(lbl)
    row.addStretch()
    return holder


def _reveal(path: Path) -> None:
    import os
    import subprocess
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _clients_suffix(ev: dict) -> str:
    """« — 5 πελάτες (+2 υπό προϋπ.)» δίπλα στον τίτλο μιας προθεσμίας, όταν ξέρουμε ποιους αφορά."""
    n, maybe = ev.get("n_clients"), ev.get("n_maybe") or 0
    if n is None or ev.get("kind") == "aml":
        return ""
    if not n and not maybe:
        return "  —  κανένας πελάτης"
    done, missing = ev.get("n_submitted") or 0, ev.get("n_missing") or 0
    checks = (f"  ·  ✓{done}" if done else "") + (f" ✗{missing}" if missing else "")
    return f"  —  {n} πελάτες" + (f" (+{maybe} υπό προϋπ.)" if maybe else "") + checks


class MainWindow(QMainWindow):
    def __init__(self, *, force_show: bool = False) -> None:
        super().__init__()
        self._force_show = force_show
        self.setWindowTitle(APP_TITLE)
        screen = self.screen()
        if screen is not None:
            avail = screen.availableGeometry()
            self.setMinimumSize(min(1180, avail.width()), min(680, avail.height()))
            w, h = min(1340, avail.width()), min(840, avail.height())
            self.resize(w, h)
            self.move(avail.x() + (avail.width() - w) // 2, avail.y() + (avail.height() - h) // 2)
        else:
            self.resize(1340, 840)

        self.conn = dbmod.connect()
        try:
            # διαγραμμένη ατομική στο ΓΕΜΗ = ιδιώτης — διορθώνει ήδη αποθηκευμένους χωρίς νέα ανάκτηση (τοπικά δεδομένα)
            clients.reapply_cached_gemi(self.conn)
        except Exception as exc:                       # noqa: BLE001 — ποτέ εμπόδιο στο άνοιγμα της εφαρμογής
            log.warning("reapply_cached_gemi: %s", exc)
        # Προτιμήσεις οθόνης (θέμα, πλάτη/σειρά στηλών). Με `TAXMATCH_DATA_DIR` (tests, δοκιμές με προσωρινά δεδομένα)
        # γράφονται ΔΙΠΛΑ στη βάση — πριν πήγαιναν πάντα στο μητρώο του χρήστη και κάθε test «πατούσε» τα πλάτη
        # στηλών/το θέμα της πραγματικής εγκατάστασης.
        if os.getenv("TAXMATCH_DATA_DIR"):
            self._prefs = QSettings(str(config.data_dir() / "ui_prefs.ini"), QSettings.Format.IniFormat)
        else:
            self._prefs = QSettings("scanmydata", "TaxMatch")
        self._tasks: list[Any] = []                        # κρατά ζωντανά τα background tasks (βλ. workers.run_task)
        # «Δείτε τους πελάτες» από ειδοποίηση: η λίστα δείχνει ΜΟΝΟ αυτούς ώσπου να σβηστούν τα φίλτρα
        self._notice_afms: Optional[set[str]] = None
        # Η ειδοποίηση «επιστροφή στο τρίμηνο» αναγνώστηκε σε ΑΥΤΗ τη συνεδρία → ο αστερίσκος ΦΠΑ μένει ώς την επανεκκίνηση
        self._expanded: set[str] = set()                    # εταιρείες με ανοιχτά τα μέλη τους στη λίστα πελατών
        # Χρήση εργασίας (fiscal.py — ίδιοι κανόνες με το timologio): χειροκίνητη επιλογή μόνο μέσα στο έτος της
        self._fiscal_year = fiscal.resolve_year(self._prefs.value("fiscal/year", 0, type=int),
                                                self._prefs.value("fiscal/chosen_in", 0, type=int))
        self._client_flags: dict[str, set[str]] = {}         # ΑΦΜ → κατηγορίες του φίλτρου «Εμφάνιση» (reload_clients)
        self._client_parents: dict[str, str] = {}
        self._client_detail_open = False
        self._recent_client_detail_afm = ""
        self._recent_client_detail_timer = QTimer(self)
        self._recent_client_detail_timer.setSingleShot(True)
        self._recent_client_detail_timer.setInterval(700)
        self._recent_client_detail_timer.timeout.connect(lambda: setattr(self, "_recent_client_detail_afm", ""))
        self.jobs = JobQueue(self)                          # μακριές εργασίες σε σειρά, με διακοπή (gui/job_queue.py)
        self._tour: Optional[Tour] = None
        self._title_bar_done = False

        theme = str(self._prefs.value("theme", "dark"))
        from PySide6.QtWidgets import QApplication
        apply_theme(QApplication.instance(), theme)

        self._build_ui()
        self.menu.chk_light.blockSignals(True)
        self.menu.chk_light.setChecked(theme == "light")
        self.menu.chk_light.blockSignals(False)
        self.busy = BusyOverlay(self)
        self._setup_tray()
        self.reload_all()
        QTimer.singleShot(400, self._maybe_first_run_tour)
        self._sched_timer = QTimer(self)
        self._sched_timer.setInterval(60_000)
        self._sched_timer.timeout.connect(self._check_schedule)
        self._sched_timer.start()

    # ------------------------------------------------------------------ UI σκελετός
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        self.menu = SideMenu()
        self.menu.triggered.connect(self._on_menu)
        self.menu.tooltips_toggled.connect(lambda _on: None)
        self.menu.theme_toggled.connect(self._on_theme)
        self.menu.version_clicked.connect(lambda: None)
        shell.addWidget(self.menu)

        right = QWidget()
        root = QVBoxLayout(right)
        root.setContentsMargins(14, 12, 14, 10)
        root.setSpacing(9)
        shell.addWidget(right, 1)

        quick_row = QHBoxLayout()
        # Η χρήση, πάνω αριστερά και πάντα ορατή — όπως το Timologio Downloader (ρητό αίτημα χρήστη 2026-10-27): ό,τι
        # δείχνουν Πελάτες (στήλη υποβολών), «Έλεγχος υποβολών», «Ανά εργασία» και η καρτέλα πελάτη αφορά αυτήν.
        fiscal_caption = QLabel("ΧΡΗΣΗ")
        fiscal_caption.setObjectName("statLabel")
        quick_row.addWidget(fiscal_caption)
        self.fiscal_combo = QComboBox()
        self.fiscal_combo.setObjectName("fiscalYear")
        self.fiscal_combo.setMinimumWidth(92)
        self.fiscal_combo.setStyleSheet("font-weight:700;")
        self.fiscal_combo.setToolTip("Η χρήση (φορολογικό έτος) στην οποία δουλεύετε.\n\nΑλλάζει μόνη της με την αλλαγή "
                                     "του χρόνου· αλλάξτε την εδώ αν χρειάζεται να δουλέψετε σε προηγούμενη χρήση.")
        self._fill_fiscal_combo()
        self.fiscal_combo.currentIndexChanged.connect(self._on_fiscal_combo)
        quick_row.addWidget(self.fiscal_combo)
        quick_row.addStretch()
        self.quick_search = QLineEdit()
        self.quick_search.setPlaceholderText("Αναζήτηση πελατών (ΑΦΜ, επωνυμία, …)  [Ctrl+K]")
        self.quick_search.setToolTip("Φιλτράρει τη λίστα πελατών σε όλες τις στήλες. Enter: μετάβαση στους Πελάτες "
                                     "(και άνοιγμα της καρτέλας αν μείνει ένας).")
        self.quick_search.setFixedWidth(300)
        self.quick_search.setClearButtonEnabled(True)
        self.quick_search.returnPressed.connect(self._quick_client_search)
        quick_row.addWidget(self.quick_search)
        root.addLayout(quick_row)
        from PySide6.QtGui import QKeySequence, QShortcut
        QShortcut(QKeySequence("Ctrl+K"), self, activated=lambda: (self.quick_search.setFocus(), self.quick_search.selectAll()))

        self.notices_box = QVBoxLayout()
        self.notices_box.setSpacing(6)
        root.addLayout(self.notices_box)

        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self._pages = {
            "dashboard": self._dashboard_page(), "clients": self._clients_page(),
            "calendar": self._calendar_page(), "news": self._news_page(), "aml": AmlPage(self),
            "settings": self._settings_page(),
        }
        self.aml_page: AmlPage = self._pages["aml"]
        for page in self._pages.values():
            self.stack.addWidget(page)

        self.status = QLabel("")
        self.status.setObjectName("muted")
        root.addWidget(self.status)
        self.menu.set_active("dashboard")

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._title_bar_done:
            self._title_bar_done = paint_title_bar(self, not self.menu.chk_light.isChecked())

    def closeEvent(self, event) -> None:  # noqa: N802
        if getattr(self, "tray", None) and not getattr(self, "_really_quit", False) and \
           self._prefs.value("start_minimized", False, type=bool):
            event.ignore()
            self.hide()
            return
        self.conn.close()
        if getattr(self, "tray", None):
            self.tray.hide()
        super().closeEvent(event)
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

    def _setup_tray(self) -> None:
        self.tray = Tray(self, self.windowIcon(), "")
        self.tray.show()
        self._really_quit = False

    # ------------------------------------------------------------------ πλοήγηση
    def _show_page(self, name: str) -> None:
        page = self._pages.get(name)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        self.menu.set_active(name)
        {"dashboard": self.reload_dashboard, "clients": self.reload_clients, "calendar": self.reload_calendar,
         "news": self.reload_news, "aml": self.aml_page.reload, "settings": self.reload_settings}.get(name, lambda: None)()

    def reload_all(self) -> None:
        self._reload_notices()
        self.reload_dashboard()
        self.reload_clients()
        self.reload_calendar()
        self.reload_news()
        self.aml_page.reload()
        self.reload_settings()
        self._refresh_status_bar()

    def after_aml_change(self) -> None:
        """Μετά από αποθήκευση αξιολόγησης δέουσας επιμέλειας: η επανεξέταση εμφανίζεται στο Ημερολόγιο/Αρχική και
        η ειδοποίηση για εκπρόθεσμες/ελλιπείς αξιολογήσεις ενημερώνεται αμέσως."""
        self.aml_page.reload_clients()
        self._render_calendar_from_db()
        self.reload_dashboard()
        self._reload_notices()

    def _refresh_status_bar(self) -> None:
        total = self.conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0]
        complete = self.conn.execute("SELECT COUNT(*) FROM businesses WHERE lookup_status='ok'").fetchone()[0]
        with_creds = self.conn.execute("SELECT COUNT(*) FROM client_credentials WHERE taxis_user!='' AND taxis_pass!=''").fetchone()[0]
        self.status.setText(f"{total} πελάτες · {complete} με πλήρη στοιχεία · {with_creds} με κωδικούς TAXISnet")

    def _reload_notices(self) -> None:
        while self.notices_box.count():
            item = self.notices_box.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for n in notices_mod.collect(self.conn) + self._aml_notices():
            frame = QFrame()
            frame.setObjectName("banner")
            row = QHBoxLayout(frame)
            row.setContentsMargins(10, 8, 10, 8)
            colour = {"danger": CURRENT.bad, "warn": CURRENT.warn}.get(n["level"], CURRENT.accent)
            text = QLabel(n["text"])
            text.setWordWrap(True)
            text.setStyleSheet(f"color:{colour};")
            row.addWidget(text, 1)
            btn = QPushButton(n["label"])
            if n.get("afms"):
                btn.setToolTip("Ανοίγει τους Πελάτες φιλτραρισμένους σε αυτούς της ειδοποίησης")
            btn.clicked.connect(lambda _=False, endpoint=n["endpoint"], afms=n.get("afms"):
                                self._go_from_notice(endpoint, afms))
            row.addWidget(btn)
            if n.get("dismiss"):
                read = QPushButton("Αναγνώστηκε")
                read.setToolTip("Κρύβει την ειδοποίηση· οι πελάτες κρατούν έναν αστερίσκο «*» στη στήλη ΦΠΑ ώς την "
                                "επόμενη εκκίνηση (ή μόνιμα: Ρυθμίσεις → «Μόνιμος αστερίσκος ΦΠΑ»)")
                read.clicked.connect(lambda _=False, key=n["dismiss"]: self._dismiss_notice(key))
                row.addWidget(read)
            self.notices_box.addWidget(frame)

    def _aml_notices(self) -> list[dict]:
        """Ειδοποίηση μόνο για ΕΚΠΡΟΘΕΣΜΕΣ επανεξετάσεις δέουσας επιμέλειας (όχι για «χωρίς αξιολόγηση»: θα ήταν
        μόνιμο banner για κάθε γραφείο που δεν χρησιμοποιεί το module — εκείνο φαίνεται στη σελίδα της). Ζει εδώ και
        όχι στο `notices.collect`, γιατί το web UI δεν έχει σελίδα δέουσας επιμέλειας για να οδηγήσει."""
        out = []
        overdue = aml_store.summary_counts(self.conn)["overdue"]
        if overdue:
            out.append({"level": "warn", "endpoint": "aml", "label": "Δέουσα επιμέλεια",
                        "text": f"Εκπρόθεσμη επανεξέταση δέουσας επιμέλειας σε {overdue} "
                                f"{'πελάτη' if overdue == 1 else 'πελάτες'} (άρθρο 13 παρ. 7 ν. 4557/2018)."})
        pending = aml_cases.attention(self.conn)
        if pending:
            out.append({"level": "danger", "endpoint": "aml", "label": "Υποθέσεις",
                        "text": f"{len(pending)} {'υπόθεση θέλει' if len(pending) == 1 else 'υποθέσεις θέλουν'} ενέργεια: "
                                + "; ".join(f"#{c['id']} {c['reason']}" for c in pending[:3]) + "."})
        return out

    def _go_from_notice(self, endpoint: str, afms: Optional[list[str]] = None) -> None:
        page = {"main.settings": "settings", "main.clients_list": "clients", "aml": "aml"}.get(endpoint, "dashboard")
        self._show_page(page)
        if page == "clients" and afms:
            self._notice_afms = set(afms)
            self._apply_client_filter()

    def _dismiss_notice(self, key: str) -> None:
        if key == "parepid":
            notices_mod.mark_parepid_read(self.conn)
            toast(self, "Η ειδοποίηση κρύφτηκε. Οι πελάτες έχουν αστερίσκο «*» στη στήλη «Κύριος ΚΑΔ» ώσπου να ανοίξετε "
                        "την καρτέλα τους — και βρίσκονται με το φίλτρο «Πιθανοί υπόχρεοι τέλους παρεπιδημούντων».", "ok")
        if key == "vat_quarter":
            notices_mod.mark_vat_quarter_read(self.conn)
            toast(self, "Η ειδοποίηση κρύφτηκε. Οι πελάτες έχουν αστερίσκο «*» στη στήλη ΦΠΑ ώσπου να ανοίξετε την "
                        "καρτέλα τους.", "ok")
        self._reload_notices()
        self.reload_clients()

    # ------------------------------------------------------------------ Αρχική
    def _dashboard_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        head_row = QHBoxLayout()
        title = QLabel("Σημερινό ενημερωτικό")
        title.setObjectName("h1")
        head_row.addWidget(title)
        head_row.addStretch()
        self.last_update_label = QLabel("")
        self.last_update_label.setObjectName("muted")
        head_row.addWidget(self.last_update_label)
        root.addLayout(head_row)

        kpi_row = QHBoxLayout()
        self._kpi_labels: dict[str, QLabel] = {}
        for key, label in (("clients", "πελάτες"), ("articles", "άρθρα (3 ημ.)"), ("matched", "άρθρα που αφορούν πελάτες"),
                          ("urgent", "προθεσμίες ≤ 7 ημερών")):
            box = QFrame()
            box.setObjectName("card")
            box_l = QVBoxLayout(box)
            val = QLabel("0")
            val.setObjectName("stat")
            lab = QLabel(label)
            lab.setObjectName("statLabel")
            box_l.addWidget(val)
            box_l.addWidget(lab)
            self._kpi_labels[key] = val
            kpi_row.addWidget(box)
        root.addLayout(kpi_row)

        run_row = QHBoxLayout()
        run_btn = QPushButton("  Έλεγχος τώρα")
        run_btn.setObjectName("primary")
        run_btn.clicked.connect(self._run_check)
        self._run_buttons = [run_btn]
        run_row.addWidget(run_btn)
        self.run_status = QLabel("")
        self.run_status.setObjectName("muted")
        run_row.addWidget(self.run_status, 1)
        root.addLayout(run_row)

        split = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(_section_header("Πρόσφατα νέα που αφορούν πελάτες", "bell"))
        self.dash_article_tabs = QTabWidget()
        self.dash_tax_articles = QListWidget()
        self.dash_general_articles = QListWidget()
        self.dash_articles = self.dash_tax_articles
        for label, articles in (("Φορολογικά", self.dash_tax_articles), ("Γενικά νέα", self.dash_general_articles)):
            articles.itemClicked.connect(self._open_dash_article)
            articles.setWordWrap(True)
            articles.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            articles.setToolTip("Κλικ: προεπισκόπηση και οι πελάτες που αφορά")
            self.dash_article_tabs.addTab(articles, label)
        left.addWidget(self.dash_article_tabs, 1)
        split.addLayout(left, 3)

        self.dash_deadlines = QListWidget()
        for lst in (self.dash_deadlines,):         # μεγάλοι τίτλοι: αναδίπλωση, όχι κομμένο κείμενο
            lst.setWordWrap(True)
            lst.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            lst.setToolTip("Κλικ: προεπισκόπηση και οι πελάτες που αφορά")
        self.dash_deadlines.itemClicked.connect(self._open_dash_deadline)
        right = QVBoxLayout()
        right.addWidget(_section_header("Προσεχείς προθεσμίες (21 ημέρες)", "calendar"))
        right.addWidget(self.dash_deadlines, 1)
        split.addLayout(right, 2)
        root.addLayout(split, 1)
        return page

    def reload_dashboard(self) -> None:
        today = date.today()
        groups = engine.digest_articles(self.conn, days=3)
        upcoming = deadlines.events_between(self.conn, today, today + timedelta(days=21), include_aml=True)
        urgent = sum(1 for ev in upcoming if (date.fromisoformat(ev["date"]) - today).days <= 7)
        cutoff = (datetime.now().astimezone().astimezone(tz=None) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        articles_n = self.conn.execute("SELECT COUNT(*) FROM articles WHERE COALESCE(published_at,fetched_at)>=?", (cutoff,)).fetchone()[0]
        self._kpi_labels["clients"].setText(str(clients.count(self.conn)))
        self._kpi_labels["articles"].setText(str(articles_n))
        self._kpi_labels["matched"].setText(str(len(groups)))
        self._kpi_labels["urgent"].setText(str(urgent))
        self._update_last_run_label()

        self.dash_tax_articles.clear()
        self.dash_general_articles.clear()
        for g in groups[:25]:
            text = f"{g['title']}  —  {g['n_matches']} πελάτες"
            badge = due_badge(g["deadline"])
            item = QListWidgetItem(dot_icon(kind_colour("news")), text + (f"  ·  {badge[0]}" if badge else ""))
            if badge:
                item.setForeground(QColor(badge[1]))
            item.setData(Qt.ItemDataRole.UserRole, g)
            target = self.dash_general_articles if g.get("topic", "ΑΛΛΟ") in _GENERAL_NEWS_TOPICS else self.dash_tax_articles
            target.addItem(item)

        self.dash_deadlines.clear()
        for ev in upcoming[:25]:
            badge = due_badge(ev["date"])
            item = QListWidgetItem(dot_icon(kind_colour(ev["kind"])),
                                   f"{ev['title']}{_clients_suffix(ev)}" + (f"  ·  {badge[0]}" if badge else ""))
            if badge:
                item.setForeground(QColor(badge[1]))
            item.setData(Qt.ItemDataRole.UserRole, ev)
            self.dash_deadlines.addItem(item)

    def _update_last_run_label(self) -> None:
        row = self.conn.execute(
            "SELECT finished_at, status FROM runs WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            self.last_update_label.setText("Δεν έχει τρέξει ακόμη κανένας έλεγχος.")
            return
        when = logs.format_local(row["finished_at"])
        suffix = "" if row["status"] == "ok" else "  ·  με σφάλματα"
        self.last_update_label.setText(f"Τελευταία ενημέρωση: {when}{suffix}")

    def _open_dash_article(self, item: QListWidgetItem) -> None:
        g = item.data(Qt.ItemDataRole.UserRole)
        people = [{"afm": m["afm"], "name": m["business_name"] or m["afm"], "reason": m["matched_reason"] or "",
                   "status": "yes" if m["confidence"] >= 1.0 else "maybe"} for m in g.get("matches", [])]
        people.sort(key=lambda c: (c["status"] != "yes", c["name"]))
        NewsDialog(g["title"], g["url"], meta=f"{g.get('source', '')} · {(g.get('published_at', '') or '')[:10]}",
                  summary=g.get("summary", ""), action=g.get("action_required") or "", parent=self,
                  clients=people, on_client=self.open_client_detail).exec()

    def _open_dash_deadline(self, item: QListWidgetItem) -> None:
        ev = item.data(Qt.ItemDataRole.UserRole)
        if ev.get("kind") == "aml":
            self.aml_page.open_assessment(ev["afm"])
            return
        self._open_event_dialog(ev)

    def _open_event_dialog(self, ev: dict, summary: Optional[str] = None, action: str = "") -> None:
        """Προεπισκόπηση προθεσμίας ΜΕ τους πελάτες που αφορά (υπόχρεοι / υπό προϋποθέσεις)."""
        kind = {"rule": "κανονική προθεσμία", "general": "ημερολόγιο ΑΑΔΕ/taxheaven", "news": "από άρθρο"}.get(ev["kind"], "")
        meta = date.fromisoformat(ev["date"]).strftime("%d/%m/%Y") + (f" · {kind}" if kind else "")
        NewsDialog(ev["title"], ev["url"], meta=meta, summary=ev.get("description", "") if summary is None else summary,
                  action=action, parent=self, clients=deadlines.clients_for_event(self.conn, ev),
                  on_client=self.open_client_detail).exec()

    # ------------------------------------------------------------------ Πελάτες
    def _clients_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        # Δύο γραμμές πάνω από τον πίνακα, όπως ο πίνακας πελατών του Timologio Downloader (ρητό αίτημα 2026-10-27):
        # (1) φίλτρο «Εμφάνιση» · «Καθαρισμός φίλτρων» (κόκκινο, μόνο όταν υπάρχει φίλτρο) ··· «Ανανέωση» — η
        #     αναζήτηση είναι η μπάρα πάνω-δεξιά (μία μόνο, ρητό αίτημα v0.4.1)
        # (2) πλήθος επιλεγμένων ··· επεξήγηση · Επιλογή/Αποεπιλογή όλων · «Έλεγχοι & ενημέρωση…» · Διαγραφή επιλεγμένων
        # Νέος πελάτης / Excel / εξαγωγή: από το πλαϊνό μενού (όπως πριν).
        filters = QHBoxLayout()
        filters.setSpacing(7)
        self.client_show = QComboBox()
        for label, key in _CLIENT_SHOW:
            self.client_show.addItem(label, key)
        self.client_show.setFixedWidth(210)
        self.client_show.setToolTip("Ποιους πελάτες να δείχνει ο πίνακας")
        self.client_show.currentIndexChanged.connect(lambda _i: self._apply_client_filter())
        filters.addWidget(self.client_show)
        # Αναζήτηση: η ΜΙΑ μπάρα πάνω-δεξιά (Ctrl+K) — ρητό αίτημα χρήστη v0.4.1, γι' αυτό όχι δεύτερο πεδίο εδώ.
        self.client_clear_filters_btn = QPushButton(icon("cancel", CURRENT.bad, 16), "  Καθαρισμός φίλτρων")
        self.client_clear_filters_btn.setObjectName("danger")
        self.client_clear_filters_btn.setToolTip("Επαναφορά αναζήτησης και φίλτρων (εμφάνισης και στηλών)")
        self.client_clear_filters_btn.clicked.connect(self._clear_client_filters)
        self.client_clear_filters_btn.setVisible(False)
        filters.addWidget(self.client_clear_filters_btn)
        self.client_notice_label = QLabel("")
        self.client_notice_label.setStyleSheet(f"color:{CURRENT.accent};")
        self.client_notice_label.setVisible(False)
        filters.addWidget(self.client_notice_label)
        filters.addStretch()
        refresh_btn = QPushButton(icon("refresh", CURRENT.muted, 16), "  Ανανέωση")
        refresh_btn.setToolTip("Ξαναδιαβάζει τη λίστα πελατών από τη βάση")
        refresh_btn.clicked.connect(lambda: self.reload_clients())
        filters.addWidget(refresh_btn)
        root.addLayout(filters)

        selbar = QHBoxLayout()
        selbar.setSpacing(7)
        self.client_sel_label = QLabel("Κανένας πελάτης επιλεγμένος")
        selbar.addWidget(self.client_sel_label)
        selbar.addStretch()
        hint = QLabel("Κουτάκι = επιλογή · διπλό κλικ = καρτέλα πελάτη · ▸ = μέλη εταιρείας")
        hint.setObjectName("muted")
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        selbar.addWidget(hint, 1)
        sel_all_btn = QPushButton("Επιλογή όλων")
        sel_all_btn.setToolTip("Επιλέγει όλους τους πελάτες που εμφανίζονται τώρα (σύμφωνα με την αναζήτηση/τα φίλτρα)")
        sel_all_btn.clicked.connect(lambda: self._set_all_clients_checked(True))
        selbar.addWidget(sel_all_btn)
        sel_none_btn = QPushButton("Αποεπιλογή όλων")
        sel_none_btn.setToolTip("Αποεπιλέγει όλους τους πελάτες")
        sel_none_btn.clicked.connect(lambda: self._set_all_clients_checked(False))
        selbar.addWidget(sel_none_btn)
        # ΕΝΑ κουμπί για όλους τους ελέγχους (ρητό αίτημα χρήστη 2026-10-23): popup με επιλογές — gui/checks_dialog.py
        checks_btn = QPushButton(icon("refresh", CURRENT.txt, 16), "  Έλεγχοι && ενημέρωση…")
        checks_btn.setObjectName("primary")
        checks_btn.setToolTip("Ανανέωση στοιχείων, έλεγχος υποβολών, myDATA, οφειλές, αιτήματα ΑΑΔΕ — για τους επιλεγμένους "
                              "ή, αν δεν έχει επιλεγεί κανείς, για όλους")
        checks_btn.clicked.connect(self.on_run_checks)
        selbar.addWidget(checks_btn)
        del_btn = QPushButton(icon("delete", CURRENT.bad, 16), "  Διαγραφή επιλεγμένων")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self.on_delete_selected_clients)
        selbar.addWidget(del_btn)
        root.addLayout(selbar)

        self.client_table = QTableWidget(0, len(_CLIENT_COLS))
        self.client_table.setHorizontalHeaderLabels([c[0] for c in _CLIENT_COLS])
        self.client_table.verticalHeader().setVisible(False)
        self.client_table.setAlternatingRowColors(True)
        self.client_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.client_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.client_table.setSortingEnabled(True)
        self.client_table.doubleClicked.connect(self._open_selected_client)
        self.client_table.itemClicked.connect(self._toggle_members)
        # apply_fn=self._apply_client_filter: το TableColumnFilter ξαναφιλτράρει μόνο του και ασύγχρονα μετά από
        # κάθε ξαναγέμισμα/ταξινόμηση του πίνακα (βλ. docstring της κλάσης) — χωρίς αυτό θα καλούσε το δικό του
        # `apply()` που δεν ξέρει τίποτα για το πεδίο αναζήτησης και θα έσβηνε ό,τι είχε κρύψει αυτή.
        # _apply_client_filter() συνδυάζει και τα δύο κριτήρια και είναι η ΜΟΝΗ συνάρτηση που αγγίζει setRowHidden.
        self._client_col_filter = TableColumnFilter(self.client_table,
                                                    range(_C_AFM, len(_CLIENT_COLS)),     # χωνί σε κάθε στήλη
                                                    apply_fn=lambda: self._apply_client_filter())
        self._client_col_filter.filtersChanged.connect(self._apply_client_filter)
        setup_columns(self.client_table, _CLIENT_COLS, self._prefs, "clients")
        install_cell_menu(self.client_table, self._client_col_filter)   # δεξί κλικ: αντιγραφή / φίλτρο με την τιμή
        # Ζωντανή ενημέρωση του πίνακα όσο τρέχει η ανάκτηση στοιχείων — συγχωνευμένη (μία ανανέωση το πολύ ανά
        # ~1,5"), ώστε 160 πελάτες να μη σημαίνουν 160 πλήρη ξαναγεμίσματα.
        self._client_refresh_timer = QTimer(self)
        self._client_refresh_timer.setSingleShot(True)
        self._client_refresh_timer.setInterval(1500)
        self._client_refresh_timer.timeout.connect(self.reload_clients)
        self.client_table.itemChanged.connect(lambda _i: self._sync_client_selection_label())
        self.quick_search.textChanged.connect(lambda _t: self._apply_client_filter())   # η ΜΙΑ γραμμή αναζήτησης (πάνω-δεξιά)
        root.addWidget(self.client_table, 1)
        return page

    def _reload_button(self, handler, tip: str) -> QPushButton:
        btn = QPushButton(icon("refresh", CURRENT.txt, 16), "")
        btn.setFixedWidth(38)
        btn.setToolTip(tip)
        btn.clicked.connect(lambda: handler())
        return btn

    def reload_clients(self) -> None:
        rows = clients.list_all(self.conn)
        creds = client_creds.status_map(self.conn)
        counts = {r["afm"]: r["n"] for r in self.conn.execute("SELECT afm, COUNT(*) AS n FROM matches GROUP BY afm")}
        fees = office.monthly_totals(self.conn)
        checks = easycheck.summary(self.conn, self._fiscal_year)
        # «*» στη στήλη ΦΠΑ (επιστροφή στο τρίμηνο) / στον ΚΑΔ (παρεπιδημούντες): όσο φαίνεται η ειδοποίηση, και μετά το
        # «Αναγνώστηκε» ώσπου να ανοιχτεί η καρτέλα του πελάτη (`notices.marks` / `clear_marks`)
        parents = members_mod.parent_company(self.conn)             # μέλος → εταιρεία (εμφανίζεται κάτω της)
        roles = members_mod.member_roles(self.conn)
        parepid_afms = set(notices_mod.parepid_candidates(self.conn))
        with_children = set(parents.values())
        vat_option, vat_deadline = notices_mod.vat_quarter_eligible(self.conn)
        if vat_option and notices_mod.vat_quarter_read(self.conn):
            vat_option = sorted(set(vat_option) & notices_mod.marks(self.conn, "vat"))
        parepid_marked = (parepid_afms & notices_mod.marks(self.conn, "parepid")
                          if notices_mod.parepid_read(self.conn) else parepid_afms)
        table = self.client_table
        # Τσεκαρισμένοι πελάτες, επιλεγμένη γραμμή και θέση κύλισης επιβιώνουν από το ξαναγέμισμα — ο πίνακας
        # ανανεώνεται πλέον και μόνος του όσο τρέχει η ανάκτηση στοιχείων (αναφορά χρήστη 2026-10-22).
        checked = {table.item(r, _C_CHK).data(Qt.ItemDataRole.UserRole) for r in range(table.rowCount())
                   if table.item(r, _C_CHK) and table.item(r, _C_CHK).checkState() == Qt.CheckState.Checked}
        current = table.currentRow()
        current_afm = table.item(current, _C_AFM).text() if current >= 0 and table.item(current, _C_AFM) else ""
        scroll = table.verticalScrollBar().value()
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            c = creds.get(r["afm"])
            check = _ClientItem("", (0, ""))
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Checked if r["afm"] in checked else Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, r["afm"])
            table.setItem(i, _C_CHK, check)
            state_label = {"active": "Ενεργή", "ceased": "Διακοπή", "none": "Ιδιώτης", "": "—"}.get(r["activity_state"] or "", r["activity_state"])
            # Μηνιαίος / τριμηνιαίος ΔΙΑΚΡΙΤΑ (ρητό αίτημα χρήστη 2026-10-22) — Μητρώο, περίοδοι Φ2 ή κατηγορία βιβλίων.
            period = obligations.vat_period(dict(r)) if r["vat_subject"] != 0 else ""
            vat = ("Όχι" if r["vat_subject"] == 0 else {"monthly": "Μηνιαίος", "quarterly": "Τριμηνιαίος"}.get(
                period, "Υπόχρεος" if r["vat_subject"] == 1 else "—"))
            lookup_label = {"ok": "Πλήρη", "partial": "Μερικά", "failed": "Αποτυχία", "pending": "Αναμονή", "manual": "Χειροκίνητα"}.get(r["lookup_status"], r["lookup_status"])
            creds_label = "—"
            if c and c["complete"]:
                creds_label = {"ok": "Έγκυροι", "invalid": "Πρόβλημα", "error": "Πρόβλημα"}.get(c["check_status"], "Ορισμένοι")
            cells = {
                _C_AFM: r["afm"], _C_NAME: r["name"] or "—", _C_STATE: state_label,
                _C_KIND: " · ".join(x for x in (r["legal_form"], members_mod.short_role(roles.get(r["afm"], ""))) if x) or "—",
                _C_KAD: r["kad_main_code"] or "—", _C_BOOKS: r["books_category"] or "—", _C_VAT: vat,
                _C_LOOKUP: lookup_label, _C_CREDS: creds_label, _C_MATCHES: str(counts.get(r["afm"], 0)),
            }
            name_key = sort_key(cells[_C_NAME])
            if r["afm"] in with_children:
                n_members = sum(1 for p in parents.values() if p == r["afm"])
                cells[_C_NAME] = ("▾ " if r["afm"] in self._expanded else "▸ ") + cells[_C_NAME]
            elif r["afm"] in parents:
                cells[_C_NAME] = "↳ " + cells[_C_NAME]
            for col, text in cells.items():
                item = _ClientItem(text, name_key if col == _C_NAME else None)
                if col == _C_NAME and r["afm"] in with_children:
                    item.setToolTip(f"{n_members} {'μέλος' if n_members == 1 else 'μέλη'} — κλικ στο ▸/▾ για εμφάνιση/"
                                    "απόκρυψη· διπλό κλικ ανοίγει την καρτέλα")
                elif col == _C_NAME and r["afm"] in parents:
                    item.setToolTip("Μέλος εταιρείας — ανοίγει κανονικά με διπλό κλικ")
                    item.setForeground(QColor(CURRENT.muted))
                if col == _C_NAME and r["trade_name"]:
                    item.setData(Qt.ItemDataRole.UserRole + 1, r["trade_name"])   # αναζήτηση και με διακριτικό τίτλο
                    item.setToolTip((item.toolTip() + "\n" if item.toolTip() else "") + f"Διακριτικός τίτλος: {r['trade_name']}")
                if col == _C_STATE and state_label == "Διακοπή":
                    item.setForeground(QColor(CURRENT.bad))
                elif col == _C_STATE and state_label == "Ενεργή":
                    item.setForeground(QColor(CURRENT.ok))
                if col == _C_CREDS and creds_label == "Πρόβλημα":
                    item.setForeground(QColor(CURRENT.bad))
                if col == _C_VAT and r["afm"] in vat_option:
                    item.setText(vat + " *")
                    item.setForeground(QColor(CURRENT.accent))
                    item.setToolTip("Μπορεί να επανέλθει στον τριμηνιαίο ΦΠΑ (24 μήνες από την έναρξη, Α.1049/2025) — "
                                    f"δήλωση επιλογής στο myAADE έως {vat_deadline.strftime('%d/%m/%Y')} · ο «*» "
                                    "σβήνει όταν ανοίξετε την καρτέλα του πελάτη")
                if col == _C_KAD and r["afm"] in parepid_marked:
                    item.setText(item.text() + " *")
                    item.setForeground(QColor(CURRENT.accent))
                    item.setToolTip("ΚΑΔ καταλυμάτων/εστίασης/διασκέδασης — πιθανός υπόχρεος τέλους παρεπιδημούντων / "
                                    "ακαθαρίστων εσόδων (dae.govapp.gr) · ο «*» σβήνει όταν ανοίξετε την καρτέλα του πελάτη")
                table.setItem(i, col, item)
            flags = {"creds" if c and c["complete"] else "nocreds"}
            if clients.missing_basics(dict(r)):
                flags.add("incomplete")
            if r["activity_state"] == "ceased":
                flags.add("ceased")
            elif r["activity_state"] == "none":
                flags.add("private")
            elif obligations.is_legal_entity(r["legal_form"] or ""):
                flags.add("company")
            elif r["activity_state"] == "active":
                flags.add("sole")
            if r["afm"] in parents:
                flags.add("member")
            if (checks.get(r["afm"]) or {}).get("not_submitted"):
                flags.add("missing")
            if r["afm"] in parepid_afms:
                flags.add("parepid")
            self._client_flags[r["afm"]] = flags
            fee = fees.get(r["afm"])
            fee_item = _ClientItem(office.money(fee) if fee else "—", (0, fee or 0.0))
            fee_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            table.setItem(i, _C_FEE, fee_item)
            c = checks.get(r["afm"], {})
            missing = c.get("not_submitted", 0)
            check_item = _ClientItem(f"✓{c.get('submitted', 0)} ✗{missing}" if c else "—", (0, missing if c else -1))
            if missing:
                check_item.setForeground(QColor(CURRENT.bad))
            table.setItem(i, _C_CHECKS, check_item)
        # ομάδα: τα κελιά κάθε μέλους ταξινομούνται με τις τιμές της εταιρείας του
        row_of = {table.item(i, _C_AFM).text(): i for i in range(table.rowCount())}
        for i in range(table.rowCount()):
            afm = table.item(i, _C_AFM).text()
            parent = parents.get(afm)
            parent_row = row_of.get(parent) if parent else None
            for col in range(table.columnCount()):
                item = table.item(i, col)
                if not isinstance(item, _ClientItem):
                    continue
                if parent_row is not None:
                    owner = table.item(parent_row, col)
                    item.group_key, item.group_afm, item.child = getattr(owner, "key", item.key), parent, True
                else:
                    item.group_key, item.group_afm = item.key, afm
        self._client_parents = {a: p for a, p in parents.items() if p in row_of}
        table.setSortingEnabled(True)
        resort(table, _C_NAME)
        table.blockSignals(False)
        self._apply_client_filter()
        if current_afm:
            for i in range(table.rowCount()):
                if table.item(i, _C_AFM).text() == current_afm:
                    table.setCurrentCell(i, table.currentColumn() if table.currentColumn() >= 0 else _C_AFM)
                    break
        table.verticalScrollBar().setValue(scroll)

    def _apply_client_filter(self) -> None:
        """Συνδυάζει αναζήτηση κειμένου + φίλτρα στηλών σε ΕΝΑ πέρασμα — δεν καλεί `TableColumnFilter.apply()`
        (θα ξανάγραφε το setRowHidden αγνοώντας την αναζήτηση, βλ. σχόλιο στο `_clients_page`)."""
        needle = self.quick_search.text().strip().lower()
        col_filters = self._client_col_filter.filters
        show = self.client_show.currentData() or ""
        parents = getattr(self, "_client_parents", {})
        for row in range(self.client_table.rowCount()):
            name_item = self.client_table.item(row, _C_NAME)
            trade = (name_item.data(Qt.ItemDataRole.UserRole + 1) or "") if name_item else ""
            text_ok = not needle or any(needle in (self.client_table.item(row, c).text() or "").lower()
                                        for c in range(1, self.client_table.columnCount())) or needle in trade.lower()
            parent = parents.get(self.client_table.item(row, _C_AFM).text())
            if parent and parent not in self._expanded and not needle and self._notice_afms is None:
                text_ok = False                               # μέλος «κλειστής» εταιρείας
            col_ok = all(not allowed or self.client_table.item(row, c).text() in allowed
                        for c, allowed in col_filters.items())
            row_afm = self.client_table.item(row, _C_AFM).text()
            notice_ok = self._notice_afms is None or row_afm in self._notice_afms
            show_ok = not show or show in self._client_flags.get(row_afm, set())
            self.client_table.setRowHidden(row, not (text_ok and col_ok and notice_ok and show_ok))
        active = (len([c for c, v in col_filters.items() if v]) + (1 if needle else 0)
                  + (1 if self._notice_afms is not None else 0) + (1 if show else 0))
        self.client_notice_label.setVisible(self._notice_afms is not None)
        if self._notice_afms is not None:
            self.client_notice_label.setText(f"Εμφανίζονται οι {len(self._notice_afms)} πελάτες της ειδοποίησης")
        self.client_clear_filters_btn.setVisible(bool(active))
        self.client_clear_filters_btn.setText(f"  Καθαρισμός φίλτρων ({active})")
        self._sync_client_selection_label()

    def _toggle_members(self, item: QTableWidgetItem) -> None:
        """Κλικ στο ▸/▾ μιας εταιρείας: εμφάνιση/απόκρυψη των μελών της κάτω από αυτήν."""
        if item.column() != _C_NAME or not item.text()[:1] in ("▸", "▾"):
            return
        rect = self.client_table.visualItemRect(item)
        pos = self.client_table.viewport().mapFromGlobal(QCursor.pos())
        if pos.x() - rect.left() > 26:                             # μόνο το βελάκι — αλλού είναι κανονική επιλογή
            return
        afm = self.client_table.item(item.row(), _C_AFM).text()
        if afm in self._expanded:
            self._expanded.discard(afm)
            item.setText("▸" + item.text()[1:])
        else:
            self._expanded.add(afm)
            item.setText("▾" + item.text()[1:])
        self._apply_client_filter()

    def _fill_fiscal_combo(self) -> None:
        years = {r[0] for r in self.conn.execute(
            "SELECT DISTINCT year FROM obligation_checks UNION SELECT DISTINCT CAST(substr(ym,1,4) AS INTEGER) "
            "FROM mydata_sync")} if hasattr(self, "conn") else set()
        choices = fiscal.year_choices(y for y in years if y)
        if self._fiscal_year not in choices:
            choices = sorted({*choices, self._fiscal_year}, reverse=True)
        self.fiscal_combo.blockSignals(True)
        self.fiscal_combo.clear()
        for y in choices:
            self.fiscal_combo.addItem(str(y), y)
        self.fiscal_combo.setCurrentIndex(max(self.fiscal_combo.findData(self._fiscal_year), 0))
        self.fiscal_combo.blockSignals(False)

    def _on_fiscal_combo(self, index: int) -> None:
        year = self.fiscal_combo.itemData(index)
        if year:
            self.set_fiscal_year(int(year))

    @property
    def fiscal_year(self) -> int:
        return self._fiscal_year

    def set_fiscal_year(self, year: int) -> None:
        """Αλλάζει τη χρήση σε όλη την εφαρμογή (η επιλογή ισχύει μέσα στο ημερολογιακό έτος — fiscal.resolve_year)."""
        self._prefs.setValue("fiscal/year", year)
        self._prefs.setValue("fiscal/chosen_in", date.today().year)
        self._fiscal_year = year
        if self.fiscal_combo.currentData() != year:
            self._fill_fiscal_combo()
        if hasattr(self, "work_year"):
            self.work_year.setValue(year)
        self.reload_clients()

    def _clear_client_filters(self) -> None:
        self._notice_afms = None
        self.client_show.blockSignals(True)
        self.client_show.setCurrentIndex(0)
        self.client_show.blockSignals(False)
        self.quick_search.clear()                              # το σήμα textChanged ξαναφιλτράρει
        self._client_col_filter.clear()
        self._apply_client_filter()

    def _set_all_clients_checked(self, checked: bool) -> None:
        """Επιλογή: μόνο οι ΟΡΑΤΟΙ πελάτες (σεβόμαστε φίλτρα/αναζήτηση)· αποεπιλογή: όλοι, ώστε να μη μένει
        «κρυφή» επιλογή πίσω από φίλτρο."""
        table = self.client_table
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        table.blockSignals(True)
        for row in range(table.rowCount()):
            if checked and table.isRowHidden(row):
                continue
            item = table.item(row, _C_CHK)
            if item:
                item.setCheckState(state)
        table.blockSignals(False)
        self._sync_client_selection_label()

    def _selected_client_afms(self) -> list[str]:
        out = []
        for row in range(self.client_table.rowCount()):
            if self.client_table.isRowHidden(row):
                continue
            item = self.client_table.item(row, _C_CHK)
            if item and item.checkState() == Qt.CheckState.Checked:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        return out

    def _sync_client_selection_label(self) -> None:
        n = len(self._selected_client_afms())
        total = self.client_table.rowCount()
        shown = sum(1 for r in range(total) if not self.client_table.isRowHidden(r))
        text = f"{n} επιλεγμένοι" if n else "Κανένας πελάτης επιλεγμένος"
        if shown != total:
            text += f"  ·  εμφανίζονται {shown} από {total}"
        self.client_sel_label.setText(text)

    def _quick_client_search(self) -> None:
        """Ctrl+K / Enter στο πεδίο πάνω-δεξιά: φιλτράρει τους πελάτες από ΟΠΟΙΑΔΗΠΟΤΕ σελίδα και, αν μείνει
        ακριβώς ένας, ανοίγει κατευθείαν την καρτέλα του — χωρίς να χρειάζεται πρώτα να πάει κανείς στους «Πελάτες»."""
        needle = self.quick_search.text().strip()
        if not needle:
            return
        self._show_page("clients")
        self._apply_client_filter()
        visible = [row for row in range(self.client_table.rowCount()) if not self.client_table.isRowHidden(row)]
        if len(visible) == 1:
            afm = self.client_table.item(visible[0], _C_CHK).data(Qt.ItemDataRole.UserRole)
            self.quick_search.clear()
            self.open_client_detail(afm)
        else:
            self.client_table.setFocus()

    def _open_selected_client(self) -> None:
        row = self.client_table.currentRow()
        if row < 0:
            return
        afm = self.client_table.item(row, _C_CHK).data(Qt.ItemDataRole.UserRole)
        self.open_client_detail(afm)

    def on_add_client(self) -> None:
        dlg = ClientDialog(self.conn, self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if dlg.excel_path:                                     # «Εισαγωγή από Excel…» μέσα στον ίδιο διάλογο
            self._import_excel_from_path(Path(dlg.excel_path))
            return
        if dlg.result_afm:
            if dlg.existed:
                toast(self, f"Ο πελάτης {dlg.result_afm} υπήρχε ήδη — ενημερώθηκαν οι κωδικοί TAXISnet του.", "info")
            self._start_lookup([dlg.result_afm])
            self.reload_clients()
            self._refresh_status_bar()

    def on_delete_selected_clients(self) -> None:
        afms = self._selected_client_afms()
        if not afms:
            return
        if QMessageBox.question(self, "Διαγραφή", f"Διαγραφή {len(afms)} πελατών μαζί με τα matches και τους κωδικούς τους;\n\n"
                                "Το ιστορικό δέουσας επιμέλειας ΔΕΝ διαγράφεται (τήρηση 5 ετών, άρθρο 30 ν. 4557/2018).") != QMessageBox.StandardButton.Yes:
            return
        for afm in afms:
            clients.delete(self.conn, afm)
        self.reload_clients()
        self._refresh_status_bar()

    def on_run_checks(self) -> None:
        """«Έλεγχοι & ενημέρωση…»: popup με τους ελέγχους· κάθε επιλεγμένος τρέχει όπως το παλιό του κουμπί (ουρά εργασιών)."""
        from .checks_dialog import ChecksDialog
        selected = self._selected_client_afms()
        afms = selected or [self.client_table.item(r, _C_CHK).data(Qt.ItemDataRole.UserRole)
                            for r in range(self.client_table.rowCount())]
        rows = {r["afm"]: r for r in clients.list_all(self.conn)}
        incomplete = {a: m for a in afms if a in rows for m in [clients.missing_basics(rows[a])] if m}
        scope = (f"{len(selected)} επιλεγμένοι πελάτες" if len(selected) > 1 else "1 επιλεγμένος πελάτης"
                 ) if selected else f"Όλοι οι πελάτες ({len(afms)})"
        dlg = ChecksDialog(self, scope, incomplete, {a: (rows[a]["name"] if a in rows else a) for a in afms})
        if not dlg.exec():
            return
        actions = {"lookup": lambda: self._start_lookup(afms),
                   "submissions": lambda: self.on_check_submissions(want_pdf=dlg.want_pdf),
                   "mydata": self.on_fetch_mydata, "debts": self.on_fetch_debts, "requests": self.on_fetch_requests}
        for key in dlg.chosen:
            actions[key]()

    def on_refresh_selected_clients(self) -> None:
        afms = self._selected_client_afms() or [self.client_table.item(r, _C_CHK).data(Qt.ItemDataRole.UserRole) for r in range(self.client_table.rowCount())]
        self._start_lookup(afms)

    def on_check_submissions(self, want_pdf: bool = False) -> None:
        """EasyCheck για τους επιλεγμένους (ή όλους με κωδικούς TAXISnet) — για την τρέχουσα χρήση."""
        with_creds = {a for a, c in client_creds.status_map(self.conn).items() if c["complete"]}
        afms = [a for a in (self._selected_client_afms() or sorted(with_creds)) if a in with_creds]
        if not afms:
            toast(self, "Κανένας (επιλεγμένος) πελάτης δεν έχει κωδικούς TAXISnet.", "warn")
            return
        self.run_submission_checks(afms, [self._fiscal_year], want_pdf=want_pdf)

    def run_submission_checks(self, afms: list[str], years: list[int], window: Optional[QWidget] = None,
                              on_done: Optional[Callable[[], None]] = None, want_pdf: bool = False) -> None:
        """Έλεγχος υποβολών (ΑΑΔΕ) στην ουρά εργασιών: παράλληλα ανά πελάτη, διακοπή μετά τον τρέχοντα, για μία ή
        περισσότερες χρήσεις. Χρησιμοποιείται από Πελάτες, Ημερολόγιο και καρτέλα πελάτη."""
        afms = list(dict.fromkeys(a for a in afms if a))
        total = len(afms)
        names = self._client_names(afms)
        label = ", ".join(str(y) for y in sorted(set(years)))
        title = (f"έλεγχος υποβολών {label}{' με PDF δηλώσεων' if want_pdf else ''} — "
                 f"{total} {'πελάτης' if total == 1 else 'πελάτες'}")
        state = {"missing": 0, "failed": 0, "done": 0}
        started = time.monotonic()

        def work(report, cancelled):
            def one(conn, afm):
                result = {"ok": True, "entries": []}
                for y in years:
                    r = easycheck.check_client(conn, afm, y, want_pdf=want_pdf)
                    result["ok"] = result["ok"] and r.get("ok")
                    result["entries"] += r.get("entries", [])
                    apd = efka.check_apd_entries(conn, afm, y)        # ΑΠΔ e-ΕΦΚΑ + ΤΕΚΑ ανά ΑΜΕ (όχι ΑΑΔΕ)
                    result["entries"] += apd
                    # τέλη σε πύλες εκτός TAXISnet «other»: πλαστικά (Plastic), παρεπιδημούντες (dae.govapp.gr)
                    fees = fee_portals.check_entries(conn, afm, y)
                    # Intrastat στην ΕΛ.ΣΤΑΤ με τους κωδικούς του ΓΡΑΦΕΙΟΥ (μόνο όσους αφορά: κατώφλια/σημαία)
                    fees += intrastat_mod.check_entries(conn, afm, y, want_pdf=want_pdf)
                    if fees:
                        easycheck.store_results(conn, afm, y, fees)
                        result["entries"] += fees
                return result

            def each(afm, n, result):
                state["done"] = n
                state["failed"] += 0 if result.get("ok") else 1
                state["missing"] += sum(1 for e in result.get("entries", []) for p in e["periods"]
                                        if p["status"] == "not_submitted")
                report(f"Έλεγχος υποβολών {n}/{total} — {names.get(afm, afm)}", n)

            clients.lookup_many(afms, on_each=each, fetch=one, should_cancel=cancelled)
            return state["done"]

        def done(_n):
            self.reload_clients()
            self._render_calendar_from_db()
            if on_done:
                on_done()
            msg = f"Έλεγχος υποβολών ({state['done']}/{total}): {state['missing']} περίοδοι χωρίς υποβολή"
            msg += f", {state['failed']} πελάτες απέτυχαν (κωδικοί/σύνδεση)." if state["failed"] else "."
            self.offer_handoffs([h for h in handoff.pending(since=started) if h["afm"] in afms], window)
            return msg, "warn" if state["missing"] or state["failed"] else "ok"

        self.jobs.submit(title, work, total=total, window=window, on_done=done)

    def ask_submission_pdf(self, parent: QWidget) -> Optional[bool]:
        """Popup πριν τον έλεγχο υποβολών (ρητό αίτημα 2026-10-27): και PDF των υποβληθεισών δηλώσεων; None = άκυρο."""
        box = QMessageBox(parent)
        box.setWindowTitle("Έλεγχος υποβολών")
        box.setText("Να γίνει λήψη και των PDF των υποβληθεισών δηλώσεων (Φ2, Φ4/Φ5, ΦΜΥ, Intrastat…);")
        box.setInformativeText("Αποθηκεύονται στον φάκελο δεδομένων (submissions) — μία φορά ανά δήλωση. Χωρίς PDF ο "
                               "έλεγχος είναι πιο γρήγορος.")
        with_pdf = box.addButton("Έλεγχος και PDF", QMessageBox.ButtonRole.YesRole)
        only = box.addButton("Μόνο έλεγχος", QMessageBox.ButtonRole.NoRole)
        box.addButton("Άκυρο", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(only)
        box.exec()
        clicked = box.clickedButton()
        return True if clicked is with_pdf else (False if clicked is only else None)

    def offer_handoffs(self, items: list[dict], window: Optional[QWidget] = None) -> None:
        """Έλεγχοι που κόλλησαν ΜΕΤΑ τη σύνδεση: toast με «Συνέχεια στον browser» — ανοίγει Chrome/Edge συνδεδεμένο στο
        σημείο όπου σταμάτησε η εφαρμογή (handoff.py, ρητό αίτημα 2026-10-27)."""
        if not items:
            return
        names = self._client_names([h["afm"] for h in items])
        actions = [(f"Συνέχεια στον browser — {names.get(h['afm'], h['afm'])} · {h['label']}",
                    lambda _c=False, h=h: self.open_handoff(h)) for h in items[:4]]
        more = f" (και άλλοι {len(items) - 4} — από την καρτέλα κάθε πελάτη)" if len(items) > 4 else ""
        toast(window or self, f"{len(items)} {'έλεγχος σταμάτησε' if len(items) == 1 else 'έλεγχοι σταμάτησαν'} μετά "
                              f"τη σύνδεση (π.χ. επιβεβαίωση στοιχείων στον e-ΕΦΚΑ). Συνεχίστε στον browser από το ίδιο "
                              f"σημείο{more}:", "warn", actions=actions)

    def open_handoff(self, entry: dict) -> None:
        toast(self, f"Άνοιγμα browser — {entry['label']}…", "info")

        def work(_p):
            return handoff.open_in_browser(entry)

        def done(res):
            if res["ok"]:
                toast(self, "Ο browser άνοιξε στο σημείο του ελέγχου. Αν δείχνει σελίδα σύνδεσης, η συνεδρία έληξε — "
                            "ξανατρέξτε τον έλεγχο.", "ok")
            else:
                toast(self, res["reason"], "danger")
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self, m, "danger")))

    def on_fetch_debts(self) -> None:
        with_creds = {a for a, c in client_creds.status_map(self.conn).items() if c["complete"]}
        afms = [a for a in (self._selected_client_afms() or sorted(with_creds)) if a in with_creds]
        if not afms:
            toast(self, "Κανένας (επιλεγμένος) πελάτης δεν έχει κωδικούς TAXISnet.", "warn")
            return
        self.run_debt_sync(afms)

    def run_debt_sync(self, afms: list[str], window: Optional[QWidget] = None,
                      on_done: Optional[Callable[[], None]] = None, want_pdf: bool = False) -> None:
        """Οφειλές ΑΑΔΕ / ΚΕΑΟ / ΕΦΚΑ στην ουρά εργασιών (ένας πελάτης τη φορά, διακοπή μετά τον τρέχοντα). `want_pdf`:
        και τα έντυπα (ταυτότητες με δοσολόγιο, καρτέλες ΚΕΑΟ) — αλλιώς μόνο δεδομένα, όπως το Tax Center."""
        from .. import debts as debts_mod
        afms = list(dict.fromkeys(a for a in afms if a))
        total = len(afms)
        names = self._client_names(afms)
        state = {"done": 0, "failed": 0, "reason": "", "overdue": 0.0}

        def work(report, cancelled):
            conn = dbmod.connect()
            try:
                for i, afm in enumerate(afms, 1):
                    if cancelled():
                        break
                    report(f"Οφειλές {i}/{total} — {names.get(afm, afm)}", i - 1)
                    try:
                        res = debts_mod.check_client(conn, afm, want_pdf=want_pdf)
                    except Exception as exc:                   # noqa: BLE001 — ένας πελάτης δεν σταματά τους άλλους
                        res = {"ok": False, "reason": str(exc)[:120]}
                    if res["ok"]:
                        state["overdue"] += res.get("overdue", 0.0)
                    else:
                        state["failed"] += 1
                        state["reason"] = res["reason"]
                    state["done"] = i
                    report(f"Οφειλές {i}/{total} — {names.get(afm, afm)}", i)
            finally:
                conn.close()
            return state["done"]

        def done(_n):
            if on_done:
                on_done()
            if total == 1 and state["failed"]:
                return f"Οφειλές: {state['reason']}", "danger"
            msg = f"Λήψη οφειλών ({state['done']}/{total}): ληξιπρόθεσμα {office.money(state['overdue'])}"
            msg += f" — {state['failed']} πελάτες απέτυχαν." if state["failed"] else "."
            return msg, "warn" if state["failed"] else "ok"

        self.jobs.submit(f"{'έντυπα' if want_pdf else 'ενημέρωση'} οφειλών — {total} "
                         f"{'πελάτης' if total == 1 else 'πελάτες'}", work, total=total, window=window, on_done=done)

    def on_fetch_requests(self) -> None:
        with_creds = {a for a, c in client_creds.status_map(self.conn).items() if c["complete"]}
        afms = [a for a in (self._selected_client_afms() or sorted(with_creds)) if a in with_creds]
        if not afms:
            toast(self, "Κανένας (επιλεγμένος) πελάτης δεν έχει κωδικούς TAXISnet.", "warn")
            return
        self.run_requests_sync(afms)

    def run_requests_sync(self, afms: list[str], window: Optional[QWidget] = None,
                          on_done: Optional[Callable[[], None]] = None) -> None:
        """«Τα Αιτήματά μου» ΑΑΔΕ στην ουρά εργασιών — και τι άλλαξε (απαντήσεις, εκκρεμότητες) από την τελευταία φορά."""
        from .. import requests_aade
        afms = list(dict.fromkeys(a for a in afms if a))
        total = len(afms)
        names = self._client_names(afms)
        state = {"done": 0, "failed": 0, "reason": "", "changes": 0}

        def work(report, cancelled):
            conn = dbmod.connect()
            try:
                for i, afm in enumerate(afms, 1):
                    if cancelled():
                        break
                    report(f"Αιτήματα ΑΑΔΕ {i}/{total} — {names.get(afm, afm)}", i - 1)
                    try:
                        res = requests_aade.refresh(conn, afm)
                    except Exception as exc:                   # noqa: BLE001
                        res = {"ok": False, "reason": str(exc)[:120], "changes": {}}
                    state["changes"] += len(res.get("changes") or {})
                    if not res["ok"]:
                        state["failed"] += 1
                        state["reason"] = res["reason"]
                    state["done"] = i
                    report(f"Αιτήματα ΑΑΔΕ {i}/{total} — {names.get(afm, afm)}", i)
            finally:
                conn.close()
            return state["done"]

        def done(_n):
            self._reload_notices()
            if on_done:
                on_done()
            if total == 1 and state["failed"]:
                return f"Αιτήματα: {state['reason']}", "danger"
            msg = f"Αιτήματα ΑΑΔΕ ({state['done']}/{total}): {state['changes']} αλλαγές"
            msg += f" — {state['failed']} πελάτες απέτυχαν." if state["failed"] else "."
            return msg, "warn" if state["failed"] else "ok"

        self.jobs.submit(f"αιτήματα ΑΑΔΕ — {total} {'πελάτης' if total == 1 else 'πελάτες'}", work, total=total,
                         window=window, on_done=done)

    def on_fetch_mydata(self) -> None:
        """Μαζική λήψη myDATA (στοιχεία, όχι PDF): οι μήνες που χρειάζονται (τρέχων + προηγούμενος + όσοι λείπουν)."""
        from ..mydata import credentials as mydata_creds
        have = mydata_creds.with_credentials(self.conn)
        afms = [a for a in (self._selected_client_afms() or sorted(have)) if a in have]
        if not afms:
            toast(self, "Κανένας (επιλεγμένος) πελάτης δεν έχει κωδικούς myDATA.", "warn")
            return
        self.run_mydata_sync(afms)

    def run_mydata_sync(self, afms: list[str], months: Optional[list[str]] = None, window: Optional[QWidget] = None,
                        on_done: Optional[Callable[[], None]] = None, pdfs: bool = False, title: str = "") -> None:
        """Λήψη myDATA στην ουρά εργασιών, ένας πελάτης τη φορά (η ΑΑΔΕ απαντά 429 σε πολλά παράλληλα). `months=None`:
        έξυπνη επιλογή (`store.months_to_refresh`). `pdfs`: κατεβαίνουν και τα PDF των μηνών (καρτέλα πελάτη)."""
        from ..mydata import names as mydata_names, pdfs as mydata_pdfs, store as mydata_store
        afms = list(dict.fromkeys(a for a in afms if a))
        total = len(afms)
        names = self._client_names(afms)
        title = title or f"λήψη myDATA — {total} {'πελάτης' if total == 1 else 'πελάτες'}"
        state = {"failed": 0, "docs": 0, "done": 0, "reason": ""}

        def work(report, cancelled):
            conn = dbmod.connect()
            try:
                for i, afm in enumerate(afms, 1):
                    if cancelled():
                        break
                    wanted = months or mydata_store.months_to_refresh(conn, afm)
                    report(f"Λήψη myDATA {i}/{total} — {names.get(afm, afm)} ({len(wanted)} μήνες)", i - 1)
                    out = mydata_store.sync_range(conn, afm, wanted)
                    if not out["ok"]:
                        state["failed"] += 1
                        state["reason"] = out["reason"]
                    state["docs"] += out.get("n", 0)
                    if pdfs and out["ok"]:
                        for ym in wanted:
                            if cancelled():
                                break
                            report(f"PDF {ym} — {names.get(afm, afm)}", i - 1)
                            mydata_pdfs.download_month(conn, afm, ym, should_cancel=cancelled)
                    state["done"] = i
                    report(f"Λήψη myDATA {i}/{total} — {names.get(afm, afm)}", i)
                report("Επωνυμίες αντισυμβαλλομένων (VIES)…", state["done"])
                mydata_names.resolve_via_vies(conn, afms[0] if total == 1 else None, limit=60, should_cancel=cancelled)
            finally:
                conn.close()
            return state["done"]

        def done(_n):
            self._render_calendar_from_db()
            self.reload_dashboard()
            if on_done:
                on_done()
            if total == 1 and state["failed"]:
                return f"myDATA: {state['reason']}", "danger"
            msg = f"Η λήψη myDATA ολοκληρώθηκε ({state['done']}/{total}, {state['docs']} παραστατικά)"
            msg += f" — {state['failed']} πελάτες απέτυχαν (κωδικοί/σύνδεση)." if state["failed"] else "."
            return msg, "warn" if state["failed"] else "ok"

        self.jobs.submit(title, work, total=total, window=window, on_done=done)

    def _client_names(self, afms: list[str]) -> dict[str, str]:
        if not afms:
            return {}
        return {r["afm"]: r["name"] or r["afm"] for r in self.conn.execute(
            f"SELECT afm, name FROM businesses WHERE afm IN ({','.join('?' * len(afms))})", afms)}

    def on_import_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Εισαγωγή από Excel", "", "Excel/CSV (*.xlsx *.xlsm *.csv *.txt)")
        if path:
            self._import_excel_from_path(Path(path))

    def _import_excel_from_path(self, path: Path) -> None:
        try:
            res = import_excel.parse_file(path.name, path.read_bytes())
        except Exception as exc:
            toast(self, f"Δεν ήταν δυνατή η ανάγνωση του αρχείου: {exc}", "danger")
            return
        if not res.rows:
            toast(self, "Δεν βρέθηκε κανένας έγκυρος ΑΦΜ στο αρχείο.", "warn")
            return
        with_creds = [r.afm for r in res.rows if r.has_credentials]
        lookup_afms = with_creds or [r.afm for r in res.rows]
        # Προεπισκόπηση όπως στο Timologio Downloader: νέοι / υπάρχουν ήδη και θα ενημερωθούν (με τι) / χωρίς αλλαγές.
        preview = clients.preview_import(self.conn, res)
        dlg = ImportPreviewDialog(path, res, preview, len(lookup_afms), bool(with_creds), self)
        dlg.exec()
        if dlg.choice not in ("lookup", "import"):
            return
        backup_mod.create_backup(config.db_path(), reason="import")
        out = clients.import_result(self.conn, res)
        engine.rematch(self.conn)
        existing = sum(1 for p in preview if p["action"] != "new")
        toast(self, f"Προστέθηκαν {out['added']} νέοι πελάτες"
                    + (f" · {existing} υπήρχαν ήδη ({sum(1 for p in preview if p['action'] == 'update')} ενημερώθηκαν)"
                       if existing else "") + "."
                    + (f" Κωδικοί TAXISnet: {out['credentials']}." if out["credentials"] else "")
                    + (f" Κωδικοί Γ.Ε.ΜΗ.: {out['gemi']}." if out["gemi"] else "")
                    + (f" Κωδικοί myDATA: {out['mydata']}." if out["mydata"] else "")
                    + (f" ΙΚΑ εργοδότη/ΑΜΕ: {out['ika']}." if out["ika"] else ""), "ok")
        self.reload_clients()
        self._refresh_status_bar()
        if dlg.choice == "lookup":
            self._start_lookup(lookup_afms)

    def on_export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Εξαγωγή πελατών", "pelates.csv", "CSV (*.csv)")
        if not path:
            return
        import csv
        rows = clients.list_all(self.conn)
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["ΑΦΜ", "Επωνυμία", "Νομική μορφή", "ΔΟΥ", "Κύριος ΚΑΔ", "Βιβλία", "Κατάσταση"])
            for r in rows:
                w.writerow([r["afm"], r["name"], r["legal_form"], r["doy"], r["kad_main_code"], r["books_category"], r["activity_state"]])
        toast(self, "Η λίστα πελατών εξήχθη (χωρίς κωδικούς).", "ok")

    def _start_lookup(self, afms: list[str]) -> None:
        afms = [a for a in afms if a]
        if not afms:
            return
        total = len(afms)
        names = {r["afm"]: r["name"] or r["afm"] for r in self.conn.execute(
            f"SELECT afm, name FROM businesses WHERE afm IN ({','.join('?' * total)})", afms)}
        self.run_status.setText(f"Ανάκτηση στοιχείων για {total} πελάτες…")
        # Μόνιμο toast με ποσοστό και τον πελάτη που επεξεργάζεται τώρα (ρητό αίτημα χρήστη, 2026-10-22) — κλείνει
        # μόνο του όταν τελειώσει η ανάκτηση.
        stop = threading.Event()
        bar = ProgressToast(self, f"Ξεκίνησε η ανάκτηση στοιχείων για {total} {'πελάτη' if total == 1 else 'πελάτες'}…",
                            on_cancel=stop.set)
        state = {"done": 0}

        def work(progress):
            # Παράλληλα (clients.lookup_many: 6 πελάτες τη φορά, ο καθένας με ΔΙΚΗ του σύνδεση βάσης — ΠΟΤΕ self.conn,
            # που ανήκει στο UI thread). Το progress() είναι σήμα Qt, ασφαλές από οποιοδήποτε thread.
            progress(f"Ανάκτηση στοιχείων 0/{total}…")

            def each(afm, n, _result):
                state["done"] = n
                progress(f"Ανάκτηση στοιχείων {n}/{total} — {names.get(afm, afm)}")

            clients.lookup_many(afms, on_each=each, should_cancel=stop.is_set)
            progress(f"Αντιστοίχιση νέων με τους πελάτες ({state['done']}/{total})…")
            conn = dbmod.connect()
            try:
                engine.rematch(conn)
            finally:
                conn.close()
            return total

        def on_progress(msg: str) -> None:
            self.run_status.setText(msg)
            bar.update(msg, state["done"], total)
            if state["done"]:
                self._client_refresh_timer.start()      # ό,τι ολοκληρώθηκε φαίνεται ήδη στον πίνακα

        def done(_n):
            self._client_refresh_timer.stop()
            self.run_status.setText("Η ανάκτηση στοιχείων ολοκληρώθηκε.")
            if stop.is_set():
                bar.finish(f"Η ανάκτηση στοιχείων διακόπηκε μετά από {state['done']}/{total} πελάτες.", "warn")
            else:
                bar.finish(f"Η ανάκτηση στοιχείων ολοκληρώθηκε ({total} {'πελάτης' if total == 1 else 'πελάτες'}).", "ok")
            self.reload_clients()
            self.reload_dashboard()
            self._reload_notices()
            self._refresh_status_bar()

        def failed(m: str) -> None:
            self._client_refresh_timer.stop()
            self.run_status.setText(f"Σφάλμα ανάκτησης: {m}")
            bar.finish(f"Σφάλμα ανάκτησης: {m}", "danger")
            self.reload_clients()

        self._tasks.append(run_task(self, work, on_progress=on_progress, on_done=done, on_error=failed))

    def open_client_detail(self, afm: str) -> None:
        if self._client_detail_open or (self._recent_client_detail_timer.isActive()
                                        and self._recent_client_detail_afm == afm):
            return
        self._client_detail_open = True
        notices_mod.clear_marks(self.conn, afm)            # ο χρήστης είδε τον πελάτη: οι αστερίσκοι του σβήνουν
        try:
            dlg = ClientDetailDialog(self, afm)
            dlg.exec()
        finally:
            self._client_detail_open = False
            self._recent_client_detail_afm = afm
            self._recent_client_detail_timer.start()
            self.reload_clients()
            self.reload_dashboard()

    # ------------------------------------------------------------------ Ημερολόγιο
    def _calendar_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        self.calendar_tabs = QTabWidget()
        outer.addWidget(self.calendar_tabs, 1)
        calendar_page = QWidget()
        calendar_root = QVBoxLayout(calendar_page)
        self.cal_stack = QStackedWidget()
        calendar_root.addWidget(self.cal_stack, 1)

        # ---- μηνιαία προβολή: μία γραμμή ανά ημέρα με υποχρεώσεις ----
        month_page = QWidget()
        root = QVBoxLayout(month_page)
        top = QHBoxLayout()
        prev_btn = QPushButton("‹")
        prev_btn.setFixedWidth(34)
        prev_btn.clicked.connect(lambda: self._shift_month(-1))
        top.addWidget(prev_btn)
        self.cal_label = QLabel("")
        self.cal_label.setObjectName("h1")
        top.addWidget(self.cal_label)
        next_btn = QPushButton("›")
        next_btn.setFixedWidth(34)
        next_btn.clicked.connect(lambda: self._shift_month(1))
        top.addWidget(next_btn)
        top.addStretch()
        self.cal_client = QComboBox()
        self.cal_client.addItem("Όλοι οι πελάτες", "")
        self.cal_client.currentIndexChanged.connect(self.reload_calendar)
        top.addWidget(self.cal_client)
        root.addLayout(top)

        toggles = QHBoxLayout()
        self.cal_rules = QCheckBox("κανονικές προθεσμίες")
        self.cal_rules.setChecked(True)
        self.cal_cond = QCheckBox("υπό προϋποθέσεις")
        self.cal_cond.setChecked(True)
        self.cal_news = QCheckBox("προθεσμίες από νέα")
        self.cal_news.setChecked(True)
        for chk in (self.cal_rules, self.cal_cond, self.cal_news):
            chk.toggled.connect(self.reload_calendar)
            toggles.addWidget(chk)
        toggles.addStretch()
        root.addLayout(toggles)

        self.cal_grid_box = grid_box = QWidget()
        grid_root = QVBoxLayout(grid_box)
        grid_root.setContentsMargins(0, 0, 0, 0)
        grid_root.setSpacing(4)
        # ΠΡΟΣΟΧΗ: η κεφαλίδα ημερών ΠΡΕΠΕΙ να έχει το ΙΔΙΟ stretch ανά στήλη με το grid των ημερών από κάτω
        # (setColumnStretch(col, 1) παρακάτω) — χωρίς `, 1` εδώ, τα labels έμεναν στο φυσικό τους πλάτος
        # (πακεταρισμένα αριστερά) ενώ οι στήλες του grid απλώνονταν ίσες σε όλο το πλάτος, άρα οι επικεφαλίδες
        # «Δε Τρ Τε …» δεν ευθυγραμμίζονταν με τις αντίστοιχες στήλες ημερών (λάθος στοίχιση, ειδικά σε πλατύ παράθυρο).
        heads = QHBoxLayout()
        heads.setSpacing(6)
        for name in _WEEKDAY_HEADS:
            lbl = QLabel(name)
            lbl.setObjectName("muted")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            heads.addWidget(lbl, 1)
        grid_root.addLayout(heads)
        self.cal_grid = QGridLayout()
        self.cal_grid.setSpacing(6)
        for col in range(7):
            self.cal_grid.setColumnStretch(col, 1)
        grid_root.addLayout(self.cal_grid, 1)
        self.cal_empty_label = QLabel("Καμία υποχρέωση αυτόν τον μήνα με τα τρέχοντα φίλτρα.")
        self.cal_empty_label.setObjectName("muted")
        self.cal_empty_label.setVisible(False)
        grid_root.addWidget(self.cal_empty_label)
        grid_root.addWidget(self._calendar_legend())
        root.addWidget(grid_box, 1)
        self.cal_stack.addWidget(month_page)

        # ---- ημερήσια προβολή: υποχρεώσεις μίας μέρας, με κουμπί επιστροφής ----
        day_page = QWidget()
        droot = QVBoxLayout(day_page)
        dtop = QHBoxLayout()
        back_btn = QPushButton("‹ Πίσω στον μήνα")
        back_btn.clicked.connect(self._close_cal_day)
        dtop.addWidget(back_btn)
        self.cal_day_label = QLabel("")
        self.cal_day_label.setObjectName("h1")
        dtop.addWidget(self.cal_day_label)
        dtop.addStretch()
        droot.addLayout(dtop)
        dsplit = QSplitter(Qt.Orientation.Horizontal)
        left = QWidget()
        lbox = QVBoxLayout(left)
        lbox.setContentsMargins(0, 0, 0, 0)
        lbox.addWidget(_section_header("Υποχρεώσεις & νέα της ημέρας", "calendar"))
        self.cal_list = QListWidget()
        # κλικ: δείχνει δεξιά ποιους πελάτες αφορά· διπλό κλικ (ή Enter): ανοίγει το νέο σε popup
        self.cal_list.currentItemChanged.connect(lambda cur, _prev: self._show_cal_event_clients(cur))
        self.cal_list.itemActivated.connect(self._open_cal_event)
        self.cal_list.setToolTip("Κλικ: ποιους πελάτες αφορά · Διπλό κλικ: άνοιγμα του νέου")
        lbox.addWidget(self.cal_list, 1)
        dsplit.addWidget(left)
        side = QWidget()
        sbox = QVBoxLayout(side)
        sbox.setContentsMargins(0, 0, 0, 0)
        self.cal_clients_title = QLabel("Πελάτες που αφορά")
        self.cal_clients_title.setStyleSheet("font-weight:600;")
        sbox.addWidget(self.cal_clients_title)
        self.cal_clients_hint = QLabel("")
        self.cal_clients_hint.setObjectName("muted")
        self.cal_clients_hint.setWordWrap(True)
        sbox.addWidget(self.cal_clients_hint)
        self.cal_clients_list = QListWidget()
        self.cal_clients_list.setWordWrap(True)
        self.cal_clients_list.itemActivated.connect(self._open_cal_client)
        self.cal_clients_list.currentItemChanged.connect(lambda current, _previous: self._update_cal_action_buttons(current))
        self.cal_clients_list.setToolTip("Διπλό κλικ: άνοιγμα της καρτέλας του πελάτη")
        sbox.addWidget(self.cal_clients_list, 1)
        # Έλεγχος υποβολής της ΣΥΓΚΕΚΡΙΜΕΝΗΣ προθεσμίας για τους υπόχρεους πελάτες της (ρητό αίτημα χρήστη 2026-10-22)
        cal_actions = QHBoxLayout()
        self.cal_check_btn = QPushButton(icon("check", CURRENT.txt, 16), "  Έλεγχος υποβολών")
        self.cal_check_btn.setToolTip("Ρωτά την ΑΑΔΕ αν οι υπόχρεοι πελάτες (με κωδικούς TAXISnet) υπέβαλαν αυτή τη "
                                      "δήλωση — μόνο ανάγνωση. Διακοπή μετά τον τρέχοντα πελάτη από το μήνυμα προόδου.")
        self.cal_check_btn.clicked.connect(self._check_cal_event)
        cal_actions.addWidget(self.cal_check_btn)
        self.cal_done_btn = QPushButton("  Ολοκληρώθηκε")
        self.cal_done_btn.setToolTip("Σημειώνει (ή αναιρεί) την προθεσμία ως ολοκληρωμένη για τον επιλεγμένο πελάτη — για "
                                     "δηλώσεις χωρίς αυτόματο έλεγχο (π.χ. ψηφιακό τέλος συναλλαγής)")
        self.cal_done_btn.clicked.connect(self._toggle_cal_done)
        cal_actions.addWidget(self.cal_done_btn)
        self.cal_check_btn.hide()
        self.cal_done_btn.hide()
        cal_actions.addStretch()
        sbox.addLayout(cal_actions)
        dsplit.addWidget(side)
        dsplit.setStretchFactor(0, 1)
        dsplit.setStretchFactor(1, 1)
        droot.addWidget(dsplit, 1)
        self.cal_stack.addWidget(day_page)

        self._cal_month = date.today().replace(day=1)
        self._cal_view_day: Optional[date] = None
        self._cal_events: list[dict] = []
        self.calendar_tabs.addTab(calendar_page, "Ημερολόγιο")

        work_page = QWidget()
        work_root = QVBoxLayout(work_page)
        work_head = QHBoxLayout()
        work_head.addWidget(_section_header("Υποχρεώσεις ανά εργασία", "check"))
        work_head.addStretch()
        # «Υπό προϋποθέσεις» χωριστά (ρητό αίτημα 2026-10-27): προεπιλογή ΜΟΝΟ οι επιβεβαιωμένες — από myDATA, Μητρώο
        # ΑΑΔΕ, έλεγχο υποβολών ή επιλογές του Προφίλ — όχι οι πιθανές για όλους τους πελάτες
        self.work_scope = QComboBox()
        for label, value in (("Επιβεβαιωμένες υποχρεώσεις", "yes"), ("Επιβεβαιωμένες και υπό προϋποθέσεις", "all"),
                             ("Μόνο υπό προϋποθέσεις", "maybe")):
            self.work_scope.addItem(label, value)
        self.work_scope.setToolTip("Επιβεβαιωμένες = προκύπτουν από myDATA, Μητρώο ΑΑΔΕ, έλεγχο υποβολών ή το Προφίλ "
                                   "του πελάτη · υπό προϋποθέσεις = εξαρτώνται από κάτι που δεν ξέρουμε ακόμη")
        work_head.addWidget(QLabel("Εμφάνιση"))
        work_head.addWidget(self.work_scope)
        self.work_month = QComboBox()
        self.work_month.addItem("Όλοι οι μήνες", "")
        for month, name in enumerate(obligations.MONTHS_NOM, 1):
            self.work_month.addItem(name, month)
        self.work_month.setCurrentIndex(date.today().month)
        work_head.addWidget(QLabel("Μήνας"))
        work_head.addWidget(self.work_month)
        self.work_year = QSpinBox()
        self.work_year.setRange(2000, date.today().year + 5)
        self.work_year.setValue(self._fiscal_year)
        self.work_year.setFixedWidth(82)
        work_head.addWidget(QLabel("Έτος"))
        work_head.addWidget(self.work_year)
        work_root.addLayout(work_head)

        # Χωρίς καρτέλα «Συνοπτικός Πίνακας» (ρητό αίτημα 2026-10-27) — η σύνοψη μένει μία γραμμή πάνω από τις καρτέλες
        self.work_summary_counts = QLabel("")
        self.work_summary_counts.setObjectName("h2")
        work_root.addWidget(self.work_summary_counts)
        self.work_summary = QLabel("")
        self.work_summary.setWordWrap(True)
        self.work_summary.setObjectName("muted")
        work_root.addWidget(self.work_summary)
        self.work_tabs = QTabWidget()
        work_root.addWidget(self.work_tabs, 1)

        tasks_page = QWidget()
        tasks_root = QVBoxLayout(tasks_page)
        tasks_scroll = QScrollArea()
        tasks_scroll.setWidgetResizable(True)
        tasks_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Οι κάρτες ξαναστοιχίζονται με το πλάτος του παραθύρου (1–6 στήλες) αντί για σταθερές 4 (ρητό αίτημα 2026-10-27)
        self._work_scroll = tasks_scroll
        self._work_cols = 4
        self._work_cards: list[dict] = []
        tasks_scroll.viewport().installEventFilter(self)
        tasks_content = QWidget()
        tasks_content_layout = QVBoxLayout(tasks_content)
        self.work_pending_title = QLabel("Εκκρεμείς")
        self.work_pending_title.setObjectName("h2")
        tasks_content_layout.addWidget(self.work_pending_title)
        self.work_pending_grid = QGridLayout()
        self.work_pending_grid.setSpacing(8)
        tasks_content_layout.addLayout(self.work_pending_grid)
        self.work_completed_toggle = QToolButton()
        self.work_completed_toggle.setText("Ολοκληρωμένες")
        self.work_completed_toggle.setCheckable(True)
        self.work_completed_toggle.setChecked(True)
        self.work_completed_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.work_completed_toggle.setArrowType(Qt.ArrowType.DownArrow)
        self.work_completed_toggle.toggled.connect(
            lambda checked: (self.work_completed_grid_widget.setVisible(checked),
                             self.work_completed_toggle.setArrowType(Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow)))
        tasks_content_layout.addWidget(self.work_completed_toggle)
        self.work_completed_grid_widget = QWidget()
        self.work_completed_grid = QGridLayout(self.work_completed_grid_widget)
        self.work_completed_grid.setContentsMargins(0, 0, 0, 0)
        self.work_completed_grid.setSpacing(8)
        tasks_content_layout.addWidget(self.work_completed_grid_widget)
        tasks_content_layout.addStretch()
        tasks_scroll.setWidget(tasks_content)
        tasks_root.addWidget(tasks_scroll)
        self.work_tabs.addTab(tasks_page, "Εργασίες")

        obligors_page = QWidget()
        obligors_root = QVBoxLayout(obligors_page)
        self.work_selected_title = QLabel("Επιλέξτε υποχρέωση ώστε να μεταβείτε αυτόματα στην κατάσταση εργασιών.")
        self.work_selected_title.setObjectName("h2")
        obligors_root.addWidget(self.work_selected_title)
        # Ίδιος με τον πίνακα Πελατών (ρητό αίτημα 2026-10-27): checkbox για μαζικές ενέργειες, ταξινόμηση, χωνί σε
        # κάθε στήλη, στήλες Interactive με «Εργασία» να παίρνει ό,τι περισσεύει, πλάτη που θυμούνται.
        self.work_table = QTableWidget(0, 7)
        self.work_table.verticalHeader().setVisible(False)
        self.work_table.setAlternatingRowColors(True)
        self.work_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.work_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.work_kit = TableKit(self.work_table, [("", 30), ("Προθεσμία", 90), ("Μήνας", 125), ("Κατηγορία", 110),
                                                  ("Εργασία", 0), ("Πελάτης", 190), ("Κατάσταση", 145)],
                                 "calendar_work_items2", prefs=self._prefs, filterable=range(1, 7),
                                 placeholder="Αναζήτηση εργασίας, πελάτη ή μήνα…")
        self.work_table.horizontalHeaderItem(0).setToolTip("Επιλέξτε για μαζικές ενέργειες")
        obligors_root.addWidget(self.work_kit.bar)
        work_sel = QHBoxLayout()
        self.work_sel_label = QLabel("Καμία εργασία επιλεγμένη")
        self.work_sel_label.setObjectName("muted")
        work_sel.addWidget(self.work_sel_label)
        work_sel.addStretch()
        work_all = QPushButton("Επιλογή όλων")
        work_all.setToolTip("Επιλέγει όσες εργασίες φαίνονται (σέβεται αναζήτηση και φίλτρα)")
        work_all.clicked.connect(lambda: self._set_all_work_checked(True))
        work_sel.addWidget(work_all)
        work_none = QPushButton("Αποεπιλογή όλων")
        work_none.clicked.connect(lambda: self._set_all_work_checked(False))
        work_sel.addWidget(work_none)
        obligors_root.addLayout(work_sel)
        obligors_root.addWidget(self.work_table, 1)
        self.work_table.itemChanged.connect(lambda _i: self._update_work_actions())
        work_actions = QHBoxLayout()
        self.work_done_btn = QPushButton("Ολοκληρώθηκε")
        self.work_done_btn.clicked.connect(self._toggle_work_done)
        self.work_done_btn.hide()
        work_actions.addWidget(self.work_done_btn)
        self.work_check_btn = QPushButton(icon("check", CURRENT.txt, 16), "  Έλεγχος υποβολής")
        self.work_check_btn.clicked.connect(self._check_work_submission)
        self.work_check_btn.hide()
        work_actions.addWidget(self.work_check_btn)
        work_actions.addStretch()
        obligors_root.addLayout(work_actions)
        self.work_table.doubleClicked.connect(lambda *_: self._toggle_work_done())
        self.work_table.itemSelectionChanged.connect(self._update_work_actions)
        self.work_tabs.addTab(obligors_page, "Εργασίες / Υπόχρεοι")
        self._selected_work_group: Optional[tuple[str, str]] = None
        self._work_items: list[dict] = []
        self.work_month.currentIndexChanged.connect(self._reload_work_items)
        self.work_scope.currentIndexChanged.connect(self._reload_work_items)
        self.work_year.valueChanged.connect(self._reload_work_items)
        self.calendar_tabs.addTab(work_page, "Ανά εργασία")
        self.calendar_tabs.currentChanged.connect(lambda index: self._reload_work_items() if index == 1 else None)
        return page

    def _shift_month(self, delta: int) -> None:
        y, m = self._cal_month.year, self._cal_month.month + delta
        while m < 1:
            m += 12
            y -= 1
        while m > 12:
            m -= 12
            y += 1
        self._cal_month = date(y, m, 1)
        self.reload_calendar()

    def reload_calendar(self) -> None:
        """Δείχνει αμέσως ό,τι υπάρχει ήδη στη βάση, ΧΩΡΙΣ να περιμένει δίκτυο — το `ensure_month_synced` (HTTP GET
        στο taxheaven.gr) γίνεται σε background thread. Πριν έτρεχε συγχρονισμένα εδώ, και επειδή αυτή η συνάρτηση
        καλείται ΣΥΧΝΑ (εκκίνηση, αλλαγή μήνα, αλλαγή φίλτρου/πελάτη) πάγωνε όλο το παράθυρο σε κάθε τέτοια ενέργεια
        όποτε το δίκτυο ήταν αργό — αυτό ήταν το «κολλάει πολύ»."""
        self._render_calendar_from_db()
        first = self._cal_month

        def work(_progress):
            conn = dbmod.connect()  # δικό του thread — δες σχόλιο στο _start_lookup
            try:
                taxheaven_calendar.ensure_month_synced(conn, None, first.year, first.month)
            finally:
                conn.close()

        def done(_result) -> None:
            if self._cal_month != first:          # ο χρήστης μπορεί να άλλαξε μήνα όσο περιμέναμε το δίκτυο
                return
            try:
                self._render_calendar_from_db()
            except sqlite3.ProgrammingError:
                pass  # το παράθυρο έκλεισε (conn κλειστό) πριν προλάβει να παραδοθεί το αποτέλεσμα

        def failed(msg: str) -> None:
            log.warning("ensure_month_synced απέτυχε: %s", msg)

        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))

    def _render_calendar_from_db(self) -> None:
        first = self._cal_month
        self.cal_label.setText(i18n.month_year(first))
        last_day = pycal.monthrange(first.year, first.month)[1]
        afm = self.cal_client.currentData() or None
        self._cal_events = deadlines.events_between(
            self.conn, first, date(first.year, first.month, last_day), afm=afm,
            include_news=self.cal_news.isChecked(), include_rules=self.cal_rules.isChecked(),
            include_conditional=self.cal_cond.isChecked(), include_aml=True)
        self._fill_client_combo()
        if self._cal_view_day is not None:
            self._render_cal_day()
        else:
            self._render_cal_month()
        if self.calendar_tabs.currentIndex() == 1:
            self._reload_work_items()

    def _reload_work_items(self, *_args) -> None:
        if not hasattr(self, "work_table"):
            return
        year = self.work_year.value()
        month = self.work_month.currentData()
        rows = deadlines.work_items(self.conn, year)
        if month:
            rows = [r for r in rows if date.fromisoformat(r["date"]).month == month]
        scope = self.work_scope.currentData()
        if scope != "all":
            rows = [r for r in rows if r["status"] == scope]
        self._work_items = rows
        groups: dict[tuple[str, str], list[dict]] = {}
        for row in rows:
            groups.setdefault((row["rule_id"], row["date"]), []).append(row)
        cards = []
        for key, people in groups.items():
            required = [person for person in people if person["status"] == "yes"]
            completed = sum(person["submission"]["status"] in ("submitted", "done") for person in required)
            pending = len(required) - completed
            conditional = sum(person["status"] == "maybe" for person in people)
            cards.append({"key": key, "title": people[0]["title"], "date": key[1],
                          "category": people[0]["category"], "people": people, "required": len(required),
                          "completed": completed, "pending": pending, "conditional": conditional,
                          "is_complete": bool(required) and pending == 0})
        cards.sort(key=lambda card: (card["date"], card["title"]))
        self._work_cards = cards
        self._layout_work_cards()
        self.work_pending_title.setText(f"Εκκρεμείς ({sum(card['pending'] for card in cards)})")
        open_count = sum(not card["is_complete"] for card in cards)
        done_count = sum(card["is_complete"] for card in cards)
        payroll_count = sum(card["category"] == "Μισθοδοσία" for card in cards)
        self.work_summary_counts.setText(f"{open_count} εκκρεμείς εργασίες · {done_count} ολοκληρωμένες · "
                                         f"{payroll_count} εργασίες μισθοδοσίας · {len(cards) - payroll_count} εργασίες ΑΑΔΕ")
        self.work_summary.setText(f"{len(rows)} υποχρεώσεις για {obligations.MONTHS_NOM[month - 1] if month else 'όλους τους μήνες'} "
                                  f"{year}. Επιλέξτε εργασία στην καρτέλα «Εργασίες» για τους υπόχρεους.")
        selected_people = next((card["people"] for card in cards if card["key"] == self._selected_work_group), None)
        detail_rows = selected_people if selected_people is not None else rows
        self.work_selected_title.setText(
            next((card["title"] + " · " + date.fromisoformat(card["date"]).strftime("%d/%m/%Y")
                  for card in cards if card["key"] == self._selected_work_group),
                 "Όλες οι εργασίες και οι υπόχρεοι" if self._selected_work_group is None else
                 "Η επιλεγμένη εργασία δεν υπάρχει στα φίλτρα — επιλέξτε άλλη."))
        labels = {"submitted": "Υποβλήθηκε", "not_submitted": "Δεν υποβλήθηκε", "pending": "Εκκρεμεί",
                  "done": "Ολοκληρώθηκε", "": "Δεν ελέγχθηκε"}
        colours = {"submitted": CURRENT.ok, "done": CURRENT.ok, "not_submitted": CURRENT.bad,
                   "pending": CURRENT.warn, "": CURRENT.muted}
        checked = {self._work_key(d) for d in self._checked_work_rows(only_checked=True)}   # κρατιέται στο ξαναγέμισμα
        self.work_table.blockSignals(True)
        self.work_kit.begin()
        self.work_table.setRowCount(len(detail_rows))
        for index, row in enumerate(detail_rows):
            ref_month = row["ref_month"]
            parts = ref_month.split("-")
            month = (f"{obligations.MONTHS_NOM[int(parts[1]) - 1]} {parts[0]}"
                     if len(parts) == 2 and parts[1].isdigit() and 1 <= int(parts[1]) <= 12 else ref_month)
            status = row["submission"]["status"]
            cells = ["", date.fromisoformat(row["date"]).strftime("%d/%m/%Y"), month, row["category"], row["title"],
                     f"{row['name']} · {row['afm']}", labels.get(status, status)]
            for column, text in enumerate(cells):
                item = SortItem(text)
                item.setToolTip(row["reason"] or row["description"])
                if column == _W_CHK:
                    item.setData(Qt.ItemDataRole.UserRole, row)
                    item.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    item.setCheckState(Qt.CheckState.Checked if self._work_key(row) in checked else Qt.CheckState.Unchecked)
                if column == _W_STATUS:
                    item.setForeground(QColor(colours.get(status, CURRENT.muted)))
                self.work_table.setItem(index, column, item)
        self.work_kit.end()
        self.work_table.blockSignals(False)
        self._update_work_actions()

    def eventFilter(self, obj, event) -> bool:              # noqa: N802 — Qt API
        if (event.type() == QEvent.Type.Resize and hasattr(self, "_work_scroll")
                and obj is self._work_scroll.viewport() and self._work_columns() != self._work_cols):
            self._layout_work_cards()
        return super().eventFilter(obj, event)

    _WORK_CARD_MIN = 240                                     # ελάχιστο πλάτος κάρτας εργασίας (px)

    def _work_columns(self) -> int:
        width = self._work_scroll.viewport().width() if hasattr(self, "_work_scroll") else 1000
        return max(1, min(6, (width - 24) // self._WORK_CARD_MIN))

    def _layout_work_cards(self) -> None:
        self._work_cols = self._work_columns()
        self._render_work_cards([c for c in self._work_cards if not c["is_complete"]], self.work_pending_grid)
        self._render_work_cards([c for c in self._work_cards if c["is_complete"]], self.work_completed_grid)

    def _render_work_cards(self, cards: list[dict], grid: QGridLayout) -> None:
        cols = self._work_cols
        while grid.count():
            item = grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()                               # όχι «φάντασμα» ώσπου να σβηστεί
                widget.deleteLater()
        for column in range(6):
            grid.setColumnStretch(column, 1 if column < cols else 0)
        if not cards:
            empty = QLabel("Δεν υπάρχουν εργασίες σε αυτή την ενότητα.")
            empty.setObjectName("muted")
            grid.addWidget(empty, 0, 0, 1, cols)
            return
        for index, card in enumerate(cards):
            if card["is_complete"]:
                state = f"Ολοκληρώθηκαν {card['completed']}/{card['required']}"
                colour = CURRENT.ok
            else:
                state = f"Εκκρεμείς {card['pending']}/{card['required']}"
                colour = CURRENT.warn if card["conditional"] else CURRENT.bad
            if card["conditional"]:
                state += f" · υπό προϋποθέσεις {card['conditional']}"
            # Κάρτα με ΑΝΑΔΙΠΛΩΣΗ κειμένου: ένα QPushButton επιβάλλει πλάτος = μήκος τίτλου και «σπρώχνει» το παράθυρο
            # (οριζόντια κύλιση σε στενό παράθυρο — αναφορά χρήστη 2026-10-27)
            card_w = _WorkCard(f"<b>{html.escape(card['title'])}</b><br>"
                               f"{date.fromisoformat(card['date']).strftime('%d/%m/%Y')}<br>{html.escape(state)}", colour)
            card_w.setToolTip(f"{card['category']} · κλικ: εμφάνιση υπόχρεων")
            card_w.clicked.connect(lambda key=card["key"]: self._select_work_group(key))
            grid.addWidget(card_w, index // cols, index % cols)

    def _select_work_group(self, key: tuple[str, str]) -> None:
        self._selected_work_group = key
        self._reload_work_items()
        self.work_tabs.setCurrentIndex(1)

    @staticmethod
    def _work_key(row: dict) -> tuple:
        return (row["afm"], row["rule_id"], row["ref_month"], row["date"])

    def _checked_work_rows(self, only_checked: bool = False) -> list[dict]:
        """Οι τσεκαρισμένες ΟΡΑΤΕΣ εργασίες (όπως `_selected_client_afms`)· αν δεν υπάρχει καμία, η τρέχουσα γραμμή."""
        t = self.work_table
        out = []
        for r in range(t.rowCount()):
            item = t.item(r, _W_CHK)
            if item and not t.isRowHidden(r) and item.checkState() == Qt.CheckState.Checked:
                out.append(item.data(Qt.ItemDataRole.UserRole))
        if out or only_checked:
            return out
        row = t.currentRow()
        item = t.item(row, _W_CHK) if row >= 0 else None
        data = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        return [data] if data else []

    def _set_all_work_checked(self, checked: bool) -> None:
        """Επιλογή: μόνο οι ορατές· αποεπιλογή: όλες (ίδιο με τους Πελάτες)."""
        t = self.work_table
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        t.blockSignals(True)
        for r in range(t.rowCount()):
            if checked and t.isRowHidden(r):
                continue
            item = t.item(r, _W_CHK)
            if item:
                item.setCheckState(state)
        t.blockSignals(False)
        self._update_work_actions()

    def _auto_checked(self, rule_id: str, ref_month: str) -> bool:
        """Υπάρχει αυτόματος έλεγχος υποβολής; Intrastat μόνο αν έχουν οριστεί οι κωδικοί ΕΛ.ΣΤΑΤ του γραφείου —
        αλλιώς μένει το χειροκίνητο «Ολοκληρώθηκε»."""
        if rule_id == "intrastat" and not intrastat_mod.credentials(self.conn):
            return False
        return bool(obligations.rule_checks(rule_id, None, ref_month))

    def _has_auto_check(self, data: dict) -> bool:
        return self._auto_checked(data["rule_id"], data["ref_month"])

    def _toggle_work_done(self) -> None:
        rows = self._checked_work_rows()
        if not rows:
            toast(self, "Επιλέξτε πρώτα μια υποχρέωση.", "warn")
            return
        manual = [d for d in rows if not self._has_auto_check(d)]
        if not manual:
            toast(self, "Για αυτή την εργασία υπάρχει αυτόματος έλεγχος υποβολής.", "info")
            return
        done = any(d["submission"]["status"] != "done" for d in manual)   # όλες ήδη ολοκληρωμένες ⇒ αναίρεση
        for d in manual:
            deadlines.mark_done(self.conn, d["afm"], d["rule_id"], d["ref_month"], done)
        what = manual[0]["title"] if len(manual) == 1 else f"{len(manual)} εργασίες"
        skipped = len(rows) - len(manual)
        toast(self, ("Σημειώθηκε ως ολοκληρωμένη: " if done else "Αναιρέθηκε: ") + what
              + (f" · {skipped} με αυτόματο έλεγχο παραλείφθηκαν" if skipped else ""), "ok")
        self._reload_work_items()
        self._render_calendar_from_db()

    def _update_work_actions(self) -> None:
        rows = self._checked_work_rows()
        checked = self._checked_work_rows(only_checked=True)
        self.work_check_btn.setVisible(any(self._has_auto_check(d) for d in rows))
        self.work_done_btn.setVisible(any(not self._has_auto_check(d) for d in rows))
        total = self.work_table.rowCount()
        shown = sum(1 for r in range(total) if not self.work_table.isRowHidden(r))
        text = f"{len(checked)} επιλεγμένες" if checked else "Καμία εργασία επιλεγμένη"
        if shown != total:
            text += f"  ·  εμφανίζονται {shown} από {total}"
        self.work_sel_label.setText(text)

    def _check_work_submission(self) -> None:
        rows = [d for d in self._checked_work_rows() if self._has_auto_check(d)]
        if not rows:
            toast(self, "Επιλέξτε εργασία με διαθέσιμο αυτόματο έλεγχο.", "warn")
            return
        afms, years, missing = [], set(), 0
        for d in rows:
            has_taxis = bool(client_creds.get(self.conn, d["afm"]))
            has_ika = bool(efka.ika_credentials(self.conn, d["afm"]))
            if not has_taxis and not (d["rule_id"] == "apd" and has_ika):
                missing += 1
                continue
            if d["afm"] not in afms:
                afms.append(d["afm"])
            years.add(obligations.check_year(d["rule_id"], d["ref_month"]))
        if not afms:
            toast(self, "Χρειάζονται οι κωδικοί TAXISnet του πελάτη για τον έλεγχο υποβολής.", "warn")
            return
        if missing:
            toast(self, f"{missing} εργασίες παραλείφθηκαν — λείπουν οι κωδικοί TAXISnet του πελάτη.", "warn")
        want_pdf = self.ask_submission_pdf(self)
        if want_pdf is None:
            return
        self.run_submission_checks(afms, sorted(years), window=self, on_done=self._reload_work_items, want_pdf=want_pdf)

    def _calendar_legend(self) -> QWidget:
        """Οριζόντιο υπόμνημα χρωμάτων κάτω από το μηνιαίο ημερολόγιο (ρητό αίτημα χρήστη 2026-10-23)."""
        holder = QWidget()
        row = QHBoxLayout(holder)
        row.setContentsMargins(2, 6, 2, 0)
        row.setSpacing(16)
        for colour, text, mark in ((kind_colour("rule"), "προθεσμία (κανόνας)", "▌"),
                                   (kind_colour("general"), "ημερολόγιο ΑΑΔΕ/taxheaven", "▌"),
                                   (kind_colour("news"), "νέο που αφορά πελάτες", "▌"),
                                   (kind_colour("aml"), "δέουσα επιμέλεια", "▌"),
                                   (CURRENT.bad, "υπάρχουν υπόχρεοι πελάτες", "●"),
                                   (CURRENT.accent_deep, "σήμερα", "▢")):
            lbl = QLabel(f"<span style='color:{colour}; font-size:14px'>{mark}</span> {html.escape(text)}")
            lbl.setTextFormat(Qt.TextFormat.RichText)
            lbl.setObjectName("muted")
            row.addWidget(lbl)
        row.addStretch()
        return holder

    def _render_cal_month(self) -> None:
        self.cal_stack.setCurrentIndex(0)
        by_day: dict[str, list[dict]] = {}
        counts = deadlines.obligated_counts(self.conn, self._cal_events, afm=self.cal_client.currentData() or None)
        for ev in self._cal_events:
            ev["obligated"] = counts.get((ev["kind"], ev["id"]), 0)
            by_day.setdefault(ev["date"], []).append(ev)
        self.cal_empty_label.setVisible(not by_day)

        while self.cal_grid.count():
            item = self.cal_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

        first = self._cal_month
        today = date.today()
        # Ελληνικό ημερολόγιο: η εβδομάδα ξεκινά Δευτέρα (firstweekday=0), όχι Κυριακή.
        weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
        for row, week in enumerate(weeks):
            for col, day_date in enumerate(week):
                in_month = day_date.month == first.month
                evs = by_day.get(day_date.isoformat(), [])
                cell = _DayCell(day_date.day, evs, in_month=in_month, is_today=day_date == today)
                if in_month:
                    cell.clicked.connect(lambda d=day_date: self._open_cal_day_date(d))
                self.cal_grid.addWidget(cell, row, col)

    def _open_cal_day_date(self, d: date) -> None:
        self._cal_view_day = d
        self._render_cal_day()

    def _close_cal_day(self) -> None:
        self._cal_view_day = None
        self._render_cal_month()

    def _render_cal_day(self) -> None:
        self.cal_stack.setCurrentIndex(1)
        d = self._cal_view_day
        self.cal_day_label.setText(i18n.long_date(d))
        cur = self.cal_list.currentItem()
        prev = cur.data(Qt.ItemDataRole.UserRole) if cur is not None else None
        prev_key = (prev["kind"], prev["id"]) if prev else None
        self.cal_list.blockSignals(True)
        self.cal_list.clear()
        day_str = d.isoformat()
        for ev in self._cal_events:
            if ev["date"] != day_str:
                continue
            kind_label = {"news": "νέα", "rule": "κανόνας", "general": "ΑΑΔΕ/taxheaven",
                          "aml": "δέουσα επιμέλεια"}.get(ev["kind"], ev["kind"])
            text = f"{ev['title']}{_clients_suffix(ev)}  ·  {kind_label}"
            if ev.get("conditional"):
                text += " · υπό προϋποθέσεις"
            item = QListWidgetItem(dot_icon(kind_colour(ev["kind"])), text)
            badge = due_badge(ev["date"])
            if badge:
                item.setForeground(QColor(badge[1]))
            item.setData(Qt.ItemDataRole.UserRole, ev)
            self.cal_list.addItem(item)
        if self.cal_list.count() == 0:
            placeholder = QListWidgetItem("Καμία υποχρέωση αυτή την ημέρα.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.cal_list.addItem(placeholder)
        else:
            # κράτα την επιλογή αν ξαναζωγραφίζεται (π.χ. μετά τον συγχρονισμό δικτύου), αλλιώς διάλεξε την πρώτη
            row = 0
            for i in range(self.cal_list.count()):
                e = self.cal_list.item(i).data(Qt.ItemDataRole.UserRole)
                if e and (e["kind"], e["id"]) == prev_key:
                    row = i
                    break
            self.cal_list.setCurrentRow(row)
        self.cal_list.blockSignals(False)
        self._show_cal_event_clients(self.cal_list.currentItem())

    def _show_cal_event_clients(self, item: Optional[QListWidgetItem]) -> None:
        """Δεξί πλαίσιο της ημερήσιας προβολής: ποιους πελάτες αφορά η επιλεγμένη υποχρέωση/νέο."""
        lst = self.cal_clients_list
        lst.clear()
        self.cal_check_btn.hide()
        self.cal_done_btn.hide()
        ev = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        self.cal_clients_title.setText("Πελάτες που αφορά")
        self.cal_clients_hint.setText("")
        if not ev:
            self.cal_clients_hint.setText("Επιλέξτε ένα νέο από τη λίστα για να δείτε ποιους πελάτες αφορά.")
            return
        rows: list[dict] = []
        if ev["kind"] == "news":
            rows = [dict(r) for r in self.conn.execute(
                "SELECT m.afm, m.matched_reason, m.confidence, b.name FROM matches m JOIN businesses b ON b.afm=m.afm "
                "WHERE m.article_id=? ORDER BY b.name COLLATE NOCASE, b.afm", (ev["id"],))]
        elif ev["kind"] == "aml":
            b = clients.get(self.conn, ev["afm"])
            rows = [{"afm": ev["afm"], "name": b["name"] if b else "", "matched_reason": "Επανεξέταση δέουσας επιμέλειας",
                     "confidence": 1.0}]
        else:
            # κανονική προθεσμία ή γεγονός του ημερολογίου ΑΑΔΕ/taxheaven: υπόχρεοι από τα στοιχεία του Μητρώου
            people = deadlines.clients_for_event(self.conn, ev)
            if people is None:
                self.cal_clients_hint.setText("Γεγονός του ημερολογίου ΑΑΔΕ/taxheaven που δεν αντιστοιχεί σε γνωστή "
                                              "περιοδική υποχρέωση — δεν είναι γνωστό ποιους πελάτες αφορά.")
                return
            rows = [{"afm": c["afm"], "name": c["name"], "confidence": 1.0 if c["status"] == "yes" else 0.5,
                     "matched_reason": c["reason"] or "υπόχρεος", "maybe": c["status"] != "yes",
                     "submission": c.get("submission"), "rule_id": c.get("rule_id"), "ref_month": c.get("ref_month")}
                    for c in people]
            n_maybe = sum(1 for r in rows if r["maybe"])
            subs = [r["submission"]["status"] for r in rows if r.get("submission") and not r["maybe"]]
            n_ok = sum(1 for x in subs if x in ("submitted", "done"))
            n_miss = sum(1 for x in subs if x == "not_submitted")
            self.cal_clients_hint.setText(
                f"{len(rows) - n_maybe} υπόχρεοι · ✓ {n_ok} ολοκληρωμένες · ✗ {n_miss} χωρίς υποβολή · "
                f"{len(rows) - n_maybe - n_ok - n_miss} δεν έχουν ελεγχθεί"
                + (f" · {n_maybe} υπό προϋποθέσεις (π.χ. προσωπικό, ενδοκοινοτικές — άγνωστο)." if n_maybe else "."))
        self.cal_clients_title.setText(f"Πελάτες που αφορά ({len(rows)})")
        if not rows:
            self.cal_clients_hint.setText("Κανένας πελάτης δεν έχει ταιριάξει με αυτό το νέο." if ev["kind"] == "news" else
                                          "Κανένας από τους πελάτες σας δεν είναι υπόχρεος (με βάση τα στοιχεία του Μητρώου).")
            return
        sub_colour = {"submitted": CURRENT.ok, "done": CURRENT.ok, "not_submitted": CURRENT.bad, "pending": CURRENT.warn}
        for r in rows:
            text = f"{r['name'] or r['afm']}  ·  {r['afm']}\n{r['matched_reason']}"      # 2 γραμμές: χωρίς οριζόντιο scroll
            colour = None
            if r["confidence"] < 1:
                text += "  ·  υπό προϋποθέσεις" if r.get("maybe") else "  ·  να επιβεβαιωθεί"
                colour = CURRENT.warn
            sub = r.get("submission")
            if sub and sub["status"]:
                text += f"\n{sub['label']}"
                amounts = obligations.amounts_text(sub["amounts"])
                if amounts:
                    text += f" — {amounts}"
                colour = sub_colour.get(sub["status"], colour)
            elif sub is not None and not r.get("maybe"):
                text += "\n" + ("Υποβολή: χειροκίνητη σήμανση" if sub["manual"] else "Υποβολή: δεν έχει ελεγχθεί")
            it = QListWidgetItem(dot_icon(colour or CURRENT.ok), text)
            if colour and colour != CURRENT.ok:
                it.setForeground(QColor(colour))
            it.setData(Qt.ItemDataRole.UserRole, r["afm"])
            it.setData(Qt.ItemDataRole.UserRole + 1, r)
            lst.addItem(it)

    def _update_cal_action_buttons(self, item: Optional[QListWidgetItem]) -> None:
        row = item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None
        rule_id, ref_month = (row.get("rule_id"), row.get("ref_month")) if row else (None, "")
        has_check = bool(rule_id and self._auto_checked(rule_id, ref_month))
        self.cal_check_btn.setVisible(has_check and not (row or {}).get("maybe"))
        self.cal_done_btn.setVisible(bool(rule_id) and not has_check and not (row or {}).get("maybe"))

    def _cal_event_rows(self) -> list[dict]:
        out = []
        for i in range(self.cal_clients_list.count()):
            r = self.cal_clients_list.item(i).data(Qt.ItemDataRole.UserRole + 1)
            if r and r.get("rule_id"):
                out.append(r)
        return out

    def _check_cal_event(self) -> None:
        rows = [r for r in self._cal_event_rows() if not r.get("maybe")
            and self._auto_checked(r["rule_id"], r["ref_month"])]
        if not rows:
            toast(self, "Επιλέξτε μια προθεσμία με υπόχρεους πελάτες.", "warn")
            return
        with_creds = {a for a, c in client_creds.status_map(self.conn).items() if c["complete"]}
        afms = [r["afm"] for r in rows if r["afm"] in with_creds]
        if not afms:
            toast(self, "Κανένας υπόχρεος πελάτης δεν έχει κωδικούς TAXISnet.", "warn")
            return
        years = sorted({obligations.check_year(r["rule_id"], r["ref_month"]) for r in rows})
        self.run_submission_checks(afms, years, on_done=lambda: self._show_cal_event_clients(self.cal_list.currentItem()))

    def _toggle_cal_done(self) -> None:
        item = self.cal_clients_list.currentItem()
        r = item.data(Qt.ItemDataRole.UserRole + 1) if item is not None else None
        if not r or not r.get("rule_id"):
            toast(self, "Επιλέξτε πρώτα έναν πελάτη μιας προθεσμίας.", "warn")
            return
        if self._auto_checked(r["rule_id"], r["ref_month"]):
            toast(self, "Για αυτή την προθεσμία υπάρχει αυτόματος έλεγχος υποβολής.", "info")
            return
        done = (r.get("submission") or {}).get("status") != "done"
        deadlines.mark_done(self.conn, r["afm"], r["rule_id"], r["ref_month"], done)
        toast(self, f"{r['name'] or r['afm']}: " + ("σημειώθηκε ως ολοκληρωμένο." if done else "αναιρέθηκε η σήμανση."), "ok")
        self._show_cal_event_clients(self.cal_list.currentItem())

    def _open_cal_client(self, item: QListWidgetItem) -> None:
        afm = item.data(Qt.ItemDataRole.UserRole)
        if afm:
            self.open_client_detail(afm)

    def _fill_client_combo(self) -> None:
        current = self.cal_client.currentData()
        self.cal_client.blockSignals(True)
        self.cal_client.clear()
        self.cal_client.addItem("Όλοι οι πελάτες", "")
        for r in clients.list_all(self.conn):
            self.cal_client.addItem(r["name"] or r["afm"], r["afm"])
        idx = self.cal_client.findData(current or "")
        self.cal_client.setCurrentIndex(idx if idx >= 0 else 0)
        self.cal_client.blockSignals(False)

    def _open_cal_event(self, item: QListWidgetItem) -> None:
        ev = item.data(Qt.ItemDataRole.UserRole)
        if ev.get("kind") == "aml":
            self.aml_page.open_assessment(ev["afm"])
            return
        summary, action = ev.get("description", ""), ""
        if ev.get("kind") == "news":
            art = self.conn.execute("SELECT * FROM articles WHERE id=?", (ev["id"],)).fetchone()
            if art:
                summary, action = self._article_preview(art)
        self._open_event_dialog(ev, summary=summary, action=action)

    # ------------------------------------------------------------------ Νέα & Matches
    def _news_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        top = QHBoxLayout()
        title = QLabel("Νέα & Matches")
        title.setObjectName("h1")
        top.addWidget(title)
        top.addStretch()
        self.news_only = QComboBox()
        for value, label in (("matched", "Που αφορούν πελάτες"), ("all", "Όλα"), ("irrelevant", "Άσχετα"),
                             ("pending", "Σε αναμονή"), ("skipped", "Φιλτραρισμένα"), ("duplicate", "Διπλότυπα")):
            self.news_only.addItem(label, value)
        self.news_only.currentIndexChanged.connect(self.reload_news)
        top.addWidget(self.news_only)
        top.addWidget(self._reload_button(self.reload_news, "Ξαναδιαβάζει τη λίστα νέων"))
        root.addLayout(top)

        self.news_table = QTableWidget(0, len(_NEWS_COLS))
        self.news_table.verticalHeader().setVisible(False)
        self.news_table.setAlternatingRowColors(True)
        self.news_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.news_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.news_table.doubleClicked.connect(self._open_selected_news)
        self._news_kit = TableKit(self.news_table, [(c[0], c[1]) for c in _NEWS_COLS], "news", prefs=self._prefs,
                                  placeholder="Αναζήτηση άρθρου (τίτλος, πηγή, κατάσταση…)")
        self._news_col_filter = self._news_kit.col_filter
        root.addWidget(self._news_kit.bar)
        root.addWidget(self.news_table, 1)
        return page

    def reload_news(self) -> None:
        only = self.news_only.currentData() or "matched"
        cutoff = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        sql = ("SELECT a.*, (SELECT COUNT(*) FROM matches m WHERE m.article_id=a.id) AS n_matches FROM articles a "
              "WHERE COALESCE(a.published_at,a.fetched_at) >= ?")
        args: list[Any] = [cutoff]
        if only == "matched":
            sql += " AND EXISTS (SELECT 1 FROM matches m WHERE m.article_id=a.id)"
        elif only != "all":
            sql += " AND a.extraction_status=?"
            args.append(only)
        sql += " ORDER BY COALESCE(a.published_at,a.fetched_at) DESC LIMIT 300"
        rows = self.conn.execute(sql, args).fetchall()
        table = self.news_table
        self._news_kit.begin()
        table.setRowCount(len(rows))
        status_labels = {"pending": "σε αναμονή", "done": "αναλύθηκε", "failed": "αποτυχία", "irrelevant": "άσχετο",
                         "skipped": "φιλτραρίστηκε", "duplicate": "διπλότυπο"}
        for i, a in enumerate(rows):
            table.setItem(i, 0, QTableWidgetItem(a["source"]))
            table.setItem(i, 1, QTableWidgetItem((a["published_at"] or a["fetched_at"] or "")[:10]))
            title_item = QTableWidgetItem(a["title"])
            title_item.setData(Qt.ItemDataRole.UserRole, dict(a))
            table.setItem(i, 2, title_item)
            table.setItem(i, 3, QTableWidgetItem(status_labels.get(a["extraction_status"], a["extraction_status"])))
            table.setItem(i, 4, SortItem(str(a["n_matches"]), int(a["n_matches"])))
        self._news_kit.end()
        resort(table, 1)

    @staticmethod
    def _article_preview(a) -> tuple[str, str]:
        """(περίληψη, απαιτούμενη ενέργεια) για το popup ενός άρθρου (`articles` row ή dict)."""
        summary, action = "", ""
        try:
            ex = json.loads(a["extracted_json"] or "{}")
            summary, action = ex.get("summary", ""), ex.get("action_required") or ""
        except ValueError:
            pass
        if not summary:
            # Δεν έχει αναλυθεί ακόμη με LLM (σε αναμονή/φιλτραρίστηκε/απέτυχε) — δείξε ό,τι κείμενο έχουμε ήδη
            # ανακτήσει αντί για άδειο διάλογο: το πλήρες κείμενο του άρθρου, αλλιώς την περίληψη του RSS feed.
            summary = (a["full_text"] or a["raw_summary"] or "")[:2000]
        return summary, action

    def _open_selected_news(self) -> None:
        row = self.news_table.currentRow()
        if row < 0:
            return
        a = self.news_table.item(row, 2).data(Qt.ItemDataRole.UserRole)
        summary, action = self._article_preview(a)
        people = deadlines.clients_for_article(self.conn, a["id"]) if a["n_matches"] else None
        if people is None and a["extraction_status"] in ("pending", "failed"):
            # Δεν έχει αναλυθεί ακόμη από το LLM: πιθανοί πελάτες από τον ταξινομητή ML (πάντα «υπό προϋποθέσεις»).
            people = ml.suggest_for_article(self.conn, a) or None
        NewsDialog(a["title"], a["url"], meta=f"{a['source']} · {(a['published_at'] or '')[:10]}",
                  summary=summary, action=action, parent=self, clients=people,
                  on_client=self.open_client_detail).exec()

    # ------------------------------------------------------------------ Ρυθμίσεις
    def _settings_page(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        page = QWidget()
        root = QVBoxLayout(page)
        title = QLabel("Ρυθμίσεις")
        title.setObjectName("h1")
        root.addWidget(title)
        self.office_box = OfficeSettingsBox(self.conn, self)
        root.addWidget(self.office_box)
        self.services_box = ServicesBox(self.conn, self)
        root.addWidget(self.services_box)

        keys_box = QGroupBox("Κλειδιά && credentials")
        kf = QFormLayout(keys_box)
        self._settings_fields: dict[str, QLineEdit] = {}
        self._settings_status: dict[str, QLabel] = {}
        for key, label, echo in (("groq_api_key", "Groq API key", True), ("openrouter_api_key", "OpenRouter API key", True),
                                 ("business_portal_key", "Business Portal (ΓΕΜΗ) API key", True),
                                 ("aade_user", "TAXISnet χρήστης γραφείου", False), ("aade_pass", "TAXISnet κωδικός γραφείου", True),
                                 # ΕΛ.ΣΤΑΤ Intrastat: ένας λογαριασμός του γραφείου — έλεγχος υποβολής ανά πελάτη
                                 ("intrastat_user", "Intrastat ΕΛ.ΣΤΑΤ — email γραφείου", False),
                                 ("intrastat_pass", "Intrastat ΕΛ.ΣΤΑΤ — κωδικός γραφείου", True),
                                 # ειδικοί κωδικοί «Αναζήτηση Βασικών Στοιχείων Μητρώου» — ατομική επιχείρηση μελών εταιρειών
                                 ("rg_user", "Ειδικοί κωδικοί Μητρώου — χρήστης", False),
                                 ("rg_pass", "Ειδικοί κωδικοί Μητρώου — κωδικός", True)):
            field = QLineEdit()
            if echo:
                field.setEchoMode(QLineEdit.EchoMode.Password)
            self._settings_fields[key] = field
            # Το πεδίο ΠΟΤΕ δεν δείχνει την αποθηκευμένη τιμή (ασφάλεια) — χωρίς αυτή την ετικέτα ο χρήστης δεν έχει
            # τρόπο να ξέρει αν έχει ήδη αποθηκεύσει κάτι εδώ, βλέπει μόνο ένα άδειο πεδίο κάθε φορά.
            status = QLabel("")
            status.setObjectName("muted")
            self._settings_status[key] = status
            row = QHBoxLayout()
            row.addWidget(field, 1)
            row.addWidget(status)
            kf.addRow(label, row)
        save_keys = QPushButton("Αποθήκευση κλειδιών")
        save_keys.setObjectName("primary")
        save_keys.clicked.connect(self._save_secret_keys)
        kf.addRow("", save_keys)
        test_row = QHBoxLayout()
        test_llm = QPushButton("Δοκιμή LLM")
        test_llm.clicked.connect(self._test_llm)
        test_aade = QPushButton("Δοκιμή ΑΑΔΕ (TAXISnet)")
        test_aade.clicked.connect(self._test_aade)
        test_row.addWidget(test_llm)
        test_row.addWidget(test_aade)
        kf.addRow("", QWidget())
        root.addWidget(keys_box)
        root.addLayout(test_row)

        llm_box = QGroupBox("Ανάλυση άρθρων (LLM) — προτιμώνται δωρεάν μοντέλα, με αυτόματη εναλλαγή")
        lf = QFormLayout(llm_box)
        self.llm_provider = QComboBox()
        self.llm_provider.addItem("Groq", "groq")
        self.llm_provider.addItem("OpenRouter", "openrouter")
        lf.addRow("Πάροχος", self.llm_provider)
        self.llm_model_groq = QComboBox()
        self.llm_model_groq.setEditable(True)
        lf.addRow("Μοντέλο Groq", self.llm_model_groq)
        self.llm_model_openrouter = QComboBox()
        self.llm_model_openrouter.setEditable(True)
        lf.addRow("Μοντέλο OpenRouter", self.llm_model_openrouter)
        refresh_models = QPushButton("⟳  Ανανέωση λίστας μοντέλων (τρέχων πάροχος, χρειάζεται αποθηκευμένο κλειδί)")
        refresh_models.clicked.connect(self._refresh_llm_models)
        lf.addRow("", refresh_models)
        note = QLabel("Αν το μοντέλο απορριφθεί (π.χ. δεν υπάρχει πια), η εφαρμογή δοκιμάζει αυτόματα δωρεάν εναλλακτικές· "
                     "αν καμία δεν δουλέψει, θα σας προτείνει ρητά μετάβαση σε πληρωμένο μοντέλο — ποτέ αυτόματα.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        lf.addRow("", note)
        save_llm = QPushButton("Αποθήκευση")
        save_llm.clicked.connect(self._save_llm_settings)
        lf.addRow("", save_llm)
        root.addWidget(llm_box)
        ml_box = QGroupBox("Μηχανική μάθηση (όπως στο paperless-ngx)")
        ml_lay = QVBoxLayout(ml_box)
        self.ml_status_label = QLabel("")
        self.ml_status_label.setWordWrap(True)
        ml_lay.addWidget(self.ml_status_label)
        root.addWidget(ml_box)

        src_box = QGroupBox("Πηγές ειδήσεων")
        sf = QVBoxLayout(src_box)
        self._source_checks: dict[str, QCheckBox] = {}
        for s in sources.SOURCES:
            chk = QCheckBox(s.name + ("" if s.available else " (μη διαθέσιμη)"))
            chk.setEnabled(s.available)
            self._source_checks[s.id] = chk
            sf.addWidget(chk)
        save_src = QPushButton("Αποθήκευση πηγών")
        save_src.clicked.connect(self._save_sources)
        sf.addWidget(save_src)
        root.addWidget(src_box)

        sched_box = QGroupBox("Καθημερινός έλεγχος (Windows Task Scheduler)")
        sf2 = QHBoxLayout(sched_box)
        self.sched_time = QLineEdit()
        self.sched_time.setPlaceholderText("08:00")
        self.sched_time.setFixedWidth(70)
        sf2.addWidget(QLabel("Ώρα:"))
        sf2.addWidget(self.sched_time)
        sched_on = QPushButton("Ενεργοποίηση/ενημέρωση")
        sched_on.clicked.connect(self._install_schedule)
        sf2.addWidget(sched_on)
        sched_off = QPushButton("Απενεργοποίηση")
        sched_off.clicked.connect(self._remove_schedule)
        sf2.addWidget(sched_off)
        # Λήψη myDATA (έσοδα + έξοδα, μόνο οι μήνες που χρειάζονται) στον ίδιο καθημερινό έλεγχο — mydata/scheduled.py
        self.sched_mydata = QCheckBox("και λήψη myDATA")
        self.sched_mydata.setToolTip("Για πελάτες με κωδικούς myDATA: τρέχων + προηγούμενος μήνας και όσοι λείπουν — "
                                     "εντοπίζει παρακρατήσεις, τέλος ανθεκτικότητας (8.2), ψηφιακό τέλος συναλλαγής, VIES/"
                                     "Intrastat για το Ημερολόγιο. Χωρίς PDF.")
        self.sched_mydata.toggled.connect(
            lambda on: settings_store.set_value(self.conn, "daily_mydata", "1" if on else "0"))
        sf2.addWidget(self.sched_mydata)
        self.sched_requests = QCheckBox("και αιτήματα ΑΑΔΕ")
        self.sched_requests.setToolTip("«Τα Αιτήματά μου» κάθε πελάτη με κωδικούς: ειδοποίηση όταν απαντηθεί ή περιμένει "
                                       "ενέργεια του πελάτη")
        self.sched_requests.toggled.connect(
            lambda on: settings_store.set_value(self.conn, "daily_requests", "1" if on else "0"))
        sf2.addWidget(self.sched_requests)
        self.fetch_members = QCheckBox("Άντληση μελών εταιρειών")
        self.fetch_members.setToolTip("Με την ανάκτηση στοιχείων, και τα μέλη/εκπρόσωποι κάθε εταιρείας (όχι ατομικής) από "
                                      "το Μητρώο ΑΑΔΕ — καρτέλα εταιρείας → «Μέλη»")
        self.fetch_members.toggled.connect(
            lambda on: settings_store.set_value(self.conn, "fetch_members", "1" if on else "0"))
        sf2.addWidget(self.fetch_members)
        sf2.addStretch()
        root.addWidget(sched_box)
        self.email_box = EmailSettingsBox(self.conn, self)
        root.addWidget(self.email_box)

        pass_box = QGroupBox("Κύριος κωδικός")
        pf = QHBoxLayout(pass_box)
        pf.addWidget(QLabel("Προαιρετική προστασία του φακέλου δεδομένων με κωδικό."))
        pf.addStretch()
        manage_btn = QPushButton("Διαχείριση…")
        manage_btn.clicked.connect(lambda: unlock.manage(crypto.keyfile_path(), self))
        pf.addWidget(manage_btn)
        root.addWidget(pass_box)

        backup_box = QGroupBox("Αντίγραφο ασφαλείας βάσης")
        bf = QHBoxLayout(backup_box)
        bf.addWidget(QLabel("Η βάση αντιγράφεται αυτόματα πριν από κάθε εισαγωγή Excel και κάθε έλεγχο."))
        bf.addStretch()
        backup_now_btn = QPushButton(icon("backup", CURRENT.txt, 16), "  Αντίγραφο τώρα")
        backup_now_btn.clicked.connect(self._backup_now)
        bf.addWidget(backup_now_btn)
        restore_btn = QPushButton(icon("restore", CURRENT.warn, 16), "  Επαναφορά…")
        restore_btn.clicked.connect(self._restore_backup)
        bf.addWidget(restore_btn)
        root.addWidget(backup_box)
        root.addStretch()

        outer.setWidget(page)
        return outer

    def reload_settings(self) -> None:
        try:
            st = json.loads(settings_store.get(self.conn, "ml_status") or "{}")
        except ValueError:
            st = {}
        if st:
            gate = ("ενεργή" if ml.gate_ok(st) else
                    f"ανενεργή μέχρι να δείξει ακρίβεια ≥{ml.GATE_MIN_PRECISION:.0%} σε αρκετά άρθρα")
            self.ml_status_label.setText(
                f"Μαθαίνει από τις αποφάσεις της ανάλυσης και τα 👍/👎 σας: {st.get('n_relevance', 0)} άρθρα "
                f"({st.get('n_pos', 0)} σχετικά) · μοντέλα για {st.get('n_clients', 0)} πελάτες · ακρίβεια "
                f"{st.get('accuracy', 0):.0%} σε δείγμα ελέγχου · τελευταία εκπαίδευση {str(st.get('trained_at', ''))[:10]}.\n"
                f"Τα άρθρα σε αναμονή αναλύονται με σειρά πιθανότητας να αφορούν πελάτες· παράκαμψη LLM για τα σχεδόν "
                f"σίγουρα άσχετα: {gate}.")
        else:
            self.ml_status_label.setText("Θα εκπαιδευτεί στον επόμενο «Έλεγχο τώρα», από τα άρθρα που έχει ήδη αναλύσει το LLM.")
        self.llm_provider.setCurrentIndex(self.llm_provider.findData(settings_store.get(self.conn, "llm_provider")))
        self._sync_model_combo(self.llm_model_groq, "groq", settings_store.get(self.conn, "llm_model_groq"))
        self._sync_model_combo(self.llm_model_openrouter, "openrouter", settings_store.get(self.conn, "llm_model_openrouter"))
        for sid, chk in self._source_checks.items():
            src = sources.BY_ID[sid]
            chk.setChecked(settings_store.source_enabled(self.conn, sid, src.default_enabled) and src.available)
        self.sched_time.setText(settings_store.get(self.conn, "daily_time") or "08:00")
        for box, key, on_default in ((self.sched_mydata, "daily_mydata", True), (self.sched_requests, "daily_requests", False),
                                     (self.fetch_members, "fetch_members", False)):
            box.blockSignals(True)
            box.setChecked(settings_store.get(self.conn, key) == "1" or (on_default and settings_store.get(self.conn, key) != "0"))
            box.blockSignals(False)
        for key, status in self._settings_status.items():
            if settings_store.is_set(self.conn, key):
                status.setText("✓ Αποθηκευμένο")
                status.setStyleSheet(f"color:{CURRENT.ok};")
            else:
                status.setText("— δεν έχει οριστεί")
                status.setStyleSheet(f"color:{CURRENT.muted};")

    def _save_secret_keys(self) -> None:
        for key, field in self._settings_fields.items():
            value = field.text().strip()
            if not value:
                continue
            settings_store.set_value(self.conn, key, value)
            field.clear()
            if key in ("groq_api_key", "openrouter_api_key"):
                settings_store.set_value(self.conn, "llm_last_error", "")  # νέο κλειδί: σβήνει το παλιό μόνιμο banner
            if key in ("aade_user", "aade_pass"):
                settings_store.set_value(self.conn, "aade_office_status", "")  # νέοι κωδικοί: ξεκινά από την αρχή
                settings_store.set_value(self.conn, "aade_office_message", "")
        toast(self, "Τα credentials αποθηκεύτηκαν (κρυπτογραφημένα).", "ok")
        self.reload_settings()
        self._reload_notices()

    def _sync_model_combo(self, combo: QComboBox, provider: str, current: str, models: Optional[list[str]] = None) -> None:
        """Γεμίζει/ξαναταξινομεί το dropdown μοντέλων: πρώτα τα δωρεάν, μετά τα πληρωμένα (αλφαβητικά μέσα σε κάθε
        ομάδα). Το `current` (π.χ. ό,τι έχει αποθηκευτεί στις Ρυθμίσεις, ακόμη κι αν προέκυψε αυτόματα από
        `recover_model`) εμφανίζεται ΠΑΝΤΑ ως κανονική επιλογή της λίστας — όχι απλώς ελεύθερο κείμενο στο πεδίο."""
        combo.blockSignals(True)
        if models is not None:
            combo.clear()
            for m in models:
                combo.addItem(m, m)
        if current and combo.findData(current) < 0:
            combo.addItem(current, current)
        ids = [combo.itemData(i) for i in range(combo.count())]
        ids.sort(key=lambda m: (not llm_extract.is_free_model(provider, m), m.lower()))
        combo.clear()
        for m in ids:
            combo.addItem(m + ("  (δωρεάν)" if llm_extract.is_free_model(provider, m) else ""), m)
        combo.setCurrentIndex(combo.findData(current) if current else -1)
        combo.blockSignals(False)

    def _combo_model_value(self, combo: QComboBox) -> str:
        """Το μοντέλο χωρίς το «(δωρεάν)» της λίστας: αν ο χρήστης διάλεξε μια καταχώρηση της λίστας, το πραγματικό
        id είναι στο itemData· αν έγραψε κάτι ο ίδιος, παίρνουμε ακριβώς αυτό που έγραψε."""
        idx = combo.currentIndex()
        if idx >= 0 and combo.itemText(idx) == combo.currentText():
            data = combo.itemData(idx)
            if data:
                return data
        return combo.currentText().strip()

    def _save_llm_settings(self) -> None:
        settings_store.set_value(self.conn, "llm_provider", self.llm_provider.currentData())
        groq_model = self._combo_model_value(self.llm_model_groq)
        if groq_model:
            settings_store.set_value(self.conn, "llm_model_groq", groq_model)
        or_model = self._combo_model_value(self.llm_model_openrouter)
        if or_model:
            settings_store.set_value(self.conn, "llm_model_openrouter", or_model)
        toast(self, "Οι ρυθμίσεις ανάλυσης αποθηκεύτηκαν.", "ok")

    def _refresh_llm_models(self) -> None:
        provider = self.llm_provider.currentData() or "groq"
        combo = self.llm_model_groq if provider == "groq" else self.llm_model_openrouter
        cfg = llm_extract.PROVIDERS[provider]
        key = settings_store.get(self.conn, cfg["key"])
        if not key:
            toast(self, f"Δεν έχει αποθηκευτεί API key για {provider} — αποθηκεύστε το πρώτα.", "warn")
            return

        def work(_progress):
            client = llm_extract.LLMClient(provider, key, "", cfg["url"], make_session(retries=1))
            return llm_extract.list_models(client)

        def done(models: list[str]) -> None:
            self.busy.stop()
            current = self._combo_model_value(combo)
            self._sync_model_combo(combo, provider, current, models=models)
            toast(self, f"Βρέθηκαν {len(models)} διαθέσιμα μοντέλα για {provider}.", "ok")

        def failed(m):
            self.busy.stop()
            toast(self, m, "danger")

        toast(self, "Ξεκίνησε η ανανέωση της λίστας μοντέλων…", "info")
        self.busy.start("Ανανέωση λίστας μοντέλων…")
        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))

    def _save_sources(self) -> None:
        for sid, chk in self._source_checks.items():
            settings_store.set_source_enabled(self.conn, sid, chk.isChecked())
        toast(self, "Οι πηγές ενημερώθηκαν.", "ok")

    def _install_schedule(self) -> None:
        try:
            ok, msg = scheduler_win.install(self.sched_time.text().strip() or "08:00")
        except ValueError as exc:
            ok, msg = False, str(exc)
        toast(self, msg, "ok" if ok else "warn")
        if ok:
            settings_store.set_value(self.conn, "daily_time", self.sched_time.text().strip())

    def _remove_schedule(self) -> None:
        ok, msg = scheduler_win.remove()
        toast(self, msg, "ok" if ok else "warn")

    def _test_llm(self) -> None:
        def work(_progress):
            conn = dbmod.connect()  # δικό του thread — δες σχόλιο στο _start_lookup
            try:
                client = llm_extract.LLMClient.from_settings(conn)
                try:
                    client.complete_json("Απάντησε μόνο με JSON.", 'Επίστρεψε {"ok": true}', timeout=30)
                except llm_extract.LLMError as exc:
                    # "model": το μοντέλο δεν υπάρχει πια/δεν επιτρέπεται. "credits": συνήθως στιγμιαίος
                    # συνωστισμός στον upstream provider ΑΥΤΟΥ του δωρεάν μοντέλου, όχι όριο λογαριασμού — σε
                    # κάθε περίπτωση, μία αυτόματη αλλαγή δωρεάν μοντέλου αξίζει πριν πούμε ότι η δοκιμή απέτυχε.
                    if exc.kind not in ("model", "credits"):
                        raise
                    old = client.model
                    client.model = llm_extract.recover_model(conn, client)
                    client.complete_json("Απάντησε μόνο με JSON.", 'Επίστρεψε {"ok": true}', timeout=30)
                    settings_store.set_value(conn, "llm_last_error", "")
                    return f"Το μοντέλο «{old}» δεν ήταν διαθέσιμο — αυτόματη αλλαγή σε «{client.model}». Η σύνδεση λειτουργεί."
                settings_store.set_value(conn, "llm_last_error", "")  # επιτυχής δοκιμή: σβήνει το παλιό μόνιμο banner
                return f"Η σύνδεση με {client.provider} ({client.model}) λειτουργεί."
            finally:
                conn.close()

        def done(msg: str) -> None:
            self.busy.stop()
            toast(self, msg, "ok")
            self.reload_settings()  # μπορεί να άλλαξε το μοντέλο αυτόματα (recover_model) — δείξ' το στο dropdown
            self._reload_notices()  # το μόνιμο banner σφάλματος πρέπει να εξαφανιστεί αμέσως, όχι στο επόμενο reload

        def failed(msg):
            self.busy.stop()
            toast(self, msg, "danger")

        toast(self, "Ξεκίνησε η δοκιμή σύνδεσης LLM…", "info")
        self.busy.start("Δοκιμή σύνδεσης LLM…")
        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))

    def _test_aade(self) -> None:
        user, pwd = settings_store.get(self.conn, "aade_user"), settings_store.get(self.conn, "aade_pass")
        if not (user and pwd):
            toast(self, "Δεν έχουν οριστεί credentials TAXISnet γραφείου.", "warn")
            return

        def work(_progress):
            return lookup_aade.aade_login(user, pwd)

        def done(res):
            self.busy.stop()
            if res.get("ok"):
                toast(self, "Η σύνδεση στη ΑΑΔΕ (TAXISnet) πέτυχε.", "ok")
            else:
                toast(self, lookup_aade.REASONS_EL.get(res.get("reason") or "", str(res.get("reason"))), "danger")

        def failed(m):
            self.busy.stop()
            toast(self, m, "danger")

        toast(self, "Ξεκίνησε η δοκιμή σύνδεσης ΑΑΔΕ…", "info")
        self.busy.start("Δοκιμή σύνδεσης ΑΑΔΕ…")
        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))

    # ------------------------------------------------------------------ Αντίγραφο ασφαλείας / Επαναφορά
    def _backup_now(self) -> None:
        def work(_progress):
            return backup_mod.create_backup(config.db_path(), reason="manual")

        def done(path) -> None:
            self.busy.stop()
            if path:
                toast(self, f"Αντίγραφο ασφαλείας: {path.name}", "ok")
            else:
                toast(self, "Δεν βρέθηκε βάση δεδομένων για αντίγραφο.", "warn")

        def failed(m):
            self.busy.stop()
            toast(self, m, "danger")

        self.busy.start("Δημιουργία αντιγράφου ασφαλείας…")
        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))

    def _restore_backup(self) -> None:
        backups = backup_mod.list_backups(config.data_dir())
        if not backups:
            if QMessageBox.question(
                self, "Επαναφορά", "Δεν βρέθηκαν αντίγραφα ασφαλείας στον φάκελο δεδομένων.\n\n"
                "Θέλετε να επιλέξετε εσείς ένα αρχείο βάσης (.db) για επαναφορά;",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            ) == QMessageBox.StandardButton.Yes:
                self._restore_from_chosen_file()
            return
        newest, when, size = backups[0]
        answer = QMessageBox.question(
            self, "Επαναφορά βάσης",
            f"Επαναφορά από το πιο πρόσφατο αντίγραφο;\n\n{newest.name}\n"
            f"{when:%d/%m/%Y %H:%M} · {size / 1024:.0f} KB\n\n"
            "Η τρέχουσα βάση θα κρατηθεί ως αντίγραφο «pre-restore», οπότε η ενέργεια είναι αναστρέψιμη.\n\n"
            "«Άνοιγμα» για να επιλέξετε δικό σας αρχείο βάσης.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Open,
        )
        if answer == QMessageBox.StandardButton.Open:
            self._restore_from_chosen_file()
            return
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._do_restore(newest)

    def _restore_from_chosen_file(self) -> None:
        start_dir = str(backup_mod.backup_dir(config.data_dir()))
        path, _ = QFileDialog.getOpenFileName(self, "Επιλέξτε αρχείο βάσης για επαναφορά", start_dir,
                                              "Βάση SQLite (*.db);;Όλα τα αρχεία (*.*)")
        if not path:
            return
        chosen = Path(path)
        if chosen.resolve() == config.db_path().resolve():
            QMessageBox.warning(self, "Επαναφορά", "Επιλέξατε την τρέχουσα βάση — δεν έχει νόημα η επαναφορά από "
                                                    "τον εαυτό της.")
            return
        if QMessageBox.question(
            self, "Επαναφορά βάσης", f"Επαναφορά από:\n{chosen.name}\n\n"
            "Η τρέχουσα βάση θα κρατηθεί ως αντίγραφο «pre-restore», οπότε η ενέργεια είναι αναστρέψιμη. Συνέχεια;",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._do_restore(chosen)

    def _do_restore(self, source: Path) -> None:
        """Συγχρονισμένα (όχι background thread): πρέπει να κλείσει/ξανανοίξει το `self.conn`, και το sqlite3
        απαγορεύει να το κάνει αυτό άλλο thread από αυτό που το δημιούργησε (δες σχόλιο στο _start_lookup)."""
        self.busy.start("Επαναφορά βάσης…")
        try:
            self.conn.close()
            backup_mod.restore(source, config.db_path())
            self.conn = dbmod.connect()
        except Exception as exc:
            self.conn = dbmod.connect()  # η εφαρμογή δεν πρέπει να μείνει χωρίς σύνδεση
            self.busy.stop()
            QMessageBox.critical(self, "Απέτυχε η επαναφορά", f"Η επαναφορά δεν ολοκληρώθηκε:\n\n{exc}")
            return
        self.busy.stop()
        self.reload_all()
        QMessageBox.information(self, "Επαναφορά", f"Έγινε επαναφορά από:\n{source.name}")

    # ------------------------------------------------------------------ Έλεγχος τώρα
    def _run_check(self) -> None:
        for b in self._run_buttons:
            b.setEnabled(False)
        self.run_status.setText("Έναρξη…")
        toast(self, "Ξεκίνησε ο έλεγχος νέων — μπορείτε να συνεχίσετε να δουλεύετε.", "info")

        def work(progress):
            # ΧΩΡΙΣ conn=self.conn: αφήνουμε το pipeline να ανοίξει τη δική του σύνδεση σε αυτό το thread — το
            # self.conn δημιουργήθηκε στο UI thread (δες σχόλιο στο _start_lookup). Το backup γίνεται μέσα στο
            # ίδιο το pipeline (πριν από κάθε έλεγχο, χειροκίνητο ή προγραμματισμένο).
            return pipeline.run_pipeline("manual", on_progress=progress)

        def done(stats):
            for b in self._run_buttons:
                b.setEnabled(True)
            errs = stats.get("errors") or []
            match = stats.get("match") or {}
            added = match.get("added", 0)
            summary = (f"{added} νέα matches" if added else "καμία νέα αντιστοίχιση") + \
                (f" · {match['updated']} ενημερώθηκαν" if match.get("updated") else "")
            base = f"Ολοκληρώθηκε ✓ — {summary}." if not errs else f"Ολοκληρώθηκε με σημειώσεις ({summary}): " + "· ".join(errs)[:200]
            self.run_status.setText(base)
            toast(self, base, "ok" if not errs else "warn")
            self.reload_all()

        def failed(msg):
            for b in self._run_buttons:
                b.setEnabled(True)
            self.run_status.setText(f"Σφάλμα: {msg}")
            toast(self, f"Σφάλμα ελέγχου: {msg}", "danger")

        self._tasks.append(run_task(self, work, on_progress=self.run_status.setText, on_done=done, on_error=failed))

    def _check_schedule(self) -> None:
        pass  # ο πραγματικός προγραμματισμός τρέχει από το Windows Task Scheduler (--daily), ανεξάρτητα από το παράθυρο

    # ------------------------------------------------------------------ μενού / θέμα
    def _on_menu(self, action: str) -> None:
        if action in self._pages:
            self._show_page(action)
        elif action == "add_client":
            self.on_add_client()
        elif action == "import":
            self.on_import_excel()
        elif action == "export":
            self.on_export_csv()
        elif action == "run":
            self._run_check()
        elif action == "password":
            unlock.manage(crypto.keyfile_path(), self)
            self._reload_notices()
        elif action == "tour":
            self.start_tour()
        elif action == "manual":
            self.on_manual()
        elif action == "logfile":
            _reveal(config.log_dir() / "taxmatch.log")

    def _on_theme(self, light: bool) -> None:
        theme = "light" if light else "dark"
        from PySide6.QtWidgets import QApplication
        apply_theme(QApplication.instance(), theme)
        self._prefs.setValue("theme", theme)
        self.menu.restyle()
        paint_title_bar(self, not light)

    def on_manual(self) -> None:
        try:
            path = ensure_manual(config.data_dir())
        except Exception as exc:
            toast(self, f"Το εγχειρίδιο δεν μπόρεσε να ανοίξει: {exc}", "danger")
            return
        _reveal(path)

    # ------------------------------------------------------------------ ξενάγηση
    def _tour_steps(self) -> list[Step]:
        return [
            Step("Καλώς ήρθατε", "Το TaxMatch παρακολουθεί φορολογικά/εργατικά νέα και τα αντιστοιχίζει στους "
                "πελάτες σας βάσει ΚΑΔ, κατηγορίας βιβλίων, ΦΠΑ και νομικής μορφής.", lambda: self.menu),
            Step("1. Νέος πελάτης", "Γράψτε μόνο το ΑΦΜ: στο 9ο ψηφίο η επωνυμία έρχεται μόνη της από το VIES. "
                "Οι κωδικοί TAXISnet είναι προαιρετικοί — χρησιμεύουν στην αυτόματη ανάκτηση ΚΑΔ/ΔΟΥ/βιβλίων από το Μητρώο ΑΑΔΕ.",
                lambda: self.menu.button("add_client")),
            Step("2. Οι πελάτες σας", "Κάθε στήλη έχει φίλτρο τύπου Excel και η αναζήτηση πάνω-δεξιά φιλτράρει όλες τις "
                "στήλες· το «Διαγραφή φίλτρων» εμφανίζεται όσο υπάρχει φίλτρο. Τσεκάρετε πελάτες (ή «Επιλογή όλων») για "
                "μαζικές ενέργειες. Η εισαγωγή Excel ρωτά αν θέλετε αμέσως και ενημέρωση στοιχείων.", lambda: self.client_table, lambda: self._show_page("clients")),
            Step("3. Ημερολόγιο", "Συνδυάζει το γενικό ημερολόγιο taxheaven, τους κανονικούς κανόνες (ΦΠΑ/VIES/Intrastat…) "
                "και τις προθεσμίες που εντοπίστηκαν σε άρθρα. Κλικ σε μια ημέρα δείχνει τις υποχρεώσεις της· κλικ σε νέο δείχνει "
                "ποιους πελάτες αφορά και διπλό κλικ το ανοίγει.",
                lambda: self.cal_grid_box, lambda: (self._show_page("calendar"), self._close_cal_day())),
            Step("4. Νέα & Matches", "Κάθε άρθρο δείχνει ποιους πελάτες αφορά και γιατί. Διπλό κλικ ανοίγει προεπισκόπηση "
                "πριν τον σύνδεσμο — ποτέ απευθείας browser.", lambda: self.news_table, lambda: self._show_page("news")),
            Step("5. Δέουσα επιμέλεια", "Ανάλυση κινδύνου κάθε πελάτη κατά τον ν. 4557/2018 (Παραρτήματα Ι/ΙΙ, ΠΕΠ, "
                "τρίτες χώρες υψηλού κινδύνου). Διπλό κλικ σε πελάτη ανοίγει τον φάκελό του: KYC, εκπρόσωπος και "
                "πραγματικοί δικαιούχοι, συναλλαγές/προέλευση κεφαλαίων, παράγοντες κινδύνου με ζωντανό αποτέλεσμα, "
                "αυτόματη λήψη εγγράφων (Μητρώο ΑΑΔΕ, δήλωση εισοδήματος, ΚΜΠΔ), έλεγχος σε λίστες κυρώσεων ΕΕ και "
                "σύνδεση στο Γ.Ε.ΜΗ. Οι επανεξετάσεις μπαίνουν αυτόματα στο Ημερολόγιο.",
                lambda: self.aml_page.table, lambda: (self._show_page("aml"), self.aml_page.tabs.setCurrentIndex(0))),
            Step("5α. Έγγραφα πελάτη", "Για τον επιλεγμένο πελάτη: Δήλωση παροχής στοιχείων, Ερωτηματολόγιο KYC, Έκθεση "
                "αξιολόγησης και Ρήτρες σύμβασης σε PDF, Word από τα πρότυπα του γραφείου σας, αυτόματη λήψη εγγράφων "
                "και μαζικός έλεγχος κυρώσεων για όλους τους πελάτες.",
                lambda: self.aml_page.docs_btn, lambda: (self._show_page("aml"), self.aml_page.tabs.setCurrentIndex(0))),
            Step("5β. Γραφείο & υποθέσεις", "Στις υπόλοιπες καρτέλες: αυτοδιάγνωση με τα 23 σημεία της ΑΑΔΕ, φάκελος "
                "συμμόρφωσης γραφείου, υποθέσεις (αναφορές ύποπτων συναλλαγών στην Αρχή και καταγγελίες ν. 4990/2022 με "
                "τις προθεσμίες τους), μητρώα, πρότυπα Word και μεθοδολογία (Μοντέλο Α/Β).",
                lambda: self.aml_page.tabs.tabBar(), lambda: self._show_page("aml")),
            Step("6. Ρυθμίσεις", "Κλειδιά API, μοντέλο LLM (προτιμώνται δωρεάν — φαίνονται πρώτα στη λίστα — με αυτόματη "
                "εναλλαγή αν κάποιο σταματήσει να δουλεύει), πηγές ειδήσεων, προγραμματισμός, κύριος κωδικός και αντίγραφο "
                "ασφαλείας/επαναφορά της βάσης.", lambda: self.menu.button("settings"), lambda: self._show_page("settings")),
        ]

    def start_tour(self) -> None:
        if self._tour is not None:
            self._tour.deleteLater()
        self._tour = Tour(self, self._tour_steps())
        self._tour.finished.connect(self._on_tour_finished)
        self._tour.start()
        self._tour.setFocus()

    def _on_tour_finished(self, _completed: bool) -> None:
        settings_store.set_value(self.conn, "tour_seen", "1")
        settings_store.set_value(self.conn, "tour_version", str(TOUR_VERSION))

    def _maybe_first_run_tour(self) -> None:
        # Καθυστερημένο (QTimer.singleShot στο __init__): αν το παράθυρο έκλεισε πριν προλάβει να πυροδοτηθεί
        # (π.χ. στα tests, όπου κάθε test φτιάχνει/καταστρέφει το δικό του MainWindow), το self.conn μπορεί να
        # έχει ήδη κλείσει.
        try:
            seen = settings_store.get(self.conn, "tour_seen") == "1"
            seen_version = int(settings_store.get(self.conn, "tour_version") or 0)
        except sqlite3.ProgrammingError:
            return
        if seen and seen_version >= TOUR_VERSION:
            return
        if not self.isVisible():
            return
        self.start_tour()
