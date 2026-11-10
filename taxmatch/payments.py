"""Έλεγχος πληρωμών ΑΑΔΕ: αντιστοίχιση των ΑΝΟΙΧΤΩΝ οφειλών (και των Ταυτοτήτων Οφειλής τους) με τις ΠΛΗΡΩΜΕΣ της
περιόδου — «αν έχει ξεφύγει κάτι» (ρητό αίτημα χρήστη 2026-11-10). Port του runner `configs/aade-payment-match.js`
(μεθοδολογία `out/methodologies/AADE_payment_match.md`, επαληθευμένη ζωντανά εκεί), πάνω στο login του `lookup_aade`.

* **Πληρωμές** — `displayActualPaymentInfo.htm` («Στοιχεία Πληρωμών» της Προσωποποιημένης Πληροφόρησης): η σελίδα θέλει
  ΑΝΑΖΗΤΗΣΗ (POST με εύρος ημερομηνιών και ΟΛΟΥΣ τους τύπους ταυτότητας), αλλιώς «Δεν βρέθηκαν εγγραφές».
* **Επιστροφές / υπερεισπράξεις** — `displayReturnInfo.htm`: οι γραμμές με Πηγή «Υπερεισπράξεις τραπεζ. πληρωμών» είναι
  το ΑΥΘΕΝΤΙΚΟ σήμα λάθους/διπλής πληρωμής (ποσά που καταβλήθηκαν αλλά δεν πιστώθηκαν σε οφειλή). ΟΧΙ αναζήτηση της
  λέξης «υπερείσπραξη» στη σελίδα πληρωμών — υπάρχει πάντα στο στατικό κείμενο.
* **Κλειδί** = η Ταυτότητα Οφειλής (30 ψηφία: ΑΦΜ + 9 + 12): στις οφειλές τυπώνεται με κενά, στις πληρωμές χωρίς.

Τι απαντά: ποιες ανοιχτές οφειλές είναι ΑΝΕΞΟΦΛΗΤΕΣ, ποιες έχουν ήδη πληρωμή (ίσως «σε επεξεργασία» — να μην πληρωθούν
δύο φορές), ίδια ταυτότητα σε πολλές πληρωμές (δόσεις ή διπλή πληρωμή), πληρωμές πάνω από το οφειλόμενο.
Οι οφειλές είναι οι τρέχουσες ανοιχτές· οι πληρωμές περιορίζονται στο εύρος (προεπιλογή: 12 μήνες). ΜΟΝΟ ανάγνωση.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, timedelta
from typing import Any, Optional
from urllib.parse import urljoin

from . import db
from .business_profiles import lookup_aade
from .debts import BASE, _cells, amount

log = logging.getLogger(__name__)
AADE = lookup_aade.AADE
PAYMENTS_PAGE = BASE + "displayActualPaymentInfo.htm"
RETURNS_PAGE = BASE + "displayReturnInfo.htm"
MONTHS_BACK = 12
#: Όλοι οι τύποι ταυτότητας (checkboxes της φόρμας)· `docKind` = συνένωση A..J όπως η `setDocKind()` της σελίδας.
PAY_TYPES = {"docA": "0", "docB": "9", "docG": "7", "docC": "1", "docD": "2", "docH": "8", "docE": "3", "docF": "4",
             "docI": "D", "docJ": "R", "docS": "S", "docCentralA": "A", "docP": "P"}
DOC_KIND = "09123478DR"
STATUS_LABEL = {"unpaid": "Ανεξόφλητη", "with_payment": "Με πληρωμή (ίσως σε επεξεργασία)", "unknown": "Χωρίς ταυτότητα"}


def norm_code(code: Any) -> str:
    return re.sub(r"\s+", "", str(code or ""))


def _dmy(d: date) -> str:
    return d.strftime("%d/%m/%Y")


def _iso(dmy: str) -> str:
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", dmy or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else ""


def default_range(today: Optional[date] = None) -> tuple[date, date]:
    today = today or date.today()
    return today - timedelta(days=MONTHS_BACK * 31), today


# ------------------------------------------------------------------ ανάγνωση σελίδων
def parse_payments(page: str) -> list[dict[str, Any]]:
    """Γραμμές του πίνακα πληρωμών: Δ.Ο.Υ. | Ημ/νία Πληρωμής | Ποσό | Είδος Ταυτότητας | Ταυτότητα | Εκτύπωση."""
    out = []
    for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", page or "", re.S | re.I):
        if not re.search(r"doViewCollectionNoticePdf\(|doViewCCPaymentPdf\(", tr):
            continue
        c = _cells(tr)
        if len(c) < 5:
            continue
        out.append({"doy": c[0], "date": c[1], "pay_date": _iso(c[1]), "amount": amount(c[2]), "type": c[3],
                    "code": c[4].strip(), "code_norm": norm_code(c[4])})
    return out


def fetch_payments(http: Any, date_from: date, date_to: date) -> list[dict[str, Any]]:
    url = urljoin(AADE, PAYMENTS_PAGE)
    http.follow("GET", url)                              # φρέσκια φόρμα (συνεδρία)
    body = {"docKind": DOC_KIND, "cardTo": "", "diasCC": "", "paymentDate": "", "paymentAmount": "", "doy": "",
            "docType": "", "year": "", "paymentAA": "", "paymentAA2": "", "payTo_": "", "payTo": "",
            "payDateFrom": _dmy(date_from), "payDateTo": _dmy(date_to), **PAY_TYPES}
    return parse_payments(http.follow("POST", url, body)["text"])


def parse_returns(page: str) -> dict[str, Any]:
    """{total, rows, overcollections}: γραμμές επιστροφών (με ημερομηνία ΚΑΙ ποσό)· υπερεισπράξεις = Πηγή «Υπερεισπράξεις…»."""
    from .debts import strip_tags
    total = re.search(r"Συνολικό Ποσό Επιστροφής.{0,120}?(-?[\d.]+,\d{2})", strip_tags(page or ""), re.S)
    rows = []
    for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", page or "", re.S | re.I):
        cells = [c for c in _cells(tr) if c]
        joined = " ".join(cells)
        if (len(cells) >= 3 and re.search(r"\d{2}/\d{2}/\d{4}", joined) and re.search(r"[\d.]+,\d{2}", joined)
                and not re.search(r"ΕΝΗΜΕΡΩΣΗ|Κριτήρια|Συνολικό Ποσό", joined)):
            rows.append(cells)
    return {"total": amount(total.group(1)) if total else 0.0, "rows": rows,
            "overcollections": [r for r in rows if re.search(r"Υπερεισπράξ", " ".join(r), re.I)]}


def fetch_returns(http: Any) -> dict[str, Any]:
    try:
        return parse_returns(http.follow("GET", urljoin(AADE, RETURNS_PAGE))["text"])
    except Exception as exc:                            # noqa: BLE001 — οι επιστροφές δεν ρίχνουν τον έλεγχο
        log.warning("returns: %s", exc)
        return {"total": 0.0, "rows": [], "overcollections": []}


# ------------------------------------------------------------------ αντιστοίχιση
def reconcile(debts: list[dict[str, Any]], payments: list[dict[str, Any]],
              returns: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """`debts`: [{ref, kind, title, total, code}] (οι ανοιχτές οφειλές/ρυθμίσεις ΑΑΔΕ) · `payments`: `parse_payments`.
    → {debts: {ref: {status, paid, payments}}, payments (+matched, repeated), anomalies, summary}."""
    returns = returns or {"total": 0.0, "rows": [], "overcollections": []}
    by_code: dict[str, list[dict[str, Any]]] = {}
    for p in payments:
        if p["code_norm"]:
            by_code.setdefault(p["code_norm"], []).append(p)
    open_codes = {norm_code(d.get("code")): d for d in debts if norm_code(d.get("code"))}
    debt_out: dict[str, dict[str, Any]] = {}
    for d in debts:
        code = norm_code(d.get("code"))
        matched = by_code.get(code, []) if code else []
        paid = round(sum(p["amount"] for p in matched), 2)
        debt_out[f"{d['kind']}|{d['ref']}"] = {
            "status": "unknown" if not code else "with_payment" if matched else "unpaid", "paid": paid,
            "payments": [{"date": p["date"], "amount": p["amount"], "type": p["type"]} for p in matched]}
    pay_out = [{**p, "matched": p["code_norm"] in open_codes, "repeated": len(by_code.get(p["code_norm"], [])) > 1,
                "debt_title": (open_codes.get(p["code_norm"]) or {}).get("title", "")} for p in payments]
    repeated = [{"code": ps[0]["code"], "count": len(ps), "title": (open_codes.get(code) or {}).get("title", ""),
                 "instalments": (open_codes.get(code) or {}).get("instalments") or 0,
                 "payments": [{"date": p["date"], "amount": p["amount"]} for p in ps],
                 "same_amount": len({p["amount"] for p in ps}) < len(ps)}
                for code, ps in by_code.items() if len(ps) > 1]
    overpaid = [{"title": d["title"], "code": d.get("code", ""), "owed": d["total"],
                 "paid": debt_out[f"{d['kind']}|{d['ref']}"]["paid"]}
                for d in debts if d.get("total", 0) > 0 and debt_out[f"{d['kind']}|{d['ref']}"]["paid"] > d["total"] + 0.01
                and d["kind"] != "arrangement"]           # ρύθμιση: οι δόσεις αθροίζουν νόμιμα πάνω από το υπόλοιπο
    unpaid = [d for d in debts if debt_out[f"{d['kind']}|{d['ref']}"]["status"] == "unpaid"]
    summary = {"open": len(debts), "unpaid": len(unpaid), "unpaid_total": round(sum(d.get("total") or 0 for d in unpaid), 2),
               "with_payment": sum(1 for v in debt_out.values() if v["status"] == "with_payment"),
               "payments": len(payments), "paid_total": round(sum(p["amount"] for p in payments), 2),
               "payments_open": sum(1 for p in pay_out if p["matched"]), "refund_total": returns["total"],
               "overcollections": len(returns["overcollections"]), "repeated": len(repeated), "overpaid": len(overpaid)}
    return {"debts": debt_out, "payments": pay_out, "summary": summary,
            "anomalies": {"overcollections": returns["overcollections"], "repeated": repeated, "overpaid": overpaid}}


# ------------------------------------------------------------------ αποθήκευση / προβολή
def check(http: Any, debts: list[dict[str, Any]], today: Optional[date] = None) -> dict[str, Any]:
    """Με ήδη συνδεδεμένο `http` (η ίδια συνεδρία της λήψης οφειλών): πληρωμές 12 μηνών + επιστροφές + αντιστοίχιση."""
    date_from, date_to = default_range(today)
    result = reconcile(debts, fetch_payments(http, date_from, date_to), fetch_returns(http))
    result["date_from"], result["date_to"] = date_from.isoformat(), date_to.isoformat()
    return result


def store(conn: sqlite3.Connection, afm: str, result: dict[str, Any]) -> None:
    now = db.utcnow()
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM aade_payments WHERE afm=?", (afm,))
        for i, p in enumerate(result["payments"]):
            conn.execute("INSERT INTO aade_payments(afm, seq, pay_date, amount, doy, type, code, matched, repeated, "
                         "debt_title) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (afm, i, p["pay_date"], p["amount"], p["doy"], p["type"], p["code"], int(p["matched"]),
                          int(p["repeated"]), p.get("debt_title", "")))
        conn.execute("INSERT INTO payment_sync(afm, synced_at, date_from, date_to, status, message, result_json) "
                     "VALUES (?,?,?,?,?,?,?) ON CONFLICT(afm) DO UPDATE SET synced_at=excluded.synced_at, "
                     "date_from=excluded.date_from, date_to=excluded.date_to, status=excluded.status, "
                     "message=excluded.message, result_json=excluded.result_json",
                     (afm, now, result.get("date_from", ""), result.get("date_to", ""), "ok", "",
                      json.dumps({k: result[k] for k in ("debts", "summary", "anomalies")}, ensure_ascii=False)))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def fail(conn: sqlite3.Connection, afm: str, message: str) -> None:
    conn.execute("INSERT INTO payment_sync(afm, synced_at, status, message) VALUES (?,?,?,?) ON CONFLICT(afm) DO UPDATE "
                 "SET synced_at=excluded.synced_at, status=excluded.status, message=excluded.message",
                 (afm, db.utcnow(), "error", message[:200]))


def info(conn: sqlite3.Connection, afm: str) -> Optional[dict[str, Any]]:
    """{synced_at, date_from, date_to, status, message, debts, summary, anomalies} ή None αν δεν έχει γίνει έλεγχος."""
    row = conn.execute("SELECT * FROM payment_sync WHERE afm=?", (afm,)).fetchone()
    if not row:
        return None
    data = json.loads(row["result_json"] or "{}")
    return {**{k: row[k] for k in ("synced_at", "date_from", "date_to", "status", "message")},
            "debts": data.get("debts") or {}, "summary": data.get("summary") or {}, "anomalies": data.get("anomalies") or {}}


def client_payments(conn: sqlite3.Connection, afm: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM aade_payments WHERE afm=? ORDER BY pay_date DESC, seq", (afm,))]


def debt_status(pay_info: Optional[dict[str, Any]], d: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Η κατάσταση πληρωμής μιας οφειλής/ρύθμισης ΑΑΔΕ από τον τελευταίο έλεγχο (None = δεν ελέγχθηκε)."""
    if not pay_info or pay_info.get("status") != "ok" or d.get("source", "aade") != "aade":
        return None
    return (pay_info.get("debts") or {}).get(f"{d['kind']}|{d['ref']}")


def money(v: float) -> str:
    return f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def status_text(st: Optional[dict[str, Any]]) -> str:
    if not st:
        return ""
    if st["status"] == "with_payment":
        last = st["payments"][0] if st.get("payments") else None
        return (f"Πληρωμή {money(st['paid'])}" + (f" ({len(st['payments'])})" if len(st["payments"]) > 1 else "")
                + (f" · {last['date']}" if last else ""))
    return STATUS_LABEL.get(st["status"], "")


def anomaly_lines(pay_info: Optional[dict[str, Any]]) -> list[str]:
    """Σύντομες προειδοποιήσεις για την καρτέλα/ειδοποιήσεις: υπερεισπράξεις, πληρωμή πάνω από το οφειλόμενο, ίδια
    ταυτότητα με ίδιο ποσό πολλές φορές (οι δόσεις με διαφορετικά ποσά δεν αναφέρονται)."""
    if not pay_info or pay_info.get("status") != "ok":
        return []
    a, out = pay_info.get("anomalies") or {}, []
    if a.get("overcollections"):
        out.append(f"{len(a['overcollections'])} υπερείσπραξη(-εις) στις Επιστροφές της ΑΑΔΕ — ποσό που πληρώθηκε αλλά "
                   "δεν πιστώθηκε σε οφειλή (λάθος ταυτότητα ή διπλή πληρωμή)")
    for o in a.get("overpaid") or []:
        out.append(f"«{o['title']}»: πληρώθηκαν {money(o['paid'])} για οφειλή {money(o['owed'])}")
    for r in a.get("repeated") or []:
        if r.get("same_amount") and not r.get("title"):
            continue                                     # εξοφλημένη οφειλή σε ίσες δόσεις — δεν είναι εκκρεμότητα
        if r.get("instalments"):
            continue                                     # οφειλή σε δόσεις: ίσα ποσά με την ίδια ταυτότητα είναι οι δόσεις
        if r.get("same_amount"):
            out.append(f"«{r['title']}»: η ίδια ταυτότητα πληρώθηκε {r['count']} φορές με ίδιο ποσό — δόσεις ή διπλή "
                       "πληρωμή (έλεγχος)")
    return out


def clients_with_anomalies(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """ΑΦΜ → προειδοποιήσεις (υπερεισπράξεις / πληρωμή πάνω από το οφειλόμενο) — για το κέντρο ειδοποιήσεων."""
    out: dict[str, list[str]] = {}
    for r in conn.execute("SELECT afm, result_json FROM payment_sync WHERE status='ok' AND result_json LIKE '%\"over%'"):
        a = (json.loads(r["result_json"] or "{}").get("anomalies") or {})
        lines = anomaly_lines({"status": "ok", "anomalies": {"overcollections": a.get("overcollections"),
                                                             "overpaid": a.get("overpaid")}})
        if lines:
            out[r["afm"]] = lines
    return out
