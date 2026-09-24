"""Αυτόματη λήψη εγγράφων του φακέλου δέουσας επιμέλειας (καθαρό HTTP, χωρίς browser):

* **Εκτύπωση στοιχείων Μητρώου ΑΑΔΕ** — myAADE `saadeapps3/comregistry` → `getPrintPDFepixSection` (τμήμα
  επιχείρησης· σημαία νομικού προσώπου όταν δεν υπάρχει μητρώο φυσικού· αλλιώς τμήμα φυσικού προσώπου).
* **Δήλωση φορολογίας εισοδήματος** — νομικά πρόσωπα: Έντυπο Ν (ΦΕΝΠ, `taxisnet/income`)· φυσικά πρόσωπα: Ε1 και Ε3
  (`webtax/incomefp`).
* **Κεντρικό Μητρώο Πραγματικών Δικαιούχων (Κ.Μ.Π.Δ.)** — «Εκτύπωση δικαιούχων» + «Βεβαίωση οριστικοποίησης»
  (`webapps.gsis.gr/dsae/boregistry`, JSF). ΓΙΑ ΕΤΑΙΡΕΙΑ: κωδικοί του ΝΟΜΙΜΟΥ ΕΚΠΡΟΣΩΠΟΥ + ΑΦΜ εταιρείας.

Python port των reverse-engineered flows του χρήστη (`recerse-engineer/runner/configs/aade-registry.js`,
`aade-fenp.js`, `aade-income.js`, `lib/kmpd-http.js` — επαληθευμένα ζωντανά εκεί). ΜΟΝΟ ανάγνωση/εκτυπώσεις:
ΠΟΤΕ υποβολή/τροποποιητική/οριστικοποίηση. Κωδικοί ποτέ σε logs· τα PDF μένουν στον φάκελο δεδομένων
(`<data>/aml_docs/<ΑΦΜ>/`) και καταγράφονται στο `aml_files` (db v9). Τρέχει ΠΑΝΤΑ σε background thread με ΔΙΚΗ του
σύνδεση βάσης (βλ. CLAUDE.md §Κανόνες).
"""
from __future__ import annotations

import html
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlencode, urljoin

from .. import config, crypto, db
from ..business_profiles import credentials, lookup_aade
from . import kmpd_pdf, store

log = logging.getLogger(__name__)

AADE = lookup_aade.AADE
KMPD_APP = "https://webapps.gsis.gr/dsae/boregistry"

#: kind -> κλειδί της λίστας εγγράφων (content.DOCUMENTS_BY_KIND)
DOC_KEY_BY_KIND = {"registry": "aade", "income_n": "tax_return", "income_e1": "tax_return", "income_e3": "tax_return",
                   "kmpd": "ubo_registry", "kmpd_cert": "ubo_registry"}
KIND_LABEL = {"registry": "Στοιχεία Μητρώου ΑΑΔΕ", "income_n": "Έντυπο Ν", "income_e1": "Ε1", "income_e3": "Ε3",
              "kmpd": "ΚΜΠΔ - Δικαιούχοι", "kmpd_cert": "ΚΜΠΔ - Βεβαίωση οριστικοποίησης"}

_ROW = re.compile(r'<tr\b[^>]*class="tblRow[12]"[^>]*>(.*?)</tr>', re.S | re.I)
_TD = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)


def _strip(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _rows(page: str) -> list[list[str]]:
    return [_TD.findall(r) for r in _ROW.findall(page or "")]


def _pdf(resp: dict[str, Any]) -> Optional[bytes]:
    data = resp.get("content") or b""
    return data if resp.get("status") == 200 and data[:4] == b"%PDF" and len(data) > 500 else None


# ------------------------------------------------------------------ Μητρώο ΑΑΔΕ
def registry_print_url(afm: str, section: str) -> str:
    """section: 'business' (νομικό), 'business_natural' (ατομική), 'person' (φυσικό πρόσωπο)."""
    w = AADE + "/saadeapps3/comregistry/webresources/infomytaxisnet/getPrintPDFepixSection/" + afm
    if section == "person":
        return w + "/0/0/0/1/0/1/1/0/0/0/0/1/0/0/0/3"
    return w + "/1/1/1/1/" + ("1" if section == "business" else "0") + "/1/1/1/1/0/0/0/0/0/0/4"


def fetch_registry(http: Any, afm: str) -> tuple[Optional[bytes], str]:
    """(PDF, τμήμα). Προτιμά το τμήμα επιχείρησης (αν υπάρχει έναρξη)· αλλιώς του φυσικού προσώπου."""
    reg = AADE + "/saadeapps3/comregistry"
    w = reg + "/webresources/infomytaxisnet"
    http.follow("GET", reg + "/")
    http.follow("GET", w + "/getuserdata/username")
    fysiko = http.follow("GET", w + "/getMhtrwoFusikou/" + afm)["text"]
    is_physical = f"<afm>{afm}</afm>" in fysiko
    epix = http.follow("GET", w + "/getMhtrwoEpixeirhshs/" + afm)["text"]
    if "<hmenarxhs>" in epix:
        section = "business_natural" if is_physical else "business"
        data = _pdf(http.follow("GET", registry_print_url(afm, section)))
        if data:
            return data, section
    if is_physical:
        return _pdf(http.follow("GET", registry_print_url(afm, "person"))), "person"
    return None, ""


# ------------------------------------------------------------------ Έντυπο Ν (ΦΕΝΠ)
def _call_args(page: str, fn: str) -> Optional[list[str]]:
    """Ορίσματα της ΚΛΗΣΗΣ `fn(document.form, a, b…);` (όχι του ορισμού της συνάρτησης), χωρίς το form."""
    m = re.search(re.escape(fn) + r"\s*\(\s*document\.[A-Za-z0-9_]+([^;]*?)\)\s*;", page)
    if not m:
        return None
    return [a.replace('"', " ").replace("'", " ").strip() for a in m.group(1).lstrip(",").split(",") if a.strip()]


def fenp_list_params(landing: str, reference_year: int) -> Optional[dict[str, str]]:
    """Η γραμμή «Επεξεργασία Δηλώσεων» για τη χρήση reference_year-1 → παράμετροι λίστας δηλώσεων."""
    for tds in _rows(landing):
        if len(tds) < 3:
            continue
        m = re.search(r"(\d{4})", _strip(tds[0]).split("-")[0])
        if not m or int(m.group(1)) != reference_year - 1:
            continue
        if _strip(tds[2]).replace(" ", "") != "ΕπεξεργασίαΔηλώσεων":
            continue
        i = tds[2].find("doDisplayDeclarationsList(")
        if i < 0:
            continue
        a = [x.replace('"', " ").replace("'", " ").strip() for x in tds[2][i + 26: tds[2].find(");", i)].split(",")]
        if len(a) >= 8:
            return {"declarationType": a[1], "year": a[2], "periodType": a[3], "periodStart": a[4], "periodEnd": a[5],
                    "effectivePeriodStart": a[6], "effectivePeriodEnd": a[7]}
    return None


def fenp_pdf_params(listing: str) -> Optional[dict[str, str]]:
    a = _call_args(listing, "doViewPdfTaxisnet")
    if a and len(a) >= 2:
        return {"declarationDatabaseId": a[0], "declarationType": a[1]}
    a = _call_args(listing, "doViewPdfTaxis")
    if a and len(a) >= 12 and "income" in a[0].lower():
        return {"taxisPK.num": a[3], "taxisPK.doy": a[4], "taxisPK.year": a[5], "taxisPK.taxArea": a[6],
                "taxisPK.docType": a[7], "effectivePeriod.start": a[8], "effectivePeriod.end": a[9],
                "submissionDate": a[10], "submissionType": a[1], "declarationType": a[0], "periodType": a[2],
                "referenceYear": a[11]}
    return None


def fetch_income_n(http: Any, reference_year: int) -> Optional[bytes]:
    """Έντυπο Ν του έτους αναφοράς (χρήση reference_year-1). Μόνο προβολή του ήδη υποβληθέντος PDF."""
    p = lambda rel: AADE + "/taxisnet/income/protected/" + rel  # noqa: E731
    http.follow("GET", p("displayActorRoles.htm"))
    types = http.follow("POST", p("displayDeclarationTypes.htm"), {"actorRole": "SELF_SERVICE"})["text"]
    m = re.search(r'<form[^>]*name="gotoincomeN"[^>]*action="([^"]*)"', types, re.I) or \
        re.search(r'<form[^>]*action="([^"]*)"[^>]*name="gotoincomeN"', types, re.I)
    if not m:
        return None
    dtype = (re.search(r'name="declarationType"[^>]*value="([^"]*)"', types, re.I) or [None, "incomeN"])[1]
    landing = http.follow("GET", urljoin(p(""), html.unescape(m.group(1))) + "?" +
                          urlencode({"declarationType": dtype, "year": str(reference_year)}))["text"]
    params = fenp_list_params(landing, reference_year)
    if not params:
        return None
    listing = http.follow("GET", p("displayDeclarationsList.htm") + "?" + urlencode(params))["text"]
    pdf_params = fenp_pdf_params(listing)
    return _pdf(http.follow("POST", p("viewPdf.htm"), pdf_params)) if pdf_params else None


# ------------------------------------------------------------------ Ε1 / Ε3 (φυσικά πρόσωπα)
_INCOME_FORMS = {"E1": ("e1_print", "e1_print", "PBE1_PRINT_PDF", "PRINT_CODE"),
                 "E3": ("e3_print", "e3_print", "PBE3_PRINT_PDF", "PRINT_CODE_E3")}


def income_form_available(menu: str, button: str, code: str) -> bool:
    m = re.search(r'<(?:button|input)[^>]*name="' + button + '"[^>]*>', menu, re.I)
    if m:
        return "disabled" not in m.group(0).lower()
    return bool(code)


def fetch_income_fp(http: Any, year: int, forms: tuple[str, ...] = ("E1", "E3")) -> list[tuple[str, bytes]]:
    """Ε1/Ε3 του φορολογικού έτους `year` (μόνο για έτη ≥ 2023 — νεότερος μηχανισμός εκτύπωσης)."""
    if year < 2023:
        return []
    h = lambda rel: AADE + "/webtax/incomefp/" + rel  # noqa: E731
    menu = http.follow("GET", h(f"year{year}-income-menu.do"))["text"]
    code = lambda name: (re.search(r'name="' + name + r'"[^>]*value="([^"]*)"', menu, re.I) or [None, ""])[1]  # noqa: E731
    out = []
    for form in forms:
        flag_key, flag_val, button, code_name = _INCOME_FORMS[form]
        if not income_form_available(menu, button, code(code_name)):
            continue
        base: dict[str, str] = {"YEAR": str(year), "e1_print": "", "e3_print": "", "print_e2_ypo": "", "print_e2_syz": ""}
        if year == 2023:
            base.update({"PRINT_CODE": code("PRINT_CODE"), "PRINT_CODE_SYZ": code("PRINT_CODE_SYZ"),
                         "PRINT_CODE_E2": code("PRINT_CODE_E2"), "PRINT_CODE_E2_SYZYGOY": code("PRINT_CODE_E2_SYZYGOY"),
                         f"PBMod{year}": ""})
        data = _pdf(http.follow("POST", h(f"year{year}-income-menuPrint.do"), {**base, flag_key: flag_val}))
        if data:
            out.append((form, data))
    return out


# ------------------------------------------------------------------ Κ.Μ.Π.Δ. (JSF / PrimeFaces)
def jsf_viewstate(page: str) -> str:
    m = re.search(r'name="javax\.faces\.ViewState"[^>]*value="([^"]*)"', page) or \
        re.search(r'<update id="[^"]*ViewState[^"]*"><!\[CDATA\[([^\]]*)\]\]', page)
    return m.group(1) if m else ""


def jsf_partial_redirect(xml: str) -> str:
    m = re.search(r'<redirect url="([^"]*)"', xml)
    return html.unescape(m.group(1)) if m else ""


def jsf_data_rows(page: str) -> list[tuple[str, list[str]]]:
    return [(html.unescape(rk), [_strip(c) for c in _TD.findall(body)])
            for rk, body in re.findall(r'<tr\b[^>]*data-rk="([^"]*)"[^>]*>(.*?)</tr>', page, re.S | re.I)]


def jsf_button_id(page: str, label: str) -> str:
    for bid, inner in re.findall(r'<(?:button|a)\b[^>]*\bid="([^"]*)"[^>]*>(.*?)</(?:button|a)>', page, re.S | re.I):
        if _strip(inner) == label:
            return html.unescape(bid)
    m = re.search(r'PrimeFaces\.ab\(\{s:"([^"]+)"', page)
    return html.unescape(m.group(1)) if m else ""


KMPD_TRIGGERS = {"kmpd": "form1:tabViewMainForm:print", "kmpd_cert": "form1:tabViewMainForm:print_bebaiosi"}


def fetch_kmpd(user: str, password: str, afm: str, http_factory: Callable[[], Any] = lookup_aade._HyperHttp
               ) -> dict[str, Any]:
    """{ok, name, files:[(kind, bytes)], reason}. user/password = του νόμιμου εκπροσώπου· afm = της εταιρείας."""
    http = http_factory()
    r = http.follow("GET", KMPD_APP)
    req = re.search(r'name="request_id"[^>]*value="([^"]*)"', r["text"]) or \
        re.search(r'value="([^"]*)"[^>]*name="request_id"', r["text"])
    action = re.search(r'<form[^>]*\baction="([^"]*)"', r["text"], re.I)
    if not req or not action:
        return {"ok": False, "reason": "NoLoginForm", "files": []}
    r = http.follow("POST", urljoin(r["url"], html.unescape(action.group(1))),
                    {"username": user, "password": password, "request_id": req.group(1), "btn_login": ""})
    if 'name="request_id"' in r["text"] and 'name="password"' in r["text"]:
        return {"ok": False, "reason": "InvalidCredentials", "files": []}
    if "boregistry" not in r["url"]:
        r = http.follow("GET", KMPD_APP + "/faces/pages/mainmenu/index.xhtml")
    base = re.sub(r"/faces/pages/mainmenu/.*$", "/faces/pages/mainmenu/", r["url"])
    jsid = (re.search(r";jsessionid=([^?]+)", r["url"]) or [None, ""])[1]
    url = lambda name: base + name + (f";jsessionid={jsid}" if jsid else "")  # noqa: E731
    vs = jsf_viewstate(r["text"])
    if not vs:
        return {"ok": False, "reason": "NoViewState", "files": []}
    ajax = {"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/xml, text/xml, */*; q=0.01"}
    r = http.follow("POST", url("index.xhtml"), {
        "javax.faces.partial.ajax": "true", "javax.faces.source": "form1:enterButton", "javax.faces.partial.execute": "@all",
        "javax.faces.partial.render": "form1", "form1:enterButton": "form1:enterButton", "form1": "form1",
        "javax.faces.ViewState": vs}, headers=ajax)
    red = jsf_partial_redirect(r["text"])
    vs = jsf_viewstate(r["text"]) or vs
    r = http.follow("GET", urljoin(base, red) if red else url("selectrole.xhtml"))
    vs = jsf_viewstate(r["text"]) or vs
    entity = next(((rk, cells) for rk, cells in jsf_data_rows(r["text"]) if afm in cells), None)
    if entity is None:
        http.follow("POST", url("selectrole.xhtml"), {
            "javax.faces.partial.ajax": "true", "javax.faces.source": "form1:radioDT",
            "javax.faces.partial.execute": "form1:radioDT", "javax.faces.partial.render": "form1:radioDT",
            "form1:radioDT": "form1:radioDT", "form1:radioDT_pagination": "true", "form1:radioDT_first": "0",
            "form1:radioDT_rows": "100", "form1:radioDT_rppDD": "100", "form1": "form1", "javax.faces.ViewState": vs},
            headers=ajax)
        r = http.follow("GET", url("selectrole.xhtml"))
        vs = jsf_viewstate(r["text"]) or vs
        entity = next(((rk, cells) for rk, cells in jsf_data_rows(r["text"]) if afm in cells), None)
    if entity is None:
        return {"ok": False, "reason": "AfmNotFound", "files": []}
    rk, cells = entity
    name = next((c for c in cells if c and c != afm and re.search(r"[Α-Ωα-ω]", c) and len(c) > 3), "")
    enter = jsf_button_id(r["text"], "Είσοδος") or "form1:j_idt51"
    r = http.follow("POST", url("selectrole.xhtml"), {
        "javax.faces.partial.ajax": "true", "javax.faces.source": enter, "javax.faces.partial.execute": "@all",
        "javax.faces.partial.render": "form1", enter: enter, "form1": "form1", "form1:radioDT_radio": "on",
        "form1:radioDT_rppDD": "5", "form1:radioDT_selection": rk, "javax.faces.ViewState": vs}, headers=ajax)
    red = jsf_partial_redirect(r["text"])
    vs = jsf_viewstate(r["text"]) or vs
    r = http.follow("GET", urljoin(base, red) if red else url("entrance.xhtml"))
    vs = jsf_viewstate(r["text"]) or vs
    files = []
    for kind, trigger in KMPD_TRIGGERS.items():
        data = _pdf(http.follow("POST", url("entrance.xhtml"), {
            "form1": "form1", trigger: "", "form1:tabViewMainForm:eidosId_filter": "",
            "form1:tabViewMainForm:countryCompId_filter": "", "form1:tabViewMainForm:countryReprId_filter": "",
            "form1:tabViewMainForm:ownerHolderSelectionDt_selection": "", "form1:tabViewMainForm_activeIndex": "0",
            "javax.faces.ViewState": vs}))
        if data:
            files.append((kind, data))
    return {"ok": bool(files), "name": name, "files": files, "reason": "" if files else "NoPdf"}


# ------------------------------------------------------------------ κωδικοί νόμιμου εκπροσώπου
def get_rep_credentials(conn: sqlite3.Connection, afm: str) -> Optional[tuple[str, str]]:
    row = conn.execute("SELECT taxis_user, taxis_pass FROM aml_rep_credentials WHERE afm=?", (afm,)).fetchone()
    if not row or not row["taxis_user"] or not row["taxis_pass"]:
        return None
    return crypto.dec(row["taxis_user"]), crypto.dec(row["taxis_pass"])


def set_rep_credentials(conn: sqlite3.Connection, afm: str, user: str, password: str) -> bool:
    """Κενό password κρατά τον υπάρχοντα (όπως στους κωδικούς πελάτη). Ποτέ δεν επιστρέφεται ο κωδικός στο UI."""
    user, password = (user or "").strip(), password or ""
    if not user and not password:
        return False
    old = conn.execute("SELECT taxis_user, taxis_pass FROM aml_rep_credentials WHERE afm=?", (afm,)).fetchone()
    conn.execute("INSERT INTO aml_rep_credentials(afm, taxis_user, taxis_pass, updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(afm) DO UPDATE SET taxis_user=excluded.taxis_user, taxis_pass=excluded.taxis_pass, "
                 "updated_at=excluded.updated_at",
                 (afm, crypto.enc(user) if user else (old["taxis_user"] if old else ""),
                  crypto.enc(password) if password else (old["taxis_pass"] if old else ""), db.utcnow()))
    return True


def clear_rep_credentials(conn: sqlite3.Connection, afm: str) -> None:
    conn.execute("DELETE FROM aml_rep_credentials WHERE afm=?", (afm,))


def rep_user(conn: sqlite3.Connection, afm: str) -> str:
    row = conn.execute("SELECT taxis_user FROM aml_rep_credentials WHERE afm=?", (afm,)).fetchone()
    return crypto.dec(row["taxis_user"]) if row and row["taxis_user"] else ""


# ------------------------------------------------------------------ αρχεία φακέλου
def client_dir(afm: str) -> Path:
    return config.data_dir() / "aml_docs" / afm


def _safe(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name or "")).strip()[:120]


def save_file(conn: sqlite3.Connection, afm: str, kind: str, data: bytes, client_name: str = "", suffix: str = "",
              source: str = "aade") -> dict[str, Any]:
    """Αποθηκεύει το PDF, το καταγράφει και σημειώνει το αντίστοιχο έγγραφο ως παραληφθέν (σήμερα)."""
    folder = client_dir(afm)
    folder.mkdir(parents=True, exist_ok=True)
    fname = _safe(" - ".join(x for x in (client_name, afm, KIND_LABEL.get(kind, kind), suffix) if x)) + ".pdf"
    path = folder / fname
    path.write_bytes(data)
    doc_key = DOC_KEY_BY_KIND.get(kind, "")
    conn.execute("INSERT INTO aml_files(afm, doc_key, kind, filename, path, bytes, source, retrieved_at) "
                 "VALUES (?,?,?,?,?,?,?,?)", (afm, doc_key, kind, fname, str(path), len(data), source, db.utcnow()))
    profile = store.get_profile(conn, afm)
    if doc_key and doc_key not in profile["docs"]:
        docs = {**profile["docs"], doc_key: date.today().isoformat()}
        store.save_profile(conn, afm, pep_status=profile["pep_status"], relationship_start=profile["relationship_start"],
                           relationship_end=profile["relationship_end"], purpose=profile["purpose"], kyc=profile["kyc"],
                           notes=profile["notes"], docs=docs)
    return {"kind": kind, "filename": fname, "path": str(path), "bytes": len(data), "doc_key": doc_key}


def import_kmpd_owners(conn: sqlite3.Connection, afm: str, data: bytes) -> dict[str, Any]:
    """Διαβάζει την «Εκτύπωση δικαιούχων» του ΚΜΠΔ και συμπληρώνει τον φάκελο: πίνακας δικαιούχων (συγχώνευση — ΠΕΠ/
    ταυτότητα που έγραψε ο χρήστης μένουν), κενά στοιχεία εκπροσώπου, σημείωση με αριθμό καταχώρισης. Κατάσταση
    δικαιούχων: μόνο αν ήταν κενή → «εκκρεμεί ταυτοποίηση» (η ταυτοποίηση με έγγραφα παραμένει δουλειά του λογιστή).
    -> {ok, added, updated, rep_filled, registration_no, reason}"""
    try:
        parsed = kmpd_pdf.parse_owners(data)
    except kmpd_pdf.KmpdPdfError as exc:
        return {"ok": False, "reason": str(exc)}
    if parsed.get("entity_afm") and parsed["entity_afm"] != afm:
        return {"ok": False, "reason": f"το PDF αφορά άλλο ΑΦΜ ({parsed['entity_afm']})"}
    found = kmpd_pdf.to_ubos(parsed)
    profile = store.get_profile(conn, afm)
    ubos, added, updated = kmpd_pdf.merge_ubos(profile["ubos"], found)
    rep = dict(profile["rep"])
    rep_filled = 0
    for key, value in (parsed.get("rep") or {}).items():
        if value and not rep.get(key):
            rep[key] = value
            rep_filled += 1
    if rep_filled and not rep.get("role"):
        rep["role"] = "Νόμιμος εκπρόσωπος (ΚΜΠΔ)"
    note = (f"ΚΜΠΔ: αρ. καταχώρισης {parsed.get('registration_no') or '—'}, τελευταία τροποποίηση "
            f"{parsed.get('modified') or '—'} (εκτύπωση {parsed.get('printed') or '—'}).")
    notes = profile["ubo_notes"]
    if note not in notes:
        notes = "\n".join(x for x in (re.sub(r"(?m)^ΚΜΠΔ: αρ\. καταχώρισης.*$\n?", "", notes).strip(), note) if x)
    state = profile["ubo_state"] or ("pending" if found else "")
    store.save_profile(conn, afm, pep_status=profile["pep_status"], relationship_start=profile["relationship_start"],
                       relationship_end=profile["relationship_end"], purpose=profile["purpose"], kyc=profile["kyc"],
                       notes=profile["notes"], ubos=ubos, rep=rep, ubo_notes=notes, ubo_state=state)
    return {"ok": True, "added": added, "updated": updated, "rep_filled": rep_filled, "owners": len(found),
            "registration_no": parsed.get("registration_no", ""), "reason": "",
            # για τον ανοιχτό διάλογο: να συγχωνεύσει και στη δική του (ίσως μη αποθηκευμένη) κατάσταση
            "found": found, "rep": parsed.get("rep") or {}, "note": note}


def list_files(conn: sqlite3.Connection, afm: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM aml_files WHERE afm=? ORDER BY retrieved_at DESC, id DESC", (afm,))]


# ------------------------------------------------------------------ ορχήστρωση
TARGETS = (("registry", "Στοιχεία Μητρώου ΑΑΔΕ"), ("income", "Δήλωση εισοδήματος (Ν ή Ε1/Ε3)"),
           ("kmpd", "Μητρώο Πραγματικών Δικαιούχων (ΚΜΠΔ)"))
REASON_EL = {**lookup_aade.REASONS_EL, "NoLoginForm": "Δεν φόρτωσε η σελίδα σύνδεσης.",
             "NoViewState": "Η σελίδα του ΚΜΠΔ δεν αναγνωρίστηκε.",
             "AfmNotFound": "Ο εκπρόσωπος δεν εμφανίζεται να εκπροσωπεί αυτό το ΑΦΜ στο ΚΜΠΔ.",
             "NoPdf": "Δεν επιστράφηκε PDF."}


def retrieve_for_client(conn: sqlite3.Connection, afm: str, targets: tuple[str, ...] = ("registry", "income", "kmpd"),
                        progress: Callable[[str], None] = lambda _m: None, legal: Optional[bool] = None,
                        login: Callable[[str, str], dict[str, Any]] = lookup_aade.aade_login,
                        kmpd: Callable[..., dict[str, Any]] = fetch_kmpd, today: Optional[date] = None) -> dict[str, Any]:
    """Κατεβάζει ό,τι ζητήθηκε. Επιστρέφει {files:[…], errors:[…]} — μερική επιτυχία είναι φυσιολογική."""
    today = today or date.today()
    b = conn.execute("SELECT name, legal_form, activity_state FROM businesses WHERE afm=?", (afm,)).fetchone()
    name = b["name"] if b else ""
    if legal is None:
        kind = store.effective_kind(store.get_profile(conn, afm), b["legal_form"] if b else "", b["activity_state"] if b else "")
        legal = kind in ("company", "partnership", "public", "trust")
    files: list[dict[str, Any]] = []
    errors: list[str] = []
    ubo_import: Optional[dict[str, Any]] = None

    if "registry" in targets or "income" in targets:
        creds = credentials.get(conn, afm)
        if not creds:
            errors.append("Μητρώο/δήλωση: δεν υπάρχουν κωδικοί TAXISnet του πελάτη (καρτέλα πελάτη → Κωδικοί TAXISnet).")
        else:
            progress("Σύνδεση στο TAXISnet…")
            res = login(*creds)
            if not res.get("ok"):
                errors.append("TAXISnet: " + REASON_EL.get(res.get("reason", ""), res.get("reason", "")))
            else:
                http = res["http"]
                if "registry" in targets:
                    progress("Εκτύπωση στοιχείων Μητρώου…")
                    try:
                        data, section = fetch_registry(http, afm)
                        if data:
                            files.append(save_file(conn, afm, "registry", data, name))
                        else:
                            errors.append("Μητρώο: δεν επιστράφηκε PDF.")
                    except Exception as exc:                               # δίκτυο / αλλαγή σελίδας
                        errors.append(f"Μητρώο: {str(exc)[:120]}")
                if "income" in targets:
                    progress("Δήλωση φορολογίας εισοδήματος…")
                    try:
                        got = False
                        if legal:
                            for ref in (today.year, today.year - 1):
                                data = fetch_income_n(http, ref)
                                if data:
                                    files.append(save_file(conn, afm, "income_n", data, name, f"χρήση {ref - 1}"))
                                    got = True
                                    break
                        else:
                            for year in (today.year - 1, today.year - 2):
                                for form, data in fetch_income_fp(http, year):
                                    files.append(save_file(conn, afm, f"income_{form.lower()}", data, name, str(year)))
                                    got = True
                                if got:
                                    break
                        if not got:
                            errors.append("Δήλωση εισοδήματος: δεν βρέθηκε υποβληθείσα δήλωση των δύο τελευταίων ετών.")
                    except Exception as exc:
                        errors.append(f"Δήλωση εισοδήματος: {str(exc)[:120]}")

    if "kmpd" in targets:
        if not legal:
            errors.append("ΚΜΠΔ: αφορά νομικά πρόσωπα/οντότητες — παραλείφθηκε.")
        else:
            rep = get_rep_credentials(conn, afm)
            if not rep:
                errors.append("ΚΜΠΔ: δεν υπάρχουν κωδικοί TAXISnet του νόμιμου εκπροσώπου.")
            else:
                progress("Μητρώο Πραγματικών Δικαιούχων…")
                try:
                    r = kmpd(rep[0], rep[1], afm)
                    for kind, data in r.get("files", []):
                        files.append(save_file(conn, afm, kind, data, name or r.get("name", ""), source="kmpd"))
                        if kind == "kmpd":
                            imp = import_kmpd_owners(conn, afm, data)
                            if imp["ok"]:
                                ubo_import = imp
                            else:
                                errors.append("ΚΜΠΔ: ο πίνακας δικαιούχων δεν διαβάστηκε — " + imp["reason"])
                    if not r.get("ok"):
                        errors.append("ΚΜΠΔ: " + REASON_EL.get(r.get("reason", ""), r.get("reason", "")))
                except Exception as exc:
                    errors.append(f"ΚΜΠΔ: {str(exc)[:120]}")
    log.info("[aml-retrieval] %s: %d αρχεία, %d σφάλματα", afm, len(files), len(errors))
    return {"files": files, "errors": errors, "ubo_import": ubo_import}
