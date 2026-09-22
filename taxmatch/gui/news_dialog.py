"""Προεπισκόπηση νέου/υποχρέωσης πριν ανοίξει ο εξωτερικός σύνδεσμος — native ισοδύναμο του popup της web εκδοχής.

Σε native εφαρμογή δεν υπάρχει καν ο πειρασμός να «πεταχτεί» browser tab: το QDesktopServices.openUrl ανοίγει
πάντα ρητά, με το κουμπί «Άνοιγμα συνδέσμου» — ποτέ από μόνο του το κλικ σε ένα άρθρο.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QVBoxLayout, QWidget

from .theme import CURRENT


class NewsDialog(QDialog):
    def __init__(self, title: str, url: str, meta: str = "", summary: str = "", action: str = "",
                parent: Optional[QWidget] = None) -> None:
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

        buttons = QDialogButtonBox()
        buttons.addButton("Κλείσιμο", QDialogButtonBox.ButtonRole.RejectRole)
        open_btn = buttons.addButton("Άνοιγμα συνδέσμου", QDialogButtonBox.ButtonRole.AcceptRole)
        open_btn.setObjectName("primary")
        open_btn.setEnabled(bool(url))
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(lambda: (QDesktopServices.openUrl(QUrl(url)), self.accept()))
        root.addWidget(buttons)
