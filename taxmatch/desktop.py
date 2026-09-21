"""Desktop shell: τοπικός Flask server (127.0.0.1, τυχαία θύρα) μέσα σε native παράθυρο pywebview."""
from __future__ import annotations

import logging
import secrets
import threading
import time
import webbrowser
from typing import Optional

from werkzeug.serving import make_server

from . import APP_TITLE, config, db, logging_setup
from .web import create_app

log = logging.getLogger(__name__)


class LocalServer:
    """Flask σε background thread· `url` περιέχει το one-time token που ορίζει το cookie συνεδρίας."""

    def __init__(self) -> None:
        self.token = secrets.token_urlsafe(32)
        self.app = create_app(self.token)
        self._server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.port = self._server.server_port
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="flask")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> "LocalServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()


def run_gui(use_browser: bool = False, headless: bool = False) -> int:
    config.load_env()
    logging_setup.setup("app.log")
    db.connect().close()                                   # δημιουργία/migration πριν ανοίξει το UI
    server = LocalServer().start()
    log.info("Server στο http://127.0.0.1:%s", server.port)
    try:
        if headless:
            print(server.url, flush=True)
            _wait_forever()
        elif use_browser or not _open_webview(server.url):
            webbrowser.open(server.url)
            _wait_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


def _open_webview(url: str) -> bool:
    """True αν άνοιξε και έκλεισε κανονικά το native παράθυρο· False αν το pywebview/WebView2 δεν είναι διαθέσιμο."""
    try:
        import webview
    except Exception:
        log.warning("Το pywebview δεν είναι διαθέσιμο — άνοιγμα στον προεπιλεγμένο browser.")
        return False
    icon = config.resource_dir() / "packaging" / "taxmatch.ico"
    try:
        webview.create_window(APP_TITLE, url, width=1360, height=860, min_size=(1000, 640), text_select=True)
        webview.start(icon=str(icon) if icon.exists() else None,
                      storage_path=str(config.data_dir() / "webview"), private_mode=False)
        return True
    except Exception:
        log.exception("Αποτυχία ανοίγματος παραθύρου WebView2")
        return False


def _wait_forever() -> None:
    while True:
        time.sleep(3600)
