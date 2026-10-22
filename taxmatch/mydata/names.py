"""Επωνυμίες αντισυμβαλλομένων myDATA — ΑΦΜ → επωνυμία, με την ιεραρχία πηγών του ScanMyData
(`ScanmyData_private/vat_name_cache.py` + `vat_validator.py`, ρητό αίτημα χρήστη 2026-10-22: «η επωνυμία
συναλλασσόμενου προέρχεται από το VIES»):

    client   (3)  η λίστα πελατών του γραφείου (επιμελημένη)
    vies     (2)  μητρώο ΦΠΑ της ΕΕ (VIES REST — ίδιο με `business_profiles/vies.py` και το Timologio Downloader)·
                  fallback στο Business Portal (ΓΕΜΗ) όταν υπάρχει κλειδί, όπως το `vat_validator` του ScanMyData
    invoice  (1)  ό,τι γράφει το ίδιο το παραστατικό (`<issuer><name>` — λείπει σε ~30% και συχνά είναι διακριτικός τίτλος)

Χαμηλότερη πηγή ΔΕΝ πατά ανώτερη, μόνο γεμίζει κενά. Το VIES ρωτιέται για κάθε ΑΦΜ που δεν έχει ακόμη επίσημη επωνυμία
(σειριακά, ~1 κλήση/δευτερόλεπτο, `limit` ανά εκτέλεση — συνεχίζει στον επόμενο καθημερινό έλεγχο)· αστοχίες κρατιούνται
(`source='vies_miss'`) ώστε να μην ξαναρωτηθούν.
"""
from __future__ import annotations

import sqlite3
from typing import Callable, Optional

from .. import db

TRUST = {"client": 3, "vies": 2, "business": 2, "invoice": 1, "vies_miss": 0}


def _upsert(conn: sqlite3.Connection, vat: str, name: str, source: str) -> None:
    """Γράφει μόνο αν η νέα πηγή είναι τουλάχιστον ίδιας εμπιστοσύνης ή αν δεν υπάρχει όνομα."""
    row = conn.execute("SELECT name, source FROM party_names WHERE vat=?", (vat,)).fetchone()
    name = (name or "").strip()
    if row and row["name"] and TRUST.get(row["source"], 0) > TRUST.get(source, 0):
        return
    if row and row["name"] and not name:                    # αστοχία VIES δεν σβήνει γνωστό όνομα
        return
    if row and row["source"] == "vies_miss" and source == "invoice":
        # ρωτήθηκε ήδη στο VIES χωρίς απάντηση: κράτα το όνομα του παραστατικού, όχι ξανά στην ουρά του VIES
        conn.execute("UPDATE party_names SET name=?, updated_at=? WHERE vat=?", (name, db.utcnow(), vat))
        return
    conn.execute("INSERT INTO party_names(vat, name, source, updated_at) VALUES (?,?,?,?) ON CONFLICT(vat) DO UPDATE "
                 "SET name=excluded.name, source=excluded.source, updated_at=excluded.updated_at",
                 (vat, name, source, db.utcnow()))


def learn(conn: sqlite3.Connection, afm: Optional[str] = None) -> int:
    """Πηγές «invoice» και «client» (χωρίς δίκτυο)."""
    n = 0
    sql = "SELECT DISTINCT issuer_vat AS vat, issuer_name AS name FROM mydata_docs WHERE issuer_name<>'' AND issuer_vat<>''"
    sql2 = "SELECT DISTINCT counter_vat AS vat, counter_name AS name FROM mydata_docs WHERE counter_name<>'' AND counter_vat<>''"
    args: tuple = ()
    if afm:
        sql, sql2, args = sql + " AND afm=?", sql2 + " AND afm=?", (afm,)
    for q in (sql, sql2):
        for r in conn.execute(q, args).fetchall():
            _upsert(conn, r["vat"], r["name"], "invoice")
            n += 1
    for r in conn.execute("SELECT afm, name FROM businesses WHERE name<>''").fetchall():
        _upsert(conn, r["afm"], r["name"], "client")
    return n


def name_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["vat"]: r["name"] for r in conn.execute("SELECT vat, name FROM party_names WHERE name<>''")}


def source_map(conn: sqlite3.Connection) -> dict[str, str]:
    return {r["vat"]: r["source"] for r in conn.execute("SELECT vat, source FROM party_names WHERE name<>''")}


def pending(conn: sqlite3.Connection, afm: Optional[str] = None) -> list[str]:
    """Ελληνικά ΑΦΜ (9 ψηφία) αντισυμβαλλομένων χωρίς ΕΠΙΣΗΜΗ επωνυμία (πελάτης/VIES) που δεν έχουν ήδη ρωτηθεί ανεπιτυχώς."""
    where, args = ("AND afm=?", (afm, afm)) if afm else ("", ())
    rows = conn.execute(
        f"SELECT DISTINCT vat FROM (SELECT issuer_vat AS vat FROM mydata_docs WHERE issuer_vat<>'' {where} "
        f"UNION SELECT counter_vat AS vat FROM mydata_docs WHERE counter_vat<>'' {where}) "
        "WHERE vat NOT IN (SELECT vat FROM party_names WHERE source IN ('client','vies','business','vies_miss'))",
        args).fetchall()
    return [r["vat"] for r in rows if len(r["vat"]) == 9 and r["vat"].isdigit()]


missing = pending                                           # παλιό όνομα


def _default_lookup(conn: sqlite3.Connection) -> Callable[[str], tuple[str, str]]:
    from .. import settings_store
    from ..business_profiles import lookup_business_portal, vies
    bp_key = settings_store.get(conn, "business_portal_key")

    def lookup(vat: str) -> tuple[str, str]:
        res = vies.lookup(vat)                              # το σημείο που αντικαθιστούν τα tests (ποτέ πραγματική κλήση)
        if res.valid and res.name:
            return res.name, "vies"
        if bp_key:                                          # όπως το vat_validator του ScanMyData: fallback Business Portal
            try:
                company = lookup_business_portal.lookup(vat, bp_key)
                if company.get("name"):
                    return company["name"], "business"
            except Exception:                               # noqa: BLE001
                pass
        if res.error:
            raise ConnectionError(res.error)                # δίκτυο ≠ «δεν υπάρχει»: να ξαναρωτηθεί αργότερα
        return "", "vies_miss"
    return lookup


def resolve_via_vies(conn: sqlite3.Connection, afm: Optional[str] = None, limit: int = 100,
                     lookup: Optional[Callable[[str], object]] = None,
                     should_cancel: Optional[Callable[[], bool]] = None) -> int:
    """Πηγή «vies» (και «business»). Επιστρέφει πόσες επωνυμίες βρέθηκαν. `lookup(vat)` → (όνομα, πηγή) ή όνομα."""
    lookup = lookup or _default_lookup(conn)
    found = 0
    for vat in pending(conn, afm)[:limit]:
        if should_cancel and should_cancel():
            break
        try:
            res = lookup(vat)
        except Exception:                                   # noqa: BLE001 — δίκτυο: ξαναδοκιμάζεται την επόμενη φορά
            continue
        name, source = res if isinstance(res, tuple) else (res or "", "vies" if res else "vies_miss")
        if name:
            _upsert(conn, vat, name, source)
            found += 1
        else:
            if not conn.execute("SELECT 1 FROM party_names WHERE vat=? AND name<>''", (vat,)).fetchone():
                _upsert(conn, vat, "", "vies_miss")
            else:                                           # κράτα το όνομα του παραστατικού, σημείωσε την αστοχία
                conn.execute("UPDATE party_names SET source='vies_miss' WHERE vat=? AND source='invoice'", (vat,))
    return found


def display_name(doc: dict, names: dict[str, str], afm: str = "") -> tuple[str, str]:
    """(επωνυμία, ΑΦΜ) του αντισυμβαλλόμενου: πρώτα το ευρετήριο (πελάτης/VIES/…), αλλιώς ό,τι γράφει το παραστατικό."""
    from .pdfs import other_party
    name, vat = other_party(doc, afm)
    return (names.get(vat) or name), vat
