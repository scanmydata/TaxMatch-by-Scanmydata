"""Πραγματικός έλεγχος υποβολής τελών σε πύλες ΕΚΤΟΣ του TAXISnet «other» (ρητό αίτημα χρήστη 2026-10-27):

* **ΠΛΑΣΤΙΚΑ** — «Περιβαλλοντική Εισφορά για τα Πλαστικά Προϊόντα» (ν. 4736/2020 αρ. 4), εφαρμογή ΑΑΔΕ
  `www1.aade.gr/saadeapps3/Plastic` (AngularJS + REST). Port του runner `configs/aade-plastic-check.js` (LIVE εκεί
  23/10/2026): μετά το login TAXISnet (GSIS OAM) → `getToken` (Authorization σε κάθε POST) → ΠΡΩΤΑ `getDownTime` +
  `getuserdata` (αλλιώς όλα 500) → `getSearchApplication` (υποβληθείσες: έτος/τρίμηνο/σύνολο/transId) +
  `getIsActiveEpitid/{χρήστης}/{ΑΦΜ}/{έτος}/{τρίμηνο}` (υπόχρεος την περίοδο;) + `getMarginalDateData` (προθεσμία).
  Σώματα POST = base64(JSON), απαντήσεις {result: base64url(JSON)}. Ταυτότητα οφειλής: από το PDF «Ειδοποίηση
  Πληρωμής» (`getPrintPDF` p3=2), γραμμή «ΑΦΜ ΑΦΜ 12ψήφιο» — με pypdf αντί για mupdf.
* **ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ** — «Τέλος Διαμονής Παρεπιδημούντων & Τέλος επί Ακαθαρίστων Εσόδων», `dae.govapp.gr` (ΑΑΔΕ/ΚΕΔΕ,
  από 01/04/2025). Port του runner `configs/dae-parepid-check.js` (LIVE εκεί 23/10/2026): login GSIS OAuth2
  (`/authorization/ExternalLogin?provider=Gsis`) → `/api/Helper/Periodoi` · `/api/StatementsEpixirisi/GetPendingPeriodikes`
  (Φ2 χωρίς δήλωση τέλους = εκκρεμεί) · `/api/Periodoi/GetCompletedPeriods` · `/api/StatementsYpovoli/GetYpovolesPeriodou`
  (ποσά/ΤΟ/κατάσταση πληρωμής — η ΛΙΣΤΑ είναι η πηγή αλήθειας για πληρωμές, όχι το detail).

Αποτέλεσμα: εγγραφές για `easycheck.store_results` (`obligation_checks`), ίδιο σχήμα με τον έλεγχο υποβολών. ΜΟΝΟ ανάγνωση.
**Δεν έχουν δοκιμαστεί ζωντανά από το TaxMatch** — μόνο τα JS του runner (ψεύτικο HTTP στα tests).
"""
from __future__ import annotations

import base64
import io
import json
import logging
import random
import re
import sqlite3
import time
from datetime import date
from typing import Any, Callable, Optional

from . import config
from .business_profiles import credentials, lookup_aade
from .business_profiles.lookup_aade import _HyperHttp  # noqa: PLC2701
from .obligations import MONTHS_NOM, QUARTERS, month_end

log = logging.getLogger(__name__)

AADE = "https://www1.aade.gr"
PLASTIC = AADE + "/saadeapps3/Plastic/"
PLASTIC_WS = PLASTIC + "webresources/plastic/"
DAE = "https://dae.govapp.gr"
_XHR = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
_TO_RE = re.compile(r"\b\d{9} \d{9} \d{12}\b")


# ------------------------------------------------------------------ βοηθητικά
def _b64u_json(text: str) -> Any:
    """{result: base64url(JSON)} → τιμή· κάποια endpoints διπλο-κωδικοποιούν (JSON string μέσα σε JSON)."""
    raw = json.loads(text)["result"]
    raw += "=" * (-len(raw) % 4)
    decoded = base64.b64decode(raw.replace("-", "+").replace("_", "/")).decode("utf-8")
    try:
        value = json.loads(decoded)
    except ValueError:
        return decoded
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _body(data: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(data or {}, ensure_ascii=False).encode("utf-8")).decode("ascii")


def _quarter_label(year: int, q: int) -> str:
    return f"{QUARTERS[q - 1]} {year}"


def notice_payment_id(pdf: bytes) -> str:
    """Ταυτότητα οφειλής από την «Ειδοποίηση Πληρωμής» (γραμμή «ΑΦΜ ΑΦΜ 12ψήφιο»)."""
    try:
        from pypdf import PdfReader
        text = " ".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages)
    except Exception:                                  # noqa: BLE001 — άκυρο PDF: απλώς χωρίς ΤΟ
        return ""
    m = _TO_RE.search(re.sub(r"[ \t]+", " ", text))
    return m.group(0) if m else ""


def _save_pdf(afm: str, name: str, content: bytes) -> str:
    folder = config.data_dir() / "fees" / afm
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / re.sub(r'[\\/:*?"<>|]', "-", name)
    path.write_bytes(content)
    return str(path)


# ------------------------------------------------------------------ Πλαστικά (Plastic)
class _Plastic:
    def __init__(self, http: _HyperHttp) -> None:
        self.http = http

    def token(self) -> str:
        r = self.http.follow("GET", f"{PLASTIC_WS}getToken?{random.random()}", headers={"Accept": "application/json"})
        if r["status"] != 200:
            raise RuntimeError(f"getToken HTTP {r['status']}")
        return str(_b64u_json(r["text"]))

    def post(self, ep: str, data: Optional[dict[str, Any]] = None) -> Any:
        r = self.http.post_raw(f"{PLASTIC_WS}{ep}?{int(time.time() * 1000)}", _body(data or {}),
                               {"Content-Type": "application/json;charset=utf-8;", "Authorization": self.token()})
        if r["status"] == 204 or (r["status"] == 200 and not r["text"]):
            return None                                 # getDownTime απαντά συχνά 204
        if r["status"] != 200:
            raise RuntimeError(f"{ep} HTTP {r['status']}")
        return _b64u_json(r["text"])

    def get_json(self, ep: str) -> Any:
        r = self.http.follow("GET", f"{PLASTIC_WS}{ep}?{int(time.time() * 1000)}", headers={"Accept": "application/json"})
        if r["status"] != 200:
            raise RuntimeError(f"{ep} HTTP {r['status']}")
        return json.loads(r["text"])

    def pdf(self, data: dict[str, Any]) -> Optional[bytes]:
        h = self.http
        headers = {"User-Agent": lookup_aade.UA, "Content-Type": "application/json;charset=utf-8;",
                   "Authorization": self.token()}
        url = f"{PLASTIC_WS}getPrintPDF?{int(time.time() * 1000)}"
        ck = h._cookie(url)                              # noqa: SLF001 — ίδιο jar με το post_raw
        if ck:
            headers["Cookie"] = ck
        resp = h.session.request("POST", url, headers=headers, data=_body(data).encode("ascii"), timeout=h.timeout)
        return resp.content if resp.content[:4] == b"%PDF" else None


def plastics_entry(http: _HyperHttp, afm: str, year: int, today: Optional[date] = None,
                   want_pdf: bool = True, liable: Optional[set[int]] = None) -> dict[str, Any]:
    """Εγγραφή «ΠΛΑΣΤΙΚΑ» της χρήσης: ανά τρίμηνο υποβλήθηκε (ποσό/ΤΟ) · δεν υποβλήθηκε · δεν αφορά. Τρίμηνα που
    δεν έληξαν μένουν έξω (εκτός αν υπάρχει ήδη δήλωση). ΠΡΟΣΟΧΗ: το `getIsActiveEpitid` δίνει isActive=true για ΚΑΘΕ
    ενεργή επιχείρηση (ζωντανά 2026-10-27) — δεν σημαίνει υποχρέωση. `liable` = τρίμηνα με εισφορά στο myDATA· μη
    υποβληθέν τρίμηνο εκτός αυτών δεν γράφεται."""
    today = today or date.today()
    p = _Plastic(http)
    http.follow("GET", PLASTIC)
    p.post("getDownTime")
    user = p.post("getuserdata") or {}
    me = str(user.get("afm") or afm)
    subs = p.post("getSearchApplication", {"p1": afm, "p2": me}) or []
    periods, obligated_any = [], False
    for q in range(1, 5):
        end = month_end(year, q * 3)
        mine = [s for s in subs if isinstance(s, dict) and int(s.get("etos") or 0) == year
                and int(s.get("trimhno") or 0) == q]
        base = {"period": _quarter_label(year, q), "period_end": end.isoformat(), "period_type": "quarter"}
        if mine:
            total = round(sum(float(s.get("synolo") or 0) for s in mine), 2)
            amounts: dict[str, Any] = {"total_due": total} if total else {}
            last = mine[-1]
            if want_pdf and total > 0 and last.get("transId"):
                try:
                    notice = p.pdf({"p1": afm, "p2": last["transId"], "p3": 2, "p4": me})
                    if notice:
                        amounts["pdf"] = _save_pdf(afm, f"PLASTIC_{afm}_{year}-Q{q}_{last['transId']}_EIDOPOIISI.pdf",
                                                   notice)
                        to = notice_payment_id(notice)
                        if to:
                            amounts["payment_id"] = to
                except Exception as exc:                # noqa: BLE001 — το PDF είναι επιπλέον
                    log.warning("plastic pdf %s: %s", afm, exc)
            obligated_any = True
            periods.append({**base, "status": "submitted", "amounts": amounts,
                            "detail": f"Δήλωση εισφοράς πλαστικών · {total:.2f} €".replace(".", ",")})
            continue
        if end >= today or (liable is not None and q not in liable):
            continue                                    # δεν έληξε ακόμη / δεν τιμολόγησε εισφορά στο τρίμηνο
        try:
            active = p.get_json(f"getIsActiveEpitid/{me}/{afm}/{year}/{q}")
            is_active = active.get("isActive") in (True, "true")
        except Exception:                               # noqa: BLE001
            is_active = None
        deadline = ""
        try:
            marg = p.get_json(f"getMarginalDateData/{me}/{afm}/{year}/{q}")
            deadline = str(marg.get("targetDate") or "")[:10]
        except Exception:                               # noqa: BLE001
            pass
        if is_active is False:
            periods.append({**base, "status": "not_obligated", "detail": "Η εφαρμογή δεν ζητά δήλωση για την περίοδο"})
        else:
            obligated_any = True
            periods.append({**base, "status": "not_submitted",
                            "detail": "Υπόχρεος χωρίς δήλωση" + (f" (προθεσμία {deadline})" if deadline else "")})
    status = ("not_obligated" if not obligated_any else
              "not_submitted" if any(x["status"] == "not_submitted" for x in periods) else "submitted")
    return {"obligation": "ΠΛΑΣΤΙΚΑ", "label": "Εισφορά πλαστικών προϊόντων (ν. 4736/2020)", "periods": periods,
            "status": status}


# ------------------------------------------------------------------ Παρεπιδημούντες (dae.govapp.gr)
def dae_login(http: _HyperHttp, user: str, password: str) -> dict[str, Any]:
    from .efka import _gsis                             # noqa: PLC0415 — ίδιο GSIS OAuth2 (j_spring_security_check + approval)
    r = http.follow("GET", f"{DAE}/authorization/ExternalLogin?provider=Gsis")
    if "j_password" not in r["text"]:
        return {"ok": False, "reason": "Δεν φόρτωσε η σύνδεση TAXISnet του dae.govapp.gr."}
    g = _gsis(http, user, password)
    if not g["ok"]:
        return {"ok": False, "reason": "Λάθος κωδικοί TAXISnet."}
    if "dae.govapp.gr" not in g["page"]["url"]:
        return {"ok": False, "reason": "Η σύνδεση δεν επέστρεψε στο dae.govapp.gr."}
    return {"ok": True}


def _dae_json(http: _HyperHttp, method: str, path: str, body: Optional[dict[str, Any]] = None) -> Any:
    if method == "GET":
        r = http.follow("GET", DAE + path, headers=_XHR)
    else:
        r = http.post_raw(DAE + path, json.dumps(body or {}), _XHR)
    if r["status"] in (401, 403):
        raise PermissionError(f"dae.govapp.gr {r['status']}")
    if r["status"] != 200:
        raise RuntimeError(f"dae.govapp.gr HTTP {r['status']} {path}")
    return json.loads(r["text"])


def _paged(cols: list[dict[str, Any]]) -> dict[str, Any]:
    return {"start": 0, "length": 100, "searchColumns": cols, "orders": []}


def _dae_period(p: dict[str, Any]) -> tuple[str, str, str]:
    """(ετικέτα, period_end ISO, period_type) περιόδου της πύλης."""
    start = str(p.get("periodikiDilosiApo") or "")[:10]
    end = str(p.get("periodikiDilosiEos") or "")[:10]
    quarter = p.get("typosPeriodouYpovolis") == 2 or "Τρίμηνο" in str(p.get("perigrafi") or "")
    try:
        y, m = int(start[:4]), int(start[5:7])
    except ValueError:
        return str(p.get("perigrafi") or ""), end, "quarter" if quarter else "month"
    if quarter:
        return _quarter_label(y, (m - 1) // 3 + 1), end or month_end(y, m + 2).isoformat(), "quarter"
    return f"{MONTHS_NOM[m - 1]} {y}", end or month_end(y, m).isoformat(), "month"


def parepid_entry(http: _HyperHttp, year: int, today: Optional[date] = None) -> dict[str, Any]:
    """Εγγραφή «ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ» της χρήσης (ήδη συνδεδεμένο `http`, βλ. `dae_login`)."""
    today = today or date.today()
    periods_all = _dae_json(http, "GET", "/api/Helper/Periodoi") or []
    pending = _dae_json(http, "GET", "/api/StatementsEpixirisi/GetPendingPeriodikes") or []
    completed = (_dae_json(http, "POST", "/api/Periodoi/GetCompletedPeriods", _paged([])) or {}).get("data") or []
    pend_ids = {x.get("periodId") for x in pending}
    done_ids = {x.get("id") for x in completed}
    out = []
    for p in periods_all:
        if str(p.get("periodikiDilosiApo") or "")[:4] != str(year) or p.get("id") not in pend_ids | done_ids:
            continue
        label, end, ptype = _dae_period(p)
        base = {"period": label, "period_end": end, "period_type": ptype}
        if p["id"] in pend_ids:
            pend = next(x for x in pending if x.get("periodId") == p["id"])
            deadline = str(pend.get("teleutaiaImeraPrinProstimo") or "")[:10]
            out.append({**base, "status": "not_submitted",
                        "detail": "Εκκρεμεί δήλωση τέλους (υπάρχει Φ2 της περιόδου)"
                                  + (f" — χωρίς πρόστιμο έως {deadline}" if deadline else "")})
            continue
        subs: list[dict[str, Any]] = []
        for _attempt in range(3):                       # το endpoint επιστρέφει περιστασιακά data:[] με recordsFiltered>0
            r = _dae_json(http, "POST", "/api/StatementsYpovoli/GetYpovolesPeriodou",
                          _paged([{"columnName": "periodId", "value": str(p["id"]), "regex": False}])) or {}
            subs = r.get("data") or []
            if subs or not r.get("recordsFiltered"):
                break
        due = round(sum(float(s.get("posoVevaiosis") or 0) for s in subs), 2)
        unpaid = round(sum(float(s.get("posoYpoloipo") or 0) for s in subs), 2)
        to = next((s.get("tautotitaOfeilis") for s in subs if s.get("tautotitaOfeilis")), "")
        amounts: dict[str, Any] = {}
        if due:
            amounts["total_due"] = due
        if unpaid:
            amounts["unpaid"] = unpaid
        if to:
            amounts["payment_id"] = to
        pay_by = next((str(s.get("teleutaiaImeraPrinProstimo") or "")[:10] for s in subs
                       if s.get("teleutaiaImeraPrinProstimo")), "")
        if pay_by:
            amounts["pay_by"] = pay_by
        out.append({**base, "status": "submitted", "amounts": amounts,
                    "detail": f"{len(subs)} δήλωση(-εις) τέλους" + (" · ανεξόφλητο" if unpaid else "")})
    status = ("not_obligated" if not out else
              "not_submitted" if any(x["status"] == "not_submitted" for x in out) else "submitted")
    return {"obligation": "ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ", "label": "Τέλος παρεπιδημούντων / ακαθαρίστων εσόδων (dae.govapp.gr)",
            "periods": out, "status": status}


# ------------------------------------------------------------------ για τον έλεγχο υποβολών
def wanted(conn: sqlite3.Connection, afm: str, year: int) -> set[str]:
    """Ποιες πύλες αφορούν τον πελάτη: από το myDATA (κατηγορίες τελών), τον ΚΑΔ (παρεπιδημούντες) ή προηγούμενο έλεγχο."""
    from . import parepid
    from .mydata.analysis import FEE_GROUPS
    out: set[str] = set()
    for r in conn.execute("SELECT taxes_json FROM mydata_docs WHERE afm=? AND direction='outgoing' AND cancelled=0 "
                          "AND substr(issue_date,1,4)=? AND taxes_json NOT IN ('', '{}')", (afm, str(year))):
        taxes = json.loads(r["taxes_json"] or "{}")
        for key, group in (("ΠΛΑΣΤΙΚΑ", "plastics"), ("ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ", "parepid")):
            kind, cats = FEE_GROUPS[group]
            if any(c in (taxes.get(kind) or {}) for c in cats):
                out.add(key)
    kads = [r["code"] for r in conn.execute("SELECT code FROM business_kad WHERE afm=?", (afm,))]
    if parepid.kad_matches(kads):
        out.add("ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ")
    for r in conn.execute("SELECT DISTINCT obligation FROM obligation_checks WHERE afm=? AND obligation IN "
                          "('ΠΛΑΣΤΙΚΑ','ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ') AND status<>'not_obligated'", (afm,)):
        out.add(r["obligation"])
    return out


def check_entries(conn: sqlite3.Connection, afm: str, year: int,
                  aade_login: Callable[[str, str], dict] = lookup_aade.aade_login,
                  http_factory: Callable[[], _HyperHttp] = _HyperHttp) -> list[dict[str, Any]]:
    """Έλεγχος των πυλών που αφορούν τον πελάτη (`wanted`) με τους κωδικούς TAXISnet ΤΟΥ ΠΕΛΑΤΗ → εγγραφές για το
    `obligation_checks`. Σφάλμα = εγγραφή με status 'error' (όχι εξαίρεση)."""
    keys = wanted(conn, afm, year)
    creds = credentials.get(conn, afm)
    if not keys or not creds:
        return []
    out = []
    if "ΠΛΑΣΤΙΚΑ" in keys:
        try:
            login = aade_login(*creds)
            if not login.get("ok"):
                raise RuntimeError(lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης TAXISnet."))
            from .mydata.analysis import fee_months
            quarters = {(int(ym[5:7]) - 1) // 3 + 1 for ym in fee_months(conn, afm, year, "plastics")}
            out.append(plastics_entry(login["http"], afm, year, liable=quarters))
        except Exception as exc:                        # noqa: BLE001 — δίκτυο / αλλαγή εφαρμογής
            log.warning("plastic %s/%s: %s", afm, year, exc)
            out.append({"obligation": "ΠΛΑΣΤΙΚΑ", "label": "Εισφορά πλαστικών προϊόντων", "status": "error",
                        "periods": [], "detail": f"Ο έλεγχος δεν ολοκληρώθηκε ({str(exc)[:120]})."})
    if "ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ" in keys:
        try:
            http = http_factory()
            login = dae_login(http, *creds)
            if not login["ok"]:
                raise RuntimeError(login["reason"])
            out.append(parepid_entry(http, year))
        except Exception as exc:                        # noqa: BLE001
            log.warning("dae %s/%s: %s", afm, year, exc)
            out.append({"obligation": "ΠΑΡΕΠΙΔΗΜΟΥΝΤΕΣ", "label": "Τέλος παρεπιδημούντων", "status": "error",
                        "periods": [], "detail": f"Ο έλεγχος δεν ολοκληρώθηκε ({str(exc)[:120]})."})
    return out
