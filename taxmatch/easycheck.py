"""Έλεγχος ΟΛΟΚΛΗΡΩΣΗΣ υποχρεώσεων απευθείας από την ΑΑΔΕ («EasyCheck»): έχει υποβληθεί η Φ2 του τριμήνου; η ΦΜΥ του
μήνα; η δήλωση εισοδήματος; (ρητό αίτημα χρήστη 2026-10-22 — «αν δεν έχουμε, πάρ' το από τους φακέλους»).

Πιστό port του `recerse-engineer/runner/lib/easycheck-obligations.js` (ίδιο αρχείο είναι vendored και στο ScanMyData
Tax Center), που με τη σειρά του αναπαράγει το `Hyper.Server.Tax Easy_Aade.GetGeneralType1Year`:

* `GET /taxisnet/{category}/protected/displayLiabilitiesForYear.htm?declarationType={filter}&year={Y}` → γραμμές
  `tr.tblRow1|2`· η στήλη κατάστασης γράφει «Έχει Υποβληθεί Δήλωση» / «Υπάρχει δήλωση σε εκκρεμότητα» / «Δεν …».
  «Δεν έχετε υποχρεώσεις υποβολής…» = δεν είναι υπόχρεος.
* Λογαριασμός λογιστικού γραφείου → «για τον εαυτό μου» (`AccountantSetAsMySelf`).
* Υποκατηγορίες ΦΜΥ (μισθωτών/επιχ./μερίσματα…): drill-down `displayDeclarationsList.htm`, «Οριστική» με τον τύπο.
* Ε1/Ε3 φυσικών: άλλος μηχανισμός — `webtax/incomefp/year{Y}-income-menu.do`, υποβλήθηκε ⇔ «E1-<id> / <ημ/νία>».

**ΜΟΝΟ ανάγνωση** — ποτέ υποβολή. Χρειάζεται τους κωδικούς TAXISnet ΤΟΥ ΠΕΛΑΤΗ (η ΑΑΔΕ δείχνει τις υποχρεώσεις του
ΑΦΜ που συνδέθηκε). Αποτελέσματα στον πίνακα `obligation_checks` (db v11).
**ΔΕΝ έχει δοκιμαστεί ζωντανά από την εφαρμογή** — tests μόνο σε ψεύτικο HTTP με τη δομή HTML του runner.
"""
from __future__ import annotations

import calendar
import html as _html
import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode, urljoin

from . import db
from .business_profiles import credentials, lookup_aade

log = logging.getLogger(__name__)
AADE = lookup_aade.AADE


@dataclass(frozen=True)
class Obligation:
    key: str
    label: str
    category: str = ""
    filter: str = ""
    period_col: int = 0
    status_col: int = 2
    button_col: int = 3
    extra: str = ""
    search_type: str = ""
    kind: str = "liability"                 # liability | income
    code_prefix: str = ""
    requires_date: bool = False


_FMY = "&periodType=oneMonth&typeBtn=Μήνας"
OBLIGATIONS: dict[str, Obligation] = {o.key: o for o in (
    Obligation("Φ2", "ΦΠΑ Φ2 (περιοδική)", "vat", "vatF2"),
    Obligation("Φ4", "ΦΠΑ Φ4 (ανακεφαλαιωτικός VIES)", "vat", "vatF4"),
    Obligation("Φ5", "ΦΠΑ Φ5 (ανακεφαλαιωτικός αποκτήσεων)", "vat", "vatF5"),
    Obligation("ΕΙΣΟΔΗΜΑ_ΝΠ", "Δήλωση φορολογίας εισοδήματος Ν.Π. (Ν)", "income", "incomeN"),
    Obligation("ΜΥΦ", "Συγκεντρωτικές καταστάσεις (ΜΥΦ)", "kvs", "kvsD13"),
    Obligation("E1", "Δήλωση φορολογίας εισοδήματος (Ε1)", kind="income", code_prefix="E1-", requires_date=True),
    Obligation("E3", "Κατάσταση οικονομικών στοιχείων (Ε3)", kind="income", code_prefix="E3-"),
    Obligation("ΠΕΡΙΒΑΛΛΟΝ", "Περιβαλλοντικό τέλος πλαστικής σακούλας (H7)", "other", "otherAppH7"),
    Obligation("ΔΙΑΜΟΝΗ", "Τέλος ανθεκτικότητας / διαμονής (H6)", "other", "otherAppH6"),
    Obligation("ΣΥΜΦΩΝΗΤΙΚΑ", "Κατάσταση συμφωνητικών", "deduction", "deductAgreement", -1, 1, 2),
    Obligation("ΕΡΓΟΛΑΒΩΝ", "Φόρος εργολάβων", "deduction", "deductContractor", -1, 1, 2),
    Obligation("ΦΜΥ", "Παρακρατούμενοι ΦΜΥ μισθωτών", "deduction", "deductFMYTemporary", -1, 1, 2, _FMY, "( ΦΜΥ )"),
    Obligation("ΦΜΥ_ΕΠΙΧ", "ΦΜΥ αμοιβών επιχειρηματικής δραστηριότητας", "deduction", "deductFMYTemporary", -1, 1, 2, _FMY,
               "σε αμοιβές από Επιχειρηματική Δραστηριότητα )"),
    Obligation("ΦΜΥ_ΜΕΡΙΣΜΑΤΑ", "Παρακρατούμενοι μερισμάτων", "deduction", "deductFMYTemporary", -1, 1, 2, _FMY,
               "( Μερίσματα )"),
    # Ίδιος μηχανισμός (TaxSystem: EarnCert_Dikaiwmata / τόκοι) — για παρακρατήσεις που βρίσκονται στο myDATA (κατ. 1/2)
    Obligation("ΦΜΥ_ΔΙΚΑΙΩΜΑΤΑ", "Παρακρατούμενοι δικαιωμάτων", "deduction", "deductFMYTemporary", -1, 1, 2, _FMY,
               "( Δικαιώματα )"),
    Obligation("ΦΜΥ_ΤΟΚΟΙ", "Παρακρατούμενοι τόκων", "deduction", "deductFMYTemporary", -1, 1, 2, _FMY, "( Τόκοι )"),
)}

STATUS_LABEL = {"submitted": "Υποβλήθηκε", "pending": "Σε εκκρεμότητα", "not_submitted": "ΔΕΝ υποβλήθηκε",
                "not_obligated": "Δεν είναι υπόχρεος", "no_data": "Χωρίς στοιχεία", "error": "Σφάλμα"}

_ROW = re.compile(r'<tr\b[^>]*class="tblRow[12]"[^>]*>(.*?)</tr>', re.S | re.I)
_CELL = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_STATUS_RE = re.compile(r"Έχει Υποβληθεί Δήλωση|Έχουν Υποβληθεί Δηλώσεις|Υπάρχει δήλωση σε εκκρεμότητα|σε εκκρεμότητα"
                        r"|Δεν έχει υποβληθεί δήλωση|Δεν έχουν υποβληθεί")
_PERIOD_RE = re.compile(r"(Δεκαπενθήμερο|Μήνας|Τρίμηνο|Εξάμηνο|Έτος|Ετήσια|\b\d{1,2}ο[ςυ]?\b|\b\d{4}\b)")
_NOT_OBLIGATED = "Δεν έχετε υποχρεώσεις υποβολής για το συγκεκριμένο έντυπο"


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def rows(page: str) -> list[list[str]]:
    return [_CELL.findall(r) for r in _ROW.findall(page or "")]


def classify(status_text: str) -> str:
    if re.search(r"Έχει Υποβληθεί Δήλωση|Έχουν Υποβληθεί Δηλώσεις", status_text):
        return "submitted"
    if re.search(r"Υπάρχει δήλωση σε εκκρεμότητα|σε εκκρεμότητα", status_text):
        return "pending"
    return "not_submitted"


def decl_list_args(button_cell: str) -> Optional[dict[str, str]]:
    """`doDisplayDeclarationsList(document.displayDeclarationsListForm,"type","year","pType","pStart","pEnd","eStart","eEnd")`"""
    m = re.search(r"doDisplayDeclarationsList\(document\.displayDeclarationsListForm,(.*?)\);", _html.unescape(button_cell or ""), re.S)
    if not m:
        return None
    parts = [x.replace('"', "").replace("'", "").strip() for x in m.group(1).split(",")]
    if len(parts) < 7:
        return None
    keys = ("declarationType", "year", "periodType", "periodStart", "periodEnd", "effectivePeriodStart", "effectivePeriodEnd")
    return dict(zip(keys, parts))


def _dmy(s: str) -> Optional[tuple[int, int, int]]:
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", s or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def period_from_args(args: Optional[dict[str, str]]) -> str:
    if not args:
        return ""
    if args["periodType"] == "oneMonth":
        d = _dmy(args["periodStart"])
        return f"{d[1]}ος Μήνας" if d else args["periodStart"]
    if args["periodType"] == "threeMonths":
        d = _dmy(args["effectivePeriodStart"])
        return f"{(d[1] - 1) // 3 + 1}ο Τρίμηνο" if d else args["effectivePeriodStart"]
    return args.get("periodStart", "")


def interpret_row(o: Obligation, cells: list[str]) -> tuple[str, str, str]:
    """(περίοδος, status, κείμενο κατάστασης) — πρώτα οι γνωστές στήλες, μετά σάρωση (ανθεκτικό σε αλλαγές σελίδας)."""
    texts = [strip_tags(c) for c in cells]
    status_text = texts[o.status_col] if o.status_col < len(texts) and _STATUS_RE.search(texts[o.status_col]) else ""
    if not status_text:
        status_text = next((t for t in texts if _STATUS_RE.search(t)), texts[o.status_col] if o.status_col < len(texts) else "")
    period = texts[o.period_col] if 0 <= o.period_col < len(texts) else ""
    if not period:
        period = period_from_args(decl_list_args(cells[o.button_col] if o.button_col < len(cells) else ""))
    if not period:
        period = next((t for t in texts if _PERIOD_RE.search(t) and not _STATUS_RE.search(t)), "")
    return period, classify(status_text), status_text


def income_submission(menu_text: str, o: Obligation, raw: str = "") -> tuple[bool, str]:
    p = re.escape(o.code_prefix)
    date_re = r"(\d{2}/\d{2}/\d{4}(?:\s+\d{2}:\d{2})?)"
    if o.requires_date:
        m = re.search(p + r"\d+\s*/\s*" + date_re, menu_text)
        if m:
            return True, m.group(1)
    else:
        m = re.search(p + r"\d+", menu_text)
        if m:
            d = re.search(date_re, menu_text[m.start():m.start() + 80])
            return True, d.group(1) if d else ""
    c = re.search(r"sCode\s*=\s*['\"]" + p + r"\d+['\"]", raw or "")         # παλιά διάταξη (<2019)
    if c:
        d = re.search(r"sDate\s*=\s*['\"]" + date_re, raw[c.start():c.start() + 140])
        return True, d.group(1) if d else ""
    return False, ""


# ------------------------------------------------------------------ μηχανή (με τη σύνδεση του lookup_aade)
class _Ctx:
    def __init__(self, http, afm: str = "", want_pdf: bool = False) -> None:
        self.http = http
        self.afm = afm                     # για την αποθήκευση PDF ταυτοτήτων οφειλής (κενό = χωρίς PDF)
        self.want_pdf = want_pdf           # και το PDF κάθε υποβληθείσας δήλωσης (viewPdf.htm) — επιλογή του χρήστη

    def get(self, url: str) -> str:
        return self.http.follow("GET", url)["text"]

    def post(self, url: str, form: dict[str, str]) -> str:
        return self.http.follow("POST", url, form)["text"]


def _set_as_myself(ctx: _Ctx, page: str, category: str) -> bool:
    if not re.search(r"searchTaxpayer|Επιλογή Λογιστικού Γραφείου", page or ""):
        return False
    t = ctx.post(f"{AADE}/taxisnet/{category}/protected/displayDeclarationTypes.htm",
                 {"actorRole": "SELF_SERVICE", "_eventId_chooseIndividual": "για τον εαυτό μου"})
    return "Καλωσήρθατε στην Υπηρεσία ηλεκτρονικής υποβολής δηλώσεων" in t


_STATE_CALL = re.compile(r'doDisplayDeclarationState\(document\.displayDeclarationStateForm,\s*"([^"]*)"\s*,\s*"([^"]*)"')
_DEBT_CALL = re.compile(
    r'doDisplayDebtCode\s*\([^,]*?,\s*(?:&quot;|[\'"])?([^\'",;&]+?)(?:&quot;|[\'"])?\s*,\s*(?:&quot;|[\'"])?([^\'",;&]+?)(?:&quot;|[\'"])?\s*,\s*(?:&quot;|[\'"])?(.*?)(?:&quot;|[\'"])?\s*\)',
    re.I,
)

def parse_debt_call(call_str: str) -> Optional[dict[str, str]]:
    """Αναλύει κλήση doDisplayDebtCode(frm, declType, dbId, returnView, [fmyType]) handling 3-5 ορίσματα."""
    m = re.search(r'doDisplayDebtCode\s*\((.*?)\)', call_str, re.S | re.I)
    if not m:
        return None
    raw = m.group(1)
    parts: list[str] = []
    for item in re.finditer(r"""(?:&quot;|['"])(.*?)(?:&quot;|['"])|([^,\s()]+)""", raw):
        token = (item.group(1) if item.group(1) is not None else item.group(2)).strip()
        if token.startswith("document.") or token == "this":
            continue
        parts.append(_html.unescape(token))
    if len(parts) >= 2:
        return {
            "decl_type": parts[0],
            "db_id": parts[1],
            "ret_view": parts[2] if len(parts) > 2 else "displayDebtCode",
            "fmy_type": parts[3] if len(parts) > 3 else "",
        }
    return None

#: Υποχρεώσεις για τις οποίες διαβάζονται και τα ΠΟΣΑ της δήλωσης (εκκαθάριση + ταυτότητα οφειλής). Φ2: πιστό port του
#: runner `lib/aade-vat-http.js` (επαληθευμένο εκεί). Οι υπόλοιπες ακολουθούν την ίδια δομή σελίδων της ΑΑΔΕ στη δική
#: τους κατηγορία — ΔΕΝ έχει επαληθευτεί· αν η σελίδα δεν έχει τα πεδία, απλώς δεν εμφανίζονται ποσά.
AMOUNT_KEYS = {"Φ2", "ΦΜΥ", "ΦΜΥ_ΕΠΙΧ", "ΕΡΓΟΛΑΒΩΝ", "ΦΜΥ_ΔΙΚΑΙΩΜΑΤΑ", "ΦΜΥ_ΤΟΚΟΙ", "ΦΜΥ_ΜΕΡΙΣΜΑΤΑ", "ΔΙΑΜΟΝΗ",
               "ΠΕΡΙΒΑΛΛΟΝ"}
_MONTH_ORD = re.compile(r"(\d{1,2})\s*ο[ςυ]?\s*Μήνας")
_QUARTER_ORD = re.compile(r"(\d)\s*ο\s*Τρίμηνο")


def to_num(s: str) -> Optional[float]:
    """Ελληνικό ποσό «2.217,42» -> 2217.42 (None αν λείπει)."""
    m = re.search(r"\d{1,3}(?:\.\d{3})*(?:,\d+)?|\d+(?:,\d+)?", s or "")
    if not m:
        return None
    try:
        return float(m.group(0).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def label_map(page: str) -> dict[str, str]:
    """label → τιμή από γραμμές πινάκων με ΔΥΟ μη κενά κελιά (κρατά την πρώτη εμφάνιση) — όπως το labelMap του runner."""
    out: dict[str, str] = {}
    for tr in re.findall(r"<tr\b[^>]*>(.*?)</tr>", page or "", re.S | re.I):
        cells = [c for c in (strip_tags(x) for x in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", tr, re.S | re.I)) if c]
        if len(cells) == 2:
            key = re.sub(r"[:：]\s*$", "", cells[0]).strip()
            if key and key not in out:
                out[key] = cells[1]
    return out


def _find(m: dict[str, str], pattern: str) -> str:
    rx = re.compile(pattern)
    return next((v for k, v in m.items() if rx.search(k)), "")


def _last_day(y: int, m: int) -> str:
    return f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def period_end(args: Optional[dict[str, str]], year: int, period_text: str = "") -> tuple[str, str]:
    """(ISO τελευταία ημέρα της περιόδου, 'month' | 'quarter' | '') — για αντιστοίχιση με τις προθεσμίες του Ημερολογίου
    (μήνας αναφοράς = μήνας της ημερομηνίας αυτής)."""
    if args:
        ptype = {"oneMonth": "month", "threeMonths": "quarter"}.get(args.get("periodType", ""), "")
        end = _dmy(args.get("effectivePeriodEnd", "")) or _dmy(args.get("periodEnd", ""))
        if end:
            return _last_day(end[2], end[1]), ptype
        start = _dmy(args.get("effectivePeriodStart", "")) or _dmy(args.get("periodStart", ""))
        if start:
            m = start[1] + (2 if ptype == "quarter" else 0)
            return _last_day(start[2] + (m - 1) // 12, (m - 1) % 12 + 1), ptype
    mm = _MONTH_ORD.search(period_text or "")
    if mm and 1 <= int(mm.group(1)) <= 12:
        return _last_day(year, int(mm.group(1))), "month"
    q = _QUARTER_ORD.search(period_text or "")
    if q and 1 <= int(q.group(1)) <= 4:
        return _last_day(year, int(q.group(1)) * 3), "quarter"
    return "", ""


def _declarations(ctx: _Ctx, o: Obligation, args: dict[str, str]) -> Optional[list[dict[str, str]]]:
    """Οι δηλώσεις μιας περιόδου (`displayDeclarationsList`): αρ. καταχώρισης, ημ/νία, τύπος, Οριστική. None = άγνωστο."""
    try:
        page = ctx.get(f"{AADE}/taxisnet/{o.category}/protected/displayDeclarationsList.htm?{urlencode(args)}")
    except Exception:                                   # noqa: BLE001 — άγνωστο, όχι «δεν υποβλήθηκε»
        return None
    out = []
    for row in _ROW.findall(page or ""):
        texts = [strip_tags(c) for c in _CELL.findall(row)]
        raw_un = _html.unescape(row)
        call = _STATE_CALL.search(raw_un)
        debt_info = parse_debt_call(row) or parse_debt_call(raw_un)
        db_id = (call.group(2) if call else (debt_info["db_id"] if debt_info else (texts[2] if len(texts) > 2 else ""))).strip()
        res_code = texts[6].strip().upper() if len(texts) > 6 else ""
        result = "Χρεωστική" if (res_code in ("Χ", "X") or "ΧΡΕΩΣ" in res_code or debt_info) else (
            "Πιστωτική" if (res_code in ("Π", "P") or "ΠΙΣΤ" in res_code) else (
                "Μηδενική" if (res_code in ("Μ", "M") or "ΜΗΔ" in res_code) else ""
            )
        )
        out.append({"db_id": db_id,
                    "submitted_at": texts[3] if len(texts) > 3 else "", "type": texts[4] if len(texts) > 4 else "",
                    "final": texts[5] if len(texts) > 5 else "", "result": result,
                    "has_debt_code": bool(debt_info) or (result == "Χρεωστική"),
                    "debt_info": debt_info})
    return out


def fetch_debt_code_page(ctx: _Ctx, base_url: str, decl_type: str, db_id: str, ret_view: str, fmy_type: str = "") -> str:
    """Ανάκτηση της σελίδας displayDebtCode.htm μέσω GET (όπως στον browser / URL TAXISnet) ή fallback POST."""
    target_url = urljoin(base_url, "displayDebtCode.htm")
    data = {"declarationType": decl_type, "declarationDatabaseId": db_id, "returnView": ret_view}
    if fmy_type:
        data["fmyTempDeclType"] = fmy_type
    try:
        q = urlencode(data)
        txt = ctx.get(f"{target_url}?{q}")
        if txt and ("Ταυτότητα" in txt or "Συνολικό" in txt or "debtCode" in txt or "displayDebtCode" in txt):
            return txt
    except Exception as exc:
        log.debug("GET displayDebtCode failed: %s", exc)
        txt = ""
    if hasattr(ctx.http, "follow"):
        try:
            res = ctx.http.follow("POST", target_url, data)
            txt2 = res.get("text", "")
            if txt2 and ("Ταυτότητα" in txt2 or "Συνολικό" in txt2 or "debtCode" in txt2 or "displayDebtCode" in txt2):
                return txt2
        except Exception as exc:
            log.debug("POST displayDebtCode failed: %s", exc)
    return txt


def declaration_amounts(ctx: _Ctx, o: Obligation, db_id: str, ret_view: str,
                        debt_info: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Εκκαθάριση + ταυτότητα οφειλής μιας δήλωσης (port του `declarationAmounts` του runner). Μόνο ανάγνωση."""
    out: dict[str, Any] = {}
    base = f"{AADE}/taxisnet/{o.category}/protected/"
    q = f"declarationType={o.filter}&declarationDatabaseId={db_id}&returnView={quote(ret_view)}"
    try:
        m = label_map(ctx.get(base + "displayDeclarationState.htm?" + q))
        res = _find(m, r"Αποτέλεσμα")
        if res:
            out["result"] = ("Χρεωστική" if "Χρεωστικ" in res else "Πιστωτική" if "Πιστωτικ" in res
                             else "Μηδενική" if "Μηδενικ" in res else res)
        pay = _find(m, r"Δυνατότητες Πληρωμής")
        lump = re.search(r"Εφάπαξ ποσό:\s*([\d.,]+)", pay)
        if lump:
            out["lump_sum"] = to_num(lump.group(1))
        first = re.search(r"Ποσό 1ης Δόσης:\s*([\d.,]+)", pay)
        if first:
            out["first_installment"] = to_num(first.group(1))
        for key, pattern in (("deduction", r"Ποσό για Έκπτωση"), ("refund", r"Ποσό για Επιστροφή")):
            value = to_num(_find(m, pattern))
            if value is not None:
                out[key] = value
        state = _find(m, r"Τρέχουσα Κατάσταση")
        if state:
            out["state"] = state
    except Exception as exc:                            # noqa: BLE001 — χωρίς ποσά, όχι αποτυχία ελέγχου
        log.info("easycheck declarationState %s: %s", o.key, exc)
    try:
        decl_type = (debt_info or {}).get("decl_type") or o.filter
        d_id = (debt_info or {}).get("db_id") or db_id
        r_view = (debt_info or {}).get("ret_view") or ret_view
        fmy_t = (debt_info or {}).get("fmy_type") or ""
        page = fetch_debt_code_page(ctx, base, decl_type, d_id, r_view, fmy_type=fmy_t)
        if re.search(r"Ταυτότητα\s*Οφειλής|Συνολικό\s*ποσό\s*οφειλής", page or "", re.I):
            m = label_map(page)
            total = to_num(_find(m, r"Συνολικό ποσό οφειλής"))
            if total is not None:
                out["total_due"] = total
            debt = re.search(r"[\d ]{10,}", _find(m, r"Ταυτότητα Οφειλής"))
            if not debt and page:
                debt = re.search(r"Ταυτότητα\s*Οφειλής[^0-9<]{0,50}([\d\s]{12,35})", page, re.I)
            if debt:
                out["debt_id"] = debt.group(0 if not hasattr(debt, "group") else (1 if debt.lastindex else 0)).strip()
            dose = next((k for k in m if re.search(r"Ποσό δόσης", k)), "")
            if dose:
                out["installment"] = to_num(m[dose])
                when = re.search(r"(\d{2}/\d{2}/\d{4})", dose)
                if when:
                    out["pay_by"] = when.group(1)
            for key, pattern in (("doy", r"^ΔΟΥ$|Δ\.?Ο\.?Υ"), ("tax_kind", r"Είδος Φόρου"), ("issued_at", r"Ημ/νία Έκδοσης")):
                value = _find(m, pattern)
                if value:
                    out[key] = value
            if ctx.afm:
                out["pdf"] = _debt_pdf(ctx, base + "displayDebtCode.htm?" + q, page, o.key, d_id, out, decl_type=decl_type)
                out["pdf_checked"] = True              # δεν ξαναζητείται σε κάθε έλεγχο αν η σελίδα δεν έχει εκτύπωση
    except Exception as exc:                            # noqa: BLE001
        log.info("easycheck debtCode %s: %s", o.key, exc)
    return out


def _debt_voucher_pdf(debt_id: str, total_due: str, period: str, doy: str, tax_kind: str,
                      installment: str, pay_by: str, afm: str, name: str, issued_at: str) -> bytes:
    """Παραγωγή του επίσημου σημειώματος πληρωμής TAXISnet (πιστό αντίγραφο του original ΑΑΔΕ PDF)."""
    import os
    import sys
    import tempfile
    import subprocess

    dose_row = f'<tr><td class="lbl">Ποσό δόσης δήλωσης της {pay_by}</td><td class="val">{installment}</td></tr>' if installment else ""
    inst_note = (f'Θα πρέπει να πληρώσετε το ποσό της 1ης δόσης μέχρι τις <b>{pay_by}</b>. Η καθυστέρηση πληρωμής '
                 f'του οφειλόμενου ποσού συνεπάγεται τις νόμιμες επιβαρύνσεις (Ν. 4174/13, αρθρ. 53 & 57).'
                 if pay_by else 'Η καθυστέρηση πληρωμής του οφειλόμενου ποσού συνεπάγεται τις νόμιμες επιβαρύνσεις (Ν. 4174/13, αρθρ. 53 & 57).')

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
    @page {{
        size: A4 portrait;
        margin: 20mm 20mm 20mm 20mm;
    }}
    body {{
        font-family: Arial, "Helvetica Neue", Helvetica, sans-serif;
        color: #000000;
        margin: 0;
        padding: 0;
        font-size: 13.5px;
        line-height: 1.4;
    }}
    .header-main {{
        font-size: 20px;
        font-weight: bold;
        margin-bottom: 3px;
    }}
    .header-sub {{
        font-size: 16px;
        font-weight: bold;
        margin-bottom: 25px;
    }}
    .sec-head {{
        font-size: 14.5px;
        font-weight: bold;
        margin-top: 18px;
        margin-bottom: 8px;
    }}
    table.data-tbl {{
        width: 100%;
        border-collapse: collapse;
        margin-bottom: 12px;
    }}
    table.data-tbl td {{
        padding: 3px 0;
        vertical-align: top;
        font-size: 13px;
    }}
    table.data-tbl td.lbl {{
        width: 38%;
        color: #111;
        font-weight: normal;
    }}
    table.data-tbl td.val {{
        width: 62%;
        color: #000;
        font-weight: bold;
    }}
    .debt-code-box {{
        font-family: "Courier New", Courier, monospace;
        font-size: 15px;
        font-weight: bold;
        letter-spacing: 1px;
    }}
    .warn-title {{
        font-size: 13.5px;
        font-weight: bold;
        margin-top: 25px;
        margin-bottom: 6px;
    }}
    .warn-body {{
        font-size: 12px;
        line-height: 1.5;
        color: #111;
    }}
</style>
</head>
<body>
    <div class="header-main">TAXISnet - Υπηρεσίες Πληρωμής</div>
    <div class="header-sub">Σημείωμα για Πληρωμή</div>

    <div class="sec-head">Στοιχεία Οφειλέτη</div>
    <table class="data-tbl">
        <tr><td class="lbl">Α.Φ.Μ.</td><td class="val">{afm}</td></tr>
        <tr><td class="lbl">Ονοματεπώνυμο/ Επωνυμία</td><td class="val">{name}</td></tr>
    </table>

    <div class="sec-head">Στοιχεία Οφειλής</div>
    <table class="data-tbl">
        <tr><td class="lbl">Τύπος Πληρωμής</td><td class="val">Πληρωμή Βεβαιωμένων Οφειλών εκτός Ρύθμισης</td></tr>
        <tr><td class="lbl">ΔΟΥ</td><td class="val">{doy}</td></tr>
        <tr><td class="lbl">Είδος Φόρου</td><td class="val">{tax_kind}</td></tr>
        <tr><td class="lbl">Ημερολογιακή Περίοδος</td><td class="val">{period}</td></tr>
        <tr><td class="lbl">Συνολικό ποσό οφειλής δήλωσης</td><td class="val">{total_due}</td></tr>
        {dose_row}
        <tr><td class="lbl">Ταυτότητα Οφειλής</td><td class="val debt-code-box">{debt_id}</td></tr>
        <tr><td class="lbl">Ημ/νία Έκδοσης</td><td class="val">{issued_at}</td></tr>
    </table>

    <div class="warn-title">Προσοχή:</div>
    <div class="warn-body">
        Η «Ταυτότητα Οφειλής» ακολουθεί την οφειλή μέχρι την εξόφλησή της. Μπορείτε να πληρώνετε μέρος ή το σύνολο αυτής, κάνοντας χρήση των τρόπων πληρωμής που διαθέτουν οι συνεργαζόμενες με το Υπουργείο Οικονομικών τράπεζες και τα ΕΛ.ΤΑ.<br><br>
        {inst_note}
    </div>
</body>
</html>"""

    # 1. Προσπάθεια παραγωγής original Chromium/Skia PDF μέσω headless Edge/Chrome
    browser_exes = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    browser = next((b for b in browser_exes if os.path.exists(b)), None)
    if browser:
        tmp_html = ""
        tmp_pdf = ""
        try:
            with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as tf:
                tf.write(html)
                tmp_html = tf.name
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
                tmp_pdf = tf.name
            cmd = [browser, "--headless", "--disable-gpu", f"--print-to-pdf={tmp_pdf}", tmp_html]
            res = subprocess.run(cmd, capture_output=True, timeout=15)
            if res.returncode == 0 and os.path.exists(tmp_pdf) and os.path.getsize(tmp_pdf) > 1000:
                with open(tmp_pdf, "rb") as f:
                    return f.read()
        except Exception as exc:
            log.debug("Headless browser print-to-pdf failed: %s", exc)
        finally:
            for p_file in (tmp_html, tmp_pdf):
                if p_file and os.path.exists(p_file):
                    try:
                        os.remove(p_file)
                    except Exception:
                        pass

    # 2. Fallback: PySide6 QPainter PDF rendering με ακριβείς στήλες
    try:
        from PySide6.QtWidgets import QApplication
        from PySide6.QtGui import QPdfWriter, QPageSize, QPageLayout, QPainter, QFont, QColor
        from PySide6.QtCore import QMarginsF, QRectF, Qt

        app = QApplication.instance()
        if app is None:
            app = QApplication(sys.argv)

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tmp_path = tf.name

        try:
            writer = QPdfWriter(tmp_path)
            writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
            writer.setPageMargins(QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter)
            writer.setResolution(72)

            painter = QPainter(writer)
            fn_sans = "Segoe UI"
            f_title = QFont(fn_sans, 15, QFont.Weight.Bold)
            f_sub = QFont(fn_sans, 13, QFont.Weight.Bold)
            f_sec = QFont(fn_sans, 11, QFont.Weight.Bold)
            f_lbl = QFont(fn_sans, 9.5, QFont.Weight.Normal)
            f_val = QFont(fn_sans, 9.5, QFont.Weight.Bold)
            f_mono = QFont("Courier New", 10.5, QFont.Weight.Bold)
            f_warn_t = QFont(fn_sans, 10, QFont.Weight.Bold)
            f_warn_b = QFont(fn_sans, 9, QFont.Weight.Normal)

            x_left = 50
            x_val = 220
            y = 55

            painter.setPen(QColor("#000000"))
            painter.setFont(f_title)
            painter.drawText(x_left, y, "TAXISnet - Υπηρεσίες Πληρωμής")
            y += 24
            painter.setFont(f_sub)
            painter.drawText(x_left, y, "Σημείωμα για Πληρωμή")
            y += 35

            painter.setFont(f_sec)
            painter.drawText(x_left, y, "Στοιχεία Οφειλέτη")
            y += 20

            for lbl_t, val_t, is_m in [("Α.Φ.Μ.", afm, False), ("Ονοματεπώνυμο/ Επωνυμία", name, False)]:
                painter.setFont(f_lbl)
                painter.drawText(x_left, y, lbl_t)
                painter.setFont(f_mono if is_m else f_val)
                painter.drawText(x_val, y, val_t)
                y += 18

            y += 15
            painter.setFont(f_sec)
            painter.drawText(x_left, y, "Στοιχεία Οφειλής")
            y += 20

            rows_to = [
                ("Τύπος Πληρωμής", "Πληρωμή Βεβαιωμένων Οφειλών εκτός Ρύθμισης", False),
                ("ΔΟΥ", doy, False),
                ("Είδος Φόρου", tax_kind, False),
                ("Ημερολογιακή Περίοδος", period, False),
                ("Συνολικό ποσό οφειλής δήλωσης", total_due, False),
            ]
            if installment and pay_by:
                rows_to.append((f"Ποσό δόσης δήλωσης της {pay_by}", installment, False))
            rows_to += [("Ταυτότητα Οφειλής", debt_id, True), ("Ημ/νία Έκδοσης", issued_at, False)]

            for lbl_t, val_t, is_m in rows_to:
                painter.setFont(f_lbl)
                painter.drawText(x_left, y, lbl_t)
                painter.setFont(f_mono if is_m else f_val)
                painter.drawText(x_val, y, val_t)
                y += 18

            y += 25
            painter.setFont(f_warn_t)
            painter.drawText(x_left, y, "Προσοχή:")
            y += 16
            w_text = (
                "Η «Ταυτότητα Οφειλής» ακολουθεί την οφειλή μέχρι την εξόφλησή της. Μπορείτε να πληρώνετε "
                "μέρος ή το σύνολο αυτής, κάνοντας χρήση των τρόπων πληρωμής που διαθέτουν οι "
                "συνεργαζόμενες με το Υπουργείο Οικονομικών τράπεζες και τα ΕΛ.ΤΑ.\n\n"
                f"{inst_note.replace('<b>', '').replace('</b>', '')}"
            )
            painter.setFont(f_warn_b)
            rect = QRectF(x_left, y, 480, 200)
            painter.drawText(rect, Qt.TextWordWrap, w_text)
            painter.end()
            del writer

            with open(tmp_path, "rb") as f:
                return f.read()
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass
    except Exception as exc:
        log.warning("_debt_voucher_pdf failed: %s", exc)
        return b""

def _debt_pdf(ctx: _Ctx, url: str, page: str, key: str, db_id: str, out: Optional[dict[str, Any]] = None,
              decl_type: str = "", dest_path: Optional[Path | str] = None) -> str:
    """PDF της ταυτότητας οφειλής μιας χρεωστικής δήλωσης — λήψη του επίσημου PDF από την ΑΑΔΕ
    (κουμπί «Εκτύπωση» / debtCodePdf.htm / print_form), ή παραγωγή επίσημου δελτίου πληρωμής
    αν η σελίδα της ΑΑΔΕ είναι HTML (window.print)."""
    from . import debts
    if not ctx.afm:
        return ""

    pdf_content = b""

    # 1. Προσπάθεια μέσω print_form (doViewPdf, doViewArrPdf, doPrint, κ.λπ.)
    if hasattr(ctx.http, "follow"):
        for fn in ("doViewPdf", "doViewArrPdf", "doPrint", "doPrintDebtCode"):
            pf = debts.print_form(page, fn)
            if pf:
                try:
                    res = ctx.http.follow("POST", urljoin(url, pf["action"]), pf["fields"])
                    if res.get("is_pdf") or (res.get("content") or b"")[:4] == b"%PDF":
                        pdf_content = res["content"]
                        break
                except Exception as exc:
                    log.debug("print_form debt PDF failed (%s): %s", fn, exc)

        # 2. Αναζήτηση απευθείας φόρμας εκτύπωσης / action μέσα στο page (π.χ. debtCodePdf.htm, viewPdf.htm)
        if not pdf_content:
            m_action = re.search(r'''action=['"]([^'"]*(?:debtCodePdf|debtInfoPdf|viewPdf|print)[^'"]*)['"]''', page or "", re.I)
            if m_action:
                action_url = urljoin(url, m_action.group(1))
                form_fields: dict[str, str] = {"declarationDatabaseId": db_id}
                if decl_type:
                    form_fields["declarationType"] = decl_type
                for m_inp in re.finditer(r'''<input[^>]*name=['"]([^'"]+)['"][^>]*value=['"]([^'"]*)['"]''', page or "", re.I):
                    form_fields[m_inp.group(1)] = m_inp.group(2)
                try:
                    res = ctx.http.follow("POST", action_url, form_fields)
                    if res.get("is_pdf") or (res.get("content") or b"")[:4] == b"%PDF":
                        pdf_content = res["content"]
                except Exception as exc:
                    log.debug("form action debt PDF failed: %s", exc)

        # 3. Απευθείας λήψη του επίσημου PDF από το endpoint debtCodePdf.htm / debtInfoPdf.htm
        if not pdf_content:
            base_folder = url.rsplit("/", 1)[0] + "/"
            for target in ("debtCodePdf.htm", "debtInfoPdf.htm"):
                target_url = urljoin(base_folder, target)
                attempts = []
                if decl_type:
                    attempts.append({"declarationDatabaseId": db_id, "declarationType": decl_type})
                attempts.append({"declarationDatabaseId": db_id})
                for post_data in attempts:
                    try:
                        res = ctx.http.follow("POST", target_url, post_data)
                        if res.get("is_pdf") or (res.get("content") or b"")[:4] == b"%PDF":
                            pdf_content = res["content"]
                            break
                    except Exception as exc:
                        log.debug("direct %s failed: %s", target, exc)
                if pdf_content:
                    break

    # 4. Fallback: Αν η ΑΑΔΕ επιστρέφει σελίδα HTML (όπως στο Φ2 displayDebtCode.htm που έχει window.print),
    # παράγουμε το επίσημο δελτίο πληρωμής της ταυτότητας οφειλής
    if not pdf_content:
        m = label_map(page) if page else {}
        debt_id = (out or {}).get("debt_id") or ""
        if not debt_id and page:
            m_id = re.search(r"[\d ]{10,}", _find(m, r"Ταυτότητα Οφειλής"))
            if not m_id:
                m_id = re.search(r"Ταυτότητα\s*Οφειλής[^0-9<]{0,50}([\d\s]{12,35})", page, re.I)
            debt_id = m_id.group(0 if not hasattr(m_id, "group") else (1 if m_id.lastindex else 0)).strip() if m_id else ""
        if not debt_id:
            return ""

        doy = (out or {}).get("doy") or _find(m, r"^ΔΟΥ$|Δ\.?Ο\.?Υ")
        period = (out or {}).get("period") or _find(m, r"Ημερολογιακή Περίοδος")
        tax_kind = (out or {}).get("tax_kind") or _find(m, r"Είδος Φόρου") or key
        issued_at = (out or {}).get("issued_at") or _find(m, r"Ημ/νία Έκδοσης") or ""
        total = (out or {}).get("total_due")
        total_str = f"{total:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if total is not None else (_find(m, r"Συνολικό ποσό οφειλής") or "")
        inst = (out or {}).get("installment")
        inst_str = f"{inst:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".") if inst is not None else ""
        if not inst_str and page:
            inst_str = _find(m, r"Ποσό\s*δόσης") or ""
        pay_by = (out or {}).get("pay_by") or ""
        if not pay_by and page:
            m_pb = re.search(r"Ποσό\s*δόσης.*?(?:της|έως|μέχρι)\s*(\d{1,2}/\d{1,2}/\d{4})", page, re.I) or re.search(r"μέχρι\s*τις\s*(\d{1,2}/\d{1,2}/\d{4})", page, re.I)
            if m_pb:
                pay_by = m_pb.group(1)

        name = _find(m, r"Ονοματεπώνυμο|Επωνυμία")
        if (not name or not doy) and ctx.afm and hasattr(ctx, "conn") and ctx.conn:
            b_row = ctx.conn.execute("SELECT name, trade_name, doy FROM businesses WHERE afm=?", (ctx.afm,)).fetchone()
            if b_row:
                if not name:
                    name = b_row["name"] or b_row["trade_name"] or ""
                if not doy:
                    doy = b_row["doy"] or ""

        pdf_content = _debt_voucher_pdf(debt_id, total_str, period, doy, tax_kind, inst_str, pay_by, ctx.afm, name, issued_at)

    if not pdf_content:
        return ""

    filename = f"TO_{debts._safe(key)}_{debts._safe(db_id)}.pdf"
    if dest_path:
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(pdf_content)
        try:
            debts._save_pdf(ctx.afm, filename, pdf_content)  # noqa: SLF001
        except Exception:
            pass
        return str(p)

    return debts._save_pdf(ctx.afm, filename, pdf_content)  # noqa: SLF001


def _period_details(ctx: _Ctx, o: Obligation, args: dict[str, str], known: Optional[dict] = None
                    ) -> tuple[Optional[bool], dict[str, Any]]:
    """(υποβλήθηκε οριστική του ζητούμενου τύπου — None αν δεν φιλτράρεται τύπος· ποσά της ισχύουσας δήλωσης).
    `known`: ποσά που έχουν ήδη διαβαστεί για την ΙΔΙΑ δήλωση (ίδιος αριθμός καταχώρισης) — δεν ξαναζητούνται."""
    decls = _declarations(ctx, o, args)
    if decls is None:
        return None, {}
    mine = [d for d in decls if not o.search_type or o.search_type in d["type"]]
    finals = [d for d in mine if d["final"] == "Οριστική"]
    submitted = bool(finals) if o.search_type else None
    effective = (finals or mine or [None])[-1]           # τροποποιητική υπερισχύει της αρχικής (τελευταία οριστική)
    if not effective or not effective["db_id"]:
        return submitted, {}
    ret_view_path = "displayDeclarationsList.htm?" + urlencode(args)
    eff_result = effective.get("result") or (known or {}).get("result")
    is_debt = bool(effective.get("has_debt_code") or eff_result == "Χρεωστική")

    if o.key not in AMOUNT_KEYS and not is_debt:
        amounts: dict[str, Any] = {}
    elif known and known.get("registration_no") == effective["db_id"] and (
            not is_debt or not ctx.afm
            or (known.get("pdf") and Path(known["pdf"]).exists())
            or (known.get("pdf_checked") and not ctx.want_pdf)):
        amounts = dict(known)
        if eff_result and not amounts.get("result"):
            amounts["result"] = eff_result
    else:
        amounts = declaration_amounts(ctx, o, effective["db_id"], ret_view_path, debt_info=effective.get("debt_info"))
        amounts.update(registration_no=effective["db_id"], submitted_at=effective["submitted_at"], amended=len(finals) > 1)
        if eff_result and not amounts.get("result"):
            amounts["result"] = eff_result

    # Όταν κατεβαίνει το έντυπο (ή όταν ζητούνται PDF): κατεβαίνει ΚΑΙ το έντυπο ΚΑΙ η ταυτότητα οφειλής (αν είναι χρεωστική / υπάρχει κουμπί Τ.Ο.)
    if ctx.want_pdf and ctx.afm and effective["final"] == "Οριστική":
        from . import config
        year = (args.get("year") or "")[:4] or str(date.today().year)
        start = re.sub(r"\D", "", args.get("periodStart") or "")[:8] or effective["db_id"]
        sub_folder = config.data_dir() / "submissions" / ctx.afm / year
        sub_folder.mkdir(parents=True, exist_ok=True)
        to_path = sub_folder / f"TO_{re.sub(r'[^0-9A-Za-zΑ-Ωα-ω_]', '_', o.key)}_{ctx.afm}_{start}_{effective['db_id']}.pdf"

        # 1. Έντυπο δήλωσης (submission_pdf)
        old = (known or {}).get("submission_pdf", "")
        if old and Path(old).exists() and (known or {}).get("registration_no") == effective["db_id"]:
            amounts["submission_pdf"] = old
        else:
            pdf = submission_pdf(ctx, o, effective["db_id"], args)
            if pdf:
                amounts["submission_pdf"] = pdf

        # 2. Ταυτότητα Οφειλής (κουμπί doDisplayDebtCode ή χρεωστική δήλωση)
        has_debt = bool(effective.get("has_debt_code") or amounts.get("result") == "Χρεωστική" or effective.get("result") == "Χρεωστική" or amounts.get("debt_id"))
        if has_debt:
            if to_path.exists():
                amounts["pdf"] = str(to_path)
                amounts["pdf_checked"] = True
            else:
                old_to = amounts.get("pdf") or (known or {}).get("pdf", "")
                if old_to and Path(old_to).exists() and (known or {}).get("registration_no") == effective["db_id"]:
                    amounts["pdf"] = old_to
                else:
                    d_info = effective.get("debt_info") or {}
                    decl_t = d_info.get("decl_type") or o.filter
                    d_id = d_info.get("db_id") or effective["db_id"]
                    r_view = d_info.get("ret_view") or ret_view_path
                    fmy_t = d_info.get("fmy_type") or ""
                    base = f"{AADE}/taxisnet/{o.category}/protected/"
                    try:
                        debt_page = fetch_debt_code_page(ctx, base, decl_t, d_id, r_view, fmy_type=fmy_t)
                        if debt_page:
                            m_debt = label_map(debt_page)
                            if not amounts.get("total_due"):
                                tot = to_num(_find(m_debt, r"Συνολικό ποσό οφειλής"))
                                if tot is not None:
                                    amounts["total_due"] = tot
                            if not amounts.get("debt_id"):
                                m_did = re.search(r"[\d ]{10,}", _find(m_debt, r"Ταυτότητα Οφειλής"))
                                if not m_did:
                                    m_did = re.search(r"Ταυτότητα\s*Οφειλής[^0-9<]{0,50}([\d\s]{12,35})", debt_page, re.I)
                                if m_did:
                                    amounts["debt_id"] = m_did.group(0 if not hasattr(m_did, "group") else (1 if m_did.lastindex else 0)).strip()
                            to_pdf = _debt_pdf(ctx, base + "displayDebtCode.htm", debt_page, o.key, d_id, amounts, decl_type=decl_t, dest_path=to_path)
                            if to_pdf:
                                amounts["pdf"] = to_pdf
                                amounts["pdf_checked"] = True
                    except Exception as exc:
                        log.warning("failed fetching debt code pdf for %s/%s: %s", ctx.afm, o.key, exc)

        # 3. Ενοποίηση της λίστας submission_pdfs ώστε το UI να περιέχει και τα δύο αρχεία
        all_pdfs = [f for f in [amounts.get("submission_pdf"), amounts.get("pdf")] if f and Path(f).exists()]
        if all_pdfs:
            amounts["submission_pdfs"] = all_pdfs

        amounts.setdefault("registration_no", effective["db_id"])
    return submitted, amounts


def submission_pdf(ctx: _Ctx, o: Obligation, db_id: str, args: dict[str, str]) -> str:
    """PDF της υποβληθείσας δήλωσης (`viewPdf.htm`, runner `configs/aade-declarations.js` — Hyper.Server
    `GetGeneralType1Year`) → <data>/submissions/<ΑΦΜ>/<έτος>/. '' αν δεν ήρθε PDF. Μόνο ανάγνωση."""
    from . import config
    if not hasattr(ctx.http, "follow"):
        return ""
    res = ctx.http.follow("POST", f"{AADE}/taxisnet/{o.category}/protected/viewPdf.htm",
                          {"declarationDatabaseId": db_id, "declarationType": o.filter})
    if not res.get("is_pdf"):
        return ""
    year = (args.get("year") or "")[:4] or str(date.today().year)
    start = re.sub(r"\D", "", args.get("periodStart") or "")[:8] or db_id
    folder = config.data_dir() / "submissions" / ctx.afm / year
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{re.sub(r'[^0-9A-Za-zΑ-Ωα-ω_]', '_', o.key)}_{ctx.afm}_{start}_{db_id}.pdf"
    path.write_bytes(res["content"])
    return str(path)


def check_obligation(ctx: _Ctx, key: str, year: int, cache: Optional[dict] = None,
                     known: Optional[dict[str, dict]] = None) -> dict[str, Any]:
    """`year` = φορολογική χρήση. `known`: περίοδος -> ποσά που έχουν ήδη αποθηκευτεί (δεν ξαναδιαβάζονται)."""
    o = OBLIGATIONS[key]
    entry: dict[str, Any] = {"obligation": key, "label": o.label, "periods": [], "status": ""}
    if o.kind == "income":
        page = ctx.get(f"{AADE}/webtax/incomefp/year{year}-income-menu.do")
        if not re.search(r"income-menu|PRINT_CODE|PB[A-Z0-9_]*_PRINT|income-menuMod", page, re.I):
            entry["status"] = "no_data"
            return entry
        text = strip_tags(page)
        done, when = income_submission(text, o, page)
        amend = " (τροποποιητική)" if re.search(r"τροποποιητικ", text, re.I) else ""
        entry["periods"].append({"period": str(year), "status": "submitted" if done else "not_submitted",
                                 "detail": f"Υποβλήθηκε {when}{amend}".strip() if done else "Δεν έχει υποβληθεί δήλωση",
                                 "period_end": f"{year}-12-31", "period_type": "year"})
        entry["status"] = "submitted" if done else "not_submitted"
        return entry
    cache = cache if cache is not None else {}
    url = f"{AADE}/taxisnet/{o.category}/protected/displayLiabilitiesForYear.htm?declarationType={o.filter}&year={year}{o.extra}"
    if url not in cache:
        page = ctx.get(url)
        if _set_as_myself(ctx, page, o.category):
            page = ctx.get(url)
        cache[url] = page
    page = cache[url]
    if _NOT_OBLIGATED in page:
        entry["status"] = "not_obligated"
        return entry
    found = rows(page)
    if not found:
        entry["status"] = "no_data"
        return entry
    for cells in found:
        period, status, text = interpret_row(o, cells)
        args = decl_list_args(cells[o.button_col] if o.button_col < len(cells) else "")
        end, ptype = period_end(args, year, period)
        amounts: dict[str, Any] = {}
        # και Φ4/Φ5 κ.λπ. όταν ζητήθηκαν PDF — πριν μόνο Φ2/ΦΜΥ/τέλη, οπότε οι ανακεφαλαιωτικοί VIES δεν είχαν ποτέ PDF
        if args and status != "not_submitted" and (o.search_type or o.key in AMOUNT_KEYS or ctx.want_pdf):
            submitted, amounts = _period_details(ctx, o, args, (known or {}).get(period))
            if submitted is False:
                status = "not_submitted"
        entry["periods"].append({"period": period, "status": status, "detail": text, "period_end": end,
                                 "period_type": ptype, "amounts": amounts})
    statuses = {p["status"] for p in entry["periods"]}
    entry["status"] = "not_submitted" if "not_submitted" in statuses else ("pending" if "pending" in statuses else "submitted")
    return entry


def vat_period_from_checks(entry: dict[str, Any]) -> str:
    """'monthly' | 'quarterly' | '' από τις ΠΡΑΓΜΑΤΙΚΕΣ περιόδους Φ2 της χρήσης (η ΑΑΔΕ τις βγάζει από το καθεστώς του
    πελάτη) — πιο αξιόπιστο από την κατηγορία βιβλίων. Αλλαγή περιόδου μέσα στη χρήση ⇒ μετρά η τελευταία."""
    types = [p.get("period_type") for p in entry.get("periods", []) if p.get("period_type") in ("month", "quarter")]
    if not types:
        return ""
    return "monthly" if types[-1] == "month" else "quarterly"


def default_keys(business: dict[str, Any], withheld_keys: Optional[set[str]] = None) -> list[str]:
    """Ποιες υποχρεώσεις ελέγχονται για έναν πελάτη, από ό,τι ξέρουμε (ίδια λογική με το obligations.liability).
    `withheld_keys`: δηλώσεις παρακράτησης που προκύπτουν από τα παραστατικά myDATA του έτους (ΦΜΥ_ΕΠΙΧ, ΕΡΓΟΛΑΒΩΝ…)."""
    from .obligations import is_legal_entity
    keys: list[str] = []
    state = business.get("activity_state") or ""
    if state != "none" and business.get("vat_subject") != 0:
        keys.append("Φ2")
        # Φ4 (ανακεφαλαιωτικός VIES) για ΚΑΘΕ υπόχρεο ΦΠΑ — πριν μόνο με σημαία Intrastat από το Excel, οπότε ο έλεγχος
        # «ενδοκοινοτικών» δεν γινόταν σχεδόν ποτέ (αναφορά χρήστη 2026-10-22). Όποιος δεν έχει, η ΑΑΔΕ απαντά «Δεν έχετε
        # υποχρεώσεις υποβολής» → «δεν είναι υπόχρεος». Φ5 = αποκτήσεις/λήψεις υπηρεσιών (το Φ4 είναι μόνο οι
        # παραδόσεις/παροχές) — ποιο από τα δύο οφείλεται κάθε μήνα το λέει το myDATA (obligations.vies_forms).
        keys += ["Φ4", "Φ5"]
    elif business.get("intrastat"):
        keys += ["Φ4", "Φ5"]
    if business.get("employer") != 0 and state != "none":
        keys.append("ΦΜΥ")                       # άγνωστο ή εργοδότης: ο έλεγχος το ξεκαθαρίζει («δεν είναι υπόχρεος»)
    if is_legal_entity(business.get("legal_form") or ""):
        keys += ["ΕΙΣΟΔΗΜΑ_ΝΠ"]
    else:
        keys += ["E1"] + (["E3"] if state not in ("none", "ceased") else [])
    if state not in ("none",):
        keys.append("ΜΥΦ")
    keys += sorted(k for k in (withheld_keys or set()) if k in OBLIGATIONS and k not in keys)
    return keys


def withheld_keys(conn: sqlite3.Connection, afm: str, year: int) -> set[str]:
    """Δηλώσεις παρακράτησης που οφείλει ο πελάτης κατά τα παραστατικά myDATA του έτους
    ή που έχουν ήδη ελεγχθεί μία φορά (για να μην χάνονται)."""
    from .mydata.analysis import DECLARATION, PAYER_CATEGORIES
    out: set[str] = set()
    for r in conn.execute("SELECT withheld_json FROM mydata_docs WHERE afm=? AND direction='incoming' AND cancelled=0 "
                          "AND substr(issue_date,1,4)=? AND withheld > 0", (afm, str(year))):
        for cat in json.loads(r["withheld_json"] or "{}"):
            if cat in PAYER_CATEGORIES and DECLARATION.get(cat, ("",))[0]:
                out.add(DECLARATION[cat][0])
    try:
        for r in conn.execute("SELECT DISTINCT obligation FROM obligation_checks WHERE afm=? AND year=? AND status!='error'", (afm, year)):
            if r[0] in ("ΦΜΥ_ΕΠΙΧ", "ΕΡΓΟΛΑΒΩΝ", "ΦΜΥ_ΔΙΚΑΙΩΜΑΤΑ", "ΦΜΥ_ΤΟΚΟΙ", "ΦΜΥ_ΜΕΡΙΣΜΑΤΑ"):
                out.add(r[0])
    except Exception:
        pass
    return out


def fee_keys(conn: sqlite3.Connection, afm: str, year: int) -> set[str]:
    """Δηλώσεις τελών TAXISnet («other») που οφείλει ο πελάτης κατά το myDATA του έτους (ρητό αίτημα 2026-10-27):
    ΔΙΑΜΟΝΗ (otherAppH6) = στοιχεία 8.2 ή λοιποί φόροι διαμονής· ΠΕΡΙΒΑΛΛΟΝ (otherAppH7) = τέλος πλαστικής σακούλας.
    Και όσα έχουν ήδη ελεγχθεί μια φορά (να μη «χάνονται» αν λείπει μήνας myDATA)."""
    from .mydata.analysis import CLIMATE_TYPE, FEE_GROUPS
    out: set[str] = set()
    if conn.execute("SELECT 1 FROM mydata_docs WHERE afm=? AND direction='outgoing' AND cancelled=0 AND invoice_type=? "
                    "AND substr(issue_date,1,4)=? LIMIT 1", (afm, CLIMATE_TYPE, str(year))).fetchone():
        out.add("ΔΙΑΜΟΝΗ")
    for r in conn.execute("SELECT taxes_json FROM mydata_docs WHERE afm=? AND direction='outgoing' AND cancelled=0 "
                          "AND substr(issue_date,1,4)=? AND taxes_json NOT IN ('', '{}')", (afm, str(year))):
        taxes = json.loads(r["taxes_json"] or "{}")
        for key, group in (("ΔΙΑΜΟΝΗ", "stay"), ("ΠΕΡΙΒΑΛΛΟΝ", "plastic_bag")):
            kind, cats = FEE_GROUPS[group]
            if any(c in (taxes.get(kind) or {}) for c in cats):
                out.add(key)
    for r in conn.execute("SELECT DISTINCT obligation FROM obligation_checks WHERE afm=? AND obligation IN "
                          "('ΔΙΑΜΟΝΗ','ΠΕΡΙΒΑΛΛΟΝ') AND status<>'not_obligated'", (afm,)):
        out.add(r["obligation"])
    return out


#: Δηλώσεις τελών όπου το TAXISnet γράφει «Δεν έχει υποβληθεί» σε ΚΑΘΕ περίοδο για ΟΛΟΥΣ (ζωντανά 2026-10-27, δύο
#: εταιρείες χωρίς καμία σχέση με τα τέλη): η υποχρέωση = μήνες με το τέλος στο myDATA. Κλειδί → (ομάδα, τρίμηνο;).
_FEE_LIABILITY = {"ΔΙΑΜΟΝΗ": ("stay", False), "ΠΕΡΙΒΑΛΛΟΝ": ("plastic_bag", True)}


def _only_liable_fee_periods(conn: sqlite3.Connection, afm: str, year: int, entry: dict[str, Any]) -> dict[str, Any]:
    """Κρατά τις υποβληθείσες περιόδους και ΜΟΝΟ όσες μη υποβληθείσες έχουν το τέλος στο myDATA."""
    rule = _FEE_LIABILITY.get(entry.get("obligation", ""))
    if not rule or not entry.get("periods"):
        return entry
    from .mydata.analysis import fee_months
    group, quarterly = rule
    months = fee_months(conn, afm, year, group)

    def liable(p: dict[str, Any]) -> bool:
        ym = (p.get("period_end") or "")[:7]
        if not ym:
            return True
        if quarterly:
            m = int(ym[5:7])
            return any(f"{ym[:4]}-{mm:02d}" in months for mm in range(m - 2, m + 1))
        return ym in months
    periods = [p for p in entry["periods"] if p["status"] == "submitted" or liable(p)]
    statuses = {p["status"] for p in periods}
    status = ("not_submitted" if "not_submitted" in statuses else "pending" if "pending" in statuses
              else "submitted" if periods else "not_obligated")
    return {**entry, "periods": periods, "status": status}


def check_client(conn: sqlite3.Connection, afm: str, year: Optional[int] = None, keys: Optional[list[str]] = None,
                 login_fn: Callable[[str, str], dict] = lookup_aade.aade_login, want_pdf: bool = False) -> dict[str, Any]:
    """Login με τους κωδικούς ΤΟΥ ΠΕΛΑΤΗ, έλεγχος, αποθήκευση στο `obligation_checks`. Επιστρέφει {ok, reason, entries}."""
    year = year or date.today().year
    creds = credentials.get(conn, afm)
    if not creds:
        log.warning("Έλεγχος υποβολών: δεν υπάρχουν κωδικοί TAXISnet για ΑΦΜ %s", afm)
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί TAXISnet του πελάτη.", "entries": []}
    b = conn.execute("SELECT * FROM businesses WHERE afm=?", (afm,)).fetchone()
    keys = keys or default_keys(dict(b) if b else {}, withheld_keys(conn, afm, year) | fee_keys(conn, afm, year))
    login = login_fn(*creds)
    if not login.get("ok"):
        reason_msg = lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης TAXISnet.")
        log.warning("Έλεγχος υποβολών: αποτυχία σύνδεσης TAXISnet για ΑΦΜ %s (%s)", afm, reason_msg)
        return {"ok": False, "reason": reason_msg, "entries": []}
    ctx, cache, entries = _Ctx(login["http"], afm, want_pdf), {}, []
    stored: dict[str, dict[str, dict]] = {}
    for r in conn.execute("SELECT obligation, period, amounts_json FROM obligation_checks WHERE afm=? AND year=? "
                          "AND status='submitted'", (afm, year)):
        stored.setdefault(r["obligation"], {})[r["period"]] = json.loads(r["amounts_json"] or "{}")
    for key in keys:
        try:
            entries.append(check_obligation(ctx, key, year, cache, stored.get(key)))
        except Exception as exc:                        # noqa: BLE001 — μία υποχρέωση δεν ρίχνει τις υπόλοιπες
            log.error("Σφάλμα ελέγχου υποχρέωσης %s για ΑΦΜ %s χρήση %s: %s", key, afm, year, exc, exc_info=True)
            from . import handoff
            handoff.remember(f"ΑΑΔΕ — {OBLIGATIONS[key].label}", afm, login["http"])
            entries.append({"obligation": key, "label": OBLIGATIONS[key].label, "status": "error", "periods": [],
                            "detail": f"Σφάλμα ελέγχου ({type(exc).__name__}: {str(exc)[:120]})"})
    entries = [_only_liable_fee_periods(conn, afm, year, e) for e in entries]
    store_results(conn, afm, year, entries)
    vat_type = next((vat_period_from_checks(e) for e in entries if e["obligation"] == "Φ2"), "")
    if vat_type:                                       # οι πραγματικές περίοδοι Φ2 υπερισχύουν της κατηγορίας βιβλίων
        conn.execute("UPDATE businesses SET vat_period_type=?, vat_period_source='easycheck' WHERE afm=?", (vat_type, afm))
    log.info("Ολοκληρώθηκε ο έλεγχος υποβολών TAXISnet για ΑΦΜ %s χρήση %s (%d υποχρεώσεις ελέγχθηκαν)", afm, year, len(entries))
    return {"ok": True, "reason": "", "entries": entries}


def store_results(conn: sqlite3.Connection, afm: str, year: int, entries: list[dict[str, Any]]) -> None:
    now = db.utcnow()
    conn.execute("BEGIN")
    try:
        for e in entries:
            conn.execute("DELETE FROM obligation_checks WHERE afm=? AND year=? AND obligation=?", (afm, year, e["obligation"]))
            periods = e["periods"] or [{"period": "", "status": e["status"], "detail": e.get("detail", "")}]
            for p in periods:
                conn.execute("INSERT OR REPLACE INTO obligation_checks(afm, year, obligation, period, status, detail, "
                             "checked_at, period_end, period_type, amounts_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (afm, year, e["obligation"], p["period"], p["status"], p.get("detail", ""), now,
                              p.get("period_end", ""), p.get("period_type", ""),
                              json.dumps(p.get("amounts") or {}, ensure_ascii=False)))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def results(conn: sqlite3.Connection, afm: str, year: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM obligation_checks WHERE afm=? AND year=? ORDER BY obligation, period", (afm, year))]


def summary(conn: sqlite3.Connection, year: int) -> dict[str, dict[str, int]]:
    """ΑΦΜ -> {'submitted': n, 'pending': n, 'not_submitted': n} για τη στήλη του πίνακα πελατών."""
    out: dict[str, dict[str, int]] = {}
    for r in conn.execute("SELECT afm, status, COUNT(*) AS n FROM obligation_checks WHERE year=? GROUP BY afm, status",
                          (year,)):
        out.setdefault(r["afm"], {})[r["status"]] = r["n"]
    return out
