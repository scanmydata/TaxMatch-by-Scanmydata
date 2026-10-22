"""Κωδικοί myDATA (όνομα χρήστη + κλειδί API «Ocp-Apim-Subscription-Key») ανά πελάτη — κρυπτογραφημένοι (enc:1:).

Όπως στο Timologio Downloader: το κλειδί ΔΕΝ επιστρέφεται ποτέ στο UI (μόνο «ορισμένο»), δεν γράφεται σε logs/CSV.
Ο κωδικός «Συνθηματικό myData» του Excel είναι ο κωδικός της ΙΣΤΟΣΕΛΙΔΑΣ, όχι το κλειδί API — δεν διαβάζεται.
"""
from __future__ import annotations

import re
import sqlite3
from typing import Optional

from .. import crypto, db

#: Το κλειδί API είναι GUID χωρίς παύλες: 32 δεκαεξαδικά (μετρημένο στο timologio).
KEY_SHAPE = re.compile(r"^[0-9a-fA-F]{32}$")


def get(conn: sqlite3.Connection, afm: str) -> Optional[tuple[str, str]]:
    row = conn.execute("SELECT mydata_user, mydata_key FROM mydata_credentials WHERE afm=?", (afm,)).fetchone()
    if not row or not row["mydata_user"] or not row["mydata_key"]:
        return None
    return crypto.dec(row["mydata_user"]), crypto.dec(row["mydata_key"])


def set_(conn: sqlite3.Connection, afm: str, user: str, key: str) -> bool:
    """Κενό κλειδί = μένει το αποθηκευμένο (η φόρμα δεν το ξαναδείχνει ποτέ)."""
    user, key = (user or "").strip(), (key or "").strip()
    if not user and not key:
        return False
    old = conn.execute("SELECT mydata_user, mydata_key FROM mydata_credentials WHERE afm=?", (afm,)).fetchone()
    conn.execute("INSERT INTO mydata_credentials(afm, mydata_user, mydata_key, check_status, updated_at) VALUES (?,?,?,'',?) "
                 "ON CONFLICT(afm) DO UPDATE SET mydata_user=excluded.mydata_user, mydata_key=excluded.mydata_key, "
                 "check_status='', updated_at=excluded.updated_at",
                 (afm, crypto.enc(user) if user else (old["mydata_user"] if old else ""),
                  crypto.enc(key) if key else (old["mydata_key"] if old else ""), db.utcnow()))
    return True


def clear(conn: sqlite3.Connection, afm: str) -> None:
    conn.execute("DELETE FROM mydata_credentials WHERE afm=?", (afm,))


def user_of(conn: sqlite3.Connection, afm: str) -> str:
    row = conn.execute("SELECT mydata_user FROM mydata_credentials WHERE afm=?", (afm,)).fetchone()
    return crypto.dec(row["mydata_user"]) if row and row["mydata_user"] else ""


def record_check(conn: sqlite3.Connection, afm: str, status: str) -> None:
    conn.execute("UPDATE mydata_credentials SET check_status=? WHERE afm=?", (status, afm))


def with_credentials(conn: sqlite3.Connection) -> set[str]:
    return {r["afm"] for r in conn.execute("SELECT afm FROM mydata_credentials WHERE mydata_user!='' AND mydata_key!=''")}
