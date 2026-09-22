"""Εικονίδιο δίπλα στο ρολόι.

Ο server δεν είναι διεργασία-υπηρεσία: είναι η ίδια εφαρμογή, ανοιχτή στον
υπολογιστή που κρατά τον φάκελο. Πρέπει λοιπόν να μπορεί να μένει ανοιχτή χωρίς
να καταλαμβάνει την επιφάνεια εργασίας — και, κυρίως, χωρίς να την κλείσει
κάποιος κατά λάθος πατώντας το ✕, αφήνοντας τα τερματικά χωρίς φάκελο.

Γι' αυτό, όταν το tray είναι ενεργό, το ✕ μαζεύει αντί να κλείνει. Η έξοδος
γίνεται ρητά από το μενού του εικονιδίου: μια ενέργεια που πρέπει να είναι
σκόπιμη, όχι αντανακλαστική.
"""

from __future__ import annotations

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import QMenu, QSystemTrayIcon, QWidget


class Tray(QSystemTrayIcon):
    def __init__(self, window: QWidget, app_icon: QIcon, role_label: str) -> None:
        super().__init__(app_icon, window)
        self._window = window
        self.setToolTip(f"Timologio Downloader — {role_label}")

        menu = QMenu()
        self.act_show = QAction("Άνοιγμα", menu)
        self.act_show.triggered.connect(self.show_window)
        menu.addAction(self.act_show)

        menu.addSeparator()
        self.act_quit = QAction("Έξοδος", menu)
        # Το window κλείνει με force ώστε να μη μαζευτεί ξανά στο tray.
        self.act_quit.triggered.connect(self.quit_app)
        menu.addAction(self.act_quit)

        self.setContextMenu(menu)
        # Κρατάμε αναφορά: χωρίς αυτήν το QMenu καταστρέφεται από τον garbage
        # collector και το δεξί κλικ δεν εμφανίζει τίποτα.
        self._menu = menu

        self.activated.connect(self._on_activated)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.DoubleClick,
            QSystemTrayIcon.ActivationReason.Trigger,
        ):
            self.show_window()

    def show_window(self) -> None:
        # Ίδια διαδρομή με το single-instance: ξεμαζεύει, φέρνει μπροστά και
        # δείχνει τυχόν εκκρεμή ξενάγηση (idempotent — δεν κάνει τίποτα αν έχει
        # ήδη ιδωθεί).
        bring = getattr(self._window, "bring_to_front", None)
        if callable(bring):
            bring()
            return
        self._window.showNormal()
        self._window.raise_()
        self._window.activateWindow()

    def quit_app(self) -> None:
        setattr(self._window, "_really_quit", True)
        self._window.close()

    def notify_minimized(self) -> None:
        """Μία φορά: αλλιώς ο χρήστης νομίζει ότι έκλεισε την εφαρμογή."""
        self.showMessage(
            "Η εφαρμογή συνεχίζει να τρέχει",
            "Ο φάκελος παραμένει διαθέσιμος στα τερματικά. Διπλό κλικ εδώ για "
            "να ανοίξει ξανά το παράθυρο.",
            QSystemTrayIcon.MessageIcon.Information,
            5000,
        )

    def notify(self, title: str, body: str, msecs: int = 6000) -> None:
        """Ειδοποίηση των Windows (κάτω δεξιά) όταν ολοκληρώνεται μια εργασία."""
        self.showMessage(title, body, QSystemTrayIcon.MessageIcon.Information, msecs)
