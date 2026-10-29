"""Χρονοπρογραμματισμός ΟΛΩΝ των εργασιών της εφαρμογής (ρητό αίτημα 2026-10-29) — τρέχουν από το Windows Task Scheduler
(`--daily`, βλ. daily.py / scheduler_win.py) χωρίς ανοιχτό παράθυρο.

Το task ξυπνά μία φορά τη μέρα· κάθε εργασία έχει δική της συχνότητα (Ανενεργό / Καθημερινά / Εβδομαδιαία / Μηνιαία)
και τρέχει μόνο όταν «οφείλεται» — με βάση την τελευταία επιτυχημένη εκτέλεση, άρα αν ο υπολογιστής ήταν κλειστός την
ημέρα της εβδομαδιαίας/μηνιαίας εργασίας, τρέχει στο επόμενο ξύπνημα. Κάθε εργασία είναι ανεξάρτητη: μία που αποτυγχάνει
δεν σταματά τις υπόλοιπες, και η ανάλυση άρθρων με LLM έχει χρονικό όριο (πριν τελείωνε η ώρα του task εκεί και ΤΙΠΟΤΑ
άλλο δεν έτρεχε — ζωντανά, 28–29/10/2026: τερματισμός από τα Windows στη 1 ώρα, κωδικός 0x41306).

Κατάσταση ανά εργασία στις ρυθμίσεις: `sched_<id>` (συχνότητα), `sched_last_<id>` (ISO ώρα τελευταίας εκτέλεσης),
`sched_result_<id>` (σύντομο αποτέλεσμα για την οθόνη Ρυθμίσεων)."""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Optional

from . import db, settings_store

log = logging.getLogger(__name__)

FREQS = {"off": "Ανενεργό", "daily": "Καθημερινά", "weekly": "Εβδομαδιαία", "monthly": "Μηνιαία"}
WEEKDAYS = ["Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή"]
NEWS_TIME_BUDGET = 25 * 60          # δευτερόλεπτα για την ανάλυση άρθρων με LLM σε προγραμματισμένη εκτέλεση


@dataclass(frozen=True)
class Job:
    id: str
    label: str
    tooltip: str
    default: str = "off"
    legacy: str = ""                 # παλιά ρύθμιση «1/0» (πριν τη συχνότητα ανά εργασία)


JOBS = [
    Job("news", "Νέα & ανάλυση άρθρων", "Λήψη νέων από τις πηγές, ημερολόγιο ΑΑΔΕ/taxheaven, ανάλυση με LLM (έως "
        f"{NEWS_TIME_BUDGET // 60}′) και αντιστοίχιση σε πελάτες", default="daily"),
    Job("mydata", "Λήψη myDATA", "Πελάτες με κωδικούς myDATA: τρέχων + προηγούμενος μήνας και όσοι λείπουν (χωρίς PDF) — "
        "παρακρατήσεις, τέλη, VIES/Intrastat για το Ημερολόγιο", default="daily", legacy="daily_mydata"),
    Job("submissions", "Έλεγχος υποβολών", "ΑΑΔΕ (Φ2, Φ4/Φ5, ΦΜΥ, …), ΑΠΔ e-ΕΦΚΑ/ΤΕΚΑ, πύλες τελών, Intrastat ΕΛ.ΣΤΑΤ — "
        "τρέχουσα χρήση, πελάτες με κωδικούς TAXISnet (χωρίς PDF)", default="weekly"),
    Job("debts", "Λήψη οφειλών", "ΑΑΔΕ (εκτός/σε ρύθμιση), ΚΕΑΟ ανά φορέα, εισφορές e-ΕΦΚΑ — πελάτες με κωδικούς TAXISnet",
        legacy="daily_debts"),
    Job("requests", "Αιτήματα ΑΑΔΕ", "«Τα Αιτήματά μου»: ειδοποίηση όταν απαντηθεί ή περιμένει ενέργεια του πελάτη",
        legacy="daily_requests"),
    Job("lookup", "Ανανέωση στοιχείων πελατών", "Μητρώο ΑΑΔΕ / ΓΕΜΗ: ΚΑΔ, διακοπή, ΦΠΑ, στοιχεία επικοινωνίας, μέλη "
        "εταιρειών (πελάτες με κωδικούς TAXISnet ή με ΓΕΜΗ)", default="monthly"),
    Job("emails", "Αυτόματα email προς πελάτες", "Οφειλές (όταν αλλάξουν) / νέα — σύμφωνα με τις ρυθμίσεις email",
        default="daily"),
]
BY_ID = {j.id: j for j in JOBS}


def freq(conn: sqlite3.Connection, job_id: str) -> str:
    job = BY_ID[job_id]
    value = settings_store.get(conn, f"sched_{job_id}")
    if value in FREQS:
        return value
    if job.legacy:
        old = settings_store.get(conn, job.legacy)
        if old in ("0", "1"):
            return "daily" if old == "1" else "off"
    return job.default


def set_freq(conn: sqlite3.Connection, job_id: str, value: str) -> None:
    if value not in FREQS:
        raise ValueError(value)
    settings_store.set_value(conn, f"sched_{job_id}", value)
    if BY_ID[job_id].legacy:                         # τα παλιά σημεία που διαβάζουν «daily_*» βλέπουν το ίδιο
        settings_store.set_value(conn, BY_ID[job_id].legacy, "0" if value == "off" else "1")


def weekday(conn: sqlite3.Connection) -> int:
    return min(7, max(1, settings_store.get_int(conn, "sched_weekday", 1)))


def monthday(conn: sqlite3.Connection) -> int:
    return min(28, max(1, settings_store.get_int(conn, "sched_monthday", 1)))


def last_run(conn: sqlite3.Connection, job_id: str) -> Optional[date]:
    raw = settings_store.get(conn, f"sched_last_{job_id}")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date() if raw else None
    except ValueError:
        return None


def is_due(conn: sqlite3.Connection, job_id: str, today: Optional[date] = None) -> bool:
    """Οφείλεται σήμερα; Εβδομαδιαία: την επιλεγμένη ημέρα — ή αργότερα, αν εκείνη χάθηκε (κλειστός υπολογιστής).
    Μηνιαία: από την επιλεγμένη ημέρα του μήνα και μετά, μία φορά τον μήνα."""
    today = today or date.today()
    f, last = freq(conn, job_id), last_run(conn, job_id)
    if f == "off" or last == today:
        return False
    if f == "daily":
        return True
    if f == "weekly":
        if last is None:
            return today.isoweekday() == weekday(conn)
        gap = (today - last).days
        return gap >= 7 or (gap >= 2 and today.isoweekday() == weekday(conn))
    if f == "monthly":
        return today.day >= monthday(conn) and (last is None or (last.year, last.month) != (today.year, today.month))
    return False


def _taxis_afms(conn: sqlite3.Connection) -> list[str]:
    return [r["afm"] for r in conn.execute(
        "SELECT c.afm FROM client_credentials c JOIN businesses b ON b.afm=c.afm "
        "WHERE c.taxis_user<>'' AND c.taxis_pass<>'' ORDER BY c.afm")]


def submission_check_client(conn: sqlite3.Connection, afm: str, years: list[int], want_pdf: bool = False) -> dict[str, Any]:
    """Πλήρης έλεγχος υποβολών ενός πελάτη (ίδιος για GUI και χρονοπρογραμματισμό): ΑΑΔΕ, ΑΠΔ e-ΕΦΚΑ/ΤΕΚΑ ανά ΑΜΕ,
    πύλες τελών (Plastic, dae.govapp.gr) και Intrastat ΕΛ.ΣΤΑΤ με τους κωδικούς του γραφείου."""
    from . import easycheck, efka, fee_portals, intrastat
    result: dict[str, Any] = {"ok": True, "entries": []}
    for y in years:
        r = easycheck.check_client(conn, afm, y, want_pdf=want_pdf)
        result["ok"] = result["ok"] and r.get("ok")
        result["entries"] += r.get("entries", [])
        result["entries"] += efka.check_apd_entries(conn, afm, y)
        fees = fee_portals.check_entries(conn, afm, y)
        fees += intrastat.check_entries(conn, afm, y, want_pdf=want_pdf)
        if fees:
            easycheck.store_results(conn, afm, y, fees)
            result["entries"] += fees
    return result


# ---------------------------------------------------------------- εκτελέσεις (headless — δική τους σύνδεση όπου χρειάζεται)
def _run_news(conn: sqlite3.Connection) -> str:
    from . import pipeline
    try:
        stats = pipeline.run_pipeline("scheduled", time_budget=NEWS_TIME_BUDGET)
    except pipeline.AlreadyRunning:
        return "παραλείφθηκε — τρέχει ήδη έλεγχος νέων"
    for err in stats.get("errors", []):
        log.warning("%s", err)
    ext, match = stats.get("extract") or {}, stats.get("match") or {}
    text = f"{ext.get('done', 0)} άρθρα αναλύθηκαν, {match.get('added', 0)} νέα matches"
    if ext.get("remaining"):
        text += f" · {ext['remaining']} άρθρα για την επόμενη φορά"
    return text


def _run_mydata(conn: sqlite3.Connection) -> str:
    from .mydata import scheduled
    stats = scheduled.run(conn)
    settings_store.set_value(conn, "mydata_last_run", db.utcnow())
    return f"{stats['ok']}/{stats['clients']} πελάτες, {stats['docs']} παραστατικά" + (
        f", {stats['failed']} απέτυχαν" if stats["failed"] else "")


def _run_submissions(conn: sqlite3.Connection) -> str:
    from .business_profiles import service
    afms, year, state = _taxis_afms(conn), date.today().year, {"failed": 0, "missing": 0}

    def each(_afm: str, _n: int, result: dict[str, Any]) -> None:
        state["failed"] += 0 if result.get("ok") else 1
        state["missing"] += sum(1 for e in result.get("entries", []) for p in e["periods"] if p["status"] == "not_submitted")
    service.lookup_many(afms, on_each=each, fetch=lambda c, a: submission_check_client(c, a, [year]))
    return f"{len(afms)} πελάτες, {state['missing']} περίοδοι χωρίς υποβολή" + (
        f", {state['failed']} απέτυχαν" if state["failed"] else "")


def _run_debts(conn: sqlite3.Connection) -> str:
    from . import debts
    ok = failed = 0
    for afm in _taxis_afms(conn):
        try:
            if debts.check_client(conn, afm).get("ok"):
                ok += 1
            else:
                failed += 1
        except Exception:                                  # noqa: BLE001 — ένας πελάτης δεν σταματά τους υπόλοιπους
            log.exception("Οφειλές %s: αποτυχία", afm)
            failed += 1
    return f"{ok} πελάτες" + (f", {failed} απέτυχαν" if failed else "")


def _run_requests(conn: sqlite3.Connection) -> str:
    from . import requests_aade
    changed = failed = 0
    for afm in _taxis_afms(conn):
        try:
            res = requests_aade.refresh(conn, afm)
            changed += len(res.get("changes") or {})
            failed += 0 if res.get("ok") else 1
        except Exception:                                  # noqa: BLE001
            log.exception("Αιτήματα %s: αποτυχία", afm)
            failed += 1
    waiting = len(requests_aade.waiting_on_client(conn))
    return f"{changed} αλλαγές" + (f", {waiting} σε εκκρεμότητα από τον πελάτη" if waiting else "") + (
        f", {failed} απέτυχαν" if failed else "")


def _run_lookup(conn: sqlite3.Connection) -> str:
    from .business_profiles import service
    afms = [r["afm"] for r in conn.execute("SELECT afm FROM businesses ORDER BY afm")]
    state = {"failed": 0}

    def each(_afm: str, _n: int, result: dict[str, Any]) -> None:
        state["failed"] += 1 if result.get("status") == "failed" else 0
    service.lookup_many(afms, on_each=each)
    return f"{len(afms)} πελάτες" + (f", {state['failed']} απέτυχαν" if state["failed"] else "")


def _run_emails(conn: sqlite3.Connection) -> str:
    from . import mailer
    stats = mailer.run_scheduled(conn, refresh_debts=last_run(conn, "debts") != date.today())
    return (f"{stats['debts']} για οφειλές, {stats['news']} για νέα" + (f", {stats['failed']} απέτυχαν" if stats["failed"]
                                                                        else "")) if any(stats.values()) else "τίποτα να σταλεί"


RUNNERS: dict[str, Callable[[sqlite3.Connection], str]] = {
    "news": _run_news, "mydata": _run_mydata, "submissions": _run_submissions, "debts": _run_debts,
    "requests": _run_requests, "lookup": _run_lookup, "emails": _run_emails,
}


def run_job(conn: sqlite3.Connection, job_id: str) -> tuple[bool, str]:
    started = time.monotonic()
    log.info("Προγραμματισμένη εργασία: %s", BY_ID[job_id].label)
    try:
        text, ok = RUNNERS[job_id](conn), True
    except Exception as exc:                               # noqa: BLE001 — οι υπόλοιπες εργασίες συνεχίζουν
        log.exception("Προγραμματισμένη εργασία %s απέτυχε", job_id)
        text, ok = f"σφάλμα: {str(exc)[:160]}", False
    minutes = int((time.monotonic() - started) // 60)
    text += f" ({minutes}′)" if minutes else ""
    settings_store.set_value(conn, f"sched_last_{job_id}", db.utcnow())
    settings_store.set_value(conn, f"sched_result_{job_id}", ("✓ " if ok else "✗ ") + text)
    log.info("%s: %s", BY_ID[job_id].label, text)
    return ok, text


def run_due(conn: sqlite3.Connection, today: Optional[date] = None, force: bool = False) -> dict[str, tuple[bool, str]]:
    """Όσες εργασίες οφείλονται (ή όλες οι ενεργές με `force`), με τη σειρά του `JOBS`."""
    out = {}
    for job in JOBS:
        if (force and freq(conn, job.id) != "off") or is_due(conn, job.id, today):
            out[job.id] = run_job(conn, job.id)
    settings_store.set_value(conn, "sched_last_wake", db.utcnow())
    return out
