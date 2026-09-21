"""Κωδικοί TAXISnet ΑΝΑ ΠΕΛΑΤΗ — προαιρετικοί, κρυπτογραφημένοι (`enc:1:`), ποτέ σε logs/UI/εξαγωγές.

Χρησιμοποιούνται μόνο για το lookup του Μητρώου ΑΑΔΕ αυτού του ΑΦΜ (ΔΟΥ, καθεστώς ΦΠΑ, κατηγορία βιβλίων).
Αν ένας πελάτης δεν έχει δικούς του, το lookup πέφτει στον λογαριασμό γραφείου των Ρυθμίσεων (αν υπάρχει).
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable, Iterable, Optional

from .. import crypto, db
from . import lookup_aade


def get(conn: sqlite3.Connection, afm: str) -> Optional[tuple[str, str]]:
    row = conn.execute("SELECT taxis_user, taxis_pass FROM client_credentials WHERE afm=?", (afm,)).fetchone()
    if not row or not row["taxis_user"] or not row["taxis_pass"]:
        return None
    return crypto.dec(row["taxis_user"]), crypto.dec(row["taxis_pass"])


def set_(conn: sqlite3.Connection, afm: str, user: str, password: str) -> bool:
    """Αποθηκεύει (ή ενημερώνει) τους κωδικούς. Κενό `password` κρατά τον υπάρχοντα (όπως στη φόρμα προφίλ)·
    κενός `user` ΔΕΝ σβήνει (η διαγραφή είναι ρητή, βλ. clear). Επιστρέφει False αν δεν υπάρχει τίποτα να αποθηκευτεί."""
    user, password = (user or "").strip(), password or ""
    existing = conn.execute("SELECT taxis_user, taxis_pass FROM client_credentials WHERE afm=?", (afm,)).fetchone()
    if not user and not password:
        return False
    user_enc = crypto.enc(user) if user else (existing["taxis_user"] if existing else "")
    pass_enc = crypto.enc(password) if password else (existing["taxis_pass"] if existing else "")
    conn.execute(
        "INSERT INTO client_credentials(afm, taxis_user, taxis_pass, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(afm) DO UPDATE SET taxis_user=excluded.taxis_user, taxis_pass=excluded.taxis_pass, "
        "check_status='', check_message='', checked_at=NULL, updated_at=excluded.updated_at",
        (afm, user_enc, pass_enc, db.utcnow()))
    return True


def clear(conn: sqlite3.Connection, afm: str) -> None:
    conn.execute("DELETE FROM client_credentials WHERE afm=?", (afm,))


def status_map(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    """{afm: {check_status, check_message, has_user}} — ΧΩΡΙΣ να αποκρυπτογραφεί τίποτα."""
    return {r["afm"]: {"check_status": r["check_status"], "check_message": r["check_message"],
                       "complete": bool(r["taxis_user"] and r["taxis_pass"])}
            for r in conn.execute("SELECT afm, taxis_user, taxis_pass, check_status, check_message FROM client_credentials")}


def masked_user(conn: sqlite3.Connection, afm: str) -> str:
    """Το όνομα χρήστη (όχι ο κωδικός) για προβολή στη φόρμα, ώστε ο χρήστης να ξέρει τι έχει αποθηκευτεί."""
    row = conn.execute("SELECT taxis_user FROM client_credentials WHERE afm=?", (afm,)).fetchone()
    return crypto.dec(row["taxis_user"]) if row and row["taxis_user"] else ""


def test(conn: sqlite3.Connection, afm: str,
         login: Callable[[str, str], dict[str, Any]] = lookup_aade.aade_login) -> tuple[bool, str]:
    """Δοκιμάζει σύνδεση στο TAXISnet με τους κωδικούς του πελάτη και αποθηκεύει το αποτέλεσμα (όχι τον κωδικό)."""
    creds = get(conn, afm)
    if not creds:
        return False, "Δεν έχουν οριστεί κωδικοί TAXISnet για αυτόν τον πελάτη."
    try:
        res = login(*creds)
    except Exception as exc:                       # δίκτυο / αλλαγή σελίδας ΑΑΔΕ
        msg, status, ok = f"Η δοκιμή δεν ολοκληρώθηκε ({str(exc)[:100]})", "error", False
    else:
        if res.get("ok"):
            msg, status, ok = "Η σύνδεση στο TAXISnet πέτυχε.", "ok", True
        else:
            reason = res.get("reason") or ""
            msg = lookup_aade.REASONS_EL.get(reason, reason or "Αποτυχία σύνδεσης")
            status, ok = ("invalid" if reason == "InvalidCredentials" else "error"), False
    conn.execute("UPDATE client_credentials SET check_status=?, check_message=?, checked_at=? WHERE afm=?",
                 (status, msg, db.utcnow(), afm))
    return ok, msg


def bulk_set(conn: sqlite3.Connection, rows: Iterable[tuple[str, str, str]]) -> dict[str, int]:
    """rows: (afm, user, password) για ΥΠΑΡΧΟΝΤΕΣ πελάτες. Επιστρέφει {saved, skipped_unknown}."""
    saved = skipped = 0
    for afm, user, password in rows:
        if not conn.execute("SELECT 1 FROM businesses WHERE afm=?", (afm,)).fetchone():
            skipped += 1
            continue
        if set_(conn, afm, user, password):
            saved += 1
    return {"saved": saved, "skipped_unknown": skipped}
