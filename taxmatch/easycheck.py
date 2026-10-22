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
from typing import Any, Callable, Optional
from urllib.parse import quote, urlencode

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
    Obligation("Φ5", "ΦΠΑ Φ5 (Intrastat/ΦΠΑ)", "vat", "vatF5"),
    Obligation("ΕΙΣΟΔΗΜΑ_ΝΠ", "Δήλωση φορολογίας εισοδήματος Ν.Π. (Ν)", "income", "incomeN"),
    Obligation("ΜΥΦ", "Συγκεντρωτικές καταστάσεις (ΜΥΦ)", "kvs", "kvsD13"),
    Obligation("E1", "Δήλωση φορολογίας εισοδήματος (Ε1)", kind="income", code_prefix="E1-", requires_date=True),
    Obligation("E3", "Κατάσταση οικονομικών στοιχείων (Ε3)", kind="income", code_prefix="E3-"),
    Obligation("ΠΕΡΙΒΑΛΛΟΝ", "Περιβαλλοντικό τέλος", "other", "otherAppH7"),
    Obligation("ΔΙΑΜΟΝΗ", "Φόρος / τέλος διαμονής", "other", "otherAppH6"),
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
    def __init__(self, http) -> None:
        self.http = http

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
#: Υποχρεώσεις για τις οποίες διαβάζονται και τα ΠΟΣΑ της δήλωσης (εκκαθάριση + ταυτότητα οφειλής). Φ2: πιστό port του
#: runner `lib/aade-vat-http.js` (επαληθευμένο εκεί). Οι υπόλοιπες ακολουθούν την ίδια δομή σελίδων της ΑΑΔΕ στη δική
#: τους κατηγορία — ΔΕΝ έχει επαληθευτεί· αν η σελίδα δεν έχει τα πεδία, απλώς δεν εμφανίζονται ποσά.
AMOUNT_KEYS = {"Φ2", "ΦΜΥ", "ΦΜΥ_ΕΠΙΧ", "ΕΡΓΟΛΑΒΩΝ", "ΦΜΥ_ΔΙΚΑΙΩΜΑΤΑ", "ΦΜΥ_ΤΟΚΟΙ", "ΦΜΥ_ΜΕΡΙΣΜΑΤΑ", "ΔΙΑΜΟΝΗ"}
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
        call = _STATE_CALL.search(_html.unescape(row))
        out.append({"db_id": (call.group(2) if call else (texts[2] if len(texts) > 2 else "")).strip(),
                    "submitted_at": texts[3] if len(texts) > 3 else "", "type": texts[4] if len(texts) > 4 else "",
                    "final": texts[5] if len(texts) > 5 else ""})
    return out


def declaration_amounts(ctx: _Ctx, o: Obligation, db_id: str, ret_view: str) -> dict[str, Any]:
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
        page = ctx.get(base + "displayDebtCode.htm?" + q)
        if re.search(r"Ταυτότητα Οφειλής|Συνολικό ποσό οφειλής", page or ""):
            m = label_map(page)
            total = to_num(_find(m, r"Συνολικό ποσό οφειλής"))
            if total is not None:
                out["total_due"] = total
            debt = re.search(r"[\d ]{10,}", _find(m, r"Ταυτότητα Οφειλής"))
            if debt:
                out["debt_id"] = debt.group(0).strip()
    except Exception as exc:                            # noqa: BLE001
        log.info("easycheck debtCode %s: %s", o.key, exc)
    return out


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
    if o.key not in AMOUNT_KEYS or not effective or not effective["db_id"]:
        return submitted, {}
    if known and known.get("registration_no") == effective["db_id"]:
        return submitted, known
    amounts = declaration_amounts(ctx, o, effective["db_id"], "displayDeclarationsList.htm?" + urlencode(args))
    amounts.update(registration_no=effective["db_id"], submitted_at=effective["submitted_at"], amended=len(finals) > 1)
    return submitted, amounts


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
        if args and status != "not_submitted" and (o.search_type or o.key in AMOUNT_KEYS):
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
    if business.get("intrastat"):
        keys.append("Φ4")
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
    """Δηλώσεις παρακράτησης που οφείλει ο πελάτης κατά τα παραστατικά myDATA του έτους."""
    from .mydata.analysis import DECLARATION, PAYER_CATEGORIES
    out: set[str] = set()
    for r in conn.execute("SELECT withheld_json FROM mydata_docs WHERE afm=? AND direction='incoming' AND cancelled=0 "
                          "AND substr(issue_date,1,4)=? AND withheld > 0", (afm, str(year))):
        for cat in json.loads(r["withheld_json"] or "{}"):
            if cat in PAYER_CATEGORIES and DECLARATION.get(cat, ("",))[0]:
                out.add(DECLARATION[cat][0])
    return out


def check_client(conn: sqlite3.Connection, afm: str, year: Optional[int] = None, keys: Optional[list[str]] = None,
                 login_fn: Callable[[str, str], dict] = lookup_aade.aade_login) -> dict[str, Any]:
    """Login με τους κωδικούς ΤΟΥ ΠΕΛΑΤΗ, έλεγχος, αποθήκευση στο `obligation_checks`. Επιστρέφει {ok, reason, entries}."""
    year = year or date.today().year
    creds = credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί TAXISnet του πελάτη.", "entries": []}
    b = conn.execute("SELECT * FROM businesses WHERE afm=?", (afm,)).fetchone()
    keys = keys or default_keys(dict(b) if b else {}, withheld_keys(conn, afm, year))
    login = login_fn(*creds)
    if not login.get("ok"):
        return {"ok": False, "reason": lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης TAXISnet."),
                "entries": []}
    ctx, cache, entries = _Ctx(login["http"]), {}, []
    stored: dict[str, dict[str, dict]] = {}
    for r in conn.execute("SELECT obligation, period, amounts_json FROM obligation_checks WHERE afm=? AND year=? "
                          "AND status='submitted'", (afm, year)):
        stored.setdefault(r["obligation"], {})[r["period"]] = json.loads(r["amounts_json"] or "{}")
    for key in keys:
        try:
            entries.append(check_obligation(ctx, key, year, cache, stored.get(key)))
        except Exception as exc:                        # noqa: BLE001 — μία υποχρέωση δεν ρίχνει τις υπόλοιπες
            log.warning("easycheck %s/%s: %s", afm, key, exc)
            entries.append({"obligation": key, "label": OBLIGATIONS[key].label, "status": "error", "periods": [],
                            "detail": str(exc)[:200]})
    store_results(conn, afm, year, entries)
    vat_type = next((vat_period_from_checks(e) for e in entries if e["obligation"] == "Φ2"), "")
    if vat_type:                                       # οι πραγματικές περίοδοι Φ2 υπερισχύουν της κατηγορίας βιβλίων
        conn.execute("UPDATE businesses SET vat_period_type=?, vat_period_source='easycheck' WHERE afm=?", (vat_type, afm))
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
