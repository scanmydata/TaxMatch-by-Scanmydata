"""Υποθέσεις δέουσας επιμέλειας (πίνακας `aml_cases`, db.py v7) — δύο διαδικασίες που ΔΕΝ μπλέκονται:

* `suspicion`: εσωτερική αναφορά ύποπτης συναλλαγής/δραστηριότητας πελάτη προς τον υπεύθυνο συμμόρφωσης → γραπτή
  απόφαση αν θα αναφερθεί στην Αρχή Καταπολέμησης της Νομιμοποίησης Εσόδων (άρθρο 22). Και η απόφαση «δεν
  αναφέρθηκε» τηρείται με αιτιολογία (άρθρο 30 παρ. 1γ). Ποτέ γνωστοποίηση στον πελάτη (άρθρο 27).
* `whistle`: εσωτερική καταγγελία παράβασης ΑΠΟ το ίδιο το γραφείο (ν. 4990/2022, άρθρο 35 παρ. 3β ν. 4557/2018):
  βεβαίωση παραλαβής εντός 7 εργάσιμων, ενημέρωση του καταγγέλλοντος εντός 3 μηνών.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional

from .. import db, obligations
from .content import (
    CASE_KIND_LABEL, CASE_STATUS_LABEL, RED_FLAG_LABEL, SUSPICION_DECISION_LABEL, WHISTLE_ACK_WORKDAYS,
    WHISTLE_FEEDBACK_MONTHS, WHISTLE_OUTCOME_LABEL,
)
from .store import add_months

FIELDS = ("kind", "received_on", "channel", "afm", "category", "description", "reporter", "anonymous", "handler",
          "status", "ack_on", "feedback_on", "tx_description", "tx_amount", "tx_date", "red_flags", "decision",
          "decision_reason", "decided_on", "authority_ref", "actions", "closed_on")


def add_workdays(d: date, n: int) -> date:
    """n εργάσιμες μετά το d (ελληνικές αργίες, όπως στο ημερολόγιο υποχρεώσεων)."""
    while n > 0:
        d += timedelta(days=1)
        if obligations.is_business_day(d):
            n -= 1
    return d


def deadlines(case: dict[str, Any]) -> dict[str, str]:
    """Προθεσμίες ν. 4990/2022 για τις εσωτερικές καταγγελίες ({} για τις άλλες)."""
    if case.get("kind") != "whistle":
        return {}
    try:
        received = date.fromisoformat(case["received_on"])
    except (KeyError, ValueError):
        return {}
    return {"ack_due": add_workdays(received, WHISTLE_ACK_WORKDAYS).isoformat(),
            "feedback_due": add_months(received, WHISTLE_FEEDBACK_MONTHS).isoformat()}


def validate(case: dict[str, Any]) -> list[str]:
    """Λάθη που εμποδίζουν το κλείσιμο/την αποθήκευση μιας υπόθεσης."""
    errors = []
    if case.get("kind") not in CASE_KIND_LABEL:
        errors.append("Άγνωστο είδος υπόθεσης.")
    if not case.get("description", "").strip():
        errors.append("Χρειάζεται περιγραφή.")
    if case.get("kind") == "suspicion":
        if not case.get("afm"):
            errors.append("Η αναφορά ύποπτης συναλλαγής αφορά πελάτη — επιλέξτε ΑΦΜ.")
        if case.get("decision") in ("no_report", "exempt") and not case.get("decision_reason", "").strip():
            errors.append("Η απόφαση μη αναφοράς χρειάζεται γραπτή αιτιολογία (άρθρο 30 παρ. 1γ).")
        if case.get("status") == "closed" and not case.get("decision"):
            errors.append("Υπόθεση ύποπτης συναλλαγής δεν κλείνει χωρίς απόφαση για αναφορά στην Αρχή.")
    if case.get("kind") == "whistle" and case.get("status") == "closed" and not case.get("decision"):
        errors.append("Επιλέξτε αποτέλεσμα πριν κλείσετε την καταγγελία.")
    return errors


def save(conn: sqlite3.Connection, case: dict[str, Any], case_id: Optional[int] = None) -> int:
    errors = validate(case)
    if errors:
        raise ValueError(" ".join(errors))
    row = {f: case.get(f, "") for f in FIELDS}
    row["anonymous"] = 1 if case.get("anonymous") else 0
    if row["anonymous"]:
        row["reporter"] = ""                             # ανωνυμία: ούτε κατά λάθος όνομα στη βάση
    row["red_flags_json"] = json.dumps([f for f in case.get("red_flags", []) if f in RED_FLAG_LABEL])
    del row["red_flags"]
    if row["status"] not in CASE_STATUS_LABEL:
        row["status"] = "received"
    if row["status"] == "closed" and not row["closed_on"]:
        row["closed_on"] = date.today().isoformat()
    if row["decision"] and not row["decided_on"]:
        row["decided_on"] = date.today().isoformat()
    name = ""
    if row["afm"]:
        r = conn.execute("SELECT name FROM businesses WHERE afm=?", (row["afm"],)).fetchone()
        name = r["name"] if r else case.get("client_name", "")
    row["client_name"] = name
    now = db.utcnow()
    if case_id:
        sets = ", ".join(f"{k}=?" for k in row)
        conn.execute(f"UPDATE aml_cases SET {sets}, updated_at=? WHERE id=?", (*row.values(), now, case_id))
        return case_id
    cols = ", ".join(row)
    cur = conn.execute(f"INSERT INTO aml_cases({cols}, created_at, updated_at) VALUES ({', '.join('?' * len(row))}, ?, ?)",
                       (*row.values(), now, now))
    return int(cur.lastrowid)


def _decode(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    try:
        d["red_flags"] = json.loads(d.pop("red_flags_json") or "[]")
    except ValueError:
        d["red_flags"] = []
    d.update(deadlines(d))
    return d


def get(conn: sqlite3.Connection, case_id: int) -> Optional[dict[str, Any]]:
    r = conn.execute("SELECT * FROM aml_cases WHERE id=?", (case_id,)).fetchone()
    return _decode(r) if r else None


def list_cases(conn: sqlite3.Connection, kind: str = "", afm: str = "") -> list[dict[str, Any]]:
    sql, args = "SELECT * FROM aml_cases WHERE 1=1", []
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    if afm:
        sql += " AND afm=?"
        args.append(afm)
    return [_decode(r) for r in conn.execute(sql + " ORDER BY received_on DESC, id DESC", args)]


def attention(conn: sqlite3.Connection, today: Optional[date] = None) -> list[dict[str, Any]]:
    """Ανοιχτές υποθέσεις που θέλουν ενέργεια τώρα: καταγγελία χωρίς βεβαίωση παραλαβής ή ενημέρωση μετά την
    προθεσμία, ή ύποπτη συναλλαγή χωρίς απόφαση για πάνω από 30 ημέρες (η αναφορά πρέπει να γίνεται «αμελλητί»)."""
    today = today or date.today()
    out = []
    for c in list_cases(conn):
        if c["status"] == "closed":
            continue
        reason = ""
        if c["kind"] == "whistle":
            if not c["ack_on"] and c.get("ack_due") and c["ack_due"] < today.isoformat():
                reason = f"εκπρόθεσμη βεβαίωση παραλαβής (έως {c['ack_due']})"
            elif not c["feedback_on"] and c.get("feedback_due") and c["feedback_due"] < today.isoformat():
                reason = f"εκπρόθεσμη ενημέρωση καταγγέλλοντος (έως {c['feedback_due']})"
        elif not c["decision"]:
            try:
                age = (today - date.fromisoformat(c["received_on"])).days
            except ValueError:
                age = 0
            if age > 30:
                reason = f"χωρίς απόφαση για αναφορά εδώ και {age} ημέρες"
        if reason:
            out.append({**c, "reason": reason})
    return out


def outcome_label(case: dict[str, Any]) -> str:
    labels = SUSPICION_DECISION_LABEL if case["kind"] == "suspicion" else WHISTLE_OUTCOME_LABEL
    return labels.get(case.get("decision", ""), "")
