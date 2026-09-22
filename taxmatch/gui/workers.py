"""Γενικός εκτελεστής εργασιών σε background thread (QThread) — δουλειά που αγγίζει δίκτυο/LLM/ΑΑΔΕ δεν πρέπει
ΠΟΤΕ να τρέξει στο thread του UI (θα πάγωνε το παράθυρο). Ισοδύναμο του `taxmatch/jobs.py` (Flask, polling),
αλλά με Qt signals: πρόοδος/αποτέλεσμα/σφάλμα φτάνουν στο UI thread αυτόματα μέσω queued connections."""
from __future__ import annotations

from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QThread, Signal


class _Runnable(QObject):
    progress = Signal(str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[Callable[[str], None]], Any]) -> None:
        super().__init__()
        self._fn = fn

    def run(self) -> None:
        try:
            result = self._fn(lambda msg: self.progress.emit(msg))
        except Exception as exc:                        # το task δεν πρέπει ΠΟΤΕ να ρίξει το UI
            self.failed.emit(getattr(exc, "message_el", "") or str(exc) or exc.__class__.__name__)
            return
        self.finished.emit(result)


class Task:
    """Κρατά ζωντανά το thread+worker όσο τρέχει (αλλιώς ο garbage collector τα μαζεύει στη μέση της εκτέλεσης).
    Ο καλών πρέπει να κρατήσει αναφορά στο ίδιο το `Task` (π.χ. `self._task = run_task(...)`)."""

    def __init__(self, thread: QThread, worker: _Runnable) -> None:
        self.thread = thread
        self.worker = worker

    def _cleanup(self, *_args: Any) -> None:
        self.thread.quit()
        self.thread.wait(5000)


def run_task(parent: QObject, fn: Callable[[Callable[[str], None]], Any],
            on_progress: Optional[Callable[[str], None]] = None,
            on_done: Optional[Callable[[Any], None]] = None,
            on_error: Optional[Callable[[str], None]] = None) -> Task:
    """Τρέχει `fn(progress_callback)` σε background thread. Επιστρέφει `Task` — κρατήστε αναφορά (π.χ. `self._task`)."""
    thread = QThread(parent)
    worker = _Runnable(fn)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    task = Task(thread, worker)
    if on_progress:
        worker.progress.connect(on_progress)
    if on_done:
        worker.finished.connect(on_done)
    if on_error:
        worker.failed.connect(on_error)
    worker.finished.connect(task._cleanup)
    worker.failed.connect(task._cleanup)
    thread.start()
    return task
