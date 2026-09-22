"""Καρτέλα πελάτη: προφίλ (επεξεργάσιμο), κωδικοί TAXISnet, προσεχείς προθεσμίες, νέα που τον αφορούν."""
from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import deadlines
from ..business_profiles import credentials as client_creds, service as clients
from ..matching import engine
from .icons import dot_icon, icon
from .news_dialog import NewsDialog
from .theme import CURRENT
from .toast import toast
from .widgets import due_badge, kind_colour
from .workers import run_task

if TYPE_CHECKING:
    from .main_window import MainWindow


class ClientDetailDialog(QDialog):
    def __init__(self, main: "MainWindow", afm: str) -> None:
        super().__init__(main)
        self.main = main
        self.conn = main.conn
        self.afm = afm
        b = clients.get(self.conn, afm)
        self.setWindowTitle(b["name"] or afm)
        self.resize(720, 640)
        self._tasks: list = []

        root = QVBoxLayout(self)
        header = QHBoxLayout()
        title = QLabel(b["name"] or "Χωρίς επωνυμία")
        title.setObjectName("h1")
        header.addWidget(title)
        header.addStretch()
        refresh_btn = QPushButton(icon("refresh", CURRENT.txt, 16), "  Ανανέωση στοιχείων")
        refresh_btn.clicked.connect(self._refresh_lookup)
        header.addWidget(refresh_btn)
        del_btn = QPushButton(icon("delete", CURRENT.bad, 16), "  Διαγραφή")
        del_btn.setObjectName("danger")
        del_btn.clicked.connect(self._delete_client)
        header.addWidget(del_btn)
        root.addLayout(header)

        self.status_banner = QLabel("")
        self.status_banner.setWordWrap(True)
        root.addWidget(self.status_banner)

        tabs = QTabWidget()
        tabs.setIconSize(QSize(16, 16))
        root.addWidget(tabs, 1)
        tabs.addTab(self._profile_tab(), icon("edit", CURRENT.muted, 16), "Προφίλ")
        tabs.addTab(self._credentials_tab(), icon("key", CURRENT.muted, 16), "Κωδικοί TAXISnet")
        tabs.addTab(self._deadlines_tab(), icon("calendar", CURRENT.muted, 16), "Προσεχείς προθεσμίες")
        news_index = tabs.addTab(self._matches_tab(), icon("bell", CURRENT.muted, 16), "Νέα που τον αφορούν")
        if b["lookup_status"] == "ok":
            # Τα στοιχεία του πελάτη είναι ήδη πλήρη· αυτό που θα κοιτάξει πρώτα ο λογιστής είναι τι νέο τον αφορά,
            # όχι το προφίλ που δεν έχει αλλάξει.
            tabs.setCurrentIndex(news_index)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Κλείσιμο")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        root.addLayout(close_row)
        self._reload()

    # ------------------------------------------------------------ Προφίλ
    def _profile_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.f_name = QLineEdit()
        form.addRow("Επωνυμία", self.f_name)
        self.f_legal_form = QLineEdit()
        form.addRow("Νομική μορφή", self.f_legal_form)
        self.f_doy = QLineEdit()
        form.addRow("ΔΟΥ", self.f_doy)
        self.f_address = QLineEdit()
        form.addRow("Διεύθυνση έδρας", self.f_address)
        self.f_books = QComboBox()
        self.f_books.addItem("— άγνωστη —", "")
        self.f_books.addItem("Β (απλογραφικά)", "Β")
        self.f_books.addItem("Γ (διπλογραφικά)", "Γ")
        form.addRow("Κατηγορία βιβλίων", self.f_books)
        self.f_vat = QComboBox()
        self.f_vat.addItem("— άγνωστο —", "")
        self.f_vat.addItem("Υπόχρεος ΦΠΑ", "1")
        self.f_vat.addItem("Μη υπόχρεος / απαλλασσόμενος", "0")
        form.addRow("Καθεστώς ΦΠΑ", self.f_vat)
        self.f_status = QLineEdit()
        form.addRow("Κατάσταση", self.f_status)
        self.f_kads = QTextEdit()
        self.f_kads.setPlaceholderText("Ένας ΚΑΔ ανά γραμμή: «κωδικός περιγραφή» — ο πρώτος είναι ο κύριος")
        self.f_kads.setFixedHeight(90)
        form.addRow("ΚΑΔ", self.f_kads)
        self.f_notes = QTextEdit()
        self.f_notes.setFixedHeight(60)
        form.addRow("Σημειώσεις", self.f_notes)
        save = QPushButton("Αποθήκευση")
        save.setObjectName("primary")
        save.clicked.connect(self._save_profile)
        form.addRow("", save)
        return page

    def _save_profile(self) -> None:
        vat = self.f_vat.currentData()
        clients.update_fields(self.conn, self.afm, {
            "name": self.f_name.text().strip(), "legal_form": self.f_legal_form.text().strip(),
            "doy": self.f_doy.text().strip(), "address": self.f_address.text().strip(),
            "books_category": self.f_books.currentData(), "status": self.f_status.text().strip(),
            "notes": self.f_notes.toPlainText().strip(),
        }, vat_subject={"1": True, "0": False}.get(vat, None))
        kads = []
        for line in self.f_kads.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            code, _, descr = line.partition(" ")
            kads.append({"code": code.strip(" -–:"), "descr": descr.strip(" -–:"), "is_main": not kads})
        clients.set_kads(self.conn, self.afm, kads)
        engine.rematch(self.conn)
        toast(self, "Τα στοιχεία αποθηκεύτηκαν.", "ok")
        self._reload()

    # ------------------------------------------------------------ Κωδικοί TAXISnet
    def _credentials_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.hint_label = QLabel("")
        self.hint_label.setWordWrap(True)
        self.hint_label.setObjectName("muted")
        form.addRow(self.hint_label)
        self.c_user = QLineEdit()
        form.addRow("Χρήστης TAXISnet", self.c_user)
        self.c_pass = QLineEdit()
        self.c_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.c_pass.setPlaceholderText("(αφήστε κενό για να μείνει ως έχει)")
        form.addRow("Κωδικός TAXISnet", self.c_pass)
        self.c_status = QLabel("")
        self.c_status.setWordWrap(True)
        form.addRow("Κατάσταση", self.c_status)
        row = QHBoxLayout()
        save = QPushButton("Αποθήκευση")
        save.setObjectName("primary")
        save.clicked.connect(self._save_credentials)
        row.addWidget(save)
        test = QPushButton(icon("network", CURRENT.txt, 16), "  Δοκιμή σύνδεσης")
        test.clicked.connect(self._test_credentials)
        row.addWidget(test)
        clear = QPushButton(icon("delete", CURRENT.bad, 16), "  Διαγραφή κωδικών")
        clear.setObjectName("danger")
        clear.clicked.connect(self._clear_credentials)
        row.addWidget(clear)
        form.addRow("", row)
        return page

    def _save_credentials(self) -> None:
        user, pwd = self.c_user.text().strip(), self.c_pass.text()
        if not client_creds.set_(self.conn, self.afm, user, pwd):
            toast(self, "Δεν δόθηκαν κωδικοί.", "warn")
            return
        self.c_pass.clear()
        toast(self, "Οι κωδικοί αποθηκεύτηκαν (κρυπτογραφημένοι). Τα στοιχεία ανακτώνται αυτόματα.", "ok")
        self._start_lookup()

    def _test_credentials(self) -> None:
        if not client_creds.get(self.conn, self.afm):
            toast(self, "Δεν έχουν οριστεί κωδικοί TAXISnet για τον πελάτη.", "warn")
            return

        def work(_progress):
            return client_creds.test(self.conn, self.afm)

        def done(result):
            ok, msg = result
            toast(self, msg, "ok" if ok else "danger", ms=None if ok else 0)
            self._reload()
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self, m, "danger", ms=0)))

    def _clear_credentials(self) -> None:
        client_creds.clear(self.conn, self.afm)
        toast(self, "Οι κωδικοί TAXISnet του πελάτη διαγράφηκαν.", "ok")
        self._reload()

    # ------------------------------------------------------------ Προθεσμίες / Νέα
    def _deadlines_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        self.deadlines_list = QListWidget()
        self.deadlines_list.itemActivated.connect(self._open_deadline)
        box.addWidget(self.deadlines_list)
        return page

    def _matches_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        self.matches_list = QListWidget()
        self.matches_list.itemActivated.connect(self._open_match)
        box.addWidget(self.matches_list)
        return page

    def _open_deadline(self, item: QListWidgetItem) -> None:
        ev = item.data(Qt.ItemDataRole.UserRole)
        NewsDialog(ev["title"], ev["url"], meta=ev["date"], summary=ev.get("description", ""), parent=self).exec()

    def _open_match(self, item: QListWidgetItem) -> None:
        m = item.data(Qt.ItemDataRole.UserRole)
        NewsDialog(m["title"], m["url"], meta=m.get("published_at", "") or "", summary=m.get("summary", ""),
                  action=m.get("action_required") or "", parent=self).exec()

    # ------------------------------------------------------------ ενέργειες
    def _refresh_lookup(self) -> None:
        self._start_lookup()

    def _start_lookup(self) -> None:
        def work(_progress):
            try:
                out = clients.lookup_and_store(self.conn, self.afm)
            except KeyError:
                return None
            engine.rematch(self.conn)
            return out

        def done(_out):
            self._reload()
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self, m, "danger", ms=0)))

    def _delete_client(self) -> None:
        if QMessageBox.question(self, "Διαγραφή", "Διαγραφή του πελάτη, των matches και των κωδικών του;") != QMessageBox.StandardButton.Yes:
            return
        clients.delete(self.conn, self.afm)
        self.accept()

    def _reload(self) -> None:
        b = clients.get(self.conn, self.afm)
        if not b:
            self.accept()
            return
        self.setWindowTitle(b["name"] or self.afm)
        self.f_name.setText(b["name"])
        self.f_legal_form.setText(b["legal_form"])
        self.f_doy.setText(b["doy"])
        self.f_address.setText(b["address"])
        self.f_books.setCurrentIndex(max(0, self.f_books.findData(b["books_category"] or "")))
        vat_value = "" if b["vat_subject"] is None else str(int(b["vat_subject"]))
        self.f_vat.setCurrentIndex(max(0, self.f_vat.findData(vat_value)))
        self.f_status.setText(b["status"])
        self.f_kads.setPlainText("\n".join(f"{k['code']} {k['descr']}".strip() for k in b["kads"]))
        self.f_notes.setPlainText(b["notes"])

        cred = client_creds.status_map(self.conn).get(self.afm)
        self.c_user.setText(client_creds.masked_user(self.conn, self.afm) if cred else "")
        if cred and cred["check_status"]:
            colour = CURRENT.ok if cred["check_status"] == "ok" else CURRENT.bad
            self.c_status.setText(cred["check_message"])
            self.c_status.setStyleSheet(f"color:{colour};")
        else:
            self.c_status.setText("Δεν έχουν δοκιμαστεί ακόμη." if cred else "")
            self.c_status.setStyleSheet("")
        self.hint_label.setText("Χωρίς δικούς του κωδικούς, το Μητρώο ΑΑΔΕ δεν δίνει στοιχεία για αυτόν τον πελάτη "
                                "(δείχνει μόνο το ΑΦΜ με το οποίο έγινε η σύνδεση) — θα χρησιμοποιηθούν ΓΕΜΗ/VIES όπου υπάρχουν."
                                if not cred else "")

        if b["activity_state"] == "ceased":
            self.status_banner.setText(f"Διακοπή εργασιών{' από ' + b['cease_date'] if b['cease_date'] else ''}"
                                       f"{' (' + b['cease_reason'] + ')' if b['cease_reason'] else ''} — σύμφωνα με το Μητρώο ΑΑΔΕ.")
            self.status_banner.setStyleSheet(f"color:{CURRENT.warn};")
        elif b["lookup_status"] in ("failed", "partial") and b["lookup_error"]:
            self.status_banner.setText("Ανάκτηση στοιχείων: " + b["lookup_error"])
            self.status_banner.setStyleSheet(f"color:{CURRENT.bad if b['lookup_status'] == 'failed' else CURRENT.warn};")
        elif b["lookup_status"] == "pending":
            self.status_banner.setText("Τα στοιχεία ανακτώνται αυτόματα (ΑΑΔΕ → ΓΕΜΗ → VIES).")
            self.status_banner.setStyleSheet(f"color:{CURRENT.muted};")
        else:
            self.status_banner.setText("")

        today = date.today()
        events = deadlines.events_between(self.conn, today, today + timedelta(days=45), afm=self.afm, include_conditional=False)
        self.deadlines_list.clear()
        if not events:
            placeholder = QListWidgetItem("Καμία προσεχής προθεσμία.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.deadlines_list.addItem(placeholder)
        for ev in events[:20]:
            badge = due_badge(ev["date"])
            text = f"{ev['title']}  ·  {badge[0]}" if badge else ev["title"]
            item = QListWidgetItem(dot_icon(kind_colour(ev["kind"])), text)
            if badge:
                item.setForeground(QColor(badge[1]))
            item.setData(Qt.ItemDataRole.UserRole, ev)
            self.deadlines_list.addItem(item)

        matches = engine.digest(self.conn, days=90, afm=self.afm)
        self.matches_list.clear()
        if not matches:
            placeholder = QListWidgetItem("Κανένα άρθρο δεν έχει ταιριάξει ακόμη.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.matches_list.addItem(placeholder)
        for m in matches[:50]:
            feedback_dot = {1: CURRENT.ok, -1: CURRENT.bad}.get(m["user_feedback"], CURRENT.muted)
            text = f"{(m['published_at'] or '')[:10]}  {m['title']}  ·  {m['matched_reason']}"
            colour = None
            if m["confidence"] < 1:
                text += "  ·  να επιβεβαιωθεί"
                colour = CURRENT.warn
            elif m["deadline"]:
                badge = due_badge(m["deadline"])
                if badge:
                    text += f"  ·  {badge[0]}"
                    colour = badge[1]
            item = QListWidgetItem(dot_icon(feedback_dot), text)
            if colour:
                item.setForeground(QColor(colour))
            item.setData(Qt.ItemDataRole.UserRole, m)
            self.matches_list.addItem(item)
