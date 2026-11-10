"""Υποχρεώσεις ΦΥΣΙΚΩΝ προσώπων (ιδιώτες, μέλη εταιρειών, ατομικές) που δεν είναι δηλώσεις — ρητό αίτημα 2026-11-03:
«για πελάτες ιδιώτες (και για τα μέλη της εταιρείας) από το recerse-engineer να βρεις τις διαδικασίες ανάκτησης και τις
υποχρεώσεις τους (ε1-ε2-ε3, τέλη κυκλοφορίας κτλπ)». Ports των ροών του runner (επαληθευμένες ζωντανά εκεί):

* **Τέλη κυκλοφορίας** — `configs/aade-traffic-fees.js` (myCAR, REST): `get5Years` → `getAllVehicles` (χωρίς «Επιλογή
  όλων» = κανένα όχημα) → `getChkAfmArkyklPeriod` (`<fiscalyear>`) → `getPrintPDF3` (ειδοποιητήριο PDF). Τα τέλη του έτους
  Υ πληρώνονται έως 31/12 του Υ−1 — γι' αυτό αποθηκεύονται στη χρήση Υ−1 (μήνας αναφοράς Δεκέμβριος).
* **ΕΝΦΙΑ** — `configs/aade-enfia-http.js` (ETAK, Oracle ADF με καθαρό HTTP): είσοδος `_afrLoop` → ViewState → γεγονότα
  `pt1:cbEnter` / `pt1:cbOpenOwnAFM` → επιλογή έτους → «Εκτύπωση Εκκαθαριστικού» (`pt1:clPrintEkk…`). Εκκαθαριστικό =
  υπόχρεος ΕΝΦΙΑ του έτους (δόσεις Μαρτίου–Φεβρουαρίου)· Περιουσιακή κατάσταση από το myPROPERTY (`getPrintPeriousiaki`).
* Ε1/Ε2/Ε3/εκκαθαριστικό φόρου εισοδήματος: `aml/retrieval.fetch_income_fp` (port `aade-income.js`).

Είναι ΠΛΗΡΩΜΕΣ, όχι υποβολές: η εφαρμογή ξέρει ότι εκδόθηκε ειδοποιητήριο/εκκαθαριστικό (άρα υπάρχει υποχρέωση) — όχι
αν εξοφλήθηκε· η εξόφληση φαίνεται στις οφειλές ΑΑΔΕ ή σημειώνεται «Ολοκληρώθηκε». ΜΟΝΟ ανάγνωση.
"""
from __future__ import annotations

import html as _html
import json
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote, urljoin

from . import config
from .business_profiles import credentials, lookup_aade

log = logging.getLogger(__name__)

AADE = lookup_aade.AADE
CAR = AADE + "/saadeapps3/myCAR"
PROPERTY = AADE + "/saadeapps3/myPROPERTY"
_ALL = quote("Επιλογή όλων")
TRAFFIC, ENFIA = "ΤΕΛΗ_ΚΥΚΛΟΦΟΡΙΑΣ", "ΕΝΦΙΑ"
TRAFFIC_LABEL, ENFIA_LABEL = "Τέλη κυκλοφορίας", "ΕΝΦΙΑ"

_ACTION = '<m xmlns="http://oracle.com/richClient/comm"><k v="type"><s>action</s></k></m>'
_VALUECHANGE = ('<m xmlns="http://oracle.com/richClient/comm"><k v="autoSubmit"><b>1</b></k>'
                '<k v="suppressMessageShow"><s>true</s></k><k v="type"><s>valueChange</s></k></m>')


def _pdf(resp: dict[str, Any]) -> Optional[bytes]:
    data = resp.get("content") or b""
    return data if resp.get("status") == 200 and data[:4] == b"%PDF" and len(data) > 500 else None


def _save(folder: Optional[Path], name: str, data: Optional[bytes]) -> str:
    if folder is None or not data:
        return ""
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_bytes(data)
    return str(target)


# ------------------------------------------------------------------ τέλη κυκλοφορίας (myCAR)
def traffic_fees(http: Any, years: list[int], folder_of: Callable[[int], Optional[Path]]) -> dict[int, dict[str, Any]]:
    """{έτος τελών: {status: notice|no_vehicles|not_found|not_available, pdf}}. `folder_of(έτος)` → φάκελος PDF ή None."""
    g = lambda rel: http.follow("GET", CAR + rel)["text"]  # noqa: E731
    http.follow("GET", CAR + "/?#!/roadtax")
    userdata = g("/webresources/carcommon/getuserdata/username")
    m = re.search(r"<afm>\s*(\d{9})\s*</afm>", userdata, re.I) or re.search(r"(\d{9})", re.sub(r"<[^>]+>", " ", userdata))
    if not m:
        raise RuntimeError("myCAR: δεν βρέθηκε ΑΦΜ")
    afm = m.group(1)
    g("/webresources/carcommon/getDownTime/username")
    valid = {int(y) for y in re.findall(r'"validfiscalyear"\s*:\s*"(\d{4})"', g("/webresources/tkcommon/get5Years/"))}
    vehicles: Optional[bool] = None
    out: dict[int, dict[str, Any]] = {}
    for y in years:
        if y not in valid:
            out[y] = {"status": "not_available"}
            continue
        if vehicles is None:
            vehicles = "Επιλογή όλων" in g(f"/webresources/tkcommon/getAllVehicles/{afm}")
        if not vehicles:
            out[y] = {"status": "no_vehicles"}
            continue
        chk = g(f"/webresources/tkcommon/getChkAfmArkyklPeriod/{afm}/{_ALL}/{y}/12")
        if f"<fiscalyear>{y}</fiscalyear>" not in chk:
            out[y] = {"status": "not_found"}
            continue
        target = folder_of(y)
        pdf = ""
        if target is not None:
            existing = target / f"ΤΕΛΗ_ΚΥΚΛΟΦΟΡΙΑΣ_{afm}_{y}.pdf"
            pdf = str(existing) if existing.exists() else _save(
                target, existing.name, _pdf(http.follow("GET", CAR + f"/webresources/tkcommon/getPrintPDF3/{afm}/{_ALL}/{y}/12")))
        out[y] = {"status": "notice", "pdf": pdf}
    return out


def traffic_entry(fee_year: int, res: dict[str, Any]) -> dict[str, Any]:
    """`obligation_checks` για τα τέλη του `fee_year` — αποθηκεύονται στη χρήση fee_year−1 (πληρωμή έως 31/12)."""
    period = {"period": f"Τέλη κυκλοφορίας {fee_year}", "period_end": f"{fee_year - 1}-12-31", "period_type": "year"}
    if res.get("status") == "notice":
        pdf = res.get("pdf") or ""
        return {"obligation": TRAFFIC, "label": TRAFFIC_LABEL, "status": "pending",
                "periods": [{**period, "status": "pending",
                             "detail": "Εκδόθηκε ειδοποιητήριο τελών κυκλοφορίας (myCAR) — η εξόφληση φαίνεται στις "
                                       "οφειλές ΑΑΔΕ",
                             "amounts": {"submission_pdf": pdf} if pdf else {}}]}
    detail = {"no_vehicles": "Κανένα όχημα στο myCAR", "not_found": f"Χωρίς τέλη κυκλοφορίας {fee_year} στο myCAR",
              "not_available": f"Το myCAR δεν έχει ακόμη τα τέλη {fee_year}"}.get(res.get("status", ""), "")
    if res.get("status") == "not_available":
        return {}
    return {"obligation": TRAFFIC, "label": TRAFFIC_LABEL, "status": "not_obligated", "periods": [], "detail": detail}


# ------------------------------------------------------------------ ΕΝΦΙΑ (ETAK — Oracle ADF)
def _dec(s: str) -> str:
    return _html.unescape(s or "")


def _view_state(page: str) -> tuple[str, str]:
    form = re.search(r'<form\b[^>]*\bid="f1"[^>]*>', page, re.I)
    action = (re.search(r'\baction="([^"]*)"', form.group(0), re.I) or [None, ""])[1] if form else ""
    vs = (re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]*)"', page, re.I)
          or re.search(r'value="([^"]*)"[^>]*name="javax\.faces\.ViewState"', page, re.I))
    return _dec(action), vs.group(1) if vs else ""


def year_option(page: str, year: int) -> Optional[tuple[str, bool]]:
    """(value, selected) της επιλογής έτους στο `pt1:yearSelect` — None αν το έτος δεν υπάρχει (όχι υπόχρεος)."""
    sel = re.search(r'id="pt1:yearSelect::content"[\s\S]*?</select>', page, re.I)
    for m in re.finditer(r"<option\b([^>]*)>([\s\S]*?)</option>", sel.group(0) if sel else page, re.I):
        if re.sub(r"<[^>]+>", "", m.group(2)).strip() == str(year):
            return (re.search(r'value="([^"]*)"', m.group(1)) or [None, ""])[1], bool(re.search(r"\bselected\b", m.group(1)))
    return None


def enfia_status(page: str) -> dict[str, bool]:
    text = _dec(page)
    return {"ekk": bool(re.search(r'id="pt1:clPrintEkk\d{4}"', page) or "lnkPrintEkk" in page
                        or "Εκτύπωση Εκκαθαριστικού" in text),
            "doseis": bool(re.search(r"lnkPrintEnfiaDoseis|clPrintEnfiaDoseis", page) or "Δόσεις ΕΝΦΙΑ" in text)}


def enfia(http: Any, years: list[int], folder_of: Callable[[int], Optional[Path]]) -> dict[int, dict[str, Any]]:
    """{έτος: {status: notice|no_notice|not_obligated, pdf, property_pdf}} — port του `aade-enfia-http.js`."""
    def adf(ctrl: str, fields: dict[str, str], extra: Optional[dict[str, str]] = None) -> dict[str, Any]:
        r = http.follow("POST", urljoin(AADE, ctrl), fields, {"Adf-Rich-Message": "true", **(extra or {})})
        m = re.search(r'<redirect url="([^"]*)"', r.get("text") or "", re.I)
        return http.follow("GET", urljoin(AADE, _dec(m.group(1)))) if m else r

    def evt(eid: str, vs: str, val: str, win: str) -> dict[str, str]:
        f = {"org.apache.myfaces.trinidad.faces.FORM": "f1", "javax.faces.ViewState": vs, "event": eid,
             f"event.{eid}": val, "oracle.adf.view.rich.PROCESS": eid}
        if win:
            f["Adf-Window-Id"] = win
        return f

    entry = http.follow("GET", AADE + "/etak/")["text"]
    loop = (re.search(r"'_afrLoop'\s*,\s*'([^']+)'", entry) or [None, ""])[1]
    win = (re.search(r"'_afrPage'\s*,\s*''\s*,\s*'([^']+)'", entry) or [None, ""])[1]
    if not loop:
        raise RuntimeError("ETAK: δεν βρέθηκε _afrLoop")
    jsid = next((kv.get("JSESSIONID-ETAK") for h, kv in getattr(http, "jar", {}).items()
                 if "aade.gr" in h and kv.get("JSESSIONID-ETAK")), "")
    page = http.follow("GET", AADE + "/etak/" + (f";JSESSIONID-ETAK={jsid}" if jsid else "")
                       + f"?_afrLoop={loop}&_afrWindowMode=2&Adf-Window-Id={win}&_afrFS=16&_afrMT=screen")["text"]
    ctrl, vs = _view_state(page)
    for eid in ("pt1:cbEnter", "pt1:j_id_id8", "pt1:cbOpenOwnAFM"):
        if eid != "pt1:cbEnter" and f'"{eid}"' not in page:
            continue
        page = adf(ctrl, evt(eid, vs, _ACTION, win))["text"]
        ctrl, vs = _view_state(page) if _view_state(page)[0] else (ctrl, vs)
    out: dict[int, dict[str, Any]] = {}
    for y in years:
        opt = year_option(page, y)
        if not opt:
            out[y] = {"status": "not_obligated"}
            continue
        main = page
        if not opt[1] and opt[0]:
            main = adf(ctrl, {**evt("pt1:yearSelect", vs, _VALUECHANGE, win), "pt1:yearSelect": opt[0]},
                       {"Adf-Ads-Page-Id": "1"})["text"]
            nctrl, nvs = _view_state(main)
            if nctrl:
                ctrl, vs = nctrl, nvs
        st = enfia_status(main)
        res: dict[str, Any] = {"status": "notice" if st["ekk"] else "no_notice", "pdf": "", "property_pdf": ""}
        folder = folder_of(y)
        if folder is not None and st["ekk"]:
            existing = folder / f"ΕΝΦΙΑ_ΕΚΚΑΘΑΡΙΣΤΙΚΟ_{y}.pdf"
            if existing.exists():
                res["pdf"] = str(existing)
            else:
                ekk_id = (re.search(r'id="(pt1:clPrintEkk\d{4})"', main) or [None, ""])[1]
                if ekk_id:
                    res["pdf"] = _save(folder, existing.name, _pdf(http.follow(
                        "POST", urljoin(AADE, ctrl), {**evt(ekk_id, vs, _ACTION, win), "pt1:yearSelect": opt[0]})))
        out[y] = res
    if any(folder_of(y) is not None for y in years):
        try:
            _property_pdfs(http, out, folder_of)
        except Exception as exc:                        # noqa: BLE001 — η περιουσιακή είναι επιπλέον
            log.warning("myPROPERTY: %s", exc)
    return out


def _property_pdfs(http: Any, out: dict[int, dict[str, Any]], folder_of: Callable[[int], Optional[Path]]) -> None:
    """Περιουσιακή κατάσταση του έτους από το myPROPERTY (ίδια συνεδρία) — `getPrintPeriousiaki/{afm}/{έτος}/{afm}`."""
    w = PROPERTY + "/webresources/capitalcommon"
    http.follow("GET", PROPERTY + "?")
    try:
        afm = json.loads(http.follow("GET", w + "/getuserdata/username")["text"] or "{}").get("afm") or ""
    except ValueError:
        afm = ""
    if not afm:
        return
    for y, res in out.items():
        folder = folder_of(y)
        if folder is None or res.get("status") == "not_obligated":
            continue
        target = folder / f"ΠΕΡΙΟΥΣΙΑΚΗ_ΚΑΤΑΣΤΑΣΗ_{y}.pdf"
        res["property_pdf"] = str(target) if target.exists() else _save(
            folder, target.name, _pdf(http.follow("GET", f"{w}/getPrintPeriousiaki/{afm}/{y}/{afm}")))


def enfia_entry(year: int, res: dict[str, Any]) -> dict[str, Any]:
    """`obligation_checks` «ΕΝΦΙΑ» του έτους: μία περίοδος ανά δόση (Μάρτιος … Φεβρουάριος του επόμενου)."""
    if res.get("status") != "notice":
        return {"obligation": ENFIA, "label": ENFIA_LABEL, "status": "not_obligated", "periods": [],
                "detail": "Χωρίς εκκαθαριστικό ΕΝΦΙΑ του έτους στην ΑΑΔΕ"}
    pdfs = [p for p in (res.get("pdf"), res.get("property_pdf")) if p]
    amounts = {"submission_pdf": pdfs[0], "submission_pdfs": pdfs} if pdfs else {}
    periods = []
    for n, (y, m) in enumerate([(year, m) for m in range(3, 13)] + [(year + 1, 1), (year + 1, 2)], start=1):
        periods.append({"period": f"{n}η δόση ΕΝΦΙΑ {year}", "period_end": f"{y}-{m:02d}-28", "period_type": "month",
                        "status": "pending", "amounts": amounts,
                        "detail": "Εκδόθηκε εκκαθαριστικό ΕΝΦΙΑ — η εξόφληση φαίνεται στις οφειλές ΑΑΔΕ"})
    return {"obligation": ENFIA, "label": ENFIA_LABEL, "status": "pending", "periods": periods}


# ------------------------------------------------------------------ ένας πελάτης
def check_entries(conn: sqlite3.Connection, afm: str, year: int, want_pdf: bool = False,
                  login_fn: Callable[[str, str], dict] = lookup_aade.aade_login) -> dict[int, list[dict[str, Any]]]:
    """{χρήση αποθήκευσης: [entries]} — τέλη κυκλοφορίας `year`+1 (πληρωμή έως 31/12/`year`) και `year`, ΕΝΦΙΑ `year`.
    Με τους κωδικούς TAXISnet του ίδιου του πελάτη· χωρίς κωδικούς: τίποτα."""
    creds = credentials.get(conn, afm)
    if not creds:
        return {}
    login = login_fn(*creds)
    if not login.get("ok"):
        return {}
    http = login["http"]
    base = config.data_dir() / "submissions" / afm
    out: dict[int, list[dict[str, Any]]] = {}
    try:
        fees = traffic_fees(http, [year + 1, year], lambda y: base / str(y - 1) if want_pdf else None)
        for fee_year, res in fees.items():
            e = traffic_entry(fee_year, res)
            if e:
                out.setdefault(fee_year - 1, []).append(e)
    except Exception as exc:                            # noqa: BLE001 — κάθε πύλη ανεξάρτητη
        log.warning("myCAR %s: %s", afm, exc)
    try:
        for y, res in enfia(http, [year], lambda y: base / str(y) if want_pdf else None).items():
            out.setdefault(y, []).append(enfia_entry(y, res))
    except Exception as exc:                            # noqa: BLE001
        log.warning("ETAK %s: %s", afm, exc)
    return out


def fetch_traffic(conn: sqlite3.Connection, afm: str, fee_year: int,
                  login_fn: Callable[[str, str], dict] = lookup_aade.aade_login) -> dict[str, Any]:
    """Μόνο το ειδοποιητήριο τελών κυκλοφορίας `fee_year` (myCAR) — για «Λήψη & αποστολή» από την καρτέλα πελάτη
    (ρητό αίτημα 2026-11-10, όπως στο Tax Center: «τέλη κυκλοφορίας 2027»). Αποθηκεύεται στη χρήση fee_year−1.
    {ok, reason, status, pdf}."""
    from .easycheck import store_results
    creds = credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί TAXISnet του πελάτη."}
    login = login_fn(*creds)
    if not login.get("ok"):
        return {"ok": False, "reason": lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης TAXISnet.")}
    folder = config.data_dir() / "submissions" / afm / str(fee_year - 1)
    res = traffic_fees(login["http"], [fee_year], lambda _y: folder).get(fee_year, {})
    entry = traffic_entry(fee_year, res)
    if entry:
        store_results(conn, afm, fee_year - 1, [entry])
    reason = {"no_vehicles": "Κανένα όχημα στο myCAR.", "not_found": f"Δεν βρέθηκαν τέλη κυκλοφορίας {fee_year}.",
              "not_available": f"Το myCAR δεν έχει ακόμη τα τέλη {fee_year}."}.get(res.get("status", ""), "")
    return {"ok": res.get("status") == "notice", "reason": reason, "status": res.get("status", ""),
            "pdf": res.get("pdf", "")}


def traffic_pdf(conn: sqlite3.Connection, afm: str, fee_year: int) -> str:
    """Το ειδοποιητήριο τελών `fee_year` που έχει ήδη κατέβει ('' αν δεν υπάρχει)."""
    row = conn.execute("SELECT amounts_json FROM obligation_checks WHERE afm=? AND year=? AND obligation=?",
                       (afm, fee_year - 1, TRAFFIC)).fetchone()
    pdf = (json.loads(row["amounts_json"] or "{}").get("submission_pdf") if row else "") or ""
    return pdf if pdf and Path(pdf).exists() else ""
