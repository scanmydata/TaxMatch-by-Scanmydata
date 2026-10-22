"""Παράμετροι γραφείου, τιμοκατάλογος υπηρεσιών και αμοιβές ανά πελάτη (db.py v11, ρητό αίτημα χρήστη 2026-10-22).

* **Παράμετροι γραφείου** — επωνυμία, ΑΦΜ/ΔΟΥ, διεύθυνση, επικοινωνία, αρ. μητρώου ΟΕΕ, υπεύθυνος λογιστής, IBAN.
  Ζουν στον πίνακα `settings` (κλειδιά `office_*`). Από αυτές βγαίνει και η κεφαλίδα των εγγράφων της Δέουσας
  επιμέλειας (`office_lines`), εκτός αν ο χρήστης έχει γράψει δική του στη «Μεθοδολογία» (`aml_office_info`).
* **Τιμοκατάλογος** (`office_services`) — οι υπηρεσίες του γραφείου με προτεινόμενη αμοιβή και περιοδικότητα.
* **Αμοιβές πελάτη** (`client_fees`) — ό,τι έχει συμφωνηθεί με κάθε πελάτη· φαίνεται στην καρτέλα του, στον πίνακα
  πελατών («Αμοιβή/μήνα») και γεμίζει το ιδιωτικό συμφωνητικό (`fee_*` πεδία).

Τα ποσά είναι ΠΡΟ ΦΠΑ· ο συντελεστής ΦΠΑ είναι παράμετρος του γραφείου (προεπιλογή 24%).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from . import db, settings_store


@dataclass(frozen=True)
class OfficeField:
    key: str
    label: str
    placeholder: str = ""


#: Παράμετροι γραφείου (σειρά = σειρά στη φόρμα των Ρυθμίσεων).
OFFICE_FIELDS: tuple[OfficeField, ...] = (
    OfficeField("office_name", "Επωνυμία γραφείου", "π.χ. ΛΟΓΙΣΤΙΚΟ ΓΡΑΦΕΙΟ ΠΑΠΑΔΟΠΟΥΛΟΥ"),
    OfficeField("office_activity", "Δραστηριότητα", "Λογιστικές – φοροτεχνικές υπηρεσίες"),
    OfficeField("office_afm", "ΑΦΜ", ""),
    OfficeField("office_doy", "ΔΟΥ", ""),
    OfficeField("office_address", "Διεύθυνση", "Οδός, αριθμός, ΤΚ"),
    OfficeField("office_city", "Πόλη", "π.χ. Αθήνα (και τόπος υπογραφής εγγράφων)"),
    OfficeField("office_phone", "Τηλέφωνο", ""),
    OfficeField("office_email", "Email", ""),
    OfficeField("office_oee", "Αρ. μητρώου ΟΕΕ / άδεια", "Αριθμός άδειας λογιστή-φοροτεχνικού"),
    OfficeField("office_accountant", "Υπεύθυνος λογιστής", "Ονοματεπώνυμο"),
    OfficeField("office_iban", "IBAN για εξόφληση αμοιβών", "GR…"),
    OfficeField("office_vat_rate", "ΦΠΑ αμοιβών (%)", "24"),
)

PERIODS: tuple[tuple[str, str], ...] = (("monthly", "Μηνιαία"), ("quarterly", "Τριμηνιαία"), ("yearly", "Ετήσια"),
                                         ("once", "Εφάπαξ"))
PERIOD_LABEL = dict(PERIODS)
_PER_MONTH = {"monthly": 1.0, "quarterly": 1 / 3, "yearly": 1 / 12, "once": 0.0}

#: Προτεινόμενες υπηρεσίες για άδειο τιμοκατάλογο (ποσό 0 — τα ορίζει το γραφείο).
SUGGESTED_SERVICES: tuple[tuple[str, str], ...] = (
    ("Τήρηση βιβλίων Β' κατηγορίας (απλογραφικά)", "monthly"),
    ("Τήρηση βιβλίων Γ' κατηγορίας (διπλογραφικά)", "monthly"),
    ("Μισθοδοσία (ανά εργαζόμενο)", "monthly"),
    ("Περιοδική δήλωση ΦΠΑ", "quarterly"),
    ("Δήλωση φορολογίας εισοδήματος (Ε1/Ε2/Ε3)", "yearly"),
    ("Δήλωση φορολογίας νομικού προσώπου (Ν)", "yearly"),
    ("Ισολογισμός / οικονομικές καταστάσεις", "yearly"),
    ("Σύσταση εταιρείας / έναρξη εργασιών", "once"),
)


def get_office(conn: sqlite3.Connection) -> dict[str, str]:
    return {f.key: settings_store.get(conn, f.key) for f in OFFICE_FIELDS}


def save_office(conn: sqlite3.Connection, values: dict[str, str]) -> None:
    for f in OFFICE_FIELDS:
        if f.key in values:
            settings_store.set_value(conn, f.key, (values[f.key] or "").strip())


def vat_rate(conn: sqlite3.Connection) -> float:
    try:
        return float((settings_store.get(conn, "office_vat_rate") or "24").replace(",", "."))
    except ValueError:
        return 24.0


def office_lines(conn: sqlite3.Connection) -> list[str]:
    """Έως 4 γραμμές κεφαλίδας εγγράφων. Η δική του κεφαλίδα (Μεθοδολογία → Στοιχεία γραφείου) υπερισχύει·
    αλλιώς βγαίνει από τις παραμέτρους γραφείου."""
    custom = [ln.strip() for ln in settings_store.get(conn, "aml_office_info").splitlines() if ln.strip()]
    if custom:
        return custom[:4]
    o = get_office(conn)
    address = ", ".join(x for x in (o["office_address"], o["office_city"]) if x)
    ids = " · ".join(x for x in (f"ΑΦΜ {o['office_afm']}" if o["office_afm"] else "",
                                 f"ΔΟΥ {o['office_doy']}" if o["office_doy"] else "",
                                 f"τηλ. {o['office_phone']}" if o["office_phone"] else "",
                                 o["office_email"]) if x)
    return [x for x in (o["office_name"], address, o["office_activity"], ids) if x]


# ------------------------------------------------------------------ τιμοκατάλογος
def list_services(conn: sqlite3.Connection, active_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM office_services" + (" WHERE active=1" if active_only else "") + " ORDER BY sort, id"
    return [dict(r) for r in conn.execute(sql)]


def save_service(conn: sqlite3.Connection, name: str, amount: float, period: str = "monthly",
                 service_id: Optional[int] = None, active: bool = True) -> int:
    name = " ".join((name or "").split())
    if not name:
        raise ValueError("Χρειάζεται όνομα υπηρεσίας.")
    if period not in _PER_MONTH:
        raise ValueError("Άγνωστη περιοδικότητα.")
    if service_id:
        conn.execute("UPDATE office_services SET name=?, default_amount=?, period=?, active=? WHERE id=?",
                     (name, float(amount or 0), period, int(active), service_id))
        return service_id
    sort = conn.execute("SELECT COALESCE(MAX(sort), 0) + 1 FROM office_services").fetchone()[0]
    cur = conn.execute("INSERT INTO office_services(name, default_amount, period, active, sort) VALUES (?,?,?,?,?)",
                       (name, float(amount or 0), period, int(active), sort))
    return int(cur.lastrowid)


def delete_service(conn: sqlite3.Connection, service_id: int) -> None:
    """Οι αμοιβές πελατών που προέρχονται από την υπηρεσία ΜΕΝΟΥΝ (κρατούν την περιγραφή τους)."""
    conn.execute("UPDATE client_fees SET service_id=NULL WHERE service_id=?", (service_id,))
    conn.execute("DELETE FROM office_services WHERE id=?", (service_id,))


def seed_services(conn: sqlite3.Connection) -> int:
    """Γεμίζει άδειο τιμοκατάλογο με τις προτεινόμενες υπηρεσίες (ποσό 0). Επιστρέφει πόσες μπήκαν."""
    if conn.execute("SELECT 1 FROM office_services LIMIT 1").fetchone():
        return 0
    for name, period in SUGGESTED_SERVICES:
        save_service(conn, name, 0, period)
    return len(SUGGESTED_SERVICES)


# ------------------------------------------------------------------ αμοιβές πελάτη
def per_month(amount: float, period: str) -> float:
    """Μηνιαίο ισοδύναμο (οι εφάπαξ δεν μετρούν στο επαναλαμβανόμενο)."""
    return float(amount or 0) * _PER_MONTH.get(period, 0.0)


def list_fees(conn: sqlite3.Connection, afm: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM client_fees WHERE afm=? ORDER BY id", (afm,))]


def save_fee(conn: sqlite3.Connection, afm: str, description: str, amount: float, period: str = "monthly", *,
             fee_id: Optional[int] = None, service_id: Optional[int] = None, start_date: str = "", end_date: str = "",
             notes: str = "") -> int:
    description = " ".join((description or "").split())
    if not description:
        raise ValueError("Χρειάζεται περιγραφή υπηρεσίας.")
    if period not in _PER_MONTH:
        raise ValueError("Άγνωστη περιοδικότητα.")
    args = (description, float(amount or 0), period, service_id, start_date, end_date, notes, db.utcnow())
    if fee_id:
        conn.execute("UPDATE client_fees SET description=?, amount=?, period=?, service_id=?, start_date=?, end_date=?, "
                     "notes=?, updated_at=? WHERE id=? AND afm=?", args + (fee_id, afm))
        return fee_id
    cur = conn.execute("INSERT INTO client_fees(description, amount, period, service_id, start_date, end_date, notes, "
                       "updated_at, afm) VALUES (?,?,?,?,?,?,?,?,?)", args + (afm,))
    return int(cur.lastrowid)


def delete_fee(conn: sqlite3.Connection, fee_id: int) -> None:
    conn.execute("DELETE FROM client_fees WHERE id=?", (fee_id,))


def _active(fee: dict[str, Any], today: str) -> bool:
    return not fee.get("end_date") or fee["end_date"] >= today


def monthly_totals(conn: sqlite3.Connection) -> dict[str, float]:
    """ΑΦΜ -> μηνιαία επαναλαμβανόμενη αμοιβή (προ ΦΠΑ), μόνο ενεργές (χωρίς λήξη ή λήξη στο μέλλον)."""
    from datetime import date
    today = date.today().isoformat()
    out: dict[str, float] = {}
    for r in conn.execute("SELECT afm, amount, period, end_date FROM client_fees"):
        if _active(dict(r), today):
            out[r["afm"]] = out.get(r["afm"], 0.0) + per_month(r["amount"], r["period"])
    return out


def money(value: float) -> str:
    """1234.5 -> «1.234,50 €» (ελληνική μορφή)."""
    s = f"{float(value or 0):,.2f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".") + " €"


def fee_fields(conn: sqlite3.Connection, afm: str) -> dict[str, str]:
    """Πεδία για τα πρότυπα Word (ιδιωτικό συμφωνητικό): πίνακας αμοιβών και σύνολα."""
    from datetime import date
    today = date.today().isoformat()
    fees = [f for f in list_fees(conn, afm) if _active(f, today)]
    rate = vat_rate(conn)
    monthly = sum(per_month(f["amount"], f["period"]) for f in fees)
    lines = [f"{f['description']}: {money(f['amount'])} ({PERIOD_LABEL.get(f['period'], f['period']).lower()})"
             for f in fees]
    out = {"fee_table": "\n".join(lines), "fee_monthly": money(monthly) if fees else "",
           "fee_yearly": money(monthly * 12) if fees else "", "fee_vat_rate": f"{rate:g}%",
           "fee_monthly_with_vat": money(monthly * (1 + rate / 100)) if fees else ""}
    for i in range(1, 9):
        f = fees[i - 1] if i <= len(fees) else None
        out[f"fee{i}_description"] = f["description"] if f else ""
        out[f"fee{i}_amount"] = money(f["amount"]) if f else ""
        out[f"fee{i}_period"] = PERIOD_LABEL.get(f["period"], "") if f else ""
    return out
