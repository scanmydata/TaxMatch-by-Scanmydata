"""Entry point του native desktop GUI (PySide6) — καμία εξάρτηση από Flask/webview."""
from __future__ import annotations

import logging
import os
import sys

from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from .. import APP_TITLE, config, logs
from ..crypto import SecretRedactingFilter

log = logging.getLogger(__name__)

_APP_ID = "scanmydata.TaxMatch"


def _instance_key() -> str:
    """Μοναδικό όνομα socket ανά χρήστη-λογαριασμό — δύο αντίγραφα θα μοιράζονταν την ίδια βάση."""
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    return f"scanmydata.TaxMatch.{user}"


def _activate_running_instance() -> bool:
    """Αν τρέχει ήδη αντίγραφο, του ζητά να έρθει μπροστά. True αν βρέθηκε."""
    socket = QLocalSocket()
    socket.connectToServer(_instance_key())
    if not socket.waitForConnected(400):
        return False
    socket.write(b"show")
    socket.flush()
    socket.waitForBytesWritten(400)
    socket.disconnectFromServer()
    return True


def _install_instance_guard(window) -> QLocalServer | None:
    server = QLocalServer()
    QLocalServer.removeServer(_instance_key())
    if not server.listen(_instance_key()):
        log.warning("Ο φρουρός μοναδικού instance δεν στήθηκε: %s", server.errorString())
        return None

    def _on_second_instance() -> None:
        conn = server.nextPendingConnection()
        if conn is not None:
            conn.readyRead.connect(conn.readAll)
            conn.disconnected.connect(conn.deleteLater)
        window.showNormal()
        window.raise_()
        window.activateWindow()

    server.newConnection.connect(_on_second_instance)
    return server


def _set_app_user_model_id() -> None:
    if os.name != "nt" or getattr(sys, "frozen", False):
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_APP_ID)
    except (OSError, AttributeError):
        pass


def app_icon() -> QIcon:
    from pathlib import Path
    base = Path(getattr(sys, "_MEIPASS", "")) if hasattr(sys, "_MEIPASS") else None
    candidates = []
    if base:
        candidates.append(base / "taxmatch" / "gui" / "assets" / "icon.ico")
    candidates.append(config.resource_dir() / "packaging" / "taxmatch.ico")
    candidates.append(Path(__file__).resolve().parent / "assets" / "icon.ico")
    for path in candidates:
        if path.exists():
            return QIcon(str(path))
    return QIcon()


def main(argv: list[str] | None = None) -> int:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(SecretRedactingFilter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    config.load_env()
    logs.setup(config.data_dir())

    _set_app_user_model_id()
    args = argv if argv is not None else sys.argv
    app = QApplication(args)
    app.setApplicationName(APP_TITLE)
    app.setOrganizationName("scanmydata")
    app.setQuitOnLastWindowClosed(False)
    icon = app_icon()
    app.setWindowIcon(icon)

    from . import i18n
    app._greek_translator = i18n.install(app)  # noqa: SLF001 - πρέπει να μείνει ζωντανός, βλ. i18n.install

    from .theme import CURRENT, apply_theme, install_title_bar_painter
    install_title_bar_painter(app)

    if _activate_running_instance():
        log.info("Τρέχει ήδη αντίγραφο — φέρνω μπροστά το υπάρχον παράθυρο.")
        return 0

    from .. import db as dbmod
    dbmod.connect().close()                                    # δημιουργία/migration πριν ανοίξει το UI
    from . import unlock
    from ..crypto import keyfile_path
    if not unlock.ask_unlock(keyfile_path()):
        return 1

    from .main_window import MainWindow
    window = MainWindow()
    window.setWindowIcon(icon)
    window._single_instance_server = _install_instance_guard(window)  # noqa: SLF001
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
