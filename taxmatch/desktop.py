"""Εσωτερικός τοπικός Flask server (127.0.0.1, τυχαία θύρα) — ΔΕΝ είναι πλέον η εμφάνιση της εφαρμογής.

Η πραγματική εφαρμογή είναι native (PySide6, `taxmatch/gui/`) — καμία εξάρτηση από webview/browser σε κανονική
χρήση. Αυτό το module μένει μόνο ως εσωτερικό εργαλείο για το test suite (`tests/test_web.py` και συγγενή, που
ασκούν τη λογική των `business_profiles`/`matching`/`deadlines` μέσω HTTP) και για `taxmatch --serve` (ανάπτυξη)."""
from __future__ import annotations

import logging
import secrets
import threading
import time

from werkzeug.serving import make_server

from . import config, db, logs
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


def run_gui(headless: bool = True) -> int:
    """`headless=True` (η μόνη χρήση πλέον, `taxmatch --serve`): τυπώνει το URL και περιμένει — για ανάπτυξη/tests."""
    config.load_env()
    logs.setup(config.data_dir())
    db.connect().close()                                   # δημιουργία/migration πριν ξεκινήσει ο server
    server = LocalServer().start()
    log.info("Server στο http://127.0.0.1:%s", server.port)
    try:
        print(server.url, flush=True)
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0
