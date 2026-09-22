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


class Task(QObject):
    """Κρατά ζωντανά το thread+worker όσο τρέχει (αλλιώς ο garbage collector τα μαζεύει στη μέση της εκτέλεσης).
    Ο καλών πρέπει να κρατήσει αναφορά στο ίδιο το `Task` (π.χ. `self._task = run_task(...)`).

    ΚΡΙΣΙΜΟ: το `Task` ΠΑΡΑΜΕΝΕΙ στο thread που το δημιούργησε (το UI thread — ΠΟΤΕ δεν καλείται `moveToThread`
    πάνω του, μόνο πάνω στο `worker`). Το `on_progress/on_done/on_error` του καλούντος είναι απλά Python callables
    (closures/lambdas), όχι bound methods πάνω σε QObject — η Qt δεν μπορεί να καθορίσει thread affinity για αυτά,
    άρα ΔΕΝ γίνονται ποτέ αυτόματα queued connection: αν συνδέονταν απευθείας πάνω στα signals του `worker` (που
    ζει στο background thread), θα εκτελούνταν ΣΥΓΧΡΟΝΑ μέσα στο background thread — δηλαδή θα άγγιζαν widgets
    (π.χ. `toast()`, `combo.addItem()`) από μη-UI thread, κάτι που η Qt δεν υποστηρίζει (παγώνει/χαλάει το
    παράθυρο σε πραγματικό Windows platform, ακόμη κι αν φαίνεται να «δουλεύει» σε offscreen tests).
    Εδώ συνδέουμε τα signals του `worker` σε bound methods ΑΥΤΟΥ ΕΔΩ του `Task` (QObject με thread affinity = UI
    thread) — η Qt ΤΩΡΑ αναγνωρίζει σωστά το cross-thread και τα παραδίδει ως queued connection στο UI thread·
    οι δικές μας `_relay_*` μέθοδοι τρέχουν λοιπόν εγγυημένα στο UI thread και μόνο ΤΟΤΕ καλούν το callable του
    καλούντος."""

    def __init__(self, parent: QObject, thread: QThread, worker: _Runnable,
                on_progress: Optional[Callable[[str], None]] = None,
                on_done: Optional[Callable[[Any], None]] = None,
                on_error: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(parent)
        self.thread = thread
        self.worker = worker
        self._on_progress = on_progress
        self._on_done = on_done
        self._on_error = on_error
        worker.progress.connect(self._relay_progress)
        worker.finished.connect(self._relay_done)
        worker.failed.connect(self._relay_error)

    def _relay_progress(self, msg: str) -> None:
        if self._on_progress:
            self._on_progress(msg)

    def _relay_done(self, result: Any) -> None:
        self._cleanup()
        if self._on_done:
            self._on_done(result)

    def _relay_error(self, msg: str) -> None:
        self._cleanup()
        if self._on_error:
            self._on_error(msg)

    def _cleanup(self) -> None:
        self.thread.quit()
        self.thread.wait(5000)


def run_task(parent: QObject, fn: Callable[[Callable[[str], None]], Any],
            on_progress: Optional[Callable[[str], None]] = None,
            on_done: Optional[Callable[[Any], None]] = None,
            on_error: Optional[Callable[[str], None]] = None) -> Task:
    """Τρέχει `fn(progress_callback)` σε background thread. Επιστρέφει `Task` — κρατήστε αναφορά (π.χ. `self._task`).
    Τα `on_progress/on_done/on_error` παραδίδονται ΠΑΝΤΑ στο UI thread (βλ. σχόλιο στο `Task`), όποιο κι αν είναι
    το thread affinity τους — ασφαλές να αγγίζουν widgets."""
    thread = QThread(parent)
    worker = _Runnable(fn)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    task = Task(parent, thread, worker, on_progress=on_progress, on_done=on_done, on_error=on_error)
    thread.start()
    return task
