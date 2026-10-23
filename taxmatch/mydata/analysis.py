"""Υποχρεώσεις που ΠΡΟΚΥΠΤΟΥΝ από τα παραστατικά myDATA του πελάτη (ρητό αίτημα χρήστη, 2026-10-22).

1. **Παρακρατούμενος φόρος** (άρθρα 62–64 ΚΦΕ): σε παραστατικά που ΕΛΑΒΕ ο πελάτης με παρακράτηση (π.χ. 20% αμοιβές
   επιχειρηματικής δραστηριότητας, 3% εργολάβοι τεχνικών έργων), ο πελάτης-πληρωτής οφείλει να υποβάλει δήλωση
   και να αποδώσει τον φόρο **έως το τέλος του δεύτερου μήνα από την καταβολή** (ΚΦΕ άρθρο 60 / Οδηγός taxheaven).
   Το myDATA δεν δίνει ημερομηνία πληρωμής → χρησιμοποιείται η ημερομηνία έκδοσης (όπως ζήτησε ο χρήστης) —
   σε πίστωση/μεταγενέστερη εξόφληση η πραγματική προθεσμία είναι αργότερα.
2. **VIES** (ανακεφαλαιωτικός πίνακας): στην Ελλάδα δηλώνονται ΚΑΙ οι ενδοκοινοτικές αποκτήσεις/λήψεις υπηρεσιών,
   όχι μόνο οι παραδόσεις — υπόχρεος σε κάθε μήνα με 1.2 / 2.2 (εκδοθέντα) ή 14.1 / 14.3 (καταχωρήσεις λήπτη).
3. **Intrastat**: ΜΟΝΟ αγαθά (1.2 = αποστολές, 14.1 = αφίξεις), ξεχωριστά ανά ρεύμα, με τα ετήσια κατώφλια της
   ΕΛΣΤΑΤ: υπόχρεος αν τα ξεπέρασε το προηγούμενο έτος, ή από τον μήνα που τα ξεπερνά σωρευτικά μέσα στο έτος· μετά
   υποβάλλει κάθε μήνα (και μηδενική) ως το τέλος του έτους. Μόνο υπηρεσίες ⇒ μόνο VIES.

Απάντηση `None` = δεν ξέρουμε (λείπουν μήνες από το myDATA) — ποτέ εφεύρεση.
"""
from __future__ import annotations

import calendar
import json
import sqlite3
from datetime import date
from typing import Any, Optional

from .parse import WITHHELD_CATEGORIES
from .store import synced_months

#: Κατηγορίες που αποδίδει ο ΠΛΗΡΩΤΗΣ (ο πελάτης, όταν τις βρίσκει σε παραστατικά που έλαβε). Οι 8/9/10 είναι
#: προκαταβλητέος φόρος που πληρώνει ο ίδιος ο επαγγελματίας· οι 11–15 μισθοδοσία (όχι από τιμολόγια).
PAYER_CATEGORIES = ("1", "2", "3", "4", "5", "6", "7", "16", "17")
#: Σε ποια δήλωση αποδίδεται κάθε κατηγορία (κλειδιά του easycheck όπου υπάρχει έλεγχος υποβολής).
DECLARATION = {"3": ("ΦΜΥ_ΕΠΙΧ", "Δήλωση παρακράτησης φόρου αμοιβών επιχειρηματικής δραστηριότητας"),
               "4": ("ΕΡΓΟΛΑΒΩΝ", "Δήλωση παρακράτησης φόρου εργολάβων (3%)"),
               "1": ("ΦΜΥ_ΤΟΚΟΙ", "Δήλωση παρακράτησης φόρου τόκων"),
               "2": ("ΦΜΥ_ΔΙΚΑΙΩΜΑΤΑ", "Δήλωση παρακράτησης φόρου δικαιωμάτων")}

GOODS_OUT, SERVICES_OUT, GOODS_IN, SERVICES_IN = "1.2", "2.2", "14.1", "14.3"
#: Κατώφλια Intrastat ανά έτος (αφίξεις, αποστολές) — ΕΛΣΤΑΤ. 2026: 250.000 / 90.000 (από τον χρήστη, 2026-10-22).
INTRASTAT_THRESHOLDS: dict[int, tuple[float, float]] = {2026: (250_000.0, 90_000.0)}


def thresholds(year: int) -> tuple[float, float]:
    known = sorted(INTRASTAT_THRESHOLDS)
    return INTRASTAT_THRESHOLDS.get(year) or INTRASTAT_THRESHOLDS[max([y for y in known if y <= year] or known[:1])]


def _sign(invoice_type: str) -> int:
    return -1 if invoice_type.startswith("5.") else 1          # πιστωτικά μειώνουν


def _add_months(ym: str, n: int) -> tuple[int, int]:
    y, m = int(ym[:4]), int(ym[5:7]) - 1 + n
    return y + m // 12, m % 12 + 1


def withholding_due(ym: str) -> date:
    """Τέλος του δεύτερου μήνα μετά τον μήνα έκδοσης (τελευταία εργάσιμη, όπως ο ΦΠΑ στο obligations.py)."""
    from ..obligations import last_business_day
    y, m = _add_months(ym, 2)
    return last_business_day(y, m)


def withholding(conn: sqlite3.Connection, afm: str, ym: str) -> list[dict[str, Any]]:
    """Παρακρατούμενοι φόροι που οφείλει να αποδώσει ο πελάτης για παραστατικά που ΕΛΑΒΕ τον μήνα `ym`."""
    by_cat: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT invoice_type, net, withheld_json, issuer_name FROM mydata_docs WHERE afm=? AND "
                          "direction='incoming' AND cancelled=0 AND substr(issue_date,1,7)=?", (afm, ym)):
        lines = json.loads(r["withheld_json"] or "{}")
        for cat, amount in lines.items():
            if cat not in PAYER_CATEGORIES:
                continue
            e = by_cat.setdefault(cat, {"category": cat, "label": WITHHELD_CATEGORIES.get(cat, (cat, None))[0],
                                        "rate": WITHHELD_CATEGORIES.get(cat, ("", None))[1], "amount": 0.0, "base": 0.0,
                                        "n_docs": 0, "declaration": DECLARATION.get(cat, ("", "Δήλωση παρακρατούμενων φόρων"))[1],
                                        "easycheck": DECLARATION.get(cat, ("", ""))[0]})
            s = _sign(r["invoice_type"])
            e["amount"] = round(e["amount"] + s * amount, 2)
            e["base"] = round(e["base"] + s * r["net"], 2)
            e["n_docs"] += 1
    due = withholding_due(ym)
    return [{**e, "due": due.isoformat()} for e in sorted(by_cat.values(), key=lambda x: x["category"]) if e["amount"]]


def ic_flows(conn: sqlite3.Connection, afm: str, ym: str) -> dict[str, float]:
    """Ενδοκοινοτικές ροές του μήνα (καθαρή αξία): goods_out (1.2), services_out (2.2), goods_in (14.1), services_in (14.3)."""
    out = {"goods_out": 0.0, "services_out": 0.0, "goods_in": 0.0, "services_in": 0.0}
    key = {GOODS_OUT: "goods_out", SERVICES_OUT: "services_out", GOODS_IN: "goods_in", SERVICES_IN: "services_in"}
    for r in conn.execute("SELECT invoice_type, net FROM mydata_docs WHERE afm=? AND cancelled=0 AND "
                          "substr(issue_date,1,7)=? AND invoice_type IN (?,?,?,?)", (afm, ym, *key)):
        out[key[r["invoice_type"]]] = round(out[key[r["invoice_type"]]] + r["net"], 2)
    return out


def vies(conn: sqlite3.Connection, afm: str, ym: str, synced: Optional[set[str]] = None) -> Optional[bool]:
    synced = synced if synced is not None else synced_months(conn, afm).get(afm, set())
    if ym not in synced:
        return None
    return any(v for v in ic_flows(conn, afm, ym).values())


def intrastat(conn: sqlite3.Connection, afm: str, ym: str, synced: Optional[set[str]] = None) -> dict[str, Any]:
    """{'arrivals': True/False/None, 'dispatches': …, 'any': …, 'reason': …} για τον μήνα αναφοράς `ym`."""
    synced = synced if synced is not None else synced_months(conn, afm).get(afm, set())
    year, month = int(ym[:4]), int(ym[5:7])
    thr_in, thr_out = thresholds(year)
    prev_in, prev_out = thresholds(year - 1)
    prev_months = [f"{year - 1}-{m:02d}" for m in range(1, 13)]
    cur_months = [f"{year}-{m:02d}" for m in range(1, month + 1)]

    def total(months: list[str], flow: str) -> float:
        return sum(ic_flows(conn, afm, m)[flow] for m in months if m in synced)

    result: dict[str, Any] = {}
    reasons = []
    for name, flow, thr_now, thr_prev, label in (("arrivals", "goods_in", thr_in, prev_in, "αφίξεις"),
                                                  ("dispatches", "goods_out", thr_out, prev_out, "αποστολές")):
        prev_total = total(prev_months, flow)
        cum = total(cur_months, flow)
        if prev_total > thr_prev:
            result[name] = True
            reasons.append(f"{label} {year - 1}: {prev_total:,.0f} € > {thr_prev:,.0f} €")
        elif cum > thr_now:
            result[name] = True
            reasons.append(f"{label} {year} σωρευτικά: {cum:,.0f} € > {thr_now:,.0f} €")
        elif all(m in synced for m in prev_months + cur_months):
            result[name] = False
        else:
            result[name] = None                         # λείπουν μήνες — δεν μπορούμε να πούμε «όχι»
    vals = [result["arrivals"], result["dispatches"]]
    result["any"] = True if True in vals else (False if vals == [False, False] else None)
    result["reason"] = "; ".join(reasons)
    return result


CLIMATE_TYPE = "8.2"      # Ειδικό στοιχείο είσπραξης τέλους ανθεκτικότητας στην κλιματική κρίση (πρώην φόρος διαμονής)


def special_fees(conn: sqlite3.Connection, afm: str, ym: str) -> dict[str, float]:
    """Τέλη που ΕΙΣΠΡΑΤΤΕΙ ο πελάτης στα έσοδά του τον μήνα και οφείλει να δηλώσει/αποδώσει:
    'climate' = σύνολο παραστατικών 8.2 (τέλος ανθεκτικότητας — δήλωση έως το τέλος του επόμενου μήνα)·
    'digital_fee' = ψηφιακό τέλος συναλλαγής (πρώην χαρτόσημο, ν. 5135/2024 — δήλωση έως το τέλος του επόμενου μήνα)
    ΧΩΡΙΣ τα ενοίκια· 'rent_fee' = ψηφιακό τέλος σε ενοίκια (8.1 / 3,6%): ΔΕΝ δημιουργεί μηνιαία υποχρέωση — αποδίδεται
    με τη δήλωση φορολογίας εισοδήματος (ρητό αίτημα χρήστη 2026-10-22)."""
    r = conn.execute("SELECT COALESCE(SUM(CASE WHEN invoice_type=? THEN total ELSE 0 END),0) AS climate, "
                     "COALESCE(SUM(CASE WHEN rent=0 THEN stamp ELSE 0 END),0) AS digital, "
                     "COALESCE(SUM(CASE WHEN rent=1 THEN stamp ELSE 0 END),0) AS rent_fee FROM mydata_docs WHERE afm=? "
                     "AND direction='outgoing' AND cancelled=0 AND substr(issue_date,1,7)=?", (CLIMATE_TYPE, afm, ym)).fetchone()
    n82 = conn.execute("SELECT COUNT(*) FROM mydata_docs WHERE afm=? AND direction='outgoing' AND cancelled=0 AND "
                       "invoice_type=? AND substr(issue_date,1,7)=?", (afm, CLIMATE_TYPE, ym)).fetchone()[0]
    return {"climate": round(r["climate"], 2), "climate_docs": n82, "digital_fee": round(r["digital"], 2),
            "rent_fee": round(r["rent_fee"], 2)}


def vat_warnings(conn: sqlite3.Connection, afm: str, year: Optional[int] = None) -> list[dict[str, Any]]:
    """Παραστατικά με ΦΠΑ (ή χαρακτηρισμό ΦΠΑ) σε πελάτη που ΔΕΝ είναι υπόχρεος ΦΠΑ (ρητό αίτημα χρήστη 2026-10-22).
    Κενή λίστα αν ο πελάτης είναι υπόχρεος ή άγνωστο."""
    b = conn.execute("SELECT vat_subject FROM businesses WHERE afm=?", (afm,)).fetchone()
    if not b or b["vat_subject"] != 0:
        return []
    sql = "SELECT * FROM mydata_docs WHERE afm=? AND vat_class=1 AND cancelled=0"
    args: list[Any] = [afm]
    if year:
        sql += " AND substr(issue_date,1,4)=?"
        args.append(str(year))
    return [dict(r) for r in conn.execute(sql + " ORDER BY issue_date", args)]


#: Καταχωρήσεις ΛΗΠΤΗ (τις διαβιβάζει ο ίδιος ο πελάτης ως αγοραστής/μισθωτής): είναι ΕΞΟΔΑ παρότι είναι «διαβιβασμένα».
RECEIVER_TYPES = ("13.", "14.", "16.")


def is_income(doc: dict[str, Any]) -> bool:
    """Έσοδο (εκδοθέν του πελάτη) ή έξοδο (λήψη ή καταχώρηση λήπτη 13.x/14.x/16.x) — για τη στήλη «Έσοδο/Έξοδο»."""
    return doc["direction"] == "outgoing" and not str(doc.get("invoice_type") or "").startswith(RECEIVER_TYPES)


def year_totals(conn: sqlite3.Connection, afm: str, year: str) -> dict[str, float]:
    """Σύνολα χρήσης από το myDATA: καθαρή αξία εσόδων/εξόδων (πιστωτικά αφαιρούνται), ΦΠΑ, παρακρατήσεις, πλήθος."""
    out = {"income": 0.0, "expenses": 0.0, "withheld": 0.0, "n": 0}
    for r in conn.execute("SELECT direction, invoice_type, net, withheld FROM mydata_docs WHERE afm=? AND cancelled=0 "
                          "AND substr(issue_date,1,4)=?", (afm, year)):
        key = "income" if is_income(dict(r)) else "expenses"
        out[key] = round(out[key] + _sign(r["invoice_type"]) * r["net"], 2)
        if r["direction"] == "incoming":
            out["withheld"] = round(out["withheld"] + (r["withheld"] or 0), 2)
        out["n"] += 1
    return out


def vat_balance(conn: sqlite3.Connection, afm: str, months: list[str]) -> dict[str, float]:
    """ΕΝΔΕΙΚΤΙΚΟΣ ΦΠΑ περιόδου από το myDATA: εκροές (εκδοθέντα) − εισροές (ληφθέντα). Δεν ξέρει την εκπιπτόμενη
    αναλογία, αυτοπαραδόσεις, τα 14.x αντίστροφης επιβάρυνσης κ.λπ. — για σύγκριση με το αποτέλεσμα της Φ2, όχι υπολογισμό."""
    marks = ",".join("?" * len(months))
    out = {"output": 0.0, "input": 0.0}
    for r in conn.execute(f"SELECT direction, invoice_type, vat FROM mydata_docs WHERE afm=? AND cancelled=0 AND "
                          f"substr(issue_date,1,7) IN ({marks})", (afm, *months)):
        key = "output" if r["direction"] == "outgoing" else "input"
        out[key] = round(out[key] + _sign(r["invoice_type"]) * r["vat"], 2)
    out["balance"] = round(out["output"] - out["input"], 2)
    return out


def month_flags(conn: sqlite3.Connection) -> dict[str, dict[str, dict[str, Any]]]:
    """ΑΦΜ -> μήνας -> {'vies', 'intrastat', 'withholding', 'climate', 'digital_fee'} για όσους μήνες έχουν κατέβει
    (για το Ημερολόγιο)."""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for afm, months in synced_months(conn).items():
        for ym in months:
            fees = special_fees(conn, afm, ym)
            flows = ic_flows(conn, afm, ym)
            out.setdefault(afm, {})[ym] = {
                "vies": any(flows.values()),
                # Φ4 = παραδόσεις/παροχές (1.2, 2.2) · Φ5 = αποκτήσεις/λήψεις (14.1, 14.3)
                "vies_out": bool(flows["goods_out"] or flows["services_out"]),
                "vies_in": bool(flows["goods_in"] or flows["services_in"]),
                "intrastat": intrastat(conn, afm, ym, months)["any"],
                "withholding": bool(withholding(conn, afm, ym)),
                "climate": fees["climate_docs"] > 0,
                "digital_fee": fees["digital_fee"] > 0,
            }
    return out


def month_summary(conn: sqlite3.Connection, afm: str, ym: str) -> dict[str, Any]:
    flows = ic_flows(conn, afm, ym)
    synced = synced_months(conn, afm).get(afm, set())
    return {"flows": flows, "vies": vies(conn, afm, ym, synced), "intrastat": intrastat(conn, afm, ym, synced),
            "withholding": withholding(conn, afm, ym), "synced": ym in synced, "fees": special_fees(conn, afm, ym)}


def month_label(ym: str) -> str:
    from ..obligations import MONTHS_NOM
    return f"{MONTHS_NOM[int(ym[5:7]) - 1]} {ym[:4]}"


def last_day(ym: str) -> date:
    y, m = int(ym[:4]), int(ym[5:7])
    return date(y, m, calendar.monthrange(y, m)[1])
