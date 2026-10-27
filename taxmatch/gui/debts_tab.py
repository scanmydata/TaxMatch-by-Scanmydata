"""Καρτέλα πελάτη → «Οφειλές & email»: οφειλές ανά πύλη — ΑΑΔΕ (εκτός/σε ρύθμιση), ΚΕΑΟ (ανά φορέα και ρύθμιση),
τρέχουσες εισφορές e-ΕΦΚΑ — και χρεωστικές δηλώσεις, με την επόμενη δόση, την ταυτότητα πληρωμής και τα έντυπα (PDF
ταυτοτήτων με το δοσολόγιο, καρτέλες ΚΕΑΟ), email (οφειλές / νέα) και ιστορικό αποστολών. Λογική: `debts.py`,
`keao.py`, `mailer.py`. Η «Ενημέρωση οφειλών» ανανεώνει δεδομένα και κατεβάζει τα διαθέσιμα PDF."""
from __future__ import annotations

import html
from datetime import date, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QGridLayout, QHBoxLayout, QLabel, QListWidget, QMenu, QPushButton,
    QScrollArea, QTableWidget, QToolButton, QVBoxLayout, QWidget,
)

from .. import db as dbmod
from .. import debts, mailer, office
from .email_dialog import EmailDialog
from .icons import icon
from .table_filter import cell_menu_actions
from .table_kit import SortItem, TableKit
from .theme import CURRENT
from .toast import toast
from .workers import run_task

_COLS = ["Πύλη", "Είδος", "Περιγραφή", "Ληξιπρόθεσμο", "Εμπρόθεσμο", "Σύνολο", "Οφειλόμενη δόση",
         "Ταυτότητα πληρωμής", "PDF"]


def _when(iso: str) -> str:
    try:
        return datetime.strptime(iso[:16], "%Y-%m-%dT%H:%M").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return iso or ""


def _late(due: str) -> bool:
    try:
        d, m, y = (int(x) for x in due.split("/"))
        return date(y, m, d) < date.today()
    except ValueError:
        return False


class DebtsTab(QWidget):
    def __init__(self, conn, afm: str, window: QWidget, main: Any = None) -> None:
        super().__init__()
        self.conn, self.afm, self.window_, self.main = conn, afm, window, main
        self._tasks: list[Any] = []
        self._rows: list[dict[str, Any]] = []
        root = QVBoxLayout(self)
        bar = QHBoxLayout()
        fetch = QPushButton(icon("refresh", CURRENT.txt, 16), "  Ενημέρωση οφειλών")
        fetch.setObjectName("primary")
        fetch.setToolTip("Ενημερώνει στοιχεία οφειλών και κατεβάζει τα διαθέσιμα PDF ταυτότητας/δοσολογίου για ΑΑΔΕ, "
                 "ΚΕΑΟ και e-ΕΦΚΑ. Για ΚΕΑΟ/ΕΦΚΑ χρειάζεται ΑΜΚΑ ή ΑΜΕ στην καρτέλα «Κωδικοί».")
        fetch.clicked.connect(lambda: self._fetch(True))
        self.fetch_button = fetch
        bar.addWidget(fetch)
        open_pdf = QPushButton(icon("pdf", CURRENT.txt, 16), "  Άνοιγμα PDF")
        open_pdf.clicked.connect(self._open_pdf)
        bar.addWidget(open_pdf)
        bar.addStretch()
        mail_debts = QPushButton(icon("send", CURRENT.txt, 16), "  Email οφειλών…")
        mail_debts.clicked.connect(lambda: self._email("debts"))
        bar.addWidget(mail_debts)
        mail_news = QPushButton(icon("send", CURRENT.txt, 16), "  Email νέων…")
        mail_news.setToolTip("Τα νέα των τελευταίων 30 ημερών που αφορούν συγκεκριμένα τον πελάτη (όχι τα γενικά)")
        mail_news.clicked.connect(lambda: self._email("news"))
        bar.addWidget(mail_news)
        root.addLayout(bar)
        self.info = QLabel("")
        self.info.setWordWrap(True)
        self.info.setTextFormat(Qt.TextFormat.RichText)
        root.addWidget(self.info)

        self.table = QTableWidget(0, len(_COLS))
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.kit = TableKit(self.table, list(zip(_COLS, (62, 140, 0, 118, 110, 100, 175, 175, 50))), "client_debts",
                            cell_menu=False, placeholder="Αναζήτηση οφειλής (περιγραφή, ταυτότητα, ποσό…)")
        root.addWidget(self.kit.bar)
        self.table.horizontalHeaderItem(4).setToolTip("Εμπρόθεσμο υπόλοιπο ΑΑΔΕ · στο ΚΕΑΟ: ό,τι είναι σε ενεργή ρύθμιση")
        self.table.doubleClicked.connect(lambda *_: self._open_card())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._menu)
        self.table.setToolTip("Διπλό κλικ ή δεξί κλικ → «Καρτέλα οφειλής» (ανάλυση, δοσολόγιο, ταυτότητα, PDF)")
        root.addWidget(self.table, 2)
        self.inactive = QCheckBox("Εμφάνιση και των μη ενεργών ρυθμίσεων ΚΕΑΟ (ιστορικό — δεν μετρούν πουθενά)")
        self.inactive.toggled.connect(lambda _on: self.reload())
        root.addWidget(self.inactive)
        hist = QLabel("Ιστορικό email")
        hist.setObjectName("h2")
        root.addWidget(hist)
        self.history = QListWidget()
        self.history.setMaximumHeight(110)
        root.addWidget(self.history)
        self.reload()

    # ------------------------------------------------------------------ προβολή
    def reload(self) -> None:
        self._rows = debts.client_debts(self.conn, self.afm, include_inactive=self.inactive.isChecked())
        sync = debts.source_sync(self.conn, self.afm)
        row = self.conn.execute("SELECT email, email_assistant FROM businesses WHERE afm=?", (self.afm,)).fetchone()
        email, assistant = (row["email"], row["email_assistant"]) if row else ("", "")
        parts = []
        for source, label in debts.SOURCES.items():
            s = sync.get(source)
            if not s:
                continue
            if s["status"] == "ok":
                parts.append(f"{label}: <b>{office.money(s['total'])}</b> <span style='color:{CURRENT.muted}'>"
                             f"({_when(s['synced_at'])})</span>")
            else:
                parts.append(f"{label}: <span style='color:{CURRENT.bad}'>{html.escape(s['message'])}</span>")
        aade = debts.sync_info(self.conn, self.afm)
        if aade and aade["status"] == "ok" and aade["overdue"]:
            parts.append(f"ληξιπρόθεσμο ΑΑΔΕ <b style='color:{CURRENT.bad}'>{office.money(aade['overdue'])}</b>")
        if not parts:
            parts.append(f"<span style='color:{CURRENT.muted}'>Δεν έχει γίνει ενημέρωση οφειλών.</span>")
        if email:
            parts.append(f"Email πελάτη: <b>{html.escape(email)}</b>")
        elif assistant:
            parts.append(f"<span style='color:{CURRENT.warn}'>Χωρίς προσωπικό email — μόνο του βοηθού "
                         f"({html.escape(assistant)}), όχι για αυτόματα email</span>")
        else:
            parts.append(f"<span style='color:{CURRENT.warn}'>Ο πελάτης δεν έχει email (Προφίλ ή ανάκτηση στοιχείων)</span>")
        if not mailer.configured(self.conn):
            parts.append(f"<span style='color:{CURRENT.warn}'>SMTP: Ρυθμίσεις → Email προς πελάτες</span>")
        self.info.setText(" · ".join(parts))
        t = self.table
        self.kit.begin()
        t.setRowCount(len(self._rows))
        for i, d in enumerate(self._rows):
            has_pdf = bool(d["pdf_path"]) and Path(d["pdf_path"]).exists()
            nxt = ""
            if d.get("next_due"):
                nxt = d["next_due"] + (f" · {office.money(d['next_amount'])}" if d.get("next_amount") else "")
                if d.get("instalments"):
                    nxt += f" ({d['unpaid']}/{d['instalments']})"
            cells = [debts.SOURCES.get(d.get("source", "aade"), ""), debts.KIND_LABEL.get(d["kind"], d["kind"]), d["title"],
                     office.money(d["overdue"]) if d["overdue"] else "", office.money(d["non_overdue"]) if d["non_overdue"] else "",
                     office.money(d["total"]) if d["total"] else "", nxt, d["code"] or "—", "PDF" if has_pdf else "—"]
            for col, text in enumerate(cells):
                item = SortItem(text)
                if col in (3, 4, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 3 and d["overdue"]:
                    item.setForeground(QColor(CURRENT.bad))
                if col == 6 and d.get("next_due") and _late(d["next_due"]):
                    item.setForeground(QColor(CURRENT.bad))
                    item.setToolTip("Η δόση έχει λήξει χωρίς να πληρωθεί")
                if col == 8:
                    item.setForeground(QColor(CURRENT.ok if has_pdf else CURRENT.muted))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if d["kind"] == "keao_inactive":
                    item.setForeground(QColor(CURRENT.muted))
                if col == 0:
                    item.setData(Qt.ItemDataRole.UserRole, i)
                t.setItem(i, col, item)
        self.kit.end()
        self.history.clear()
        for h in mailer.history(self.conn, self.afm):
            mark = "✓" if h["status"] == "sent" else "✗"
            self.history.addItem(f"{mark} {_when(h['sent_at'])} · {mailer.KIND_LABEL.get(h['kind'], h['kind'])}"
                                 f"{' (αυτόματο)' if h['auto'] else ''} · {h['recipient']} · {h['subject']}"
                                 + (f" — {h['error']}" if h["error"] else ""))
        if not self.history.count():
            self.history.addItem("Δεν έχει σταλεί email.")

    def _selected(self) -> dict[str, Any] | None:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        if not rows:
            return None
        return self._rows[self.table.item(rows[0], 0).data(Qt.ItemDataRole.UserRole)]

    @staticmethod
    def _table_html(headers: list[str], rows: list[list[str]], limit: int = 36) -> str:
        head = "".join(f"<th align='left' style='padding:2px 8px'>{html.escape(h)}</th>" for h in headers)
        body = "".join("<tr>" + "".join(f"<td style='padding:2px 8px'>{html.escape(str(c))}</td>" for c in r) + "</tr>"
                       for r in rows[:limit])
        return f"<table><tr>{head}</tr>{body}</table>"

    @classmethod
    def details_html(cls, d: dict[str, Any], *, include_schedules: bool = True) -> str:
        info = d["details"] or {}
        lines = [f"<b>{html.escape(d['title'])}</b>"]
        if d["kind"] == "declaration":
            from .. import obligations
            lines.append(html.escape(obligations.amounts_text(info)))
            for key, label in (("doy", "ΔΟΥ"), ("tax_kind", "Είδος φόρου"), ("issued_at", "Έκδοση"),
                               ("submitted_at", "Υποβολή"), ("registration_no", "Αρ. καταχώρισης")):
                if info.get(key):
                    lines.append(f"{label}: {html.escape(str(info[key]))}")
        elif d["kind"] == "carrier":
            owed = info.get("owed") or {}
            lines.append(" · ".join(x for x in (info.get("category"), f"ΑΜΟ {info['amo']}" if info.get("amo") else "",
                                                info.get("branch")) if x))
            if not owed.get("fallback"):
                lines.append(f"Εκτός ρύθμισης {office.money(owed.get('outside', 0))} (πρόσθετα τέλη "
                             f"{office.money(owed.get('additional', 0))}) · σε ενεργές ρυθμίσεις "
                             f"{office.money(owed.get('regulated', 0))}")
            if info.get("outstanding"):
                lines.append(cls._table_html(["Ημερομηνία", "Παραστατικό", "Κύρια", "Πρόσθετα", "Σύνολο"],
                                              [[o["date"], o["document"], o["primary"], o["additional"], o["total"]]
                                               for o in info["outstanding"]]))
        elif d["kind"] in ("keao_arrangement", "keao_inactive"):
            r = info.get("regulation") or {}
            lines.append(" · ".join(x for x in (info.get("carrier"), r.get("info"), r.get("status"),
                                                f"τόκοι {r['interest']}" if r.get("interest") else "") if x))
            if r.get("instalments") and include_schedules:
                lines.append(cls._table_html(["Α/Α", "Λήξη", "Ποσό", "Καταβλήθηκε", "Υπόλοιπο", "Προσαυξήσεις"],
                                              [[i["no"], i["due"], i["amount"], i["paid"], i["balance"], i["increments"]]
                                               for i in r["instalments"]]))
        elif d["kind"] == "efka":
            lines.append(f"Μήνας {html.escape(info.get('month', ''))} · εμπρόθεσμη πληρωμή έως "
                         f"{html.escape(info.get('due', ''))}")
        else:
            for k, v in (info.get("fields") or {}).items():
                if v and not k.lower().startswith(("επιλογ", "ενέργ")):
                    lines.append(f"{html.escape(k)}: {html.escape(v)}")
            for row in info.get("general") or []:
                lines.append(" · ".join(html.escape(c) for c in row if c))
            inst = info.get("installments")
            if inst and include_schedules:
                lines.append(cls._table_html(inst["headers"], inst["rows"]))
        return "<br>".join(x for x in lines if x)

    @classmethod
    def installment_html(cls, d: dict[str, Any]) -> str:
        info = d.get("details") or {}
        if d.get("kind") in ("keao_arrangement", "keao_inactive"):
            regulation = info.get("regulation") or {}
            installments = regulation.get("instalments") or []
            if installments:
                return cls._table_html(["Α/Α", "Λήξη", "Ποσό", "Καταβλήθηκε", "Υπόλοιπο", "Προσαυξήσεις"],
                                       [[item.get("no", ""), item.get("due", ""), item.get("amount", ""),
                                         item.get("paid", ""), item.get("balance", ""), item.get("increments", "")]
                                        for item in installments])
        installments = info.get("installments") or {}
        if installments.get("rows"):
            return cls._table_html(installments.get("headers") or [], installments["rows"])
        return ""

    # ------------------------------------------------------------------ ενέργειες
    def _fetch(self, want_pdf: bool) -> None:
        if self.main is not None and hasattr(self.main, "run_debt_sync"):
            self.main.run_debt_sync([self.afm], window=self.window_, on_done=self.reload, want_pdf=want_pdf)
            return

        def work(_p):
            conn = dbmod.connect()
            try:
                return debts.check_client(conn, self.afm, want_pdf=want_pdf)
            finally:
                conn.close()

        def done(res):
            toast(self.window_, "Οι οφειλές ενημερώθηκαν." if res["ok"] else res["reason"], "ok" if res["ok"] else "danger")
            self.reload()
        self._tasks.append(run_task(self, work, on_done=done, on_error=lambda m: toast(self.window_, m, "danger")))

    def _menu(self, pos) -> None:
        item = self.table.itemAt(pos)
        if item is None:
            return
        self.table.selectRow(item.row())
        d = self._selected()
        if not d:
            return
        has_pdf = bool(d["pdf_path"]) and Path(d["pdf_path"]).exists()
        menu = QMenu(self.table)
        menu.addAction(icon("info", CURRENT.txt, 16), "Καρτέλα οφειλής…", self._open_card)
        act = menu.addAction(icon("pdf", CURRENT.txt, 16), "Άνοιγμα PDF", self._open_pdf)
        act.setEnabled(has_pdf)
        menu.addAction(icon("download", CURRENT.txt, 16), "Νέα λήψη PDF" if has_pdf
                       else "Λήψη PDF (ταυτότητα + δοσολόγιο)", lambda: self._fetch(True))
        if d["code"]:
            menu.addAction(icon("key", CURRENT.txt, 16), "Αντιγραφή ταυτότητας πληρωμής", lambda: self._copy(d["code"]))
        menu.addSeparator()
        cell_menu_actions(menu, self.table, pos, self.kit.col_filter)   # αντιγραφή κελιού / φίλτρο με την τιμή
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _copy(self, text: str) -> None:
        QGuiApplication.clipboard().setText(text)
        toast(self.window_, "Η ταυτότητα πληρωμής αντιγράφηκε.", "ok")

    def _open_card(self) -> None:
        d = self._selected()
        if d:
            DebtCard(self, d).exec()

    def _open_pdf(self) -> None:
        d = self._selected()
        if d and d["pdf_path"] and Path(d["pdf_path"]).exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(d["pdf_path"]))
        elif d:
            toast(self.window_, "Δεν υπάρχει διαθέσιμο PDF. Δοκιμάστε ξανά την «Ενημέρωση οφειλών».", "warn")

    def _email(self, kind: str) -> None:
        if not mailer.configured(self.conn):
            toast(self.window_, "Ορίστε πρώτα τον λογαριασμό email του γραφείου (Ρυθμίσεις → Email προς πελάτες).", "warn")
            return
        msg = mailer.compose_debts(self.conn, self.afm) if kind == "debts" else mailer.compose_news(self.conn, self.afm)
        if msg is None:
            toast(self.window_, "Καμία οφειλή για αποστολή." if kind == "debts"
                  else "Κανένα νέο των τελευταίων 30 ημερών δεν αφορά συγκεκριμένα αυτόν τον πελάτη.", "warn")
            return
        if kind == "debts" and not msg.attachments:
            toast(self.window_, "Δεν υπάρχουν διαθέσιμα PDF συνημμένα για αυτές τις οφειλές.", "info")
        EmailDialog(self.window_, self.afm, kind, msg, on_sent=self.reload).exec()


class DebtCard(QDialog):
    """«Καρτέλα οφειλής» (δεξί κλικ / διπλό κλικ): ποσά, οφειλόμενη δόση, ταυτότητα πληρωμής, ανάλυση και
    δοσολόγιο, με λήψη/άνοιγμα του PDF (Ταυτότητα Οφειλής + δοσολόγιο ως 2η σελίδα)."""

    def __init__(self, tab: DebtsTab, d: dict[str, Any]) -> None:
        super().__init__(tab.window_)
        self.tab, self.d = tab, d
        self.setWindowTitle("Καρτέλα οφειλής")
        self.resize(760, 620)
        root = QVBoxLayout(self)
        title = QLabel(f"{debts.SOURCES.get(d.get('source', 'aade'), '')} · "
                       f"{debts.KIND_LABEL.get(d['kind'], d['kind'])} — {d['title']}")
        title.setObjectName("h2")
        title.setWordWrap(True)
        root.addWidget(title)
        grid = QGridLayout()
        nxt = ""
        if d.get("next_due"):
            nxt = d["next_due"] + (f" · {office.money(d['next_amount'])}" if d.get("next_amount") else "")
            if d.get("instalments"):
                nxt += f" (απλήρωτες {d['unpaid']} από {d['instalments']})"
        facts = [("Ληξιπρόθεσμο", office.money(d["overdue"]) if d["overdue"] else "—"),
                 ("Εμπρόθεσμο", office.money(d["non_overdue"]) if d["non_overdue"] else "—"),
                 ("Σύνολο", office.money(d["total"]) if d["total"] else "—"),
                 ("Οφειλόμενη δόση", nxt or "—"),
                 ("Ταυτότητα πληρωμής", d["code"] or "—")]
        for row, (label, value) in enumerate(facts):
            k = QLabel(label + ":")
            k.setStyleSheet(f"color:{CURRENT.muted}")
            v = QLabel(f"<b>{html.escape(value)}</b>")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            if label == "Ληξιπρόθεσμο" and d["overdue"]:
                v.setStyleSheet(f"color:{CURRENT.bad}")
            grid.addWidget(k, row, 0)
            grid.addWidget(v, row, 1)
        grid.setColumnStretch(1, 1)
        root.addLayout(grid)
        schedule = DebtsTab.installment_html(d)
        self.schedule_scroll = None
        if schedule:
            schedule_title = QLabel("Δοσολόγιο")
            schedule_title.setObjectName("h2")
            root.addWidget(schedule_title)
            schedule_body = QLabel(schedule)
            schedule_body.setWordWrap(True)
            schedule_body.setTextFormat(Qt.TextFormat.RichText)
            schedule_body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            schedule_body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
            schedule_scroll = QScrollArea()
            schedule_scroll.setWidgetResizable(True)
            schedule_scroll.setMaximumHeight(190)
            schedule_scroll.setWidget(schedule_body)
            root.addWidget(schedule_scroll)
            self.schedule_scroll = schedule_scroll
        self.more_toggle = QToolButton()
        self.more_toggle.setText("Λοιπά στοιχεία")
        self.more_toggle.setCheckable(True)
        self.more_toggle.setChecked(False)
        self.more_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.more_toggle.toggled.connect(self._toggle_more)
        root.addWidget(self.more_toggle)
        self.more_scroll = QScrollArea()
        self.more_scroll.setWidgetResizable(True)
        self.more_scroll.setMaximumHeight(180)
        more_body = QLabel(DebtsTab.details_html(d, include_schedules=False))
        more_body.setWordWrap(True)
        more_body.setTextFormat(Qt.TextFormat.RichText)
        more_body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        more_body.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.more_scroll.setWidget(more_body)
        self.more_scroll.setVisible(False)
        root.addWidget(self.more_scroll, 1)
        bar = QHBoxLayout()
        if d["code"]:
            copy = QPushButton(icon("key", CURRENT.txt, 16), "  Αντιγραφή ταυτότητας")
            copy.clicked.connect(lambda: tab._copy(d["code"]))
            bar.addWidget(copy)
        has_pdf = bool(d["pdf_path"]) and Path(d["pdf_path"]).exists()
        get_pdf = QPushButton(icon("download", CURRENT.txt, 16), "  Νέα λήψη PDF" if has_pdf else "  Λήψη PDF")
        get_pdf.setToolTip("Ταυτότητα Οφειλής με το δοσολόγιο ως 2η σελίδα (λήψη για όλες τις οφειλές του πελάτη)")
        get_pdf.clicked.connect(self._fetch)
        bar.addWidget(get_pdf)
        open_pdf = QPushButton(icon("pdf", CURRENT.txt, 16), "  Άνοιγμα PDF")
        open_pdf.setEnabled(has_pdf)
        open_pdf.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(d["pdf_path"])))
        bar.addWidget(open_pdf)
        bar.addStretch()
        close = QPushButton("Κλείσιμο")
        close.clicked.connect(self.accept)
        bar.addWidget(close)
        root.addLayout(bar)

    def _toggle_more(self, expanded: bool) -> None:
        self.more_toggle.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.more_scroll.setVisible(expanded)

    def _fetch(self) -> None:
        self.accept()
        self.tab._fetch(True)
