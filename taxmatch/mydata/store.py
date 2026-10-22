"""Λήψη ενός μήνα από το myDATA και αποθήκευση (`mydata_docs`, `mydata_sync`)."""
from __future__ import annotations

import calendar
import json
import sqlite3
from typing import Any, Callable, Optional

from .. import db
from . import credentials
from .client import AuthError, MydataClient
from .parse import parse_page


def month_range(ym: str) -> tuple[str, str]:
    """'2026-03' -> ('01/03/2026', '31/03/2026') όπως τα θέλει το myDATA."""
    y, m = int(ym[:4]), int(ym[5:7])
    return f"01/{m:02d}/{y}", f"{calendar.monthrange(y, m)[1]:02d}/{m:02d}/{y}"


def previous_months(ym: str, n: int) -> list[str]:
    y, m = int(ym[:4]), int(ym[5:7])
    out = []
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
    return out


def sync_month(conn: sqlite3.Connection, afm: str, ym: str,
               client_factory: Callable[[str, str], MydataClient] = MydataClient) -> dict[str, Any]:
    """Κατεβάζει ΟΛΑ τα παραστατικά του μήνα (λήψεις + όσα διαβίβασε ο ίδιος) και τα αποθηκεύει.
    Επιστρέφει {ok, reason, n}. ΜΟΝΟ ανάγνωση στο myDATA."""
    creds = credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί myDATA για τον πελάτη.", "n": 0}
    date_from, date_to = month_range(ym)
    client = client_factory(*creds)
    docs: list[dict[str, Any]] = []
    try:
        for incoming in (True, False):
            for page in client.fetch_pages(incoming, date_from, date_to):
                docs += parse_page(page, "incoming" if incoming else "outgoing")
    except AuthError:
        credentials.record_check(conn, afm, "invalid")
        return {"ok": False, "reason": "Το myDATA απέρριψε τους κωδικούς (όνομα χρήστη / κλειδί API).", "n": 0}
    finally:
        client.close()
    credentials.record_check(conn, afm, "ok")
    now = db.utcnow()
    conn.execute("BEGIN")
    try:
        for d in docs:
            if not d["mark"]:
                continue
            conn.execute(
                "INSERT INTO mydata_docs(afm, mark, direction, invoice_type, series, aa, issue_date, issuer_vat, issuer_name, "
                "issuer_country, counter_vat, counter_name, counter_country, net, vat, total, withheld, withheld_json, "
                "cancelled, url, provider, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(afm, mark) DO UPDATE SET invoice_type=excluded.invoice_type, issue_date=excluded.issue_date, "
                "net=excluded.net, vat=excluded.vat, total=excluded.total, withheld=excluded.withheld, "
                "withheld_json=excluded.withheld_json, cancelled=excluded.cancelled, url=excluded.url, "
                "provider=excluded.provider, fetched_at=excluded.fetched_at",
                (afm, d["mark"], d["direction"], d["invoice_type"], d["series"], d["aa"], d["issue_date"], d["issuer_vat"],
                 d["issuer_name"], d["issuer_country"], d["counter_vat"], d["counter_name"], d["counter_country"],
                 d["net"], d["vat"], d["total"], d["withheld"], json.dumps(d["withheld_lines"]), int(d["cancelled"]),
                 d["url"], d["provider"], now))
        n = len({d["mark"] for d in docs if d["mark"]})
        conn.execute("INSERT INTO mydata_sync(afm, ym, synced_at, n_docs) VALUES (?,?,?,?) ON CONFLICT(afm, ym) DO UPDATE "
                     "SET synced_at=excluded.synced_at, n_docs=excluded.n_docs", (afm, ym, now, n))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "reason": "", "n": n}


def month_docs(conn: sqlite3.Connection, afm: str, ym: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM mydata_docs WHERE afm=? AND substr(issue_date,1,7)=? ORDER BY issue_date, mark",
                        (afm, ym)).fetchall()
    return [dict(r) for r in rows]


def synced_months(conn: sqlite3.Connection, afm: Optional[str] = None) -> dict[str, set[str]]:
    sql, args = "SELECT afm, ym FROM mydata_sync", ()
    if afm:
        sql, args = sql + " WHERE afm=?", (afm,)
    out: dict[str, set[str]] = {}
    for r in conn.execute(sql, args):
        out.setdefault(r["afm"], set()).add(r["ym"])
    return out


def check_credentials(conn: sqlite3.Connection, afm: str,
                      client_factory: Callable[[str, str], MydataClient] = MydataClient) -> tuple[bool, str]:
    creds = credentials.get(conn, afm)
    if not creds:
        return False, "Δεν έχουν οριστεί κωδικοί myDATA."
    from datetime import date
    ym = date.today().strftime("%Y-%m")
    client = client_factory(*creds)
    try:
        client.check(*month_range(ym))
    except AuthError:
        credentials.record_check(conn, afm, "invalid")
        return False, "Το myDATA απέρριψε τους κωδικούς (όνομα χρήστη / κλειδί API)."
    except Exception as exc:                      # noqa: BLE001 — δίκτυο/ΑΑΔΕ: όχι «λάθος κωδικοί»
        return False, f"Δεν ήταν δυνατός ο έλεγχος: {str(exc)[:150]}"
    finally:
        client.close()
    credentials.record_check(conn, afm, "ok")
    return True, "Οι κωδικοί myDATA έγιναν δεκτοί."
