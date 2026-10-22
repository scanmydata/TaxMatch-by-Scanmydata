"""Ουρά μακριών εργασιών (λήψη myDATA, έλεγχος υποβολών) — ρητό αίτημα χρήστη 2026-10-22:

* μία εργασία τη φορά· όποια ξεκινήσει ενώ τρέχει άλλη μπαίνει «σε αναμονή» (toast στη σειρά, κάτω από την τρέχουσα)·
* κάθε εργασία έχει «Διακοπή μετά τον τρέχοντα πελάτη»· η `work(progress, cancelled)` ελέγχει το `cancelled()` ανάμεσα
  στους πελάτες·
* εργασία που ξεκίνησε από διάλογο (καρτέλα πελάτη) δείχνει την πρόοδο ΕΚΕΙ· αν ο διάλογος κλείσει, το toast περνά στο
  κύριο παράθυρο (`adopt`) και η εργασία συνεχίζει.

Η `work` τρέχει μέσω `workers.run_task` (QThread, δική της σύνδεση βάσης — βλ. CLAUDE.md §Κανόνες)· τα `on_done/on_error`
καλούνται στο UI thread και προστατεύονται από `RuntimeError` αν ο διάλογος του caller έχει ήδη καταστραφεί.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from PySide6.QtWidgets import QWidget

from .toast import ProgressToast
from .workers import run_task

log = logging.getLogger(__name__)


@dataclass
class Job:
    title: str
    work: Callable[[Callable[[str], None], Callable[[], bool]], Any]
    on_done: Optional[Callable[[Any], None]] = None
    on_error: Optional[Callable[[str], None]] = None
    total: int = 0
    stop: threading.Event = field(default_factory=threading.Event)
    toast: Optional[ProgressToast] = None
    done: int = 0


class JobQueue:
    def __init__(self, main: QWidget) -> None:
        self._main = main
        self._pending: list[Job] = []
        self._running: Optional[Job] = None
        self._tasks: list = []

    @property
    def busy(self) -> bool:
        return self._running is not None

    def jobs(self) -> list[Job]:
        return ([self._running] if self._running else []) + list(self._pending)

    def submit(self, title: str, work, *, total: int = 0, window: Optional[QWidget] = None, on_done=None,
               on_error=None) -> Job:
        job = Job(title, work, on_done, on_error, total)
        waiting = self._running is not None
        job.toast = ProgressToast(window or self._main, ("Σε αναμονή: " if waiting else "Ξεκίνησε: ") + title,
                                  on_cancel=lambda j=job: self._cancel(j))
        self._pending.append(job)
        self._next()
        return job

    def adopt(self, window: QWidget) -> None:
        """Ο διάλογος `window` κλείνει: τα toasts των εργασιών του περνούν στο κύριο παράθυρο."""
        for job in self.jobs():
            if job.toast is not None and job.toast.window is window:
                job.toast.move_to(self._main)

    def _cancel(self, job: Job) -> None:
        job.stop.set()
        if job in self._pending:                         # δεν ξεκίνησε ακόμη: φεύγει από την ουρά
            self._pending.remove(job)
            job.toast.finish(f"Ακυρώθηκε: {job.title}", "warn")

    def _next(self) -> None:
        if self._running is not None or not self._pending:
            return
        job = self._running = self._pending.pop(0)

        def progress_cb(msg: str) -> None:
            if job.toast is not None:
                job.toast.update(msg, job.done, job.total or None)

        def work(progress):
            def report(msg: str, done: Optional[int] = None) -> None:
                if done is not None:
                    job.done = done
                progress(msg)
            return job.work(report, job.stop.is_set)

        def finished(result) -> None:
            self._running = None
            outcome = None
            try:
                if job.on_done:
                    outcome = job.on_done(result)         # μπορεί να επιστρέψει (μήνυμα, επίπεδο) για το τελικό toast
            except RuntimeError:                          # ο διάλογος του caller έκλεισε· το toast έχει ήδη μεταφερθεί
                log.debug("job %s: on_done σε κλειστό διάλογο", job.title)
            msg, level = outcome if isinstance(outcome, tuple) else (
                ("Διακόπηκε: " if job.stop.is_set() else "Ολοκληρώθηκε: ") + job.title,
                "warn" if job.stop.is_set() else "ok")
            if job.stop.is_set() and not msg.startswith("Διακόπηκε"):
                msg = "Διακόπηκε μετά τον τρέχοντα πελάτη — " + msg[0].lower() + msg[1:]
            if job.toast is not None:
                job.toast.finish(msg, level)
            self._next()

        def failed(msg: str) -> None:
            self._running = None
            try:
                if job.on_error:
                    job.on_error(msg)
            except RuntimeError:
                pass
            if job.toast is not None:
                job.toast.finish(f"Σφάλμα — {job.title}: {msg}", "danger")
            self._next()

        if job.toast is not None:
            job.toast.update("Ξεκίνησε: " + job.title, 0, job.total or None)
        self._tasks.append(run_task(self._main, work, on_progress=progress_cb, on_done=finished, on_error=failed))
