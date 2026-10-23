"""Καρτέλα πελάτη: προφίλ (επεξεργάσιμο), κωδικοί TAXISnet, προσεχείς προθεσμίες, νέα που τον αφορούν, δέουσα επιμέλεια."""
from __future__ import annotations

import html
from datetime import date, timedelta
from typing import TYPE_CHECKING

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QMessageBox, QPushButton, QTableWidget, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from .. import db as dbmod
from .. import easycheck, efka
from .. import deadlines, obligations
from ..aml import model as aml_model, store as aml_store
from ..aml.content import ASSESSMENT_LABEL
from ..business_profiles import credentials as client_creds, service as clients
from ..matching import engine
from ..matching.match import ALL_SCOPE_REASON
from .busy import BusyOverlay
from .icons import dot_icon, icon
from .news_dialog import NewsDialog
from .debts_tab import DebtsTab
from .members_tab import MembersTab
from .requests_tab import RequestsTab
from .mydata_tab import MydataTab
from .office_widgets import ClientFeesWidget
from .theme import CURRENT
from .toast import toast
from .table_kit import SortItem, TableKit
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
        self.resize(1180, 760)                               # 7 καρτέλες: σε 720px εμφανίζονταν βέλη κύλισης
        self._tasks: list = []
        self.busy = BusyOverlay(self)

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
        tabs.addTab(self._credentials_tab(), icon("key", CURRENT.muted, 16), "Κωδικοί TAXISnet && e-ΕΦΚΑ")
        tabs.addTab(self._deadlines_tab(), icon("calendar", CURRENT.muted, 16), "Προθεσμίες && υποβολές")
        news_index = tabs.addTab(self._matches_tab(), icon("bell", CURRENT.muted, 16), "Νέα που τον αφορούν")
        tabs.addTab(self._aml_tab(), icon("aml", CURRENT.muted, 16), "Δέουσα επιμέλεια")
        self.fees = ClientFeesWidget(self.conn, afm, self, on_change=self.main.reload_clients)
        tabs.addTab(self.fees, icon("stats", CURRENT.muted, 16), "Αμοιβές")
        self.mydata = MydataTab(self.conn, afm, self, main=self.main)
        tabs.addTab(self.mydata, icon("etimologio", CURRENT.muted, 16), "myDATA")
        self.debts_tab = DebtsTab(self.conn, afm, self, main=self.main)
        tabs.addTab(self.debts_tab, icon("send", CURRENT.muted, 16), "Οφειλές && email")
        self.requests_tab = RequestsTab(self.conn, afm, self, main=self.main)
        tabs.addTab(self.requests_tab, icon("bell", CURRENT.muted, 16), "Αιτήματα ΑΑΔΕ")
        self.members_tab = MembersTab(self.conn, afm, self, main=self.main)
        tabs.addTab(self.members_tab, icon("clients", CURRENT.muted, 16), "Μέλη")
        if b["lookup_status"] == "ok":
            # Τα στοιχεία του πελάτη είναι ήδη πλήρη· αυτό που θα κοιτάξει πρώτα ο λογιστής είναι τι νέο τον αφορά,
            # όχι το προφίλ που δεν έχει αλλάξει.
            tabs.setCurrentIndex(news_index)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Κλείσιμο")
        close_btn.clicked.connect(self.accept)
        self.finished.connect(lambda _r: self.main.jobs.adopt(self))
        close_row.addWidget(close_btn)
        root.addLayout(close_row)
        self._reload()

    # ------------------------------------------------------------ Προφίλ
    def _profile_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        self.f_name = QLineEdit()
        form.addRow("Επωνυμία", self.f_name)
        self.f_trade_name = QLineEdit()
        self.f_trade_name.setPlaceholderText("από το Μητρώο ΑΑΔΕ με την ανάκτηση στοιχείων")
        self.f_trade_name.setToolTip("Ο πελάτης βρίσκεται και με τον διακριτικό τίτλο στην αναζήτηση")
        form.addRow("Διακριτικός τίτλος", self.f_trade_name)
        self.f_legal_form = QLineEdit()
        form.addRow("Νομική μορφή", self.f_legal_form)
        self.f_doy = QLineEdit()
        form.addRow("ΔΟΥ", self.f_doy)
        self.f_address = QLineEdit()
        form.addRow("Διεύθυνση έδρας", self.f_address)
        # Στοιχεία επικοινωνίας από το Μητρώο ΑΑΔΕ (getLdapInfo) — αν τα αλλάξει ο χρήστης, η ανάκτηση δεν τα πατά
        contact = QHBoxLayout()
        self.f_email = QLineEdit()
        self.f_email.setPlaceholderText("email")
        self.f_mobile = QLineEdit()
        self.f_mobile.setPlaceholderText("κινητό")
        self.f_mobile.setFixedWidth(130)
        self.f_phone = QLineEdit()
        self.f_phone.setPlaceholderText("σταθερό")
        self.f_phone.setFixedWidth(130)
        contact.addWidget(self.f_email, 1)
        contact.addWidget(self.f_mobile)
        contact.addWidget(self.f_phone)
        form.addRow("Επικοινωνία", contact)
        # Προσωπικό email (κύριο, `mail2` του Μητρώου) πάνω· του βοηθού/εκπροσώπου (`mail`/`mailemep`) χωριστά από κάτω
        self.f_email_assistant = QLineEdit()
        self.f_email_assistant.setPlaceholderText("email βοηθού / εκπροσώπου (αν βρεθεί στο Μητρώο)")
        self.f_email_assistant.setToolTip("Δεν χρησιμοποιείται για αυτόματα email — μόνο αν το επιλέξετε στη χειροκίνητη "
                                          "αποστολή (προτείνεται όταν λείπει το προσωπικό email)")
        form.addRow("Email βοηθού", self.f_email_assistant)
        self.f_contact_note = QLabel("")
        self.f_contact_note.setObjectName("muted")
        self.f_email_auto = QCheckBox("Αυτόματα email (οφειλές / νέα) σε αυτόν τον πελάτη")
        crow = QHBoxLayout()
        crow.addWidget(self.f_email_auto)
        crow.addStretch()
        crow.addWidget(self.f_contact_note)
        form.addRow("", crow)
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
        self.f_vat_period = QComboBox()
        for label, value in (("— αυτόματα (Μητρώο / Φ2 / βιβλία) —", ""), ("Μηνιαίος ΦΠΑ", "monthly"),
                             ("Τριμηνιαίος ΦΠΑ", "quarterly")):
            self.f_vat_period.addItem(label, value)
        form.addRow("Περίοδος ΦΠΑ", self.f_vat_period)
        self.f_vat_note = QLabel("")
        self.f_vat_note.setWordWrap(True)
        form.addRow("", self.f_vat_note)
        self.f_employer = QComboBox()
        for label, value in (("— άγνωστο —", ""), ("Ναι — απασχολεί προσωπικό (ΑΠΔ, Ε4)", "1"), ("Όχι", "0")):
            self.f_employer.addItem(label, value)
        form.addRow("Εργοδότης", self.f_employer)
        self.f_intrastat = QComboBox()
        for label, value in (("— άγνωστο —", ""), ("Ναι — υποβάλλει Intrastat", "1"), ("Όχι", "0")):
            self.f_intrastat.addItem(label, value)
        form.addRow("Intrastat", self.f_intrastat)
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
        save.setEnabled(False)
        save.setToolTip("Ενεργό μόνο όταν αλλάξει κάτι στο προφίλ")
        save.clicked.connect(self._save_profile)
        self.save_profile_btn = save
        self._profile_snapshot: tuple = ()
        form.addRow("", save)
        # Το «Αποθήκευση» ενεργοποιείται ΜΟΝΟ όταν υπάρχει αλλαγή (ρητό αίτημα χρήστη 2026-10-23)
        for w in (self.f_name, self.f_trade_name, self.f_legal_form, self.f_doy, self.f_address, self.f_email,
                  self.f_email_assistant, self.f_mobile, self.f_phone, self.f_status):
            w.textChanged.connect(self._profile_changed)
        for w in (self.f_books, self.f_vat, self.f_vat_period, self.f_employer, self.f_intrastat):
            w.currentIndexChanged.connect(self._profile_changed)
        for w in (self.f_kads, self.f_notes):
            w.textChanged.connect(self._profile_changed)
        self.f_email_auto.toggled.connect(self._profile_changed)
        return page

    def _profile_state(self) -> tuple:
        return (self.f_name.text(), self.f_trade_name.text(), self.f_legal_form.text(), self.f_doy.text(),
                self.f_address.text(), self.f_email.text(), self.f_email_assistant.text(), self.f_mobile.text(),
                self.f_phone.text(), self.f_status.text(), self.f_books.currentIndex(), self.f_vat.currentIndex(),
                self.f_vat_period.currentIndex(), self.f_employer.currentIndex(), self.f_intrastat.currentIndex(),
                self.f_kads.toPlainText(), self.f_notes.toPlainText(), self.f_email_auto.isChecked())

    def _profile_changed(self, *_a) -> None:
        if hasattr(self, "save_profile_btn"):
            self.save_profile_btn.setEnabled(bool(self._profile_snapshot) and self._profile_state() != self._profile_snapshot)

    def _mark_profile_saved(self) -> None:
        self._profile_snapshot = self._profile_state()
        self._profile_changed()

    @staticmethod
    def _contact_note(b: dict) -> str:
        """Από πού και πότε ήρθαν τα στοιχεία επικοινωνίας — ώστε ο χρήστης να ξέρει αν είναι σωστά ενημερωμένα."""
        if b.get("contact_source") == "manual":
            return "διορθωμένα χειροκίνητα"
        if b.get("contact_source") != "aade":
            return "θα έρθουν με την ανάκτηση στοιχείων (κωδικοί TAXISnet του ίδιου του πελάτη)"
        origin = {"mail2": "προσωπικό email του υπόχρεου",
                  "mailemep": "⚠ email ΕΚΠΡΟΣΩΠΟΥ/ΛΟΓΙΣΤΗ — ο υπόχρεος δεν έχει δηλώσει δικό του",
                  "mail": "παλιό email TAXISnet"}.get(b.get("email_origin") or "", "")
        if not b.get("email") and b.get("email_assistant"):
            origin = "⚠ ο υπόχρεος δεν έχει δηλώσει προσωπικό email — υπάρχει μόνο του βοηθού"
        when = (b.get("contact_at") or "")[:10]
        return "Μητρώο ΑΑΔΕ" + (f" · {origin}" if origin else "") + (f" · {when[8:10]}/{when[5:7]}/{when[:4]}" if when else "")

    @staticmethod
    def _vat_note(b: dict) -> str:
        """Τι ισχύει ΤΩΡΑ για την περίοδο ΦΠΑ και από πού προκύπτει — μηνιαίος/τριμηνιαίος διακριτά (ρητό αίτημα)."""
        if b.get("vat_subject") == 0:
            return "Μη υπόχρεος ΦΠΑ."
        period = obligations.vat_period(b)
        if not period:
            return "Η περίοδος ΦΠΑ δεν είναι γνωστή — ορίστε την ή τρέξτε «Έλεγχος υποβολών» (περίοδοι Φ2)."
        source = {"easycheck": "από τις πραγματικές περιόδους Φ2 της ΑΑΔΕ", "manual": "επιλογή χρήστη"}.get(
            b.get("vat_period_source") or "", "από το Μητρώο ΑΑΔΕ / την κατηγορία βιβλίων"
            + (f" («{b['books_category_raw']}»)" if b.get("books_category_raw") else ""))
        text = f"<b>{'Μηνιαίος' if period == 'monthly' else 'Τριμηνιαίος'} ΦΠΑ</b> — {html.escape(source)}."
        note = obligations.vat_option_note(b)
        if note:
            text += f"<br><span style='color:{CURRENT.accent}'>{html.escape(note)}</span>"
        return text

    def _save_profile(self) -> None:
        vat = self.f_vat.currentData()
        clients.update_fields(self.conn, self.afm, {
            "name": self.f_name.text().strip(), "legal_form": self.f_legal_form.text().strip(),
            "doy": self.f_doy.text().strip(), "address": self.f_address.text().strip(),
            "books_category": self.f_books.currentData(), "status": self.f_status.text().strip(),
            "notes": self.f_notes.toPlainText().strip(),
        }, vat_subject={"1": True, "0": False}.get(vat, None))
        self.conn.execute("UPDATE businesses SET trade_name=? WHERE afm=?", (self.f_trade_name.text().strip(), self.afm))
        old = self.conn.execute("SELECT email, email_assistant, mobile, phone FROM businesses WHERE afm=?",
                                (self.afm,)).fetchone()
        contact = (self.f_email.text().strip().lower(), self.f_email_assistant.text().strip().lower(),
                   self.f_mobile.text().strip(), self.f_phone.text().strip())
        if contact != (old["email"], old["email_assistant"], old["mobile"], old["phone"]):
            self.conn.execute("UPDATE businesses SET email=?, email_assistant=?, mobile=?, phone=?, contact_source='manual' "
                              "WHERE afm=?", (*contact, self.afm))
        self.conn.execute("UPDATE businesses SET email_auto=? WHERE afm=?", (int(self.f_email_auto.isChecked()), self.afm))
        period = self.f_vat_period.currentData()
        if period:                                     # ρητή επιλογή χρήστη: η ανάκτηση Μητρώου δεν την πατά
            self.conn.execute("UPDATE businesses SET vat_period_type=?, vat_period_source='manual' WHERE afm=?",
                              (period, self.afm))
        else:
            self.conn.execute("UPDATE businesses SET vat_period_source='' WHERE afm=? AND vat_period_source='manual'",
                              (self.afm,))
        flags = {"1": 1, "0": 0}
        self.conn.execute("UPDATE businesses SET employer=?, intrastat=? WHERE afm=?",
                          (flags.get(self.f_employer.currentData()), flags.get(self.f_intrastat.currentData()), self.afm))
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

        # e-ΕΦΚΑ εργοδότη: ΑΜΕ + κωδικοί ΙΚΑ εργοδότη — για τον έλεγχο υποβολής ΑΠΔ (οικονομική καρτέλα εργοδότη).
        efka_head = QLabel("e-ΕΦΚΑ εργοδότη (έλεγχος ΑΠΔ)")
        efka_head.setObjectName("h2")
        form.addRow(efka_head)
        self._cred_form = form
        self.e_amka = QLineEdit()
        self.e_amka.setPlaceholderText("11 ψηφία — για ΚΕΑΟ και τρέχουσες εισφορές e-ΕΦΚΑ (φυσικά πρόσωπα)")
        form.addRow("ΑΜΚΑ", self.e_amka)
        self.e_ame = QLineEdit()
        self.e_ame.setPlaceholderText("10 ψηφία — από Excel, χειροκίνητα ή «Ανάκτηση ΑΜΕ»")
        form.addRow("ΑΜΕ (Αρ. Μητρώου Εργοδότη)", self.e_ame)
        self.e_user = QLineEdit()
        form.addRow("Χρήστης ΙΚΑ εργοδότη", self.e_user)
        self.e_pass = QLineEdit()
        self.e_pass.setEchoMode(QLineEdit.EchoMode.Password)
        self.e_pass.setPlaceholderText("(αφήστε κενό για να μείνει ως έχει)")
        form.addRow("Κωδικός ΙΚΑ εργοδότη", self.e_pass)
        self.e_status = QLabel("")
        self.e_status.setWordWrap(True)
        self.e_status.setObjectName("muted")
        form.addRow("", self.e_status)
        erow = QHBoxLayout()
        esave = QPushButton("Αποθήκευση")
        esave.clicked.connect(self._save_efka)
        erow.addWidget(esave)
        fetch_ame = QPushButton(icon("download", CURRENT.txt, 16), "  Ανάκτηση ΑΜΕ")
        fetch_ame.setToolTip("Από την «Πιστοποίηση Εργοδοτών» του e-ΕΦΚΑ, με τους κωδικούς ΙΚΑ εργοδότη ή TAXISnet του "
                             "πελάτη — μόνο ανάγνωση")
        fetch_ame.clicked.connect(self._retrieve_ame)
        erow.addWidget(fetch_ame)
        erow.addStretch()
        form.addRow("", erow)
        return page

    def _save_efka(self) -> None:
        ame = self.e_ame.text().strip()
        if ame and not efka.normalize_ame(ame):
            toast(self, "Ο ΑΜΕ πρέπει να είναι έως 10 ψηφία.", "warn")
            return
        user, pwd = self.e_user.text().strip(), self.e_pass.text()
        amka = self.e_amka.text().strip()
        if amka and amka != efka.get_amka(self.conn, self.afm) and not efka.set_amka(self.conn, self.afm, amka):
            toast(self, "Ο ΑΜΚΑ πρέπει να έχει 11 ψηφία.", "warn")
            return
        if amka and not (ame or user or pwd):
            toast(self, "Ο ΑΜΚΑ αποθηκεύτηκε (κρυπτογραφημένος).", "ok")
            self._reload()
            return
        if not efka.set_(self.conn, self.afm, ame=ame or None, user=user, password=pwd):
            toast(self, "Δεν δόθηκαν στοιχεία e-ΕΦΚΑ.", "warn")
            return
        self.e_pass.clear()
        toast(self, "Τα στοιχεία e-ΕΦΚΑ αποθηκεύτηκαν (οι κωδικοί κρυπτογραφημένοι).", "ok")
        self._reload()

    def _retrieve_ame(self) -> None:
        if not (efka.ika_credentials(self.conn, self.afm) or client_creds.get(self.conn, self.afm)):
            toast(self, "Χρειάζονται κωδικοί ΙΚΑ εργοδότη ή TAXISnet του πελάτη.", "warn")
            return

        def work(_p):
            conn = dbmod.connect()           # δικό του thread — ποτέ η σύνδεση του UI
            try:
                return efka.retrieve_ame(conn, self.afm)
            finally:
                conn.close()

        def done(res):
            self.busy.stop()
            toast(self, f"ΑΜΕ: {res['ame']}" if res["ok"] else res["reason"], "ok" if res["ok"] else "warn")
            self._reload()

        def failed(m):
            self.busy.stop()
            toast(self, m, "danger")

        toast(self, "Ξεκίνησε η ανάκτηση ΑΜΕ από τον e-ΕΦΚΑ…", "info")
        self.busy.start("Ανάκτηση ΑΜΕ…")
        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))

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
            conn = dbmod.connect()  # δικό του thread — το self.conn δημιουργήθηκε στο UI thread
            try:
                return client_creds.test(conn, self.afm)
            finally:
                conn.close()

        def done(result):
            self.busy.stop()
            ok, msg = result
            toast(self, msg, "ok" if ok else "danger")
            self._reload()

        def failed(m):
            self.busy.stop()
            toast(self, m, "danger")

        toast(self, "Ξεκίνησε η δοκιμή σύνδεσης TAXISnet…", "info")
        self.busy.start("Δοκιμή σύνδεσης…")
        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))

    def _clear_credentials(self) -> None:
        client_creds.clear(self.conn, self.afm)
        toast(self, "Οι κωδικοί TAXISnet του πελάτη διαγράφηκαν.", "ok")
        self._reload()

    # ------------------------------------------------------------ Προθεσμίες / Νέα
    def _deadlines_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        up = QLabel("Προσεχείς (45 ημέρες)")
        up.setObjectName("h2")
        box.addWidget(up)
        self.deadlines_list = QListWidget()
        self.deadlines_list.setMaximumHeight(110)
        self.deadlines_list.itemActivated.connect(self._open_deadline)
        box.addWidget(self.deadlines_list)

        # Ανά φορολογική χρήση: κάθε περιοδική υποχρέωση για την οποία είναι υπόχρεος (από Μητρώο, myDATA, έλεγχο
        # υποβολών), με την κατάσταση υποβολής και τα ποσά της δήλωσης (ρητό αίτημα χρήστη 2026-10-22).
        head = QHBoxLayout()
        title = QLabel("Υποχρεώσεις & υποβολές χρήσης")
        title.setObjectName("h2")
        head.addWidget(title)
        self.check_year = QComboBox()
        for y in range(date.today().year, date.today().year - 4, -1):
            self.check_year.addItem(str(y), y)
        self.check_year.currentIndexChanged.connect(lambda _i: self._reload_checks())
        head.addWidget(self.check_year)
        self.show_maybe = QCheckBox("και «υπό προϋποθέσεις»")
        self.show_maybe.setToolTip("Υποχρεώσεις που εξαρτώνται από κάτι που η εφαρμογή δεν γνωρίζει (π.χ. OSS, Intrastat "
                                   "χωρίς στοιχεία myDATA) — κρυμμένες για να φαίνονται καθαρά οι βέβαιες")
        self.show_maybe.toggled.connect(lambda _on: self._reload_checks())
        head.addWidget(self.show_maybe)
        head.addStretch()
        self.done_btn = QPushButton("  Σήμανση ως ολοκληρωμένο")
        self.done_btn.setToolTip("Για δηλώσεις χωρίς αυτόματο έλεγχο (π.χ. ψηφιακό τέλος συναλλαγής) ή όταν υποβλήθηκε "
                                 "εκτός ΑΑΔΕ — ξαναπατήστε για αναίρεση")
        self.done_btn.clicked.connect(self._toggle_done)
        head.addWidget(self.done_btn)
        self.check_btn = QPushButton(icon("check", CURRENT.txt, 16), "  Έλεγχος υποβολών")
        self.check_btn.setToolTip("Ρωτά την ΑΑΔΕ (με τους κωδικούς TAXISnet του πελάτη) αν έχουν υποβληθεί Φ2 (με ποσά), "
                                  "ΦΜΥ, δήλωση εισοδήματος, ΜΥΦ κ.λπ. της χρήσης — μόνο ανάγνωση, καμία υποβολή")
        self.check_btn.clicked.connect(self._run_easycheck)
        head.addWidget(self.check_btn)
        box.addLayout(head)
        # Σύνοψη χρήσης + πότε έγινε τελευταία φορά κάθε εργασία (έλεγχος υποβολών, λήψη myDATA)
        self.year_summary = QLabel("")
        self.year_summary.setWordWrap(True)
        self.year_summary.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.year_summary)
        self.year_table = QTableWidget(0, 6)
        self.year_table.verticalHeader().setVisible(False)
        self.year_table.setAlternatingRowColors(True)
        self.year_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.year_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # ταξινόμηση, αναζήτηση, χωνί σε κάθε στήλη, στήλες που σύρονται και θυμούνται — όπως ο πίνακας πελατών
        self.year_kit = TableKit(self.year_table, [("Προθεσμία", 90), ("Υποχρέωση", 0), ("Υπόχρεος", 110),
                                                   ("Υποβολή", 150), ("Ποσά δήλωσης", 240), ("Αιτιολογία", 220)],
                                 "client_obligations", prefs=getattr(self.main, "_prefs", None),
                                 placeholder="Αναζήτηση υποχρέωσης (τίτλος, υποβολή, αιτιολογία…)")
        box.addWidget(self.year_kit.bar)
        self.year_table.itemSelectionChanged.connect(self._show_obligation_detail)
        self.year_table.doubleClicked.connect(lambda *_: self._toggle_done())
        box.addWidget(self.year_table, 3)
        # Λεπτομέρειες της επιλεγμένης υποχρέωσης: πώς προέκυψε, τι βρήκε ο έλεγχος, ποσά, ταυτότητα οφειλής, πότε.
        self.obligation_detail = QLabel("Επιλέξτε μια υποχρέωση για λεπτομέρειες.")
        self.obligation_detail.setObjectName("card")
        self.obligation_detail.setWordWrap(True)
        self.obligation_detail.setTextFormat(Qt.TextFormat.RichText)
        self.obligation_detail.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.obligation_detail.setMinimumHeight(70)
        self.obligation_detail.setMaximumHeight(150)
        box.addWidget(self.obligation_detail)
        self.checks_list = QListWidget()                       # λοιπές δηλώσεις του ελέγχου (ΜΥΦ, Φ5, …)
        self.checks_list.setMaximumHeight(110)
        box.addWidget(self.checks_list)
        return page

    def _show_obligation_detail(self) -> None:
        row = self.year_table.currentRow()
        item = self.year_table.item(row, 0) if row >= 0 else None
        if item is None:
            return
        r = item.data(Qt.ItemDataRole.UserRole)
        sub = r["submission"]
        esc = html.escape
        lines = [f"<b>{esc(r['title'])}</b> — προθεσμία <b>{date.fromisoformat(r['date']).strftime('%d/%m/%Y')}</b>"
                 f" · περίοδος αναφοράς {esc(r['ref_month'])}",
                 f"Υπόχρεος: <b>{'Ναι' if r['status'] == 'yes' else 'υπό προϋποθέσεις'}</b>"
                 + (f" — {esc(r['reason'])}" if r["reason"] else "")]
        if sub["status"] == "done":
            lines.append(f"<b style='color:{CURRENT.ok}'>Ολοκληρώθηκε (σήμανση χρήστη)</b>"
                         + (f" στις {esc(sub['done_at'][:10])}" if sub.get("done_at") else "")
                         + " — διπλό κλικ / «Σήμανση ως ολοκληρωμένο» για αναίρεση")
        for c in sub.get("checks") or []:
            o = easycheck.OBLIGATIONS.get(c.get("obligation", ""))
            colour = {"submitted": CURRENT.ok, "not_submitted": CURRENT.bad}.get(c["status"], CURRENT.warn)
            a = c.get("amounts") or {}
            line = (f"ΑΑΔΕ — {esc(o.label if o else c.get('obligation', ''))} · {esc(c.get('period') or '')}: "
                    f"<b style='color:{colour}'>{esc(easycheck.STATUS_LABEL.get(c['status'], c['status']))}</b>")
            if a.get("submitted_at"):
                line += f" · υποβολή {esc(a['submitted_at'])}"
            if a.get("registration_no"):
                line += f" · αρ. καταχώρισης {esc(str(a['registration_no']))}"
            amounts = obligations.amounts_text(a)
            if amounts:
                line += f"<br>&nbsp;&nbsp;Ποσά: {esc(amounts)}"
            if a.get("state"):
                line += f" · κατάσταση {esc(a['state'])}"
            if c.get("checked_at"):
                line += f" <span style='color:{CURRENT.muted}'>(έλεγχος {esc(c['checked_at'][:10])})</span>"
            lines.append(line)
        if not sub["status"]:
            lines.append(f"<span style='color:{CURRENT.muted}'>"
                         + ("Δεν υπάρχει ακόμη αυτόματος έλεγχος υποβολής — σημειώστε το χειροκίνητα ως ολοκληρωμένο."
                            if sub["manual"] else "Δεν έχει ελεγχθεί — «Έλεγχος υποβολών» για τη χρήση.") + "</span>")
        lines.append(f"<span style='color:{CURRENT.muted}'>{esc(r['description'])}</span>")
        self.obligation_detail.setText("<br>".join(lines))

    def _reload_checks(self) -> None:
        year = self.check_year.currentData() or date.today().year
        all_rows = deadlines.client_year(self.conn, self.afm, year)
        rows = [r for r in all_rows if r["status"] == "yes" or self.show_maybe.isChecked()]
        colours = {"submitted": CURRENT.ok, "done": CURRENT.ok, "pending": CURRENT.warn, "not_submitted": CURRENT.bad}
        t = self.year_table
        self.year_kit.begin()
        t.setRowCount(len(rows))
        today = date.today().isoformat()
        for i, r in enumerate(rows):
            sub = r["submission"]
            label = sub["label"] if sub["status"] else ("χειροκίνητα" if sub["manual"] else "—")
            cells = [date.fromisoformat(r["date"]).strftime("%d/%m/%Y"), r["title"],
                     "Ναι" if r["status"] == "yes" else "Ίσως", label,
                     obligations.amounts_text(sub["amounts"]), r["reason"]]
            for col, text in enumerate(cells):
                item = SortItem(text)
                item.setToolTip(r["description"])
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, r)
                if col == 3 and sub["status"]:
                    item.setForeground(QColor(colours.get(sub["status"], CURRENT.muted)))
                elif col == 3 and r["date"] < today and r["status"] == "yes":
                    item.setForeground(QColor(CURRENT.warn))      # έληξε χωρίς κατάσταση — να ελεγχθεί
                if r["status"] == "maybe" and col != 3:
                    item.setForeground(QColor(CURRENT.muted))
                t.setItem(i, col, item)
        self.year_kit.end()
        n_yes = sum(1 for r in all_rows if r["status"] == "yes")
        n_maybe = len(all_rows) - n_yes
        n_ok = sum(1 for r in rows if r["submission"]["status"] in ("submitted", "done"))
        n_miss = sum(1 for r in rows if r["submission"]["status"] == "not_submitted")
        n_pend = sum(1 for r in rows if r["submission"]["status"] == "pending")
        n_open = sum(1 for r in rows if r["status"] == "yes" and not r["submission"]["status"] and r["date"] < today)
        last_check = self.conn.execute("SELECT MAX(checked_at) FROM obligation_checks WHERE afm=? AND year=?",
                                       (self.afm, year)).fetchone()[0]
        last_sync = self.conn.execute("SELECT MAX(synced_at), COUNT(*) FROM mydata_sync WHERE afm=? AND substr(ym,1,4)=?",
                                      (self.afm, str(year))).fetchone()
        self.year_summary.setText(
            f"Χρήση <b>{year}</b>: {n_yes} υποχρεώσεις"
            + (f" (+{n_maybe} υπό προϋποθέσεις{'' if self.show_maybe.isChecked() else ', κρυμμένες'})" if n_maybe else "")
            + " · "
            f"<b style='color:{CURRENT.ok}'>✓ {n_ok} ολοκληρωμένες</b> · "
            f"<b style='color:{CURRENT.bad}'>✗ {n_miss} χωρίς υποβολή</b> · {n_pend} σε εκκρεμότητα · "
            f"<span style='color:{CURRENT.warn}'>{n_open} έληξαν χωρίς έλεγχο</span><br>"
            f"<span style='color:{CURRENT.muted}'>Τελευταίος έλεγχος υποβολών: "
            f"{(last_check or '—')[:16].replace('T', ' ')} · myDATA: {last_sync[1]} μήνες της χρήσης"
            f"{', τελευταία λήψη ' + last_sync[0][:16].replace('T', ' ') if last_sync[0] else ''}</span>")
        self.checks_list.clear()
        mapped = {k for keys in obligations.RULE_CHECKS.values() for k in keys}
        others = [r for r in easycheck.results(self.conn, self.afm, year) if r["obligation"] not in mapped]
        if not others and not any(r["submission"]["status"] for r in rows):
            item = QListWidgetItem("Δεν έχει γίνει έλεγχος υποβολών για τη χρήση " + str(year) + " — πατήστε «Έλεγχος υποβολών»."
                                   if client_creds.get(self.conn, self.afm)
                                   else "Χρειάζονται οι κωδικοί TAXISnet του πελάτη (καρτέλα «Κωδικοί TAXISnet»).")
            item.setForeground(QColor(CURRENT.muted))
            self.checks_list.addItem(item)
        for r in others:
            o = easycheck.OBLIGATIONS.get(r["obligation"])
            label = o.label if o else r["obligation"]
            text = f"{easycheck.STATUS_LABEL.get(r['status'], r['status'])} — {label}" + (f" · {r['period']}" if r["period"] else "")
            item = QListWidgetItem(dot_icon(colours.get(r["status"], CURRENT.muted)), text)
            item.setToolTip(r["detail"] or "")
            self.checks_list.addItem(item)
        self.checks_list.setVisible(self.checks_list.count() > 0)
        self.checks_list.setMaximumHeight(min(110, 8 + 26 * self.checks_list.count()))

    def _toggle_done(self) -> None:
        row = self.year_table.currentRow()
        item = self.year_table.item(row, 0) if row >= 0 else None
        if item is None:
            toast(self, "Επιλέξτε πρώτα μια υποχρέωση από τον πίνακα.", "warn")
            return
        r = item.data(Qt.ItemDataRole.UserRole)
        done = r["submission"]["status"] != "done"
        deadlines.mark_done(self.conn, self.afm, r["rule_id"], r["ref_month"], done)
        toast(self, ("Σημειώθηκε ως ολοκληρωμένο: " if done else "Αναιρέθηκε: ") + r["title"], "ok")
        self._reload_checks()
        self.main._render_calendar_from_db()  # noqa: SLF001

    def _run_easycheck(self) -> None:
        if not client_creds.get(self.conn, self.afm):
            toast(self, "Χρειάζονται οι κωδικοί TAXISnet του πελάτη.", "warn")
            return
        year = self.check_year.currentData() or date.today().year
        self.main.run_submission_checks([self.afm], [year], window=self, on_done=self._after_checks)

    def _after_checks(self) -> None:
        self._reload_checks()
        self._reload()

    def _matches_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        # Προεπιλογή: ΜΟΝΟ στοχευμένα νέα (ΚΑΔ/βιβλία/ΦΠΑ/νομική μορφή της επιχείρησης). Τα «για όλες τις επιχειρήσεις»
        # (π.χ. γενικά φορολογικά νέα) αφορούν τους πάντες και πνίγουν τα πραγματικά σχετικά — προαιρετικά με τσεκάρισμα.
        self.show_general = QCheckBox("Εμφάνιση και γενικών νέων (που αφορούν όλες τις επιχειρήσεις)")
        self.show_general.toggled.connect(lambda _on: self._reload_matches())
        box.addWidget(self.show_general)
        self.matches_hint = QLabel("")
        self.matches_hint.setObjectName("muted")
        box.addWidget(self.matches_hint)
        self.matches_list = QListWidget()
        self.matches_list.itemActivated.connect(self._open_match)
        box.addWidget(self.matches_list)
        return page

    def _aml_tab(self) -> QWidget:
        page = QWidget()
        box = QVBoxLayout(page)
        self.aml_summary = QLabel("")
        self.aml_summary.setWordWrap(True)
        self.aml_summary.setTextFormat(Qt.TextFormat.RichText)
        box.addWidget(self.aml_summary)
        row = QHBoxLayout()
        assess = QPushButton(icon("aml", CURRENT.txt, 16), "  Νέα αξιολόγηση κινδύνου")
        assess.setObjectName("primary")
        assess.clicked.connect(self._new_aml_assessment)
        row.addWidget(assess)
        row.addStretch()
        box.addLayout(row)
        hist = QLabel("Ιστορικό αξιολογήσεων")
        hist.setObjectName("muted")
        box.addWidget(hist)
        self.aml_history = QListWidget()
        box.addWidget(self.aml_history, 1)
        return page

    def _new_aml_assessment(self) -> None:
        if self.main.aml_page.open_assessment(self.afm):
            self._reload()

    def _reload_aml(self) -> None:
        a = aml_store.latest(self.conn, self.afm)
        profile = aml_store.get_profile(self.conn, self.afm)
        done, total = aml_store.kyc_completeness(profile)
        pep = aml_model.PEP_LABEL.get(profile["pep_status"], "")
        if a:
            cat = a["final_category"]
            colour = {aml_model.LOW: CURRENT.ok, aml_model.MEDIUM: CURRENT.warn, aml_model.HIGH: CURRENT.bad}[cat]
            badge = due_badge(a["next_review"])
            review = badge[0] if badge else a["next_review"]
            self.aml_summary.setText(
                f"<span style='font-size:16px; font-weight:800; color:{colour}'>{aml_model.CATEGORY_LABEL[cat]} κίνδυνος</span>"
                f" → {aml_model.DD_LABEL[cat]}<br>Τελευταία αξιολόγηση: {a['assessed_on']} "
                f"({ASSESSMENT_LABEL.get(a['kind'], a['kind'])}, Μοντέλο {a['model']}) · Επόμενη επανεξέταση: {review}"
                f"<br>ΠΕΠ: {pep} · Λίστα KYC: {done}/{total}")
        else:
            self.aml_summary.setText(f"<b style='color:{CURRENT.warn}'>Δεν έχει γίνει ακόμη αξιολόγηση κινδύνου.</b> "
                                     f"Ο ν. 4557/2018 (άρθρο 13 παρ. 9) τη ζητά για κάθε πελάτη, νέο και υφιστάμενο."
                                     f"<br>ΠΕΠ: {pep} · Λίστα KYC: {done}/{total}")
        self.aml_history.clear()
        for h in aml_store.history(self.conn, self.afm):
            text = (f"{h['assessed_on']}  ·  {ASSESSMENT_LABEL.get(h['kind'], h['kind'])}  ·  "
                    f"{aml_model.CATEGORY_LABEL[h['final_category']]}  ·  "
                    + (f"{h['total_b']}/100" if h["model"] == "B" else f"{h['sum_a']:+d}")
                    + (f"  ·  {h['justification'][:80]}" if h["justification"] else ""))
            self.aml_history.addItem(QListWidgetItem(text))
        if not self.aml_history.count():
            placeholder = QListWidgetItem("Καμία αξιολόγηση ακόμη.")
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.aml_history.addItem(placeholder)

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
            conn = dbmod.connect()  # δικό του thread — το self.conn δημιουργήθηκε στο UI thread
            try:
                try:
                    out = clients.lookup_and_store(conn, self.afm)
                except KeyError:
                    return None
                engine.rematch(conn)
                return out
            finally:
                conn.close()

        def done(_out):
            self.busy.stop()
            toast(self, "Η ανάκτηση στοιχείων ολοκληρώθηκε.", "ok")
            self._reload()

        def failed(m):
            self.busy.stop()
            toast(self, m, "danger")

        toast(self, "Ξεκίνησε η ανάκτηση στοιχείων του πελάτη…", "info")
        self.busy.start("Ανάκτηση στοιχείων…")
        self._tasks.append(run_task(self, work, on_done=done, on_error=failed))

    def _delete_client(self) -> None:
        if QMessageBox.question(self, "Διαγραφή", "Διαγραφή του πελάτη, των matches και των κωδικών του;\n\n"
                                "Το ιστορικό δέουσας επιμέλειας ΔΕΝ διαγράφεται (τήρηση 5 ετών, άρθρο 30 ν. 4557/2018).") != QMessageBox.StandardButton.Yes:
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
        full = dict(self.conn.execute("SELECT * FROM businesses WHERE afm=?", (self.afm,)).fetchone())
        self.f_vat_period.setCurrentIndex(max(0, self.f_vat_period.findData(
            full["vat_period_type"] if full["vat_period_source"] == "manual" else "")))
        self.f_vat_note.setText(self._vat_note(full))
        self.f_email.setText(full["email"])
        self.f_email_assistant.setText(full.get("email_assistant") or "")
        self.f_trade_name.setText(full.get("trade_name") or "")
        self.f_mobile.setText(full["mobile"])
        self.f_phone.setText(full["phone"])
        self.f_email_auto.setChecked(bool(full["email_auto"]))
        self.f_contact_note.setText(self._contact_note(full))
        flags = self.conn.execute("SELECT employer, intrastat FROM businesses WHERE afm=?", (self.afm,)).fetchone()
        for box, value in ((self.f_employer, flags["employer"]), (self.f_intrastat, flags["intrastat"])):
            box.setCurrentIndex(max(0, box.findData("" if value is None else str(int(value)))))
        self.f_status.setText(b["status"])
        self.f_kads.setPlainText("\n".join(f"{k['code']} {k['descr']}".strip() for k in b["kads"]))
        self._mark_profile_saved()
        self._reload_checks()
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
        ef = efka.status_map(self.conn).get(self.afm) or {}
        self.e_ame.setText(ef.get("ame", ""))
        self.e_amka.setText(efka.get_amka(self.conn, self.afm))
        # Οι εταιρείες δεν έχουν ΑΜΚΑ — συνδέονται στον e-ΕΦΚΑ/ΚΕΑΟ ως εργοδότες με ΑΜΕ
        company = obligations.is_legal_entity(b.get("legal_form") or "")
        self.e_amka.setVisible(not company)
        label = self._cred_form.labelForField(self.e_amka)
        if label is not None:
            label.setVisible(not company)
        self.e_user.setText(efka.ika_user(self.conn, self.afm))
        self.e_status.setText(ef.get("check_message") or ("Ο ΑΜΕ χρειάζεται για τον έλεγχο ΑΠΔ — χωρίς αυτόν γίνεται "
                                                          "προσπάθεια ανάκτησης κατά τον έλεγχο υποβολών."
                                                          if not ef.get("ame") else ""))
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

        self._reload_aml()

        self._reload_matches()

    def _reload_matches(self) -> None:
        all_matches = engine.digest(self.conn, days=90, afm=self.afm)
        general = [m for m in all_matches if m["matched_reason"] == ALL_SCOPE_REASON]
        matches = all_matches if self.show_general.isChecked() else             [m for m in all_matches if m["matched_reason"] != ALL_SCOPE_REASON]
        self.matches_hint.setText(
            f"{len(general)} γενικά νέα κρύφτηκαν (αφορούν όλες τις επιχειρήσεις) — τσεκάρετε το παραπάνω για να τα δείτε."
            if general and not self.show_general.isChecked() else "")
        self.matches_list.clear()
        if not matches:
            placeholder = QListWidgetItem("Κανένα στοχευμένο άρθρο δεν έχει ταιριάξει ακόμη." if general
                                          else "Κανένα άρθρο δεν έχει ταιριάξει ακόμη.")
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
