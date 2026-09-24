"""Αποθήκευση δέουσας επιμέλειας (πίνακες `aml_*`, βλ. db.py v5).

Το ιστορικό ΔΕΝ διαγράφεται με τον πελάτη (τήρηση 5 ετών μετά τη λήξη της σχέσης, άρθρο 30 ν. 4557/2018): οι
αξιολογήσεις κρατούν στιγμιότυπο της επωνυμίας, και η λίστα επισκόπησης δείχνει και «πρώην πελάτες» που έχουν ιστορικό.
"""
from __future__ import annotations

import calendar as pycal
import json
import sqlite3
from datetime import date
from typing import Any, Iterable, Optional

from .. import db, settings_store
from . import model
from .content import (ALL_DOC_KEYS, CLIENT_KIND_LABEL, FEE_PAYMENTS, FILE_STATUS_ALERT, FILE_STATUS_LABEL, KYC_ITEMS,
                      TX_LABELS, UBO_LABEL, client_kind, required_documents)


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, pycal.monthrange(year, month)[1]))


def review_months(conn: sqlite3.Connection, category: str) -> int:
    return max(1, settings_store.get_int(conn, f"aml_review_{category}", model.DEFAULT_REVIEW_MONTHS[category]))


def office_model(conn: sqlite3.Connection) -> str:
    return "B" if settings_store.get(conn, "aml_model").strip().upper() == "B" else "A"


# ------------------------------------------------------------------ προφίλ KYC ανά πελάτη

def get_profile(conn: sqlite3.Connection, afm: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM aml_profile WHERE afm=?", (afm,)).fetchone()
    if not row:
        return {"afm": afm, "pep_status": "unknown", "relationship_start": "", "relationship_end": "", "purpose": "",
                "kyc": {}, "notes": "", "updated_at": "", "transactions": [], "file_status": "", "docs": {},
                "ubo_state": "", "ubo_notes": "", "fee_payment": "", "client_kind": "", "rep": {}, "ubos": []}
    d = dict(row)
    d["kyc"] = {k: v for k, v in _loads(d.pop("kyc_json"), {}).items() if v}
    d["docs"] = {k: v for k, v in _loads(d.pop("docs_json"), {}).items() if v}
    d["transactions"] = [t for t in _loads(d.pop("tx_json"), []) if isinstance(t, dict)]
    d["rep"] = _loads(d.pop("rep_json"), {})
    d["ubos"] = [u for u in _loads(d.pop("ubo_json"), []) if isinstance(u, dict)]
    return d


#: Νόμιμος εκπρόσωπος (νομικά πρόσωπα) ή ο ίδιος ο πελάτης (φυσικά πρόσωπα): στοιχεία ταυτοποίησης του άρθρου 13 παρ. 1α
#: και ΠΟΛ.1200/2018 — τηρούνται ΜΟΝΟ για τη δέουσα επιμέλεια (άρθρο 30), όχι στο `businesses.lookup_raw`.
REP_FIELDS = ("name", "afm", "role", "id_number", "address", "phone", "email", "father_name", "birth_date",
              "birth_place", "nationality")
UBO_FIELDS = ("name", "afm", "id_number", "birth_date", "nationality", "country_risk", "address", "percent",
              "control", "pep")


def clean_rep(rep: dict[str, Any]) -> dict[str, str]:
    return {k: str(rep.get(k, "")).strip()[:200] for k in REP_FIELDS if str(rep.get(k, "")).strip()}


def clean_ubos(ubos: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for u in ubos or []:
        row = {k: str(u.get(k, "")).strip()[:200] for k in UBO_FIELDS}
        if row["country_risk"] not in TX_LABELS["country"]:
            row["country_risk"] = ""
        if row["pep"] not in model.PEP_LABEL:
            row["pep"] = ""
        if row["name"] or row["afm"]:
            out.append(row)
    return out


def _loads(raw: Optional[str], default):
    try:
        value = json.loads(raw or "")
    except ValueError:
        return default
    return value if isinstance(value, type(default)) else default


def clean_transactions(transactions: Iterable[dict[str, Any]]) -> list[dict[str, str]]:
    """Κρατά μόνο γνωστές τιμές — ό,τι έρθει από τη φόρμα περνά από εδώ πριν αποθηκευτεί."""
    out = []
    for tx in transactions or []:
        row = {field: str(tx.get(field, "")) for field in ("amount", "channel", "frequency", "activity", "country")}
        row = {k: v if v in TX_LABELS[k] else "" for k, v in row.items()}
        row["justification"] = str(tx.get("justification", "")).strip()[:500]
        if any(row.values()):
            out.append(row)
    return out


_KEEP: Any = object()


def save_profile(conn: sqlite3.Connection, afm: str, *, pep_status: str, relationship_start: str = "",
                 relationship_end: str = "", purpose: str = "", kyc: Optional[dict[str, str]] = None,
                 notes: str = "", transactions: Any = _KEEP, file_status: Any = _KEEP, docs: Any = _KEEP,
                 ubo_state: Any = _KEEP, ubo_notes: Any = _KEEP, fee_payment: Any = _KEEP,
                 client_kind: Any = _KEEP, rep: Any = _KEEP, ubos: Any = _KEEP) -> None:
    """Τα πεδία με `_KEEP` (προεπιλογή) μένουν όπως ήταν — ώστε ένας caller που δεν τα ξέρει να μην τα σβήνει."""
    current = get_profile(conn, afm)
    if pep_status not in model.PEP_LABEL:
        pep_status = "unknown"
    valid = {k for k, _t, _r in KYC_ITEMS}
    kyc_clean = {k: v for k, v in (kyc or {}).items() if k in valid and v}
    txs = clean_transactions(current["transactions"] if transactions is _KEEP else transactions)
    status = current["file_status"] if file_status is _KEEP else file_status
    status = status if status in FILE_STATUS_LABEL else ""
    docs_clean = {k: v for k, v in (current["docs"] if docs is _KEEP else docs or {}).items() if k in ALL_DOC_KEYS and v}
    ubo = current["ubo_state"] if ubo_state is _KEEP else ubo_state
    ubo = ubo if ubo in UBO_LABEL else ""
    ubo_n = current["ubo_notes"] if ubo_notes is _KEEP else (ubo_notes or "").strip()
    fee = current["fee_payment"] if fee_payment is _KEEP else fee_payment
    fee = fee if fee in dict(FEE_PAYMENTS) else ""
    kind = current["client_kind"] if client_kind is _KEEP else client_kind
    kind = kind if kind in CLIENT_KIND_LABEL else ""
    rep_clean = clean_rep(current["rep"] if rep is _KEEP else rep or {})
    ubos_clean = clean_ubos(current["ubos"] if ubos is _KEEP else ubos)
    conn.execute(
        "INSERT INTO aml_profile(afm, pep_status, relationship_start, relationship_end, purpose, kyc_json, notes, "
        "tx_json, file_status, docs_json, ubo_state, ubo_notes, fee_payment, client_kind, rep_json, ubo_json, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(afm) DO UPDATE SET pep_status=excluded.pep_status, "
        "relationship_start=excluded.relationship_start, relationship_end=excluded.relationship_end, "
        "purpose=excluded.purpose, kyc_json=excluded.kyc_json, notes=excluded.notes, tx_json=excluded.tx_json, "
        "file_status=excluded.file_status, docs_json=excluded.docs_json, ubo_state=excluded.ubo_state, "
        "ubo_notes=excluded.ubo_notes, fee_payment=excluded.fee_payment, client_kind=excluded.client_kind, "
        "rep_json=excluded.rep_json, ubo_json=excluded.ubo_json, "
        "updated_at=excluded.updated_at",
        (afm, pep_status, relationship_start, relationship_end, purpose.strip(), json.dumps(kyc_clean, ensure_ascii=False),
         notes.strip(), json.dumps(txs, ensure_ascii=False), status, json.dumps(docs_clean, ensure_ascii=False), ubo,
         ubo_n, fee, kind, json.dumps(rep_clean, ensure_ascii=False), json.dumps(ubos_clean, ensure_ascii=False),
         db.utcnow()),
    )


def effective_kind(profile: dict[str, Any], legal_form: str, activity_state: str = "") -> str:
    """Το είδος πελάτη: όπως το όρισε ο χρήστης, αλλιώς από τη νομική μορφή (ΙΔΙΩΤΗΣ του Μητρώου ΑΑΔΕ = χωρίς ΚΑΔ)."""
    return profile.get("client_kind") or client_kind(legal_form, 0 if activity_state == "none" else 1)


def missing_documents(profile: dict[str, Any], legal_form: str, activity_state: str = "") -> list[str]:
    """Υποχρεωτικά έγγραφα (για το είδος πελάτη) που δεν έχουν σημειωθεί ως παραληφθέντα."""
    have = set(profile.get("docs") or {})
    return [k for k in required_documents(legal_form, effective_kind(profile, legal_form, activity_state)) if k not in have]


def kyc_completeness(profile: dict[str, Any]) -> tuple[int, int]:
    """(ολοκληρωμένα, σύνολο) — ο έλεγχος ΠΕΠ μετρά ως ολοκληρωμένος και όταν έχει δηλωθεί κατάσταση ΠΕΠ."""
    done = set(profile.get("kyc") or {})
    if profile.get("pep_status", "unknown") != "unknown":
        done.add("pep_check")
    if (profile.get("purpose") or "").strip():
        done.add("purpose")
    return len(done & {k for k, _t, _r in KYC_ITEMS}), len(KYC_ITEMS)


# ------------------------------------------------------------------ αξιολογήσεις

def save_assessment(conn: sqlite3.Connection, afm: str, *, factors: Iterable[str], overrides: Iterable[str] = (),
                    kind: str = "initial", assessed_on: Optional[date] = None, model_name: Optional[str] = None,
                    escalate_to: str = "", escalation_note: str = "", justification: str = "", assessor: str = "",
                    approved_by: str = "", next_review: Optional[date] = None) -> int:
    """Υπολογίζει ξανά (ποτέ δεν εμπιστεύεται αποτέλεσμα από το UI), αποθηκεύει και επιστρέφει το id."""
    factors = [f for f in dict.fromkeys(factors) if f in model.FACTORS_BY_KEY]
    overrides = [o for o in dict.fromkeys(overrides) if o in model.OVERRIDES_BY_KEY]
    m = model_name if model_name in ("A", "B") else office_model(conn)
    res = model.compute(factors, overrides)
    model_cat = res.category(m)
    final = model.final_category(model_cat, escalate_to)
    if final != model_cat and not escalation_note.strip():
        raise ValueError("Η αύξηση της κατάταξης πάνω από το μοντέλο χρειάζεται αιτιολογία.")
    when = assessed_on or date.today()
    review = next_review or add_months(when, review_months(conn, final))
    row = conn.execute("SELECT name FROM businesses WHERE afm=?", (afm,)).fetchone()
    name = row["name"] if row else ""
    cur = conn.execute(
        "INSERT INTO aml_assessments(afm, client_name, kind, assessed_on, model, factors_json, overrides_json, sum_a, "
        "cat_a, total_b, cat_b, model_category, final_category, escalation_note, justification, assessor, approved_by, "
        "next_review, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (afm, name, kind if kind in ("initial", "periodic", "extraordinary") else "initial", when.isoformat(), m,
         json.dumps(factors), json.dumps(overrides), res.sum_a, res.cat_a, res.total_b, res.cat_b, model_cat, final,
         escalation_note.strip() if final != model_cat else "", justification.strip(), assessor.strip(),
         approved_by.strip(), review.isoformat(), db.utcnow()),
    )
    return int(cur.lastrowid)


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["factors"] = json.loads(d.pop("factors_json") or "[]")
    d["overrides"] = json.loads(d.pop("overrides_json") or "[]")
    return d


def latest(conn: sqlite3.Connection, afm: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM aml_assessments WHERE afm=? ORDER BY assessed_on DESC, id DESC LIMIT 1",
                       (afm,)).fetchone()
    return _decode(row) if row else None


def history(conn: sqlite3.Connection, afm: str) -> list[dict[str, Any]]:
    return [_decode(r) for r in conn.execute(
        "SELECT * FROM aml_assessments WHERE afm=? ORDER BY assessed_on DESC, id DESC", (afm,))]


def get_assessment(conn: sqlite3.Connection, assessment_id: int) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM aml_assessments WHERE id=?", (assessment_id,)).fetchone()
    return _decode(row) if row else None


def overview(conn: sqlite3.Connection, today: Optional[date] = None) -> list[dict[str, Any]]:
    """Μία γραμμή ανά πελάτη (και ανά πρώην πελάτη με ιστορικό): τελευταία κατάταξη, επόμενη επανεξέταση,
    ΠΕΠ, πληρότητα KYC. `status`: missing | overdue | due_soon | ok (due_soon = μέσα σε 30 ημέρες)."""
    today = today or date.today()
    latest_rows = {r["afm"]: _decode(r) for r in conn.execute(
        "SELECT a.* FROM aml_assessments a WHERE a.id = (SELECT b.id FROM aml_assessments b WHERE b.afm=a.afm "
        "ORDER BY b.assessed_on DESC, b.id DESC LIMIT 1)")}
    profiles = {r["afm"]: get_profile(conn, r["afm"]) for r in conn.execute("SELECT afm FROM aml_profile")}
    out = []
    seen = set()
    for b in conn.execute("SELECT afm, name, legal_form, activity_state FROM businesses ORDER BY name COLLATE NOCASE"):
        seen.add(b["afm"])
        out.append(_overview_row(b["afm"], b["name"], b["legal_form"], b["activity_state"], True,
                                 latest_rows.get(b["afm"]), profiles.get(b["afm"]), today))
    for afm, a in latest_rows.items():
        if afm not in seen:
            out.append(_overview_row(afm, a["client_name"], "", "", False, a, profiles.get(afm), today))
    return out


def _overview_row(afm: str, name: str, legal_form: str, activity_state: str, is_client: bool,
                  a: Optional[dict[str, Any]], profile: Optional[dict[str, Any]], today: date) -> dict[str, Any]:
    profile = profile or {"pep_status": "unknown", "kyc": {}, "purpose": "", "docs": {}, "file_status": ""}
    done, total = kyc_completeness(profile)
    kind = effective_kind(profile, legal_form, activity_state)
    docs_total = len(required_documents(legal_form, kind))
    docs_done = docs_total - len(missing_documents(profile, legal_form, activity_state))
    status = "missing"
    if a:
        try:
            days = (date.fromisoformat(a["next_review"]) - today).days
        except ValueError:
            days = 0
        status = "overdue" if days < 0 else "due_soon" if days <= 30 else "ok"
    return {"afm": afm, "name": name, "legal_form": legal_form, "activity_state": activity_state,
            "is_client": is_client, "assessment": a, "category": a["final_category"] if a else "",
            "next_review": a["next_review"] if a else "", "status": status, "pep_status": profile["pep_status"],
            "kyc_done": done, "kyc_total": total, "docs_done": docs_done, "docs_total": docs_total,
            "file_status": profile.get("file_status", ""), "client_kind": kind}


def summary_counts(conn: sqlite3.Connection, today: Optional[date] = None) -> dict[str, int]:
    rows = [r for r in overview(conn, today) if r["is_client"]]
    return {
        "clients": len(rows),
        "missing": sum(1 for r in rows if r["status"] == "missing"),
        "overdue": sum(1 for r in rows if r["status"] == "overdue"),
        "due_soon": sum(1 for r in rows if r["status"] == "due_soon"),
        "low": sum(1 for r in rows if r["category"] == model.LOW),
        "medium": sum(1 for r in rows if r["category"] == model.MEDIUM),
        "high": sum(1 for r in rows if r["category"] == model.HIGH),
        "pep": sum(1 for r in rows if r["pep_status"] in ("domestic", "foreign", "family", "associate")),
        "docs_missing": sum(1 for r in rows if r["docs_done"] < r["docs_total"]),
        "alerts": sum(1 for r in rows if r["file_status"] in FILE_STATUS_ALERT),
    }


def reviews_between(conn: sqlite3.Connection, start: date, end: date, afm: Optional[str] = None) -> list[dict[str, Any]]:
    """Επανεξετάσεις (της ΤΕΛΕΥΤΑΙΑΣ αξιολόγησης κάθε πελάτη) με ημερομηνία στο [start, end] — για το Ημερολόγιο.
    Μόνο τρέχοντες πελάτες: πρώην πελάτης δεν επανεξετάζεται."""
    sql = ("SELECT a.afm, a.next_review, a.final_category, b.name FROM aml_assessments a JOIN businesses b ON b.afm=a.afm "
           "WHERE a.id = (SELECT x.id FROM aml_assessments x WHERE x.afm=a.afm ORDER BY x.assessed_on DESC, x.id DESC LIMIT 1) "
           "AND a.next_review BETWEEN ? AND ?")
    args: list[Any] = [start.isoformat(), end.isoformat()]
    if afm:
        sql += " AND a.afm=?"
        args.append(afm)
    return [dict(r) for r in conn.execute(sql + " ORDER BY a.next_review", args)]


# ------------------------------------------------------------------ φάκελος γραφείου (αυτοδιάγνωση/βήματα/έγγραφα)

def office_items(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    return {r["item"]: {"state": r["state"], "note": r["note"], "updated_at": r["updated_at"]}
            for r in conn.execute("SELECT * FROM aml_office")}


def set_office_item(conn: sqlite3.Connection, item: str, state: str, note: Optional[str] = None) -> None:
    if note is None:
        row = conn.execute("SELECT note FROM aml_office WHERE item=?", (item,)).fetchone()
        note = row["note"] if row else ""
    conn.execute("INSERT INTO aml_office(item, state, note, updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(item) DO UPDATE SET state=excluded.state, note=excluded.note, updated_at=excluded.updated_at",
                 (item, state, note, db.utcnow()))


# ------------------------------------------------------------------ μητρώα

def add_register(conn: sqlite3.Connection, kind: str, event_date: str, title: str, *, afm: str = "", details: str = "",
                 outcome: str = "") -> int:
    cur = conn.execute("INSERT INTO aml_register(kind, event_date, afm, title, details, outcome, created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (kind, event_date, afm, title.strip(), details.strip(), outcome.strip(),
                                                  db.utcnow()))
    return int(cur.lastrowid)


def list_register(conn: sqlite3.Connection, kind: str = "") -> list[dict[str, Any]]:
    if kind:
        rows = conn.execute("SELECT * FROM aml_register WHERE kind=? ORDER BY event_date DESC, id DESC", (kind,))
    else:
        rows = conn.execute("SELECT * FROM aml_register ORDER BY event_date DESC, id DESC")
    return [dict(r) for r in rows]


def delete_register(conn: sqlite3.Connection, entry_id: int) -> None:
    conn.execute("DELETE FROM aml_register WHERE id=?", (entry_id,))
