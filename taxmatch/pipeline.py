"""Ο καθημερινός έλεγχος: λήψη feeds -> εμπλουτισμός πελατών -> LLM extraction -> matching.

Καλείται από το UI («Έλεγξε τώρα») και από το headless scheduled task (`TaxMatch.exe --daily`). Ένα lock
αρχείο εγγυάται ότι δεν τρέχουν δύο ταυτόχρονα.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from typing import Callable, Optional

import requests

from . import backup as backup_mod
from . import config, db, settings_store
from .business_profiles import service as clients
from .extraction import llm_extract
from .ingestion import rss_fetch, sources
from .matching import engine

log = logging.getLogger(__name__)


class AlreadyRunning(Exception):
    message_el = "Ένας έλεγχος τρέχει ήδη (ίσως ο προγραμματισμένος). Δοκιμάστε σε λίγο."


@contextmanager
def run_lock():
    """Αποκλειστικό lock μεταξύ processes (msvcrt σε Windows, fcntl αλλού). Το OS το ελευθερώνει αν το process πεθάνει."""
    path = config.data_dir() / "pipeline.lock"
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise AlreadyRunning() from exc
        else:
            import fcntl
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise AlreadyRunning() from exc
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        finally:
            fh.close()


def enabled_sources(conn: sqlite3.Connection) -> list[sources.Source]:
    return [s for s in sources.SOURCES if s.available and settings_store.source_enabled(conn, s.id, s.default_enabled)]


def run_pipeline(trigger: str = "manual", on_progress: Optional[Callable[[str], None]] = None,
                 session: Optional[requests.Session] = None, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Τρέχει όλα τα βήματα. Ένα βήμα που αποτυγχάνει δεν ακυρώνει τα επόμενα. Επιστρέφει τα στατιστικά (και τα γράφει στο `runs`)."""
    own_conn = conn is None
    conn = conn or db.connect()
    progress = on_progress or (lambda _m: None)
    try:
        with run_lock():
            backup_mod.create_backup(config.db_path(), reason=trigger)  # πριν αγγίξουμε οτιδήποτε γράφει
            run_id = conn.execute("INSERT INTO runs(trigger, started_at) VALUES (?,?)", (trigger, db.utcnow())).lastrowid
            stats: dict = {}
            errors: list[str] = []
            status = "error"        # γίνεται 'ok' μόνο αν ολοκληρωθούν τα βήματα χωρίς εξαίρεση/χωρίς πλήρη αποτυχία πηγών
            try:
                progress("Λήψη πηγών…")
                stats["ingest"] = rss_fetch.ingest(conn, enabled_sources(conn), session, progress)
                all_failed = bool(stats["ingest"]) and all("error" in v for v in stats["ingest"].values())
                if all_failed:
                    errors.append("Καμία πηγή δεν ήταν προσβάσιμη (έλεγχος σύνδεσης).")

                progress("Εμπλουτισμός πελατών…")
                try:
                    stats["enrich"] = clients.enrich_pending(conn, session=session, on_progress=progress)
                except Exception as exc:
                    log.exception("enrich απέτυχε")
                    errors.append(f"Εμπλουτισμός πελατών: {exc}")

                stats["extract"] = _extract_step(conn, session, progress, errors)

                progress("Αντιστοίχιση με πελάτες…")
                stats["match"] = engine.rematch(conn)
                status = "error" if all_failed else "ok"
            except Exception as exc:
                log.exception("Το pipeline απέτυχε")
                errors.append(str(exc))
            finally:
                conn.execute("UPDATE runs SET finished_at=?, status=?, stats_json=?, error=? WHERE id=?",
                             (db.utcnow(), status, json.dumps(stats, ensure_ascii=False), "; ".join(errors), run_id))
            stats["errors"] = errors
            return stats
    finally:
        if own_conn:
            conn.close()


def _extract_step(conn: sqlite3.Connection, session: Optional[requests.Session],
                  progress: Callable[[str], None], errors: list[str]) -> dict:
    try:
        client = llm_extract.LLMClient.from_settings(conn, session)
    except llm_extract.NotConfigured as exc:
        errors.append(str(exc))
        pending = conn.execute("SELECT COUNT(*) FROM articles WHERE extraction_status='pending'").fetchone()[0]
        return {"skipped": str(exc), "pending": pending}
    progress("Ανάλυση άρθρων με LLM…")
    limit = settings_store.get_int(conn, "max_extractions_per_run", 60)
    lookback = settings_store.get_int(conn, "lookback_days", 10)
    use_text = settings_store.get(conn, "fetch_full_text") != "0"
    out = llm_extract.extract_pending(conn, client, limit, lookback, session or client.session, use_text, progress)
    if out.get("stopped"):
        errors.append(out["stopped"])
    # Για την ειδοποίηση μέσα στην εφαρμογή: μόνιμο μήνυμα όσο η ανάλυση δεν δουλεύει, σβήνει με την πρώτη επιτυχία
    if out.get("stopped") or (out.get("failed") and not (out.get("done") or out.get("irrelevant"))):
        settings_store.set_value(conn, "llm_last_error", out.get("stopped") or "Όλα τα άρθρα απέτυχαν στην ανάλυση (δείτε το Αρχείο καταγραφής).")
    elif out.get("done") or out.get("irrelevant"):
        settings_store.set_value(conn, "llm_last_error", "")
    return out


def last_run(conn: sqlite3.Connection) -> Optional[dict]:
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["stats"] = json.loads(d.pop("stats_json") or "{}")
    except ValueError:
        d["stats"] = {}
    return d
