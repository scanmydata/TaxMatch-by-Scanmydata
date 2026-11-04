"""Διασταύρωση δήλωσης παρακρατούμενων φόρων (PDF της ΑΑΔΕ) με τα παραστατικά της περιόδου στο myDATA — ρητό αίτημα
2026-11-04 («προσοχή διαφορετικό pdf για εργολάβους 3% και για επιχειρηματική δραστηριότητα 20%»).

Δύο διαφορετικά έντυπα (διάταξη επαληθευμένη σε πραγματικά PDF του `viewPdf.htm`, pypdf layout mode):
* **ΦΜΥ_ΕΠΙΧ** — «Δήλωση Παρακρατούμενων … σε αμοιβές από Επιχειρηματική Δραστηριότητα» (20%): ΜΙΑ γραμμή συνόλων
  «Μήνας <μήνας> ακαθάριστες κρατήσεις καθαρές φόρος ψηφιακό» — χωρίς ανάλυση ανά δικαιούχο.
* **ΕΡΓΟΛΑΒΩΝ** — «Δήλωση απόδοσης … εργολάβων» (3%): «Αξία … 3%: Χ», «Φόρος που αναλογεί 3% Υ» και ΓΡΑΜΜΗ ΑΝΑ
  ΔΙΚΑΙΟΥΧΟ (α/α, ημερομηνία παρακράτησης, ΑΦΜ, …, αξία, φόρος, εκπρόθεσμος, σύνολο) — εδώ η σύγκριση γίνεται και ανά ΑΦΜ.

myDATA: κατηγορίες παρακράτησης 3 (αμοιβές επιχ. δραστηριότητας) και 4 (εργολάβοι) στα ΛΗΦΘΕΝΤΑ του μήνα έκδοσης.
Η δήλωση ακολουθεί τον μήνα ΚΑΤΑΒΟΛΗΣ — διαφορά μπορεί να είναι απλώς χρονική (πληρωμή σε άλλο μήνα).
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Optional

_AMOUNT = r"\d{1,3}(?:\.\d{3})*,\d{2}"
CATEGORY = {"ΦΜΥ_ΕΠΙΧ": "3", "ΕΡΓΟΛΑΒΩΝ": "4"}
TOLERANCE = 0.05


def _num(text: str) -> float:
    return float(text.replace(".", "").replace(",", "."))


def pdf_text(path: str | Path) -> str:
    from pypdf import PdfReader
    return "\n".join(p.extract_text(extraction_mode="layout") or "" for p in PdfReader(str(path)).pages)


def parse_business(text: str) -> Optional[dict[str, Any]]:
    """ΦΜΥ επιχειρηματικής δραστηριότητας: {gross, deductions, net, tax, digital_fee} από τη γραμμή συνόλων."""
    start = text.find("ΑΚΑΘΑΡΙΣΤΕΣ")
    for line in (text[start:] if start >= 0 else text).splitlines():
        nums = re.findall(_AMOUNT, line)
        if len(nums) >= 4:
            vals = [_num(n) for n in nums]
            return {"gross": vals[0], "deductions": vals[1], "net": vals[2], "tax": vals[3],
                    "digital_fee": vals[4] if len(vals) > 4 else 0.0, "rows": []}
    return None


def parse_contractors(text: str) -> Optional[dict[str, Any]]:
    """Εργολάβοι: {gross, tax, rows: [{date, afm, value, tax}]} (σύνολα 3% + γραμμή ανά δικαιούχο)."""
    gross = re.search(r"προμήθεια\s*3%:?\s*(" + _AMOUNT + ")", text)
    tax = re.search(r"Φόρος που αναλογεί\s*3%\s*(" + _AMOUNT + ")", text)
    if not gross and not tax:
        return None
    rows = []
    for line in text.splitlines():
        m = re.match(r"\s*\d+\s+(\d{1,2}/\d{1,2}/\d{4})\s+(\d{9})\b", line)
        if not m:
            continue
        nums = re.findall(_AMOUNT, line)
        if len(nums) >= 2:
            rows.append({"date": m.group(1), "afm": m.group(2), "value": _num(nums[-4] if len(nums) >= 4 else nums[0]),
                         "tax": _num(nums[-3] if len(nums) >= 4 else nums[1])})
    return {"gross": _num(gross.group(1)) if gross else 0.0, "tax": _num(tax.group(1)) if tax else 0.0, "rows": rows}


def parse(key: str, text: str) -> Optional[dict[str, Any]]:
    return parse_contractors(text) if key == "ΕΡΓΟΛΑΒΩΝ" else parse_business(text)


def mydata_lines(conn: sqlite3.Connection, afm: str, ym: str, key: str) -> list[dict[str, Any]]:
    """Λήψεις του μήνα με παρακράτηση της κατηγορίας της δήλωσης: [{mark, afm (εκδότη), name, base, tax}]."""
    cat = CATEGORY[key]
    out = []
    for r in conn.execute("SELECT mark, invoice_type, issuer_vat, issuer_name, net, withheld_json FROM mydata_docs "
                          "WHERE afm=? AND direction='incoming' AND cancelled=0 AND substr(issue_date,1,7)=?", (afm, ym)):
        amount = (json.loads(r["withheld_json"] or "{}") or {}).get(cat)
        if amount:
            sign = -1 if str(r["invoice_type"]).startswith("5.") else 1
            out.append({"mark": r["mark"], "afm": re.sub(r"\D", "", r["issuer_vat"] or "")[-9:], "name": r["issuer_name"],
                        "base": sign * (r["net"] or 0.0), "tax": sign * amount})
    return out


def reconcile(conn: sqlite3.Connection, afm: str, ym: str, key: str, pdf: str | Path) -> dict[str, Any]:
    """Σύγκριση δήλωσης ↔ myDATA για τον μήνα. {ok, declared, mydata, diff, missing_in_pdf, missing_in_mydata,
    per_afm_diff, note}. `ok` None = το PDF δεν διαβάστηκε."""
    try:
        parsed = parse(key, pdf_text(pdf))
    except Exception:                                   # noqa: BLE001 — κατεστραμμένο/άλλο PDF
        parsed = None
    lines = mydata_lines(conn, afm, ym, key)
    mine = round(sum(x["tax"] for x in lines), 2)
    out: dict[str, Any] = {"ok": None, "declared": None, "mydata": mine, "diff": None, "missing_in_pdf": [],
                           "missing_in_mydata": [], "per_afm_diff": [], "note": ""}
    if parsed is None:
        out["note"] = "το PDF της δήλωσης δεν διαβάστηκε"
        return out
    out["declared"] = round(parsed["tax"], 2)
    out["diff"] = round(out["declared"] - mine, 2)
    if parsed["rows"]:
        pdf_by: dict[str, float] = {}
        for r in parsed["rows"]:
            pdf_by[r["afm"]] = round(pdf_by.get(r["afm"], 0.0) + r["tax"], 2)
        my_by: dict[str, float] = {}
        for x in lines:
            my_by[x["afm"]] = round(my_by.get(x["afm"], 0.0) + x["tax"], 2)
        out["missing_in_pdf"] = sorted(set(my_by) - set(pdf_by))
        out["missing_in_mydata"] = sorted(set(pdf_by) - set(my_by))
        out["per_afm_diff"] = [(a, pdf_by[a], my_by[a]) for a in sorted(set(pdf_by) & set(my_by))
                               if abs(pdf_by[a] - my_by[a]) > TOLERANCE]
    out["ok"] = (abs(out["diff"]) <= TOLERANCE and not out["missing_in_pdf"] and not out["missing_in_mydata"]
                 and not out["per_afm_diff"])
    return out


def summary_text(res: dict[str, Any], names: Optional[dict[str, str]] = None) -> str:
    """Σύντομη περιγραφή για την καρτέλα myDATA."""
    if res["ok"] is None:
        return res["note"]
    money = lambda v: f"{v:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")  # noqa: E731
    if res["ok"]:
        return f"Δήλωση (PDF) = myDATA: φόρος {money(res['declared'])} ✓"
    parts = [f"Δήλωση (PDF) φόρος {money(res['declared'])} ≠ myDATA {money(res['mydata'])} (διαφορά {money(res['diff'])})"
             if abs(res["diff"] or 0) > TOLERANCE else f"σύνολο φόρου ίδιο ({money(res['declared'])})"]
    label = lambda a: f"{(names or {}).get(a) or a}"  # noqa: E731
    if res["missing_in_pdf"]:
        parts.append("στο myDATA αλλά όχι στη δήλωση: " + ", ".join(label(a) for a in res["missing_in_pdf"][:5]))
    if res["missing_in_mydata"]:
        parts.append("στη δήλωση αλλά όχι στο myDATA του μήνα: " + ", ".join(label(a) for a in res["missing_in_mydata"][:5]))
    for a, d, m in res["per_afm_diff"][:5]:
        parts.append(f"{label(a)}: δήλωση {money(d)} / myDATA {money(m)}")
    return " · ".join(parts) + " — η δήλωση ακολουθεί τον μήνα πληρωμής, μπορεί να είναι χρονική διαφορά"
