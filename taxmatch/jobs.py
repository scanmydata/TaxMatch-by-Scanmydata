"""Ένα background job τη φορά (έλεγχος, εμπλουτισμός πελατών) με πρόοδο που διαβάζει το UI με polling."""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"running": False, "name": "", "message": "", "started": None, "finished": None,
                                       "result": None, "error": ""}

    def start(self, name: str, target: Callable[[Callable[[str], None]], Any]) -> bool:
        """False αν τρέχει ήδη κάτι. Το `target` παίρνει callback προόδου και επιστρέφει αποτέλεσμα (JSON-serializable)."""
        with self._lock:
            if self._state["running"]:
                return False
            self._state.update(running=True, name=name, message="Εκκίνηση…", started=time.time(), finished=None,
                               result=None, error="")
        threading.Thread(target=self._run, args=(target,), daemon=True, name=f"job-{name}").start()
        return True

    def _progress(self, message: str) -> None:
        with self._lock:
            self._state["message"] = message

    def _run(self, target: Callable[[Callable[[str], None]], Any]) -> None:
        result, error = None, ""
        try:
            result = target(self._progress)
        except Exception as exc:                       # το job δεν πρέπει να ρίξει το UI
            log.exception("job απέτυχε")
            error = getattr(exc, "message_el", "") or str(exc) or exc.__class__.__name__
        with self._lock:
            self._state.update(running=False, finished=time.time(), result=result, error=error,
                               message="Ολοκληρώθηκε" if not error else "Απέτυχε")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)
