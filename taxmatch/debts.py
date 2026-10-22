"""Οφειλές πελάτη στην ΑΑΔΕ (εκτός ρύθμισης / σε ρύθμιση) με τις ταυτότητες οφειλής (RF) και τα PDF τους.

Πιστό port του ScanMyData Tax Center `configs/aade-debts.js` (ρητό αίτημα χρήστη 2026-10-22 — «όπως κάνει το
taxcenter»· οι σελίδες επαληθεύτηκαν ζωντανά εκεί, 30/9/2026), πάνω στο login του `lookup_aade` (GSIS OAM):

* `/taxisnet/info/protected/displayDebtInfoAndPay.htm` — οφειλές εκτός ρύθμισης: στήλες «Ληξιπρόθεσμο Υπόλοιπο» /
  «Μη ληξιπρόθεσμο Υπόλοιπο» / «Συνολικό Ποσό», σύνολα λογαριασμού, κρυφοί πίνακες δόσεων (`installmentInfo_N`,
  `generalInstallmentInfo_N`). Κάθε γραμμή έχει `doDisplayPaymentCode(document.displayPaymentCodeForm, …)`:
  GET `displayDebtCode.htm?…` = η ταυτότητα (RF) · POST `debtInfoPdf.htm` (withoutAmounts=false) = PDF.
* `/taxisnet/info/protected/displayArrangementInfoAndPay.htm` — ρυθμίσεις: `displayArrangementCode.htm?…` (ΤΡΟ) και
  εκτύπωση με `doViewArrPdf` → `arrDebtInfoPdf.htm`.

Επιπλέον, για τις ΧΡΕΩΣΤΙΚΕΣ δηλώσεις του ελέγχου υποβολών (Φ2, παρακρατήσεις…) το `easycheck` κατεβάζει το PDF της
ταυτότητας οφειλής τους με το ίδιο `print_form` (`declaration_pdf`).

**ΜΟΝΟ ανάγνωση.** **Δεν έχει δοκιμαστεί ζωντανά από το TaxMatch** (tests σε ψεύτικες σελίδες της δομής του Tax Center).
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin

from . import config, db, reports, settings_store
from .business_profiles import credentials, lookup_aade

log = logging.getLogger(__name__)
AADE = lookup_aade.AADE
BASE = "/taxisnet/info/protected/"
DEBTS_PAGE = BASE + "displayDebtInfoAndPay.htm"
ARR_PAGE = BASE + "displayArrangementInfoAndPay.htm"
TO_ARGS = ("action", "returnView", "mchDoy", "mchDept", "mchMctCode", "mchSctCode", "mchYear", "mchSourceCode",
           "mchMcNo", "mcLineNo", "index")
ARR_ARGS = ("returnView", "arnDoy", "arnDept", "arnYear", "arrAA")
MCH = ("mchDoy", "mchDept", "mchMctCode", "mchSctCode", "mchYear", "mchSourceCode", "mchMcNo", "mcLineNo")
KIND_LABEL = {"debt": "Οφειλή ΑΑΔΕ", "arrangement": "Ρύθμιση ΑΑΔΕ", "declaration": "Χρεωστική δήλωση",
              "carrier": "ΚΕΑΟ — φορέας", "keao_arrangement": "ΚΕΑΟ — ρύθμιση", "keao_inactive": "ΚΕΑΟ — ανενεργή ρύθμιση",
              "efka": "ΕΦΚΑ — τρέχουσα εισφορά"}


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def amount(s: str) -> float:
    m = re.search(r"-?[\d.]+,\d{2}", str(s or ""))
    return float(m.group(0).replace(".", "").replace(",", ".")) if m else 0.0


def _cells(tr: str) -> list[str]:
    return [strip_tags(c) for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.S | re.I)]


def _rows(fragment: str) -> list[list[str]]:
    return [c for c in (_cells(tr) for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", fragment, re.S | re.I)) if any(c)]


def installments(page: str) -> dict[str, dict[str, Any]]:
    """Κρυφοί πίνακες δόσεων (`installmentInfo_N` + `generalInstallmentInfo_N`) ανά δείκτη γραμμής."""
    out: dict[str, dict[str, Any]] = {}
    for m in re.finditer(r'<table[^>]*id="installmentInfo_(\d+)".*?</table>', page, re.S | re.I):
        rows = _rows(m.group(0))
        if len(rows) > 1:
            out.setdefault(m.group(1), {})["installments"] = {"headers": rows[0], "rows": rows[1:]}
    for m in re.finditer(r'<table[^>]*id="generalInstallmentInfo_(\d+)".*?</table>', page, re.S | re.I):
        rows = _rows(m.group(0))
        if rows:
            out.setdefault(m.group(1), {})["general"] = rows
    return out


def payment_code(page: str) -> str:
    """Ο κωδικός πληρωμής από το κελί δίπλα στην ετικέτα «Ταυτότητα…» (η ΤΡΟ δεν έχει πρόθεμα RF)."""
    m = re.search(r"<td[^>]*>\s*Ταυτότητα[^<]*</td>\s*<td[^>]*>(.*?)</td>", page or "", re.S | re.I)
    cell = strip_tags(m.group(1)) if m else ""
    if re.search(r"\d", cell):
        return cell
    rf = re.search(r"RF\d{2}[A-Z0-9]{4,}", page or "")
    return rf.group(0) if rf else ""


def print_form(page: str, fn: str = "doViewPdf") -> Optional[dict[str, Any]]:
    """Η φόρμα του κουμπιού «Εκτύπωση» (port του `printForm` του Tax Center): action + πεδία, με τις τιμές από τα
    ορίσματα της κλήσης `fn(document.<form>, …)`. Από τις δύο φόρμες (με/χωρίς ποσά) κρατά ΑΥΤΗ με τα ποσά."""
    page = page or ""
    definition = re.search(r"function\s+" + re.escape(fn) + r"\s*\(([^)]*)\)\s*\{(.*?)\n\s*\}", page, re.S | re.I)
    params = [p.strip() for p in definition.group(1).split(",")] if definition else []
    body = definition.group(2) if definition else ""
    found = []
    for call in re.finditer(re.escape(fn) + r"\(\s*document\.([A-Za-z0-9_]+)\s*,([^)]*)\)", page, re.I):
        args = [a.strip().strip("'\"") for a in call.group(2).split(",")]
        form = re.search(r'<form[^>]*name="' + re.escape(call.group(1)) + r'"[^>]*>(.*?)</form>', page, re.S | re.I)
        if not form:
            continue
        opening = form.group(0)[:form.group(0).find(">") + 1]
        in_body = re.search(r"""frm\.action\s*=\s*['"]([^'"]+)['"]""", body)
        action = (in_body.group(1) if in_body else "") or (re.search(r'action="([^"]*)"', opening, re.I) or [None, ""])[1]
        if not action:
            continue
        fields: dict[str, str] = {}
        for tag in re.findall(r"<input[^>]*>", form.group(1), re.I):
            name = re.search(r'name="([^"]*)"', tag, re.I)
            if name:
                value = re.search(r'value="([^"]*)"', tag, re.I)
                fields[name.group(1)] = _html.unescape(value.group(1)) if value else ""
        for m in re.finditer(r"frm\.elements\['([^']+)'\]\.value\s*=\s*([A-Za-z0-9_]+)\s*;", body):
            i = params.index(m.group(2)) if m.group(2) in params else -1
            if i > 0 and i - 1 < len(args):
                fields[m.group(1)] = args[i - 1]
        found.append({"action": _html.unescape(action), "fields": fields})
    if not found:
        return None
    return next((f for f in found if str(f["fields"].get("withoutAmounts", "false")) != "true"), found[0])


def debts_dir(afm: str) -> Path:
    return config.data_dir() / "debts" / afm


def _safe(s: str) -> str:
    return re.sub(r"[^0-9A-Za-zΑ-Ωα-ωάέήίόύώϊϋΐΰ.,_-]+", "_", strip_tags(str(s or ""))).strip("_")[:60]


def _save_pdf(afm: str, name: str, content: bytes) -> str:
    folder = debts_dir(afm)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(content)
    return str(path)


# ------------------------------------------------------------------ ανάγνωση σελίδων
def parse_debts_page(page: str) -> dict[str, Any]:
    """Οφειλές εκτός ρύθμισης: [{fields, overdue, non_overdue, total, to, idx}] + σύνολα λογαριασμού."""
    header: list[str] = []
    for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", page, re.S | re.I):
        cells = _cells(tr)
        if any("Ημ/νία Βεβαίωσης" in c for c in cells) and any("Ληξιπρόθεσμο Υπόλοιπο" in c for c in cells):
            header = cells
            break

    def col(pattern: str) -> int:
        return next((i for i, h in enumerate(header) if re.search(pattern, h)), -1)
    i_over, i_non, i_total = col(r"^Ληξιπρόθεσμο Υπόλοιπο"), col(r"Μη ληξιπρόθεσμο Υπόλοιπο"), col(r"Συνολικό Ποσό")
    debts = []
    for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", page, re.S | re.I):
        call = re.search(r"doDisplayPaymentCode\(document\.displayPaymentCodeForm\s*,(.*?)\)", tr, re.S | re.I)
        if not call:
            continue
        args = re.findall(r'"([^"]*)"', _html.unescape(call.group(1)))
        cells = _cells(tr)
        fields = {h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header) if h}
        idx = re.search(r"showInstallmentInfoRadio_(\d+)|installmentInfo_(\d+)", tr)
        debts.append({"fields": fields, "to": dict(zip(TO_ARGS, args)),
                      "overdue": amount(cells[i_over]) if 0 <= i_over < len(cells) else 0.0,
                      "non_overdue": amount(cells[i_non]) if 0 <= i_non < len(cells) else 0.0,
                      "total": amount(cells[i_total]) if 0 <= i_total < len(cells) else 0.0,
                      "idx": next((g for g in idx.groups() if g), str(len(debts))) if idx else str(len(debts))})
    text = strip_tags(page)
    non = re.search(r"Μη Ληξιπρόθεσμο Υπόλοιπο Οφειλών\s*:?\s*(-?[\d.]+,\d{2})", text)
    over = re.search(r"(?<!Μη )Ληξιπρόθεσμο Υπόλοιπο Οφειλών\s*:?\s*(-?[\d.]+,\d{2})", text)
    return {"debts": debts, "overdue_total": amount(over.group(1)) if over else sum(d["overdue"] for d in debts),
            "non_overdue_total": amount(non.group(1)) if non else sum(d["non_overdue"] for d in debts)}


def parse_arrangements_page(page: str) -> list[dict[str, Any]]:
    out, header = [], []
    for tr in re.findall(r"<tr\b[^>]*>.*?</tr>", page, re.S | re.I):
        cells = _cells(tr)
        call = re.search(r"doDisplayPaymentCode\(document\.displayPaymentCodeForm\s*,(.*?)\)", tr, re.S | re.I)
        if not call:
            if len([c for c in cells if c]) >= 4:
                header = cells
            continue
        args = [a.strip().strip("'\"") for a in _html.unescape(call.group(1)).split(",")]
        idx = re.search(r"showInstallmentInfoRadio_(\d+)|showTObut_(\d+)", tr)
        out.append({"fields": {h: (cells[i] if i < len(cells) else "") for i, h in enumerate(header) if h},
                    "to": dict(zip(ARR_ARGS, args)),
                    "idx": next((g for g in idx.groups() if g), "") if idx else ""})
    return out


def _title(fields: dict[str, str]) -> str:
    for key in ("Είδος φόρου", "Είδος Φόρου", "Είδος", "Περιγραφή", "Αιτιολογία"):
        if fields.get(key):
            return fields[key]
    return next((v for v in fields.values() if v and not re.search(r"\d", v)), "")


def _first_amount(fields: dict[str, str], *patterns: str) -> float:
    for p in patterns:
        for k, v in fields.items():
            if re.search(p, k, re.I) and amount(v):
                return amount(v)
    return 0.0


def _schedule_fields(details: dict[str, Any]) -> dict[str, Any]:
    """next_due / next_amount / instalments / unpaid από τον πίνακα δόσεων της πύλης (`reports.unpaid_schedule`)."""
    inst = details.get("installments") or {}
    unpaid, count = reports.unpaid_schedule(inst.get("headers", []), inst.get("rows", []))
    due, amt = reports.next_due(unpaid)
    return {"next_due": due, "next_amount": amt, "instalments": count, "unpaid": len(unpaid)}


def schedule_report(item: dict[str, Any], client: str, afm: str, office: str, retrieved: str) -> Optional[reports.Report]:
    """Το δοσολόγιο μιας οφειλής/ρύθμισης ΑΑΔΕ (port του `DebtSchedule.kt`): σελίδα που μπαίνει ΠΙΣΩ από την Ταυτότητα
    Οφειλής — η ΑΑΔΕ τυπώνει πόσο και με ποιον κωδικό, όχι ΠΟΤΕ λήγει κάθε δόση."""
    inst = item["details"].get("installments") or {}
    if not inst.get("headers") or not inst.get("rows"):
        return None
    f = item["details"].get("fields") or {}
    identity = [("Υπόχρεος", " · ".join(x for x in (client, afm) if x)), ("Οφειλή", item["title"])]
    identity += [(k, f[k]) for k in ("ΔΟΥ", "Πηγή", "Οικ. έτος", "Ημ/νία Βεβαίωσης", "Ημ/νία Ρύθμισης") if f.get(k)]
    if item["total"]:
        identity.append(("Συνολικό ποσό", reports.money(item["total"])))
    summary = [(r[0], r[1]) for r in item["details"].get("general") or [] if len(r) >= 2 and r[0]]
    return reports.Report(
        "Δοσολόγιο ρύθμισης" if item["kind"] == "arrangement" else "Ανάλυση δόσεων οφειλής", office, identity,
        summary=summary, sections=[reports.Section("Δόσεις", reports.table_from(inst["headers"], inst["rows"]))],
        footer=[f"Άντληση από την Προσωποποιημένη Πληροφόρηση της ΑΑΔΕ{' στις ' + retrieved if retrieved else ''}.",
                "Οι δόσεις αλλάζουν με κάθε καταβολή και με τον χρόνο (προσαυξήσεις, τόκοι). Η πληρωμή γίνεται με την "
                "Ταυτότητα Οφειλής της προηγούμενης σελίδας.",
                "Η σελίδα των δόσεων προστέθηκε από το γραφείο — δεν είναι έγγραφο της ΑΑΔΕ."])


def _attach_schedule(item: dict[str, Any], afm: str, client: str, office: str, retrieved: str) -> None:
    """Το δοσολόγιο ως 2η σελίδα της Ταυτότητας Οφειλής· αν δεν βγήκε το PDF της ΑΑΔΕ, σε δικό του αρχείο (DOSEIS_…) —
    μια σιωπηλή απώλεια εδώ θα έμοιαζε με οφειλή χωρίς δόσεις."""
    rep = schedule_report(item, client, afm, office, retrieved)
    if rep is None:
        return
    folder = debts_dir(afm)
    page = folder / f"_doseis_{_safe(item['ref'])[-40:]}.pdf"
    try:
        reports.write(rep, page)
        if item["pdf_path"] and Path(item["pdf_path"]).exists():
            reports.append_pages(Path(item["pdf_path"]), page)
            page.unlink(missing_ok=True)
        else:
            target = folder / f"DOSEIS_{afm}_{_safe(item['title'])}_{_safe(reports.money(item['total']))}.pdf"
            page.replace(target)
            item["pdf_path"] = str(target)
    except Exception as exc:                            # noqa: BLE001 — χωρίς δοσολόγιο, όχι αποτυχία λήψης
        log.warning("δοσολόγιο %s: %s", afm, exc)


def fetch(http: Any, afm: str, want_pdf: bool = True, client: str = "", office: str = "") -> dict[str, Any]:
    """Οφειλές + ρυθμίσεις ΑΑΔΕ με ΤΟ/ΤΡΟ (μετά το login). `want_pdf`: και τα PDF της ΑΑΔΕ με το δοσολόγιο πίσω τους —
    αλλιώς «μόνο δεδομένα» (γρήγορη ενημέρωση, όπως το `DebtsRefresh` του Tax Center). {debts, overdue, non_overdue}."""
    rows: list[dict[str, Any]] = []
    retrieved = datetime.now().strftime("%d/%m/%Y %H:%M")
    page = http.follow("GET", urljoin(AADE, DEBTS_PAGE))["text"]
    parsed = parse_debts_page(page)
    inst = installments(page)
    for d in parsed["debts"]:
        to = d["to"]
        ref = "|".join(to.get(k, "") for k in MCH)
        details = {"fields": d["fields"], **inst.get(d["idx"], {})}
        item = {"kind": "debt", "ref": ref, "title": _title(d["fields"]), "overdue": d["overdue"],
                "non_overdue": d["non_overdue"], "total": d["total"] or round(d["overdue"] + d["non_overdue"], 2),
                "code": "", "pdf_path": "", "details": details, **_schedule_fields(details)}
        if to.get("mcLineNo"):
            mch = {k: to.get(k, "") for k in MCH}
            q = urlencode({"returnView": to.get("returnView", ""), **mch, "rtr": ""})
            code_page = http.follow("GET", urljoin(AADE, BASE + (to.get("action") or "displayDebtCode.htm") + "?" + q))
            item["code"] = payment_code(code_page["text"])
            if want_pdf:
                pdf = http.follow("POST", urljoin(AADE, BASE + "debtInfoPdf.htm"), {**mch, "withoutAmounts": "false"})
                if pdf.get("is_pdf"):
                    item["pdf_path"] = _save_pdf(afm, f"OFEILI_{afm}_{_safe(item['title'])}_{_safe(ref)[-24:]}.pdf",
                                                 pdf["content"])
        if want_pdf:
            _attach_schedule(item, afm, client, office, retrieved)
        rows.append(item)
    arr_page = http.follow("GET", urljoin(AADE, ARR_PAGE))["text"]
    arr_inst = installments(arr_page)
    for a in parse_arrangements_page(arr_page):
        to = a["to"]
        f = a["fields"]
        details = {"fields": f, **arr_inst.get(a["idx"], {})}
        item = {"kind": "arrangement", "ref": f"{to.get('arnYear', '')}|{to.get('arrAA', '')}",
                "title": _title(f) or f"Ρύθμιση {to.get('arrAA', '')}",
                "overdue": _first_amount(f, r"^Ληξιπρόθεσμ"), "non_overdue": 0.0,
                "total": _first_amount(f, r"Συνολικό Υπόλοιπο", r"Υπόλοιπο", r"Συνολικ", r"Ποσό"), "code": "",
                "pdf_path": "", "details": details, **_schedule_fields(details)}
        if to.get("arrAA"):
            q = urlencode({"returnView": to.get("returnView") or ARR_PAGE.rsplit("/", 1)[-1], "arnDoy": to.get("arnDoy", ""),
                           "arnDept": to.get("arnDept", ""), "arnYear": to.get("arnYear", ""), "arrAA": to["arrAA"]})
            code_url = urljoin(AADE, BASE + "displayArrangementCode.htm?" + q)
            code_page = http.follow("GET", code_url)["text"]
            item["code"] = payment_code(code_page)
            pf = print_form(code_page, "doViewArrPdf") or print_form(code_page)
            if want_pdf and pf:
                pdf = http.follow("POST", urljoin(code_url, pf["action"]), pf["fields"])
                if pdf.get("is_pdf"):
                    item["pdf_path"] = _save_pdf(afm, f"RYTHMISI_{afm}_{_safe(to.get('arnYear', ''))}_"
                                                      f"{_safe(to['arrAA'])}.pdf", pdf["content"])
        if want_pdf:
            _attach_schedule(item, afm, client, office, retrieved)
        rows.append(item)
    return {"debts": rows, "overdue": round(parsed["overdue_total"], 2), "non_overdue": round(parsed["non_overdue_total"], 2)}


SOURCES = {"aade": "ΑΑΔΕ", "keao": "ΚΕΑΟ", "efka": "ΕΦΚΑ"}


def store(conn: sqlite3.Connection, afm: str, result: dict[str, Any], source: str = "aade") -> None:
    """Αντικαθιστά τις οφειλές ΤΗΣ ΠΥΛΗΣ `source` (εξοφλημένες φεύγουν). Τα PDF που κατέβηκαν παλιότερα κρατιούνται όταν
    η νέα λήψη είναι «μόνο δεδομένα» (ίδια γραμμή). Οι χρεωστικές δηλώσεις μένουν στο `obligation_checks`."""
    now = db.utcnow()
    old_pdfs = {r["ref"]: r["pdf_path"] for r in conn.execute(
        "SELECT ref, pdf_path FROM aade_debts WHERE afm=? AND source=?", (afm, source))}
    conn.execute("BEGIN")
    try:
        conn.execute("DELETE FROM aade_debts WHERE afm=? AND source=?", (afm, source))
        for d in result["debts"]:
            pdf = d.get("pdf_path") or (old_pdfs.get(d["ref"], "") if Path(old_pdfs.get(d["ref"], "") or "_").exists() else "")
            conn.execute("INSERT OR REPLACE INTO aade_debts(afm, kind, ref, title, overdue, non_overdue, total, code, "
                         "pdf_path, details_json, fetched_at, source, next_due, next_amount, instalments, unpaid) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (afm, d["kind"], d["ref"], d["title"], d["overdue"], d["non_overdue"], d["total"], d["code"],
                          pdf, json.dumps(d["details"], ensure_ascii=False), now, source, d.get("next_due", ""),
                          d.get("next_amount", 0.0), d.get("instalments", 0), d.get("unpaid", 0)))
        if source == "aade":
            conn.execute("INSERT INTO debt_sync(afm, synced_at, status, message, overdue, non_overdue) VALUES (?,?,?,?,?,?) "
                         "ON CONFLICT(afm) DO UPDATE SET synced_at=excluded.synced_at, status=excluded.status, "
                         "message=excluded.message, overdue=excluded.overdue, non_overdue=excluded.non_overdue",
                         (afm, now, "ok", "", result.get("overdue", 0.0), result.get("non_overdue", 0.0)))
        total = sum(d["total"] for d in result["debts"] if d["kind"] not in ("keao_arrangement", "keao_inactive"))
        conn.execute("INSERT INTO debt_source_sync(afm, source, synced_at, status, message, total) VALUES (?,?,?,?,?,?) "
                     "ON CONFLICT(afm, source) DO UPDATE SET synced_at=excluded.synced_at, status=excluded.status, "
                     "message=excluded.message, total=excluded.total", (afm, source, now, "ok", "", round(total, 2)))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _fail(conn: sqlite3.Connection, afm: str, source: str, reason: str) -> None:
    conn.execute("INSERT INTO debt_source_sync(afm, source, synced_at, status, message) VALUES (?,?,?,?,?) "
                 "ON CONFLICT(afm, source) DO UPDATE SET synced_at=excluded.synced_at, status=excluded.status, "
                 "message=excluded.message", (afm, source, db.utcnow(), "error", reason))
    if source == "aade":
        conn.execute("INSERT INTO debt_sync(afm, synced_at, status, message) VALUES (?,?,?,?) ON CONFLICT(afm) DO UPDATE "
                     "SET synced_at=excluded.synced_at, status=excluded.status, message=excluded.message",
                     (afm, db.utcnow(), "error", reason))


def _client_info(conn: sqlite3.Connection, afm: str) -> tuple[str, bool]:
    from .obligations import is_legal_entity
    b = conn.execute("SELECT name, legal_form FROM businesses WHERE afm=?", (afm,)).fetchone()
    return (b["name"] if b else "") or "", bool(b) and is_legal_entity(b["legal_form"] or "")


def efka_plan(conn: sqlite3.Connection, afm: str) -> dict[str, str]:
    """Πώς γίνεται η σύνδεση e-ΕΦΚΑ για ΚΕΑΟ/ΕΦΚΑ (όπως `DebtsRefresh.plans`): φυσικό πρόσωπο με ΑΜΚΑ → μη μισθωτός
    (ΚΕΑΟ + τρέχουσες εισφορές)· εταιρεία → ΜΟΝΟ ως εργοδότης με ΑΜΕ (οι εταιρείες ΔΕΝ έχουν ΑΜΚΑ — ρητή παρατήρηση
    χρήστη 2026-10-22)· ατομική επιχείρηση χωρίς ΑΜΚΑ αλλά με ΑΜΕ → εργοδότης. {} = δεν γίνεται (λείπουν στοιχεία)."""
    from . import efka
    _name, legal = _client_info(conn, afm)
    amka, ame = efka.get_amka(conn, afm), efka.get_ame(conn, afm)
    if legal:
        return {"amka": "", "ame": ame} if ame else {}
    if amka:
        return {"amka": amka, "ame": ""}
    return {"amka": "", "ame": ame} if ame else {}


def check_client(conn: sqlite3.Connection, afm: str, want_pdf: bool = False,
                 login_fn: Callable[[str, str], dict] = lookup_aade.aade_login,
                 sources: tuple[str, ...] = ("aade", "keao", "efka"),
                 efka_login: Optional[Callable[..., dict]] = None) -> dict[str, Any]:
    """Login με τους κωδικούς ΤΟΥ ΠΕΛΑΤΗ → οφειλές κάθε πύλης → αποθήκευση. `want_pdf`: και τα έντυπα (ΤΟ + δοσολόγια,
    καρτέλες ΚΕΑΟ, ειδοποιητήριο ΕΦΚΑ)· αλλιώς μόνο δεδομένα. {ok, reason, n, overdue, non_overdue, errors}."""
    from . import efka, keao
    creds = credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί TAXISnet του πελάτη."}
    client, _legal = _client_info(conn, afm)
    office = settings_store.get(conn, "office_name")
    out: dict[str, Any] = {"ok": False, "reason": "", "n": 0, "overdue": 0.0, "non_overdue": 0.0, "errors": []}
    if "aade" in sources:
        login = login_fn(*creds)
        if not login.get("ok"):
            reason = lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης TAXISnet.")
            _fail(conn, afm, "aade", reason)
            return {"ok": False, "reason": reason}
        result = fetch(login["http"], afm, want_pdf, client, office)
        store(conn, afm, result, "aade")
        out.update(ok=True, n=len(result["debts"]), overdue=result["overdue"], non_overdue=result["non_overdue"])
    plan = efka_plan(conn, afm)
    if plan and ("keao" in sources or "efka" in sources):
        try:
            if plan["ame"]:
                # Εργοδότης: κωδικοί ΙΚΑ εργοδότη, αλλιώς TAXISnet (ScanMyData kartela_ergodoti.py / Tax Center)
                login = efka.employer_login_any(conn, afm, plan["ame"], (
                    (lambda u, p, a, ame: efka_login(u, p, a, ame=ame, amka="")) if efka_login else None))
            else:
                login = (efka_login or efka.services_login)(creds[0], creds[1], afm, **plan)
            if not login.get("ok"):
                raise RuntimeError(efka.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης e-ΕΦΚΑ."))
            if "keao" in sources:
                carriers = keao.fetch_keao(login["http"], login["page"]["text"])
                lines = keao.keao_lines(carriers)
                if want_pdf and carriers:
                    paths = keao.keao_reports(carriers, client, afm, office, datetime.now().strftime("%d/%m/%Y %H:%M"),
                                              debts_dir(afm))
                    for line in lines:
                        line["pdf_path"] = paths.get(line["ref"], "")
                store(conn, afm, {"debts": lines}, "keao")
                out["n"] += len(lines)
                out["ok"] = True
            if "efka" in sources and plan.get("amka"):
                cur = keao.efka_current(login["http"], login.get("links") or {}, afm, debts_dir(afm) if want_pdf else None)
                store(conn, afm, {"debts": cur["lines"]}, "efka")
                out["n"] += len(cur["lines"])
                out["ok"] = True
        except Exception as exc:                        # noqa: BLE001 — η ΑΑΔΕ έχει ήδη αποθηκευτεί
            msg = str(exc)[:160]
            for s in ("keao", "efka"):
                if s in sources:
                    _fail(conn, afm, s, msg)
            out["errors"].append(f"ΚΕΑΟ/ΕΦΚΑ: {msg}")
    elif "aade" not in sources:
        out["reason"] = "Για ΚΕΑΟ/ΕΦΚΑ χρειάζεται ΑΜΚΑ (φυσικά πρόσωπα) ή ΑΜΕ (εργοδότες)."
    return out


def declaration_debts(conn: sqlite3.Connection, afm: str) -> list[dict[str, Any]]:
    """Χρεωστικές δηλώσεις (Φ2, παρακρατήσεις…) από τον έλεγχο υποβολών — με ταυτότητα/PDF όπου διαβάστηκαν."""
    from .easycheck import OBLIGATIONS
    out = []
    for r in conn.execute("SELECT obligation, year, period, period_end, amounts_json, checked_at FROM obligation_checks "
                          "WHERE afm=? AND status='submitted' ORDER BY period_end DESC", (afm,)):
        a = json.loads(r["amounts_json"] or "{}")
        if a.get("result") != "Χρεωστική" and not a.get("total_due"):
            continue
        o = OBLIGATIONS.get(r["obligation"])
        out.append({"kind": "declaration", "source": "aade", "ref": f"{r['obligation']}|{r['period']}",
                    "title": f"{o.label if o else r['obligation']} — {r['period']}", "overdue": 0.0, "non_overdue": 0.0,
                    "total": a.get("total_due") or a.get("lump_sum") or 0.0, "code": a.get("debt_id", ""),
                    "pdf_path": a.get("pdf", ""), "details": a, "fetched_at": r["checked_at"],
                    "period_end": r["period_end"],
                    "next_due": a.get("pay_by", ""), "next_amount": a.get("installment") or 0.0, "instalments": 0,
                    "unpaid": 0})
    return out


def client_debts(conn: sqlite3.Connection, afm: str, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Όλες οι οφειλές του πελάτη για προβολή/email: ΑΑΔΕ (εκτός/σε ρύθμιση), ΚΕΑΟ (φορείς, ρυθμίσεις), ΕΦΚΑ (τρέχουσες
    εισφορές) + χρεωστικές δηλώσεις. Οι μη ενεργές ρυθμίσεις ΚΕΑΟ μόνο με `include_inactive` (ιστορικό)."""
    order = "CASE source WHEN 'aade' THEN 0 WHEN 'keao' THEN 1 ELSE 2 END, kind, overdue DESC, total DESC"
    rows = [{**dict(r), "details": json.loads(r["details_json"] or "{}")}
            for r in conn.execute(f"SELECT * FROM aade_debts WHERE afm=? ORDER BY {order}", (afm,))
            if include_inactive or r["kind"] != "keao_inactive"]
    # Με πετυχημένη λήψη οφειλών ΑΑΔΕ, ΑΥΤΗ είναι η εικόνα (έχει ήδη τις απλήρωτες δηλώσεις, π.χ. Φ2 με την ίδια
    # ταυτότητα)· οι χρεωστικές δηλώσεις του ελέγχου υποβολών δεν ξέρουν αν πληρώθηκαν — ζωντανά (2026-10-22) έβγαιναν
    # όλες οι πληρωμένες της χρονιάς. Χωρίς λήψη: μόνο του τελευταίου τριμήνου, ως ένδειξη.
    sync = sync_info(conn, afm)
    if sync and sync["status"] == "ok":
        return rows
    from datetime import date as _date
    cutoff = (_date.today().replace(day=1).toordinal() - 93)
    recent = [d for d in declaration_debts(conn, afm) if _period_ord(d) >= cutoff]
    return rows + recent


def _period_ord(d: dict[str, Any]) -> int:
    from datetime import date as _date
    try:
        return _date.fromisoformat(d.get("period_end") or "").toordinal()
    except ValueError:
        return 10 ** 9                                  # άγνωστη περίοδος: εμφανίζεται


def sync_info(conn: sqlite3.Connection, afm: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM debt_sync WHERE afm=?", (afm,)).fetchone()
    return dict(row) if row else None


def source_sync(conn: sqlite3.Connection, afm: str) -> dict[str, dict[str, Any]]:
    return {r["source"]: dict(r) for r in conn.execute("SELECT * FROM debt_source_sync WHERE afm=?", (afm,))}


def fingerprint(rows: list[dict[str, Any]]) -> str:
    """«Άλλαξε κάτι;» (Tax Center `Snapshot.fingerprint`): ποσά, κωδικοί, δόσεις — ΟΧΙ η ώρα λήψης."""
    import hashlib
    key = sorted((d.get("source", "aade"), d["kind"], d["ref"], round(d["total"] or 0, 2), round(d["overdue"] or 0, 2),
                  d["code"], d.get("unpaid", 0), d.get("next_due", "")) for d in rows if d["kind"] != "keao_inactive")
    return hashlib.sha256(repr(key).encode()).hexdigest()[:24]
