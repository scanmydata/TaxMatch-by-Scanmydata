"""Έλεγχοι υποβολής για τη διαφήμιση (ρητό αίτημα 2026-11-02) — καθαρό HTTP, port του runner του χρήστη:

* **Ειδική εισφορά διαδικτύου 2% ΕΔΟΕΑΠ (ΑΚΕΕΔ)** — `lib/akeed-http.js` + `scripts/akeed-edoeap-http-reports.js`:
  apps.e-efka.gov.gr/EDOEAPeACD → apps.ika.gr consumer → GSIS OAuth1 (TAXISnet) → 2ο στάδιο `login.xhtml` με ΑΦΜ +
  **captcha** → `ACDs.xhtml` (πίνακας `ACDsForm:ACDsResults`: Περίοδος «2026/9», κόστος διαφήμισης, εισφορά, ημερομηνίες).
  Captcha: ddddocr ΑΝ είναι διαθέσιμο (`scripts/captcha-solve-ddddocr.py` του runner — ~280 MB με onnxruntime/OpenCV,
  δεν μπαίνει στον installer), αλλιώς τη λύνει ο χρήστης (`ask_captcha` — παράθυρο της εφαρμογής). Στον
  χρονοπρογραμματισμό χωρίς ddddocr ο έλεγχος ΑΚΕΕΔ παραλείπεται.
* **Τέλος διαφήμισης 2% υπέρ δήμων (ΤΠΔ)** — `lib/tpd-http.js` + `configs/tpd-diaformos.js`: e-services.tpd.gr
  (OAuth2 GSIS + υποχρεωτική συγκατάθεση `confirmationForm`) → `requests-service/v2/history/` (αιτήματα «Πληρωμή Τελών
  Διαφήμισης», requestTypeId 2) → `proceed` με την ενέργεια «Προβολή Ιστορικού» (μόνο ανάγνωση) → πεδία (περίοδος
  ΜΜ/ΕΕΕΕ, ποσά) + `PdfFile` (base64 PDF του σημειώματος). ΠΟΤΕ `initiate` / `payment/*`.

Μόνο ανάγνωση. Κωδικοί: TAXISnet του πελάτη· ποτέ σε log."""
from __future__ import annotations

import base64
import html as _html
import json
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from . import config
from .business_profiles import credentials
from .business_profiles.lookup_aade import _HyperHttp  # noqa: PLC2701 — ίδιος http client με τις άλλες πύλες

log = logging.getLogger(__name__)

AKEED_APP = "https://apps.e-efka.gov.gr/EDOEAPeACD"
AKEED_CONSUMER = "https://apps.ika.gr/gsis-edoeap-consumer"
TPD = "https://e-services.tpd.gr"
AJAX = {"X-Requested-With": "XMLHttpRequest", "Faces-Request": "partial/ajax"}
REASONS_EL = {"NoCredentials": "Δεν υπάρχουν κωδικοί TAXISnet του πελάτη.",
              "InvalidCredentials": "Λάθος κωδικοί TAXISnet.",
              "NoLoginForm": "Δεν φόρτωσε η σελίδα σύνδεσης TAXISnet.",
              "CaptchaRequired": "Χρειάζεται ο κωδικός ασφαλείας (captcha) της εφαρμογής ΕΔΟΕΑΠ.",
              "CaptchaRejected": "Ο κωδικός ασφαλείας (captcha) δεν έγινε δεκτός.",
              "CaptchaFormParse": "Άλλαξε η σελίδα σύνδεσης της ΕΔΟΕΑΠ.",
              "NoTable": "Δεν φόρτωσε ο πίνακας των ΑΚΕΕΔ (άλλαξε η σελίδα;).",
              "NoAuthorizeRedirect": "Δεν ξεκίνησε η σύνδεση στο e-services του ΤΠΔ.",
              "NoConsentAction": "Άλλαξε η σελίδα συγκατάθεσης του ΤΠΔ.",
              "EndpointsForbidden": "Το e-services του ΤΠΔ δεν δέχτηκε τη σύνδεση."}


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _view_state(page: str) -> str:
    m = (re.search(r'name="(?:javax|jakarta)\.faces\.ViewState"[^>]*value="([^"]*)"', page or "") or
         re.search(r'value="([^"]*)"[^>]*name="(?:javax|jakarta)\.faces\.ViewState"', page or "") or
         re.search(r'<update id="[^"]*ViewState[^"]*"><!\[CDATA\[([^\]]*)', page or ""))
    return m.group(1) if m else ""


# ---------------------------------------------------------------- captcha (ddddocr αν υπάρχει)
def ddddocr_available() -> bool:
    try:
        import ddddocr  # noqa: F401 — προαιρετικό
        return True
    except Exception:                                   # noqa: BLE001 — ImportError / DLL
        return False


_OCR: list[Any] = []


def solve_captcha(image: bytes) -> str:
    """ddddocr (default + beta, όπως `captcha-solve-ddddocr.py`): κοινή απάντηση 4–7 χαρακτήρων, αλλιώς η πρώτη.
    '' αν το ddddocr δεν υπάρχει. Χωρίς την προεπεξεργασία OpenCV του runner — το ddddocr τη χρειάζεται σπάνια."""
    try:
        from ddddocr import DdddOcr
    except Exception:                                   # noqa: BLE001
        return ""
    if not _OCR:
        _OCR.extend([DdddOcr(show_ad=False), DdddOcr(beta=True, show_ad=False)])
    guesses = []
    for model in _OCR:
        try:
            g = re.sub(r"[^a-z0-9]", "", (model.classification(image) or "").lower())
        except Exception:                               # noqa: BLE001
            g = ""
        if 4 <= len(g) <= 7:
            guesses.append(g)
    if len(guesses) == 2 and guesses[0] == guesses[1]:
        return guesses[0]
    return guesses[0] if guesses else ""


# ---------------------------------------------------------------- ΑΚΕΕΔ (ΕΔΟΕΑΠ)
def _legacy_tls(http: _HyperHttp) -> None:
    """Ο apps.ika.gr δέχεται μόνο κρυπτογραφήσεις χωρίς ECDHE (π.χ. AES256-GCM-SHA384) — με τις προεπιλογές της
    OpenSSL 3 «SSLV3_ALERT_HANDSHAKE_FAILURE» (ζωντανά 2026-11-02). SECLEVEL=1 ΜΟΝΟ για αυτόν τον host."""
    import ssl
    from requests.adapters import HTTPAdapter

    class _Adapter(HTTPAdapter):
        def init_poolmanager(self, *args, **kwargs):
            ctx = ssl.create_default_context()
            ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            kwargs["ssl_context"] = ctx
            return super().init_poolmanager(*args, **kwargs)
    session = getattr(http, "session", None)
    if session is not None and hasattr(session, "mount"):
        session.mount("https://apps.ika.gr/", _Adapter())


def akeed_login(http: _HyperHttp, user: str, password: str, afm: str,
                ask_captcha: Optional[Callable[[bytes], str]] = None, attempts: int = 4) -> dict[str, Any]:
    """{ok, reason}. Ροή του `akeedDoeapLogin` (runner): landing → consumer → gsis/serviceACD → AAA login.do →
    αυτόματες συνέχειες → `login.xhtml` με ΑΦΜ + captcha (ajax `mainForm`) → index.xhtml."""
    _legacy_tls(http)
    http.follow("GET", AKEED_APP + "/")
    http.follow("GET", AKEED_CONSUMER + "/indexACD.xhtml")
    r = http.follow("GET", AKEED_CONSUMER + "/gsis/serviceACD")
    if "j_password" not in r["text"]:
        return {"ok": False, "reason": "NoLoginForm"}
    act = (re.search(r'<form\b[^>]*action="([^"]*)"[^>]*>(?:(?!</form>).)*?j_username', r["text"], re.S | re.I) or
           re.search(r'<form\b(?:(?!</form>).)*?j_username(?:(?!</form>).)*?action="([^"]*)"', r["text"], re.S | re.I))
    if not act:
        return {"ok": False, "reason": "NoLoginForm"}
    r = http.follow("POST", urljoin(r["url"], _html.unescape(act.group(1))), {"j_username": user, "j_password": password})
    if "login.jsp" in r["url"].lower() and re.search(r"name=[\"']j_password", r["text"], re.I):
        return {"ok": False, "reason": "InvalidCredentials"}
    for _ in range(4):                                  # window.location / κρυφές φόρμες oauth
        wl = re.search(r"window\.location\s*=\s*'([^']+)'", r["text"])
        if wl:
            r = http.follow("GET", urljoin(r["url"], _html.unescape(wl.group(1))))
            continue
        hf = re.search(r'<form\b[^>]*action="([^"]*)"((?:(?!</form>).)*)', r["text"], re.S | re.I)
        if hf and re.search(r"name=[\"'](oauth_token|oauth_verifier|target|requestToken|authorize)", hf.group(2), re.I) \
                and "j_password" not in hf.group(2):
            fields = dict(re.findall(r'<input\b[^>]*name="([^"]*)"[^>]*value="([^"]*)"', hf.group(2), re.I))
            r = http.follow("POST", urljoin(r["url"], _html.unescape(hf.group(1))), fields)
            continue
        break
    tries = 0
    while 'name="captchaID"' in r["text"] and tries < attempts:
        tries += 1
        src = re.search(r'id="captchaImage"[^>]*src="([^"]*)"', r["text"]) or re.search(
            r'<img[^>]*src="([^"]*captcha[^"]*)"', r["text"], re.I)
        image = http.follow("GET", urljoin(r["url"], _html.unescape(src.group(1)) if src
                                           else "/EDOEAPeACD/resources/captcha.jpg"))["content"]
        code = solve_captcha(image) or (ask_captcha(image) if ask_captcha else "")
        if not code:
            return {"ok": False, "reason": "CaptchaRequired"}
        btn = re.search(r'<button\b[^>]*id="([^"]*)"[^>]*onclick="[^"]*mainForm', r["text"]) or \
            re.search(r'<button\b[^>]*id="([^"]*)"[^>]*type="submit"', r["text"])
        action = (re.search(r'<form[^>]*id="mainForm"[^>]*action="([^"]*)"', r["text"]) or
                  re.search(r'<form[^>]*action="([^"]*)"[^>]*id="mainForm"', r["text"]))
        vs = _view_state(r["text"])
        if not btn or not vs:
            return {"ok": False, "reason": "CaptchaFormParse"}
        prefix = "jakarta" if "jakarta.faces" in r["text"] else "javax"
        r = http.follow("POST", urljoin(r["url"], _html.unescape(action.group(1)) if action else "login.xhtml"), {
            f"{prefix}.faces.partial.ajax": "true", f"{prefix}.faces.source": btn.group(1),
            f"{prefix}.faces.partial.execute": "mainForm", f"{prefix}.faces.partial.render": "mainForm",
            "mainForm": "mainForm", btn.group(1): btn.group(1), "afm": afm, "captchaID": code,
            f"{prefix}.faces.ViewState": vs}, AJAX)
        redirect = re.search(r'<redirect url="([^"]+)"', r["text"])
        if redirect:
            r = http.follow("GET", urljoin(r["url"], _html.unescape(redirect.group(1))))
        elif 'name="captchaID"' not in r["text"]:
            r = http.follow("GET", AKEED_APP + "/index.xhtml")
        if 'name="captchaID"' in r["text"] and "<partial-response" in r["text"]:
            r = http.follow("GET", r["url"] if "login.xhtml" in r["url"] else AKEED_CONSUMER + "/gsis/serviceACD")
    if 'name="captchaID"' in r["text"]:
        return {"ok": False, "reason": "CaptchaRejected"}
    return {"ok": True, "reason": ""}


def parse_acds(page: str) -> list[dict[str, Any]]:
    """Γραμμές του πίνακα ΑΚΕΕΔ (data-ri) → [{period: 'YYYY-MM', cells: [...]}]. Οι γραμμές περιέχουν εμφωλευμένα
    <tr> (tooltips) — το σώμα κόβεται από την αρχή μιας γραμμής ως την επόμενη (όπως ο runner)."""
    starts = list(re.finditer(r'<tr\b[^>]*data-ri="(\d+)"[^>]*class="[^"]*ui-datatable-(?:odd|even)[^"]*"[^>]*>', page or ""))
    out = []
    for i, m in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else min(len(page), m.end() + 8000)
        body = page[m.end():end]
        cells = [_text(c) for c in re.findall(r'<td\b[^>]*role="gridcell"[^>]*>(.*?)</td>', body, re.S)]
        period = next((re.search(r"(\d{4})/(\d{1,2})", c) for c in cells if re.search(r"^\d{4}/\d{1,2}\b", c)), None)
        # σύνδεσμος «αναφορά» της γραμμής (ReportForm) — για το PDF της ΑΚΕΕΔ
        report = re.search(r'id="(ACDsForm:ACDsResults:\d+:j_idt\d+)"[^>]*onclick="PrimeFaces\.ab\(\{s:&quot;\1&quot;,'
                           r'u:&quot;ReportForm', body)
        if period:
            out.append({"period": f"{period.group(1)}-{int(period.group(2)):02d}", "cells": cells,
                        "report": report.group(1) if report else ""})
    return out


def akeed_report_pdf(http: _HyperHttp, page: str, row: dict[str, Any], target: Path) -> str:
    """PDF της ΑΚΕΕΔ μιας περιόδου — port του `akeed-edoeap-http-reports.js` (3 POST PrimeFaces partial-ajax:
    σύνδεσμος γραμμής → ReportDialog → ReportDownloadDialog → PDF). ViewState ξανά μετά από ΚΑΘΕ απόκριση
    (αλλιώς ViewExpiredException). '' αν δεν ήρθε PDF."""
    if not row.get("report"):
        return ""
    url = AKEED_APP + "/ACDs.xhtml"
    vs = _view_state(page)
    prefix = "jakarta" if "jakarta.faces" in page else "javax"
    anchor = row["report"]
    r = http.follow("POST", url, {
        f"{prefix}.faces.partial.ajax": "true", f"{prefix}.faces.source": anchor,
        f"{prefix}.faces.partial.execute": "@all", f"{prefix}.faces.partial.render": "ReportForm", anchor: anchor,
        "ACDsForm": "ACDsForm", "ACDsForm:criteria-PERIOD_YEAR": "", "ACDsForm:ACDsResults_rppDD": "10",
        f"{prefix}.faces.ViewState": vs}, AJAX)
    vs = _view_state(r["text"]) or vs
    btn = re.search(r'id="(ReportForm:j_idt\d+)"[^>]*onclick="PrimeFaces\.ab\(', r["text"])
    rbtn = btn.group(1) if btn else "ReportForm:j_idt210"
    r = http.follow("POST", url, {
        f"{prefix}.faces.partial.ajax": "true", f"{prefix}.faces.source": rbtn, f"{prefix}.faces.partial.execute": "@all",
        f"{prefix}.faces.partial.render": "ReportForm ACDsForm:ACDsResults ACDsForm:ACDsCriteria ACDsForm:ACDsButtons",
        rbtn: rbtn, "ReportForm": "ReportForm", f"{prefix}.faces.ViewState": vs}, AJAX)
    if r["is_pdf"]:
        res = r
    else:
        vs = _view_state(r["text"]) or vs
        dl = re.search(r'id="(ReportDownloadForm:j_idt\d+)"[^>]*onclick="PrimeFaces\.(?:bcn|ab)', r["text"])
        dsrc = dl.group(1) if dl else "ReportDownloadForm:j_idt215"
        res = http.follow("POST", url, {
            f"{prefix}.faces.partial.ajax": "true", f"{prefix}.faces.source": dsrc, f"{prefix}.faces.partial.execute": "@all",
            f"{prefix}.faces.partial.render": "ReportDownloadForm", dsrc: dsrc, "ReportDownloadForm": "ReportDownloadForm",
            f"{prefix}.faces.ViewState": vs}, AJAX)
    if not res["is_pdf"]:
        return ""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(res["content"])
    return str(target)


def akeed_entry(rows: list[dict[str, Any]], year: int, liable: set[str], today: Optional[date] = None) -> dict[str, Any]:
    """`obligation_checks` «ΑΚΕΕΔ»: μήνας με γραμμή στον πίνακα = υποβλήθηκε· ληγμένος μήνας που ο πελάτης έχει
    υποχρέωση (`liable`) χωρίς γραμμή = δεν υποβλήθηκε."""
    from .obligations import MONTHS_NOM, month_end
    today = today or date.today()
    by = {r["period"]: r for r in rows}
    periods = []
    for m in range(1, 13):
        ym = f"{year}-{m:02d}"
        due = month_end(*((year + 1, 1) if m == 12 else (year, m + 1)))
        base = {"period": f"{MONTHS_NOM[m - 1]} {year}", "period_end": month_end(year, m).isoformat(),
                "period_type": "month"}
        if ym in by:
            pdf = by[ym].get("pdf") or ""
            periods.append({**base, "status": "submitted", "detail": "ΑΚΕΕΔ: " + " · ".join(by[ym]["cells"][:5]),
                            "amounts": {"submission_pdf": pdf} if pdf else {}})
        elif ym in liable and due < today:
            periods.append({**base, "status": "not_submitted", "detail": "Καμία ΑΚΕΕΔ για τον μήνα στην εφαρμογή ΕΔΟΕΑΠ"})
    status = ("not_submitted" if any(p["status"] == "not_submitted" for p in periods)
              else "submitted" if periods else "not_obligated")
    return {"obligation": "ΑΚΕΕΔ", "label": "Ειδική εισφορά διαδικτύου ΕΔΟΕΑΠ (ΑΚΕΕΔ)", "status": status,
            "periods": periods, "detail": "" if periods else "Καμία ΑΚΕΕΔ της χρήσης στην εφαρμογή ΕΔΟΕΑΠ."}


def _liable_months(conn: sqlite3.Connection, afm: str, rule_id: str, year: int) -> set[str]:
    from . import deadlines, obligations
    business = next((b for b in deadlines._businesses(conn) if b["afm"] == afm), None)  # noqa: SLF001
    if business is None:
        return set()
    return {f"{year}-{m:02d}" for m in range(1, 13)
            if obligations.liability(rule_id, business, f"{year}-{m:02d}")[0] == "yes"}


def check_akeed(conn: sqlite3.Connection, afm: str, year: int, ask_captcha: Optional[Callable[[bytes], str]] = None,
                http_factory: Callable[[], _HyperHttp] = _HyperHttp, want_pdf: bool = False) -> list[dict[str, Any]]:
    """Έλεγχος ΑΚΕΕΔ της χρήσης — μόνο για πελάτες με «Ειδική εισφορά διαδικτύου» στο Προφίλ. Χωρίς ddddocr και
    χωρίς `ask_captcha` (π.χ. χρονοπρογραμματισμός) παραλείπεται σιωπηλά — δεν είναι αποτυχία."""
    row = conn.execute("SELECT internet_contribution FROM businesses WHERE afm=?", (afm,)).fetchone()
    if not row or not row["internet_contribution"]:
        return []
    creds = credentials.get(conn, afm)
    if not creds:
        return [_error("ΑΚΕΕΔ", "Ειδική εισφορά διαδικτύου ΕΔΟΕΑΠ (ΑΚΕΕΔ)", REASONS_EL["NoCredentials"])]
    if ask_captcha is None and not ddddocr_available():
        return []
    try:
        http = http_factory()
        lg = akeed_login(http, *creds, afm=afm, ask_captcha=ask_captcha)
        if not lg["ok"]:
            return [_error("ΑΚΕΕΔ", "Ειδική εισφορά διαδικτύου ΕΔΟΕΑΠ (ΑΚΕΕΔ)", REASONS_EL.get(lg["reason"], lg["reason"]))]
        page = http.follow("GET", AKEED_APP + "/ACDs.xhtml")
        if "ACDsForm:ACDsResults" not in page["text"]:
            return [_error("ΑΚΕΕΔ", "Ειδική εισφορά διαδικτύου ΕΔΟΕΑΠ (ΑΚΕΕΔ)", REASONS_EL["NoTable"])]
        folder = config.data_dir() / "submissions" / afm / str(year)
        pages = [(page["text"], parse_acds(page["text"]))]
        for n in range(2, 7):                           # σελιδοποίηση ώσπου να περάσουμε σε παλαιότερο έτος
            seen = [r["period"] for _p, rs in pages for r in rs]
            if not seen or min(seen) < f"{year}-01":
                break
            nxt = http.follow("GET", AKEED_APP + f"/ACDs.xhtml?page={n}")["text"]
            more = [r for r in parse_acds(nxt) if r["period"] not in seen]
            if not more:
                break
            pages.append((nxt, more))
        rows = []
        for text, page_rows in pages:
            for r in page_rows:
                if not r["period"].startswith(str(year)):
                    continue
                target = folder / f"ΑΚΕΕΔ_{afm}_{r['period'].replace('-', '')}.pdf"
                if target.exists():
                    r["pdf"] = str(target)
                elif want_pdf:
                    try:
                        r["pdf"] = akeed_report_pdf(http, text, r, target)
                    except Exception as exc:            # noqa: BLE001 — το PDF είναι επιπλέον
                        log.warning("akeed pdf %s %s: %s", afm, r["period"], exc)
                rows.append(r)
        return [akeed_entry(rows, year, _liable_months(conn, afm, "internet_contribution", year))]
    except Exception as exc:                            # noqa: BLE001 — δίκτυο / αλλαγή σελίδας
        log.warning("akeed %s/%s: %s", afm, year, exc)
        return [_error("ΑΚΕΕΔ", "Ειδική εισφορά διαδικτύου ΕΔΟΕΑΠ (ΑΚΕΕΔ)", f"Ο έλεγχος δεν ολοκληρώθηκε ({str(exc)[:120]}).")]


def _error(key: str, label: str, detail: str) -> dict[str, Any]:
    return {"obligation": key, "label": label, "status": "error", "periods": [], "detail": detail}


# ---------------------------------------------------------------- ΤΠΔ e-services (τέλος διαφήμισης)
def tpd_login(http: _HyperHttp, user: str, password: str) -> dict[str, Any]:
    """OAuth2 GSIS → συγκατάθεση (`confirmationForm`: user_oauth_approval + το ΤΣΕΚΑΡΙΣΜΕΝΟ radio scope.read — χωρίς
    αυτό κάθε endpoint δίνει 403, όπως βρήκε ο runner) → επιστροφή στο e-services με SESSION. {ok, reason, types}."""
    kick = http._once("GET", TPD + "/user-service/oauth2/authorization/taxis",          # noqa: SLF001
                      {"Accept": "text/html", "Referer": TPD + "/login"})
    auth = urljoin(TPD + "/", kick.headers.get("Location") or "")
    if not kick.headers.get("Location"):
        return {"ok": False, "reason": "NoAuthorizeRedirect"}
    page = http.follow("GET", auth, headers={"Accept": "text/html"})
    if 'id="login-form"' in page["text"]:
        act = re.search(r'<form\b[^>]*id="login-form"[^>]*action="([^"]*)"', page["text"])
        cred_url = urljoin("https://oauth2.gsis.gr/oauth2server/login.jsp", act.group(1) if act else "j_spring_security_check")
        page = http.follow("POST", cred_url, {"j_username": user, "j_password": password}, {"Referer": auth})
        if re.search(r"failedLogin|authentication_error|incorrect username", page["text"], re.I):
            return {"ok": False, "reason": "InvalidCredentials"}
        if 'id="confirmationForm"' not in page["text"] and page["status"] >= 400:
            page = http.follow("GET", auth)
    if 'id="confirmationForm"' in page["text"]:
        block = re.search(r'<form\b[^>]*id="confirmationForm".*?</form>', page["text"], re.S | re.I).group(0)
        act = re.search(r'<form\b[^>]*action="([^"]*)"', block)
        if not act:
            return {"ok": False, "reason": "NoConsentAction"}
        fields = {}
        for tag in re.findall(r"<input\b[^>]*>", block, re.I):
            if re.search(r'type="radio"', tag, re.I) and "checked" not in tag.lower():
                continue
            name, value = re.search(r'name="([^"]*)"', tag), re.search(r'value="([^"]*)"', tag)
            if name:
                fields[name.group(1)] = value.group(1) if value else ""
        http.follow("POST", urljoin(page["url"], _html.unescape(act.group(1))), fields, {"Referer": auth})
    probe = http.follow("GET", TPD + "/requests-service/v2/requests/", headers={"Accept": "application/json"})
    try:
        types = json.loads(probe["text"]) if probe["status"] == 200 else None
    except ValueError:
        types = None
    if types is None:
        return {"ok": False, "reason": "EndpointsForbidden"}
    return {"ok": True, "reason": "", "types": types}


def tpd_notices(http: _HyperHttp, folder: Optional[Path] = None) -> list[dict[str, Any]]:
    """Ιστορικό «Πληρωμή Τελών Διαφήμισης» → [{request_id, status, updated, period_from, period_to, amount, fee, pdf}].
    Μόνο η ενέργεια ανάγνωσης («Προβολή Ιστορικού», id 5)· ποτέ πληρωμή/νέο αίτημα."""
    hist = json.loads(http.follow("GET", TPD + "/requests-service/v2/history/", headers={"Accept": "application/json"})["text"] or "[]")
    out = []
    for x in hist:
        if str(x.get("requestTypeId")) != "2":
            continue
        unsafe = re.compile(r"pay|initiate|submit|delete|cancel|πληρωμ|υποβολ|διαγραφ|ακύρ", re.I)
        acts = [a for a in x.get("actions") or [] if not unsafe.search(str(a.get("name", "")))]
        act = next((a for a in acts if str(a.get("id")) == "5" or re.search(r"ιστορικ|προβολ|view", str(a.get("name", "")), re.I)),
                   None)
        item = {"request_id": x.get("requestId", ""), "status": x.get("uiStatus") or x.get("status", ""),
                "updated": x.get("lastUpdated", ""), "period_from": "", "period_to": "", "amount": "", "fee": "", "pdf": ""}
        if act:
            pr = http.post_raw(TPD + "/requests-service/v2/proceed", json.dumps(
                {"requestTypeId": x.get("requestTypeId"), "actionId": act.get("id"), "requestId": x.get("requestId")}))
            try:
                fields = {f.get("fieldName"): f.get("fieldValue") for f in (json.loads(pr["text"]) or {}).get("fields", [])}
            except ValueError:
                fields = {}
            item.update(period_from=fields.get("PaymentDateFrom") or "", period_to=fields.get("PaymentDateTo") or "",
                        amount=fields.get("PaymentAmount") or "", fee=fields.get("FinalPaymentAmount") or "")
            pdf = fields.get("PdfFile") or ""
            if pdf and folder is not None:
                data = base64.b64decode(pdf)
                if data[:4] == b"%PDF":
                    folder.mkdir(parents=True, exist_ok=True)
                    path = folder / f"ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ_{str(item['request_id'])[:8]}.pdf"
                    path.write_bytes(data)
                    item["pdf"] = str(path)
        out.append(item)
    return out


def tpd_entry(notices: list[dict[str, Any]], year: int, liable: bool) -> dict[str, Any]:
    """`obligation_checks` «ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ» για τη χρήση `year` (ετήσιο — πληρώνεται τον επόμενο Ιούλιο)."""
    mine = [n for n in notices if str(year) in (n["period_from"] + n["period_to"]) or
            (not n["period_from"] and n["updated"][:4] == str(year + 1))]
    period = {"period": f"Χρήση {year}", "period_end": f"{year}-12-31", "period_type": "year"}
    if mine:
        pdfs = [n["pdf"] for n in mine if n["pdf"]]
        amounts = {"submission_pdfs": pdfs, "submission_pdf": pdfs[0]} if pdfs else {}
        detail = "; ".join(f"{n['period_from']}–{n['period_to']} · δαπάνη {n['amount'] or '—'} € · τέλος {n['fee'] or '—'} € "
                           f"({n['status']})" for n in mine)
        return {"obligation": "ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ", "label": "Τέλος διαφήμισης 2% (ΤΠΔ)", "status": "submitted",
                "periods": [{**period, "status": "submitted", "detail": detail, "amounts": amounts}]}
    due = date(year + 1, 7, 15)
    if liable and due < date.today():
        return {"obligation": "ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ", "label": "Τέλος διαφήμισης 2% (ΤΠΔ)", "status": "not_submitted",
                "periods": [{**period, "status": "not_submitted",
                             "detail": "Καμία πληρωμή τελών διαφήμισης της χρήσης στο e-services του ΤΠΔ"}]}
    return {"obligation": "ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ", "label": "Τέλος διαφήμισης 2% (ΤΠΔ)", "status": "not_obligated",
            "periods": [], "detail": "Καμία πληρωμή τελών διαφήμισης της χρήσης στο ΤΠΔ."}


def check_tpd(conn: sqlite3.Connection, afm: str, year: int, want_pdf: bool = False,
              http_factory: Callable[[], _HyperHttp] = _HyperHttp) -> list[dict[str, Any]]:
    """Τέλος διαφήμισης της χρήσης `year` — μόνο όταν η υποχρέωση προκύπτει (E3_585_015 στο myDATA) ή ήδη ελέγχθηκε."""
    liable = any(f.get("advert") for ym, f in _month_flags_of(conn, afm).items() if ym.startswith(str(year)))
    checked = conn.execute("SELECT 1 FROM obligation_checks WHERE afm=? AND obligation='ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ'",
                           (afm,)).fetchone()
    if not liable and not checked:
        return []
    creds = credentials.get(conn, afm)
    if not creds:
        return [_error("ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ", "Τέλος διαφήμισης 2% (ΤΠΔ)", REASONS_EL["NoCredentials"])]
    try:
        http = http_factory()
        lg = tpd_login(http, *creds)
        if not lg["ok"]:
            return [_error("ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ", "Τέλος διαφήμισης 2% (ΤΠΔ)", REASONS_EL.get(lg["reason"], lg["reason"]))]
        folder = config.data_dir() / "submissions" / afm / str(year) if want_pdf else None
        return [tpd_entry(tpd_notices(http, folder), year, liable)]
    except Exception as exc:                            # noqa: BLE001
        log.warning("tpd %s/%s: %s", afm, year, exc)
        return [_error("ΤΕΛΟΣ_ΔΙΑΦΗΜΙΣΗΣ", "Τέλος διαφήμισης 2% (ΤΠΔ)", f"Ο έλεγχος δεν ολοκληρώθηκε ({str(exc)[:120]}).")]


def _month_flags_of(conn: sqlite3.Connection, afm: str) -> dict[str, dict[str, Any]]:
    from .mydata import analysis
    return analysis.month_flags(conn).get(afm, {})


def google_candidates(conn: sqlite3.Connection) -> list[str]:
    """Πελάτες με 14.3 από Google στο myDATA που ΔΕΝ έχουν ενεργοποιήσει την ειδική εισφορά διαδικτύου — πρόταση."""
    from .mydata import analysis
    flags = analysis.month_flags(conn)
    off = {r["afm"] for r in conn.execute("SELECT afm FROM businesses WHERE internet_contribution IS NULL")}
    return sorted(a for a, months in flags.items() if a in off and any(f.get("google_ads") for f in months.values()))
