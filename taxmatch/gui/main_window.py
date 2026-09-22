"""Κύριο παράθυρο — native (PySide6), χωρίς Flask/webview. Οι σελίδες μιλούν απευθείας στα ίδια modules που
χρησιμοποιούσε το web UI (business_profiles.service, matching.engine, deadlines, pipeline, notices, …) — καμία
HTTP κλήση, κανένα token. Ό,τι αγγίζει δίκτυο/LLM/ΑΑΔΕ τρέχει σε background thread (`workers.run_task`)."""
from __future__ import annotations

import calendar as pycal
import json
import logging
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
    QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from .. import APP_TITLE, __version__, config, crypto, deadlines, logs, notices as notices_mod, pipeline, scheduler_win, settings_store
from ..business_profiles import credentials as client_creds, import_excel, lookup_aade, service as clients, vies
from ..extraction import llm_extract
from ..identifiers import is_valid_afm, kad_digits, normalize_afm
from ..ingestion import sources, taxheaven_calendar
from ..matching import engine
from . import i18n
from .busy import BusyOverlay
from .client_detail_dialog import ClientDetailDialog
from .client_dialog import ClientDialog
from .icons import icon, logo_pixmap
from .manual import ensure_manual
from .news_dialog import NewsDialog
from .side_menu import SideMenu
from .table_filter import TableColumnFilter
from .theme import CURRENT, apply_theme, paint_title_bar
from .toast import toast
from .tour import Step, Tour
from .tray import Tray
from . import unlock
from .widgets import ToggleSwitch, resort, setup_columns
from .workers import run_task

log = logging.getLogger(__name__)

_CLIENT_COLS = [
    ("", 30, "Επιλέξτε για μαζικές ενέργειες"), ("ΑΦΜ", 90, ""), ("Επωνυμία", 0, "Διπλό κλικ για την καρτέλα"),
    ("Είδος", 130, ""), ("Κατάσταση", 90, ""), ("Κύριος ΚΑΔ", 110, ""), ("Βιβλία", 55, ""), ("ΦΠΑ", 70, ""),
    ("Στοιχεία", 80, "Πληρότητα ανάκτησης"), ("TAXISnet", 80, ""), ("Matches", 65, ""),
]
_C_CHK, _C_AFM, _C_NAME, _C_KIND, _C_STATE, _C_KAD, _C_BOOKS, _C_VAT, _C_LOOKUP, _C_CREDS, _C_MATCHES = range(11)

_NEWS_COLS = [("Πηγή", 130, ""), ("Ημερομηνία", 90, ""), ("Τίτλος", 0, "Διπλό κλικ για προεπισκόπηση"),
             ("Κατάσταση", 110, ""), ("Πελάτες", 60, "")]

TOUR_VERSION = 1


def _reveal(path: Path) -> None:
    import os
    import subprocess
    if os.name == "nt":
        os.startfile(path)  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(path)])


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

        from .. import db as dbmod
        self.conn = dbmod.connect()
        self._prefs = QSettings("scanmydata", "TaxMatch")
        self._tasks: list[Any] = []                        # κρατά ζωντανά τα background tasks (βλ. workers.run_task)
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
        quick_row.addStretch()
        self.quick_search = QLineEdit()
        self.quick_search.setPlaceholderText("Γρήγορη αναζήτηση πελάτη (ΑΦΜ ή επωνυμία)…  [Ctrl+K]")
        self.quick_search.setFixedWidth(300)
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
            "calendar": self._calendar_page(), "news": self._news_page(), "settings": self._settings_page(),
        }
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
         "news": self.reload_news, "settings": self.reload_settings}.get(name, lambda: None)()

    def reload_all(self) -> None:
        self._reload_notices()
        self.reload_dashboard()
        self.reload_clients()
        self.reload_calendar()
        self.reload_news()
        self.reload_settings()
        self._refresh_status_bar()

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
        for n in notices_mod.collect(self.conn):
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
            btn.clicked.connect(lambda _=False, endpoint=n["endpoint"]: self._go_from_notice(endpoint))
            row.addWidget(btn)
            self.notices_box.addWidget(frame)

    def _go_from_notice(self, endpoint: str) -> None:
        page = {"main.settings": "settings", "main.clients_list": "clients"}.get(endpoint, "dashboard")
        self._show_page(page)

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
        self.dash_articles = QListWidget()
        self.dash_articles.itemActivated.connect(self._open_dash_article)
        left = QVBoxLayout()
        lbl1 = QLabel("Πρόσφατα άρθρα που αφορούν πελάτες")
        lbl1.setObjectName("muted")
        left.addWidget(lbl1)
        left.addWidget(self.dash_articles, 1)
        split.addLayout(left, 2)

        self.dash_deadlines = QListWidget()
        self.dash_deadlines.itemActivated.connect(self._open_dash_deadline)
        right = QVBoxLayout()
        lbl2 = QLabel("Προσεχείς προθεσμίες (21 ημέρες)")
        lbl2.setObjectName("muted")
        right.addWidget(lbl2)
        right.addWidget(self.dash_deadlines, 1)
        split.addLayout(right, 1)
        root.addLayout(split, 1)
        return page

    def reload_dashboard(self) -> None:
        today = date.today()
        groups = engine.digest_articles(self.conn, days=3)
        upcoming = deadlines.events_between(self.conn, today, today + timedelta(days=21))
        urgent = sum(1 for ev in upcoming if (date.fromisoformat(ev["date"]) - today).days <= 7)
        cutoff = (datetime.now().astimezone().astimezone(tz=None) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        articles_n = self.conn.execute("SELECT COUNT(*) FROM articles WHERE COALESCE(published_at,fetched_at)>=?", (cutoff,)).fetchone()[0]
        self._kpi_labels["clients"].setText(str(clients.count(self.conn)))
        self._kpi_labels["articles"].setText(str(articles_n))
        self._kpi_labels["matched"].setText(str(len(groups)))
        self._kpi_labels["urgent"].setText(str(urgent))
        self._update_last_run_label()

        self.dash_articles.clear()
        for g in groups[:25]:
            item = QListWidgetItem(f"{g['title']}  —  {g['n_matches']} πελάτες" + (f" · λήξη {g['deadline']}" if g["deadline"] else ""))
            item.setData(Qt.ItemDataRole.UserRole, g)
            self.dash_articles.addItem(item)

        self.dash_deadlines.clear()
        for ev in upcoming[:25]:
            item = QListWidgetItem(f"{ev['date']}  {ev['title']}")
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
        NewsDialog(g["title"], g["url"], meta=f"{g.get('source', '')} · {g.get('published_at', '') or ''}",
                  summary=g.get("summary", ""), action=g.get("action_required") or "", parent=self).exec()

    def _open_dash_deadline(self, item: QListWidgetItem) -> None:
        ev = item.data(Qt.ItemDataRole.UserRole)
        NewsDialog(ev["title"], ev["url"], meta=ev["date"], summary=ev.get("description", ""), parent=self).exec()

    # ------------------------------------------------------------------ Πελάτες
    def _clients_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)

        top = QHBoxLayout()
        add_btn = QPushButton("  Νέος πελάτης")
        add_btn.setObjectName("primary")
        add_btn.clicked.connect(self.on_add_client)
        top.addWidget(add_btn)
        import_btn = QPushButton("  Εισαγωγή από Excel")
        import_btn.clicked.connect(self.on_import_excel)
        top.addWidget(import_btn)
        export_btn = QPushButton("  Εξαγωγή CSV")
        export_btn.clicked.connect(self.on_export_csv)
        top.addWidget(export_btn)
        top.addStretch()
        self.client_search = QLineEdit()
        self.client_search.setPlaceholderText("Αναζήτηση σε όλες τις στήλες…")
        self.client_search.setFixedWidth(240)
        self.client_search.textChanged.connect(self._apply_client_filter)
        top.addWidget(self.client_search)
        root.addLayout(top)

        selbar = QHBoxLayout()
        self.client_sel_label = QLabel("Κανένας πελάτης επιλεγμένος")
        selbar.addWidget(self.client_sel_label)
        selbar.addStretch()
        del_btn = QPushButton("  Διαγραφή επιλεγμένων")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self.on_delete_selected_clients)
        selbar.addWidget(del_btn)
        lookup_btn = QPushButton("  Ανανέωση στοιχείων")
        lookup_btn.clicked.connect(self.on_refresh_selected_clients)
        selbar.addWidget(lookup_btn)
        root.addLayout(selbar)

        self.client_table = QTableWidget(0, len(_CLIENT_COLS))
        self.client_table.setHorizontalHeaderLabels([c[0] for c in _CLIENT_COLS])
        self.client_table.verticalHeader().setVisible(False)
        self.client_table.setAlternatingRowColors(True)
        self.client_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.client_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.client_table.setSortingEnabled(True)
        self.client_table.doubleClicked.connect(self._open_selected_client)
        # apply_fn=self._apply_client_filter: το TableColumnFilter ξαναφιλτράρει μόνο του και ασύγχρονα μετά από
        # κάθε ξαναγέμισμα/ταξινόμηση του πίνακα (βλ. docstring της κλάσης) — χωρίς αυτό θα καλούσε το δικό του
        # `apply()` που δεν ξέρει τίποτα για το πεδίο αναζήτησης και θα έσβηνε ό,τι είχε κρύψει αυτή.
        # _apply_client_filter() συνδυάζει και τα δύο κριτήρια και είναι η ΜΟΝΗ συνάρτηση που αγγίζει setRowHidden.
        self._client_col_filter = TableColumnFilter(self.client_table, (_C_KIND, _C_STATE, _C_BOOKS, _C_VAT, _C_LOOKUP, _C_CREDS),
                                                    apply_fn=lambda: self._apply_client_filter())
        self._client_col_filter.filtersChanged.connect(self._apply_client_filter)
        setup_columns(self.client_table, _CLIENT_COLS, self._prefs, "clients")
        self.client_table.itemChanged.connect(lambda _i: self._sync_client_selection_label())
        root.addWidget(self.client_table, 1)
        return page

    def reload_clients(self) -> None:
        rows = clients.list_all(self.conn)
        creds = client_creds.status_map(self.conn)
        counts = {r["afm"]: r["n"] for r in self.conn.execute("SELECT afm, COUNT(*) AS n FROM matches GROUP BY afm")}
        table = self.client_table
        table.blockSignals(True)
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            c = creds.get(r["afm"])
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            check.setCheckState(Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, r["afm"])
            table.setItem(i, _C_CHK, check)
            state_label = {"active": "Ενεργή", "ceased": "Διακοπή", "none": "Ιδιώτης", "": "—"}.get(r["activity_state"] or "", r["activity_state"])
            vat = {1: "Υπόχρεος", 0: "Όχι"}.get(r["vat_subject"], "—")
            lookup_label = {"ok": "Πλήρη", "partial": "Μερικά", "failed": "Αποτυχία", "pending": "Αναμονή", "manual": "Χειροκίνητα"}.get(r["lookup_status"], r["lookup_status"])
            creds_label = "—"
            if c and c["complete"]:
                creds_label = {"ok": "Έγκυροι", "invalid": "Πρόβλημα", "error": "Πρόβλημα"}.get(c["check_status"], "Ορισμένοι")
            cells = {
                _C_AFM: r["afm"], _C_NAME: r["name"] or "—", _C_KIND: r["legal_form"] or "—", _C_STATE: state_label,
                _C_KAD: r["kad_main_code"] or "—", _C_BOOKS: r["books_category"] or "—", _C_VAT: vat,
                _C_LOOKUP: lookup_label, _C_CREDS: creds_label, _C_MATCHES: str(counts.get(r["afm"], 0)),
            }
            for col, text in cells.items():
                item = QTableWidgetItem(text)
                if col == _C_STATE and state_label == "Διακοπή":
                    item.setForeground(QColor(CURRENT.bad))
                elif col == _C_STATE and state_label == "Ενεργή":
                    item.setForeground(QColor(CURRENT.ok))
                if col == _C_CREDS and creds_label == "Πρόβλημα":
                    item.setForeground(QColor(CURRENT.bad))
                table.setItem(i, col, item)
        table.setSortingEnabled(True)
        resort(table, _C_NAME)
        table.blockSignals(False)
        self._apply_client_filter()

    def _apply_client_filter(self) -> None:
        """Συνδυάζει αναζήτηση κειμένου + φίλτρα στηλών σε ΕΝΑ πέρασμα — δεν καλεί `TableColumnFilter.apply()`
        (θα ξανάγραφε το setRowHidden αγνοώντας την αναζήτηση, βλ. σχόλιο στο `_clients_page`)."""
        needle = self.client_search.text().strip().lower()
        col_filters = self._client_col_filter.filters
        for row in range(self.client_table.rowCount()):
            text_ok = not needle or any(needle in (self.client_table.item(row, c).text() or "").lower()
                                        for c in range(1, self.client_table.columnCount()))
            col_ok = all(not allowed or self.client_table.item(row, c).text() in allowed
                        for c, allowed in col_filters.items())
            self.client_table.setRowHidden(row, not (text_ok and col_ok))
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
        self.client_sel_label.setText(f"{n} επιλεγμένοι" if n else "Κανένας πελάτης επιλεγμένος")

    def _quick_client_search(self) -> None:
        """Ctrl+K / Enter στο πεδίο πάνω-δεξιά: φιλτράρει τους πελάτες από ΟΠΟΙΑΔΗΠΟΤΕ σελίδα και, αν μείνει
        ακριβώς ένας, ανοίγει κατευθείαν την καρτέλα του — χωρίς να χρειάζεται πρώτα να πάει κανείς στους «Πελάτες»."""
        needle = self.quick_search.text().strip()
        if not needle:
            return
        self._show_page("clients")
        self.client_search.setText(needle)
        visible = [row for row in range(self.client_table.rowCount()) if not self.client_table.isRowHidden(row)]
        if len(visible) == 1:
            afm = self.client_table.item(visible[0], _C_CHK).data(Qt.ItemDataRole.UserRole)
            self.quick_search.clear()
            self.client_search.clear()
            self.open_client_detail(afm)
        else:
            self.client_search.setFocus()

    def _open_selected_client(self) -> None:
        row = self.client_table.currentRow()
        if row < 0:
            return
        afm = self.client_table.item(row, _C_CHK).data(Qt.ItemDataRole.UserRole)
        self.open_client_detail(afm)

    def on_add_client(self) -> None:
        dlg = ClientDialog(self.conn, self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.result_afm:
            self._start_lookup([dlg.result_afm])
            self.reload_clients()
            self._refresh_status_bar()

    def on_delete_selected_clients(self) -> None:
        afms = self._selected_client_afms()
        if not afms:
            return
        if QMessageBox.question(self, "Διαγραφή", f"Διαγραφή {len(afms)} πελατών μαζί με τα matches και τους κωδικούς τους;") != QMessageBox.StandardButton.Yes:
            return
        for afm in afms:
            clients.delete(self.conn, afm)
        self.reload_clients()
        self._refresh_status_bar()

    def on_refresh_selected_clients(self) -> None:
        afms = self._selected_client_afms() or [self.client_table.item(r, _C_CHK).data(Qt.ItemDataRole.UserRole) for r in range(self.client_table.rowCount())]
        self._start_lookup(afms)

    def on_import_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Εισαγωγή από Excel", "", "Excel/CSV (*.xlsx *.xlsm *.csv *.txt)")
        if not path:
            return
        try:
            res = import_excel.parse_file(Path(path).name, Path(path).read_bytes())
        except Exception as exc:
            toast(self, f"Δεν ήταν δυνατή η ανάγνωση του αρχείου: {exc}", "danger")
            return
        msg = (f"Βρέθηκαν {len(res.rows)} πελάτες" + (f", {res.credential_count} με κωδικούς TAXISnet" if res.credential_count else "")
              + (f", {len(res.invalid)} άκυρες γραμμές" if res.invalid else "") + ". Εισαγωγή;")
        if QMessageBox.question(self, "Επιβεβαίωση εισαγωγής", msg) != QMessageBox.StandardButton.Yes:
            return
        out = clients.import_result(self.conn, res)
        engine.rematch(self.conn)
        self._start_lookup([r.afm for r in res.rows if r.has_credentials] or [r.afm for r in res.rows])
        toast(self, f"Προστέθηκαν {out['added']} πελάτες, ενημερώθηκαν {out['updated']}.", "ok")
        self.reload_clients()
        self._refresh_status_bar()

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
        self.run_status.setText(f"Ανάκτηση στοιχείων για {len(afms)} πελάτες…")

        def work(progress):
            for i, afm in enumerate(afms, 1):
                progress(f"Ανάκτηση στοιχείων {i}/{len(afms)}")
                try:
                    clients.lookup_and_store(self.conn, afm)
                except KeyError:
                    pass
            engine.rematch(self.conn)
            return len(afms)

        def done(_n):
            self.run_status.setText("Η ανάκτηση στοιχείων ολοκληρώθηκε.")
            self.reload_clients()
            self.reload_dashboard()
            self._reload_notices()
            self._refresh_status_bar()

        self._tasks.append(run_task(self, work, on_progress=self.run_status.setText, on_done=done,
                                    on_error=lambda m: self.run_status.setText(f"Σφάλμα ανάκτησης: {m}")))

    def open_client_detail(self, afm: str) -> None:
        dlg = ClientDetailDialog(self, afm)
        dlg.exec()
        self.reload_clients()
        self.reload_dashboard()

    # ------------------------------------------------------------------ Ημερολόγιο
    def _calendar_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        self.cal_stack = QStackedWidget()
        outer.addWidget(self.cal_stack, 1)

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

        self.cal_days_list = QListWidget()
        self.cal_days_list.itemActivated.connect(self._open_cal_day)
        root.addWidget(self.cal_days_list, 1)
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
        self.cal_list = QListWidget()
        self.cal_list.itemActivated.connect(self._open_cal_event)
        droot.addWidget(self.cal_list, 1)
        self.cal_stack.addWidget(day_page)

        self._cal_month = date.today().replace(day=1)
        self._cal_view_day: Optional[date] = None
        self._cal_events: list[dict] = []
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
        first = self._cal_month
        try:
            taxheaven_calendar.ensure_month_synced(self.conn, None, first.year, first.month)
        except Exception:
            log.exception("ensure_month_synced απέτυχε")
        self.cal_label.setText(i18n.month_year(first))
        last_day = pycal.monthrange(first.year, first.month)[1]
        afm = self.cal_client.currentData() or None
        self._cal_events = deadlines.events_between(
            self.conn, first, date(first.year, first.month, last_day), afm=afm,
            include_news=self.cal_news.isChecked(), include_rules=self.cal_rules.isChecked(),
            include_conditional=self.cal_cond.isChecked())
        self._fill_client_combo()
        if self._cal_view_day is not None:
            self._render_cal_day()
        else:
            self._render_cal_month()

    def _render_cal_month(self) -> None:
        self.cal_stack.setCurrentIndex(0)
        by_day: dict[str, list[dict]] = {}
        for ev in self._cal_events:
            by_day.setdefault(ev["date"], []).append(ev)
        self.cal_days_list.clear()
        for day_str in sorted(by_day):
            evs = by_day[day_str]
            n = len(evs)
            label = f"{i18n.short_day(date.fromisoformat(day_str))}  ·  {n} υποχρέωσ{'η' if n == 1 else 'εις'}"
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, day_str)
            self.cal_days_list.addItem(item)
        if not by_day:
            self.cal_days_list.addItem(QListWidgetItem("Καμία υποχρέωση αυτόν τον μήνα με τα τρέχοντα φίλτρα."))

    def _open_cal_day(self, item: QListWidgetItem) -> None:
        day_str = item.data(Qt.ItemDataRole.UserRole)
        if not day_str:
            return
        self._cal_view_day = date.fromisoformat(day_str)
        self._render_cal_day()

    def _close_cal_day(self) -> None:
        self._cal_view_day = None
        self._render_cal_month()

    def _render_cal_day(self) -> None:
        self.cal_stack.setCurrentIndex(1)
        d = self._cal_view_day
        self.cal_day_label.setText(i18n.long_date(d))
        self.cal_list.clear()
        day_str = d.isoformat()
        for ev in self._cal_events:
            if ev["date"] != day_str:
                continue
            kind_label = {"news": "νέα", "rule": "κανόνας", "general": "ΑΑΔΕ/taxheaven"}.get(ev["kind"], ev["kind"])
            item = QListWidgetItem(f"{ev['title']}  ·  [{kind_label}]")
            item.setData(Qt.ItemDataRole.UserRole, ev)
            self.cal_list.addItem(item)

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
        NewsDialog(ev["title"], ev["url"], meta=ev["date"], summary=ev.get("description", ""), parent=self).exec()

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
        root.addLayout(top)

        self.news_table = QTableWidget(0, len(_NEWS_COLS))
        self.news_table.setHorizontalHeaderLabels([c[0] for c in _NEWS_COLS])
        self.news_table.verticalHeader().setVisible(False)
        self.news_table.setAlternatingRowColors(True)
        self.news_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.news_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.news_table.setSortingEnabled(True)
        self.news_table.doubleClicked.connect(self._open_selected_news)
        self._news_col_filter = TableColumnFilter(self.news_table, (0, 3))  # Πηγή, Κατάσταση
        setup_columns(self.news_table, _NEWS_COLS, self._prefs, "news")
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
        table.setSortingEnabled(False)
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
            table.setItem(i, 4, QTableWidgetItem(str(a["n_matches"])))
        table.setSortingEnabled(True)
        resort(table, 1)

    def _open_selected_news(self) -> None:
        row = self.news_table.currentRow()
        if row < 0:
            return
        a = self.news_table.item(row, 2).data(Qt.ItemDataRole.UserRole)
        summary, action = "", ""
        try:
            ex = json.loads(a["extracted_json"] or "{}")
            summary, action = ex.get("summary", ""), ex.get("action_required") or ""
        except ValueError:
            pass
        NewsDialog(a["title"], a["url"], meta=f"{a['source']} · {(a['published_at'] or '')[:10]}",
                  summary=summary, action=action, parent=self).exec()

    # ------------------------------------------------------------------ Ρυθμίσεις
    def _settings_page(self) -> QWidget:
        outer = QScrollArea()
        outer.setWidgetResizable(True)
        page = QWidget()
        root = QVBoxLayout(page)
        title = QLabel("Ρυθμίσεις")
        title.setObjectName("h1")
        root.addWidget(title)

        keys_box = QGroupBox("Κλειδιά & credentials")
        kf = QFormLayout(keys_box)
        self._settings_fields: dict[str, QLineEdit] = {}
        for key, label, echo in (("groq_api_key", "Groq API key", True), ("openrouter_api_key", "OpenRouter API key", True),
                                 ("business_portal_key", "Business Portal (ΓΕΜΗ) API key", True),
                                 ("aade_user", "TAXISnet χρήστης γραφείου", False), ("aade_pass", "TAXISnet κωδικός γραφείου", True)):
            field = QLineEdit()
            if echo:
                field.setEchoMode(QLineEdit.EchoMode.Password)
            self._settings_fields[key] = field
            kf.addRow(label, field)
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
        self.llm_model_groq = QLineEdit()
        lf.addRow("Μοντέλο Groq", self.llm_model_groq)
        self.llm_model_openrouter = QLineEdit()
        lf.addRow("Μοντέλο OpenRouter", self.llm_model_openrouter)
        note = QLabel("Αν το μοντέλο απορριφθεί (π.χ. δεν υπάρχει πια), η εφαρμογή δοκιμάζει αυτόματα δωρεάν εναλλακτικές· "
                     "αν καμία δεν δουλέψει, θα σας προτείνει ρητά μετάβαση σε πληρωμένο μοντέλο — ποτέ αυτόματα.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        lf.addRow("", note)
        save_llm = QPushButton("Αποθήκευση")
        save_llm.clicked.connect(self._save_llm_settings)
        lf.addRow("", save_llm)
        root.addWidget(llm_box)

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
        sf2.addStretch()
        root.addWidget(sched_box)

        pass_box = QGroupBox("Κύριος κωδικός")
        pf = QHBoxLayout(pass_box)
        pf.addWidget(QLabel("Προαιρετική προστασία του φακέλου δεδομένων με κωδικό."))
        pf.addStretch()
        manage_btn = QPushButton("Διαχείριση…")
        manage_btn.clicked.connect(lambda: unlock.manage(crypto.keyfile_path(), self))
        pf.addWidget(manage_btn)
        root.addWidget(pass_box)
        root.addStretch()

        outer.setWidget(page)
        return outer

    def reload_settings(self) -> None:
        self.llm_provider.setCurrentIndex(self.llm_provider.findData(settings_store.get(self.conn, "llm_provider") or "groq"))
        self.llm_model_groq.setText(settings_store.get(self.conn, "llm_model_groq"))
        self.llm_model_openrouter.setText(settings_store.get(self.conn, "llm_model_openrouter"))
        for sid, chk in self._source_checks.items():
            src = sources.BY_ID[sid]
            chk.setChecked(settings_store.source_enabled(self.conn, sid, src.default_enabled) and src.available)
        self.sched_time.setText(settings_store.get(self.conn, "daily_time") or "08:00")

    def _save_secret_keys(self) -> None:
        for key, field in self._settings_fields.items():
            value = field.text().strip()
            if value:
                settings_store.set_value(self.conn, key, value)
                field.clear()
        toast(self, "Τα credentials αποθηκεύτηκαν (κρυπτογραφημένα).", "ok")

    def _save_llm_settings(self) -> None:
        settings_store.set_value(self.conn, "llm_provider", self.llm_provider.currentData())
        if self.llm_model_groq.text().strip():
            settings_store.set_value(self.conn, "llm_model_groq", self.llm_model_groq.text().strip())
        if self.llm_model_openrouter.text().strip():
            settings_store.set_value(self.conn, "llm_model_openrouter", self.llm_model_openrouter.text().strip())
        toast(self, "Οι ρυθμίσεις ανάλυσης αποθηκεύτηκαν.", "ok")

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
            client = llm_extract.LLMClient.from_settings(self.conn)
            try:
                client.complete_json("Απάντησε μόνο με JSON.", 'Επίστρεψε {"ok": true}', timeout=30)
            except llm_extract.LLMError as exc:
                if exc.kind != "model":
                    raise
                old = client.model
                client.model = llm_extract.recover_model(self.conn, client)
                client.complete_json("Απάντησε μόνο με JSON.", 'Επίστρεψε {"ok": true}', timeout=30)
                return f"Το μοντέλο «{old}» δεν ήταν διαθέσιμο — αυτόματη αλλαγή σε «{client.model}». Η σύνδεση λειτουργεί."
            return f"Η σύνδεση με {client.provider} ({client.model}) λειτουργεί."
        self._tasks.append(run_task(self, work, on_done=lambda msg: toast(self, msg, "ok"),
                                    on_error=lambda msg: toast(self, msg, "danger", ms=0)))

    def _test_aade(self) -> None:
        user, pwd = settings_store.get(self.conn, "aade_user"), settings_store.get(self.conn, "aade_pass")
        if not (user and pwd):
            toast(self, "Δεν έχουν οριστεί credentials TAXISnet γραφείου.", "warn")
            return

        def work(_progress):
            return lookup_aade.aade_login(user, pwd)

        def done(res):
            if res.get("ok"):
                toast(self, "Η σύνδεση στη ΑΑΔΕ (TAXISnet) πέτυχε.", "ok")
            else:
                toast(self, lookup_aade.REASONS_EL.get(res.get("reason") or "", str(res.get("reason"))), "danger", ms=0)
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self, m, "danger", ms=0)))

    # ------------------------------------------------------------------ Έλεγχος τώρα
    def _run_check(self) -> None:
        for b in self._run_buttons:
            b.setEnabled(False)
        self.run_status.setText("Έναρξη…")

        def work(progress):
            return pipeline.run_pipeline("manual", on_progress=progress, conn=self.conn)

        def done(stats):
            for b in self._run_buttons:
                b.setEnabled(True)
            errs = stats.get("errors") or []
            self.run_status.setText("Ολοκληρώθηκε ✓" if not errs else "Ολοκληρώθηκε με σημειώσεις: " + "· ".join(errs)[:200])
            self.reload_all()

        def failed(msg):
            for b in self._run_buttons:
                b.setEnabled(True)
            self.run_status.setText(f"Σφάλμα: {msg}")

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
            toast(self, f"Το εγχειρίδιο δεν μπόρεσε να ανοίξει: {exc}", "danger", ms=0)
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
            Step("2. Οι πελάτες σας", "Κάθε στήλη έχει φίλτρο τύπου Excel (περάστε το ποντίκι πάνω από την επικεφαλίδα). "
                "Τσεκάρετε πελάτες για μαζικές ενέργειες.", lambda: self.client_table, lambda: self._show_page("clients")),
            Step("3. Ημερολόγιο", "Συνδυάζει το γενικό ημερολόγιο taxheaven, τους κανονικούς κανόνες (ΦΠΑ/VIES/Intrastat…) "
                "και τις προθεσμίες που εντοπίστηκαν σε άρθρα. Κλικ σε μια ημέρα δείχνει τις υποχρεώσεις της.",
                lambda: self.cal_days_list, lambda: (self._show_page("calendar"), self._close_cal_day())),
            Step("4. Νέα & Matches", "Κάθε άρθρο δείχνει ποιους πελάτες αφορά και γιατί. Διπλό κλικ ανοίγει προεπισκόπηση "
                "πριν τον σύνδεσμο — ποτέ απευθείας browser.", lambda: self.news_table, lambda: self._show_page("news")),
            Step("5. Ρυθμίσεις", "Κλειδιά API, μοντέλο LLM (προτιμώνται δωρεάν, με αυτόματη εναλλαγή), πηγές ειδήσεων, "
                "προγραμματισμός και κύριος κωδικός.", lambda: self.menu.button("settings"), lambda: self._show_page("settings")),
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
        seen = settings_store.get(self.conn, "tour_seen") == "1"
        seen_version = int(settings_store.get(self.conn, "tour_version") or 0)
        if seen and seen_version >= TOUR_VERSION:
            return
        if not self.isVisible():
            return
        self.start_tour()
