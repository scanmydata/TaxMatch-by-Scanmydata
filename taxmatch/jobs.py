"""Background εργασίες με πρόοδο που διαβάζει το UI με polling.

* `JobManager`: ένα «βαρύ» job τη φορά (έλεγχος πηγών/ανάλυση, μαζικός εμπλουτισμός).
* `LookupQueue`: ξεχωριστή ουρά για την ανάκτηση στοιχείων πελατών (ΑΑΔΕ/ΓΕΜΗ/VIES). Ένας νέος πελάτης, ένα import ή
  μια αλλαγή κωδικών TAXISnet μπαίνει στην ουρά ΠΑΝΤΑ — ακόμη κι όταν τρέχει ο έλεγχος («Ανάλυση άρθρου 15/60») —
  αντί να απορρίπτεται σιωπηλά επειδή «τρέχει ήδη άλλη εργασία».
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Iterable, Optional

log = logging.getLogger(__name__)


class LookupQueue:
    """FIFO ουρά ΑΦΜ με ένα worker thread. Κάθε ΑΦΜ ανακτάται με `service.lookup_and_store` και αποθηκεύεται αμέσως·
    στο τέλος της παρτίδας ξαναϋπολογίζονται τα matches (τα νέα ΚΑΔ/κατηγορία βιβλίων αλλάζουν ποιον αφορά τι)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._running = False
        self._state: dict[str, Any] = self._fresh()

    @staticmethod
    def _fresh() -> dict[str, Any]:
        return {"total": 0, "done": 0, "message": "", "current": "", "ok": 0, "partial": 0, "failed": 0, "pending": 0,
                "bad_creds": 0, "bad_office_creds": 0, "finished": None}

    def enqueue(self, afms: Iterable[str]) -> int:
        """Προσθέτει ΑΦΜ (χωρίς διπλότυπα ήδη στην ουρά). Επιστρέφει πόσα προστέθηκαν."""
        with self._lock:
            new = [a for a in dict.fromkeys(afms) if a and a not in self._queued]
            if not new:
                return 0
            if not self._running:                       # νέα παρτίδα: μηδενισμός μετρητών
                self._state = self._fresh()
            self._queue.extend(new)
            self._queued.update(new)
            self._state["total"] += len(new)
            if not self._running:
                self._running = True
                threading.Thread(target=self._run, daemon=True, name="lookup-queue").start()
            return len(new)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"running": self._running, **self._state, "queued": len(self._queue)}

    def _run(self) -> None:
        from . import db                                 # lazy: αποφυγή κυκλικών imports
        from .business_profiles import service as clients
        from .matching import engine

        conn = None
        try:
            conn = db.connect()
            dirty = False
            while True:
                with self._lock:
                    afm = self._queue.popleft() if self._queue else None
                    if afm is None and not dirty:
                        self._running = False
                        self._state.update(message="", current="", finished=time.time())
                        return
                if afm is None:                          # άδεια ουρά: rematch, μετά έλεγχος αν ήρθαν νέα στο μεταξύ
                    self._set(message="Αντιστοίχιση με πελάτες…", current="")
                    try:
                        engine.rematch(conn)
                    except Exception:
                        log.exception("rematch μετά το lookup απέτυχε")
                    dirty = False
                    continue
                self._set(message=f"Ανάκτηση στοιχείων {self._state['done'] + 1}/{self._state['total']}", current=afm)
                out: Optional[dict] = None
                try:
                    out = clients.lookup_and_store(conn, afm)
                except KeyError:
                    pass                                 # ο πελάτης διαγράφηκε στο μεταξύ
                except Exception:
                    log.exception("lookup απέτυχε για %s", afm)
                    out = {"status": "failed", "issues": []}
                dirty = True
                with self._lock:
                    self._queued.discard(afm)
                    self._state["done"] += 1
                    if out:
                        self._state[out["status"]] = self._state.get(out["status"], 0) + 1
                        for issue in out.get("issues", []):
                            key = "bad_office_creds" if issue["code"] == "bad_creds_office" else "bad_creds"
                            self._state[key] += 1
        except Exception:
            log.exception("η ουρά ανάκτησης σταμάτησε")
            with self._lock:
                self._running = False
                self._queue.clear()
                self._queued.clear()
                self._state.update(message="Η ανάκτηση σταμάτησε από σφάλμα (δείτε το Αρχείο καταγραφής).", finished=time.time())
        finally:
            if conn is not None:
                conn.close()

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._state.update(kw)


class JobManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"running": False, "name": "", "message": "", "started": None, "finished": None,
                                       "result": None, "error": ""}
        self.lookups = LookupQueue()

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
            snap = dict(self._state)
        snap["lookup"] = self.lookups.snapshot()
        return snap
