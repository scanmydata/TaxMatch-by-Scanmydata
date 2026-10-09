"""Προεπισκόπηση νέου/υποχρέωσης πριν ανοίξει ο εξωτερικός σύνδεσμος — native ισοδύναμο του popup της web εκδοχής.

Σε native εφαρμογή δεν υπάρχει καν ο πειρασμός να «πεταχτεί» browser tab: το QDesktopServices.openUrl ανοίγει
πάντα ρητά, με το κουμπί «Άνοιγμα συνδέσμου» — ποτέ από μόνο του το κλικ σε ένα άρθρο.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from .theme import CURRENT


class NewsDialog(QDialog):
    """`clients`: [{afm, name, status 'yes'|'maybe', reason}] — ποιους πελάτες αφορά (None = δεν είναι γνωστό, [] = κανέναν).
    `on_client(afm)`: τι γίνεται με διπλό κλικ σε πελάτη (π.χ. άνοιγμα της καρτέλας του)."""

    def __init__(self, title: str, url: str, meta: str = "", summary: str = "", action: str = "",
                parent: Optional[QWidget] = None, clients: Optional[list[dict]] = None,
                on_client: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title[:60] or "Νέο")
        self.setMinimumWidth(440)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        heading = QLabel(title)
        heading.setObjectName("h1")
        heading.setWordWrap(True)
        root.addWidget(heading)

        if meta:
            m = QLabel(meta)
            m.setObjectName("muted")
            root.addWidget(m)

        if summary:
            s = QLabel(summary)
            s.setWordWrap(True)
            root.addWidget(s)

        if action:
            a = QLabel("➜ " + action)
            a.setWordWrap(True)
            a.setStyleSheet(f"background:{CURRENT.chip}; border:1px solid {CURRENT.accent}; color:{CURRENT.accent}; "
                            "border-radius:9px; padding:6px 10px; font-weight:600;")
            root.addWidget(a)

        self.clients_list: Optional[QListWidget] = None
        if clients is not None:
            sure = sum(1 for c in clients if c.get("status") != "maybe")
            maybe = len(clients) - sure
            if not clients:
                head = "Δεν αφορά κανέναν από τους πελάτες σας (με βάση τα στοιχεία τους)."
            elif not sure:
                head = f"Υπό προϋποθέσεις για {maybe} πελάτες:"
            else:
                head = f"Αφορά {sure} πελάτες" + (f" · {maybe} ακόμη υπό προϋποθέσεις" if maybe else "") + ":"
            h = QLabel(head)
            h.setStyleSheet("font-weight:600;")
            root.addWidget(h)
            if clients:
                self.setMinimumWidth(560)
                lst = self.clients_list = QListWidget()
                lst.setMaximumHeight(min(260, 26 * len(clients) + 8))
                for c in clients:
                    mark = "●" if c.get("status") != "maybe" else "○"
                    text = f"{mark}  {c['name']}  ·  ΑΦΜ {c['afm']}" + (f"  —  {c['reason']}" if c.get("reason") else "")
                    item = QListWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, c["afm"])
                    if c.get("status") == "maybe":
                        item.setForeground(QColor(CURRENT.muted))
                        item.setToolTip("Υπό προϋποθέσεις — εξαρτάται από στοιχείο που δεν γνωρίζει η εφαρμογή.")
                    lst.addItem(item)
                if on_client:
                    lst.itemActivated.connect(lambda it: (self.accept(), on_client(it.data(Qt.ItemDataRole.UserRole))))
                    lst.setToolTip("Διπλό κλικ: άνοιγμα της καρτέλας του πελάτη")
                root.addWidget(lst)

        buttons = QDialogButtonBox()
        buttons.addButton("Κλείσιμο", QDialogButtonBox.ButtonRole.RejectRole)
        open_btn = buttons.addButton("Άνοιγμα συνδέσμου", QDialogButtonBox.ButtonRole.AcceptRole)
        open_btn.setObjectName("primary")
        open_btn.setEnabled(bool(url))
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(lambda: (QDesktopServices.openUrl(QUrl(url)), self.accept()))
        root.addWidget(buttons)
