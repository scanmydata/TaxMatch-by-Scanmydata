"""e-ΕΦΚΑ εργοδότη: ΑΜΕ (Αριθμός Μητρώου Εργοδότη) και έλεγχος υποβολής ΑΠΔ από την οικονομική καρτέλα εργοδότη.

Η ΑΠΔ υποβάλλεται στον e-ΕΦΚΑ, όχι στην ΑΑΔΕ — γι' αυτό ο έλεγχος υποβολών της ΑΑΔΕ (easycheck) δεν την έβλεπε ποτέ
(αναφορά χρήστη 2026-10-22). Πηγές (μεθοδολογίες του χρήστη στο `recerse-engineer/out/methodologies/`):

* **Οικονομική καρτέλα εργοδότη** (`EFKA_employer_card.md`, ScanMyData Tax Center `efka-employer-card-ssp.js`, LIVE
  επαληθευμένο εκεί, ΚΑΘΑΡΟ HTTP): `services.e-efka.gov.gr` KeyCloak → «Είσοδος Εργοδότη» (`social-taxisnet-employer`)
  → TAXISnet της επιχείρησης → ρόλος `external-employer` με ΑΦΜ + **ΑΜΕ** → `ssp.efka.emp-transactions-ext/secure/
  transactions.xhtml` → «Εμφάνιση» (partial-ajax) → πίνακας `linesTable`: Ημ/νία · Είδος Κίνησης (100 = ΑΠΔ) · Αρ.
  Παραστατικού · Περίοδος · Χρέωση · Πίστωση · Υπόλοιπο. Κίνηση ΑΠΔ για μια περίοδο = η ΑΠΔ του μήνα υποβλήθηκε.
* **ΑΜΕ** (`EFKA_AME_retrieval.md`, runner `configs/efka-ame.js`): «Πιστοποίηση Εργοδοτών» (`apps.e-efka.gov.gr/ePistErg`)
  → «Κοινών Επιχειρήσεων» → `AMEs.xhtml` → `#AMEsForm:AMEsResults_data`, 1ο κελί. Ο runner το κάνει με BROWSER (JS
  redirects, υποχρεωτικό «Παράλειψη» ενημέρωσης στοιχείων)· εδώ με καθαρό HTTP, ζωντανά επαληθευμένο (ίδια βήματα με το
  `LoginEfkaOldWayTaxinet` του TaxSystem) — αν αποτύχει, ο ΑΜΕ δίνεται από το Excel (στήλη «ΑΜΕ») ή χειροκίνητα.
  Με κωδικούς **ΙΚΑ εργοδότη** (Excel) η σύνδεση eAccess γίνεται με `j_security_check` (runner `efkaErgodLogin`).

**ΜΟΝΟ ανάγνωση** — ποτέ υποβολή ΑΠΔ. **ΔΕΝ έχει δοκιμαστεί ζωντανά από την εφαρμογή** (tests σε ψεύτικο HTTP με τη
δομή των μεθοδολογιών)· η μορφή της στήλης «Περίοδος» δεν είναι γνωστή με βεβαιότητα, ο parser είναι ανεκτικός.
"""
from __future__ import annotations

import html as _html
import logging
import re
import sqlite3
from datetime import date
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from . import crypto, db
from .business_profiles import credentials as taxis_credentials
from .business_profiles.lookup_aade import _HyperHttp  # noqa: PLC2701 — ίδιος http client με το Μητρώο ΑΑΔΕ

log = logging.getLogger(__name__)

SVC = "https://services.e-efka.gov.gr/"
HOME = SVC + "ssp.commonservices.home/views/secure/index.xhtml"
TX = SVC + "ssp.efka.emp-transactions-ext/secure/transactions.xhtml"
APPS = "https://apps.e-efka.gov.gr"
GSIS_CHECK = "https://oauth2.gsis.gr/oauth2server/j_spring_security_check"
GSIS_AUTHORIZE = "https://oauth2.gsis.gr/oauth2server/oauth/authorize"
AJAX = {"Faces-Request": "partial/ajax", "X-Requested-With": "XMLHttpRequest"}
APD_KIND = "100"                          # «Είδος Κίνησης» 100 = ΑΠΔ (EFKA_employer_card.md)
PAGE_ROWS = 20

REASONS_EL = {
    "InvalidCredentials": "Λάθος κωδικοί TAXISnet / ΙΚΑ εργοδότη.",
    "NoAme": "Λείπει ο ΑΜΕ (Αριθμός Μητρώου Εργοδότη) — συμπληρώστε τον στην καρτέλα ή στο Excel (στήλη «ΑΜΕ»).",
    "NoCredentials": "Δεν υπάρχουν κωδικοί TAXISnet του πελάτη.",
    "NoAmka": "Λείπει ο ΑΜΚΑ του πελάτη (καρτέλα πελάτη → Κωδικοί, ή στήλη «ΑΜΚΑ» του Excel).",
    "LegalNoAmka": "Οι εταιρείες δεν έχουν ΑΜΚΑ — συνδέονται ως εργοδότες με ΑΜΕ.",
    "HomeFormEmployer": "Δεν φόρτωσε η σελίδα εισόδου εργοδότη του e-ΕΦΚΑ.",
    "SelectRole": "Ο e-ΕΦΚΑ δεν ζήτησε ρόλο (άλλαξε η σελίδα;).",
    "LandPage": "Ο e-ΕΦΚΑ απέρριψε τον ρόλο εργοδότη — ελέγξτε ότι ο ΑΜΕ ανήκει σε αυτόν τον ΑΦΜ.",
    "NoTransactions": "Δεν φόρτωσε η οικονομική καρτέλα εργοδότη (άλλαξε η σελίδα;).",
    "AmeNotFound": "Δεν βρέθηκε ΑΜΕ (ο πελάτης δεν είναι εγγεγραμμένος εργοδότης;).",
    "NoPistErg": "Δεν ολοκληρώθηκε η είσοδος στην «Πιστοποίηση Εργοδοτών» του e-ΕΦΚΑ. Ελέγξτε τους κωδικούς και την πρόσβαση εργοδότη.",
    "AfmMismatch": "Οι κωδικοί TAXISnet ανήκουν σε άλλο ΑΦΜ από τον πελάτη.",
    "NoToken": "Το myAMKA δεν ολοκλήρωσε τη σύνδεση (amka.gr).",
    "NotFound": "Το myAMKA δεν επέστρεψε ΑΜΚΑ για αυτόν τον λογαριασμό.",
}


# ------------------------------------------------------------------ κωδικοί / ΑΜΕ
def normalize_ame(value: Any) -> str:
    """ΑΜΕ = 10 ψηφία (π.χ. «00200xxxxx»)· το Excel χάνει τα μηδενικά μπροστά (αριθμός) — τα ξαναβάζουμε."""
    digits = re.sub(r"\D", "", str(value or "").split(".")[0])
    if not digits or len(digits) > 10:
        return ""
    return digits.zfill(10)


def get_ame(conn: sqlite3.Connection, afm: str) -> str:
    row = conn.execute("SELECT ame FROM efka_credentials WHERE afm=?", (afm,)).fetchone()
    return row["ame"] if row else ""


def get_oikodom_ame(conn: sqlite3.Connection, afm: str) -> str:
    row = conn.execute("SELECT ame_oikodomotexnikon FROM efka_credentials WHERE afm=?", (afm,)).fetchone()
    return row["ame_oikodomotexnikon"] if row else ""


def searches_oikodomotexnika(conn: sqlite3.Connection, afm: str) -> bool:
    row = conn.execute("SELECT search_oikodomotexnika FROM efka_credentials WHERE afm=?", (afm,)).fetchone()
    return bool(row["search_oikodomotexnika"]) if row else False


def ika_credentials(conn: sqlite3.Connection, afm: str) -> Optional[tuple[str, str]]:
    row = conn.execute("SELECT ika_user, ika_pass FROM efka_credentials WHERE afm=?", (afm,)).fetchone()
    if not row or not row["ika_user"] or not row["ika_pass"]:
        return None
    return crypto.dec(row["ika_user"]), crypto.dec(row["ika_pass"])


def get_amka(conn: sqlite3.Connection, afm: str) -> str:
    row = conn.execute("SELECT amka FROM efka_credentials WHERE afm=?", (afm,)).fetchone()
    return crypto.dec(row["amka"]) if row and row["amka"] else ""


def set_amka(conn: sqlite3.Connection, afm: str, amka: str) -> bool:
    """ΑΜΚΑ (11 ψηφία) — προσωπικό δεδομένο, κρυπτογραφημένο (enc:1:). Χρειάζεται για ΚΕΑΟ/ΕΦΚΑ φυσικών προσώπων."""
    digits = re.sub(r"\D", "", str(amka or "").split(".")[0])
    if len(digits) not in (10, 11):
        return False
    digits = digits.zfill(11)
    conn.execute("INSERT INTO efka_credentials(afm, amka, updated_at) VALUES (?,?,?) ON CONFLICT(afm) DO UPDATE SET "
                 "amka=excluded.amka, updated_at=excluded.updated_at", (afm, crypto.enc(digits), db.utcnow()))
    return True


MYAMKA_APP = "https://www.amka.gr/app"


def _amka_values(node: Any, acc: list[str]) -> None:
    """11ψήφιοι αριθμοί με μορφή ΑΜΚΑ (ΗΗ ≤ 31, ΜΜ ≤ 12) μέσα σε οποιοδήποτε JSON (runner `collectAmka`)."""
    if isinstance(node, (str, int)) and not isinstance(node, bool):
        t = str(node)
        if re.fullmatch(r"\d{11}", t) and 1 <= int(t[:2]) <= 31 and 1 <= int(t[2:4]) <= 12 and t not in acc:
            acc.append(t)
    elif isinstance(node, list):
        for x in node:
            _amka_values(x, acc)
    elif isinstance(node, dict):
        for x in node.values():
            _amka_values(x, acc)


def fetch_amka(user: str, password: str, http_factory: Callable[[], _HyperHttp] = _HyperHttp) -> dict[str, Any]:
    """ΑΜΚΑ του φυσικού προσώπου από το myAMKA (www.amka.gr/app) με τους κωδικούς TAXISnet του — port του runner
    `configs/amka-retrieve.js` (`myAmkaLogin` + `CTZ_GetPersonAmka`). ΜΟΝΟ ανάγνωση. {ok, amka} ή {ok: False, reason}."""
    import json
    http = http_factory()
    http.follow("GET", MYAMKA_APP + "/oauth/login")             # → σελίδα σύνδεσης GSIS
    g = _gsis(http, user, password)
    if not g["ok"]:
        return g
    tok = http.follow("GET", MYAMKA_APP + "/oauth/token")
    token = (tok.get("text") or "").strip()
    if tok.get("status") != 200 or not token or "<html" in token[:200].lower():
        return {"ok": False, "reason": "NoToken"}
    res = http.post_raw(MYAMKA_APP + "/api/AmkaCitizenAppService/CTZ_GetPersonAmka", "{}",
                        {"Authorization": "ctaf2 " + token})
    if res["status"] != 200:
        return {"ok": False, "reason": f"API_{res['status']}"}
    try:
        data = json.loads(res["text"])
    except ValueError:
        return {"ok": False, "reason": "BadJson"}
    found: list[str] = []
    _amka_values(data.get("amkaList") if isinstance(data, dict) and data.get("amkaList") else data, found)
    return {"ok": True, "amka": found[0]} if found else {"ok": False, "reason": "NotFound"}


def retrieve_amka(conn: sqlite3.Connection, afm: str, http_factory: Callable[[], _HyperHttp] = _HyperHttp) -> dict[str, Any]:
    """Ανάκτηση + αποθήκευση (κρυπτογραφημένα) του ΑΜΚΑ, με τους ΔΙΚΟΥΣ του κωδικούς TAXISnet — μόνο αν λείπει."""
    if get_amka(conn, afm):
        return {"ok": True, "amka": get_amka(conn, afm), "existing": True}
    creds = taxis_credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "NoCredentials"}
    res = fetch_amka(creds[0], creds[1], http_factory)
    if res.get("ok"):
        set_amka(conn, afm, res["amka"])
    return res


def ika_user(conn: sqlite3.Connection, afm: str) -> str:
    row = conn.execute("SELECT ika_user FROM efka_credentials WHERE afm=?", (afm,)).fetchone()
    return crypto.dec(row["ika_user"]) if row and row["ika_user"] else ""


def set_(conn: sqlite3.Connection, afm: str, ame: Optional[str] = None, user: Optional[str] = None,
         password: Optional[str] = None, source: str = "manual", construction_ame: Optional[str] = None,
         search_oikodomotexnika: Optional[bool] = None) -> bool:
    """Αποθηκεύει ΑΜΕ/κωδικούς και την επιλογή οικοδομοτεχνικών (κενά/None = μένουν ως έχουν)."""
    ame = normalize_ame(ame) if ame else ""
    construction_ame = normalize_ame(construction_ame) if construction_ame else ""
    user, password = (user or "").strip(), password or ""
    if not ame and not construction_ame and not user and not password and search_oikodomotexnika is None:
        return False
    row = conn.execute("SELECT * FROM efka_credentials WHERE afm=?", (afm,)).fetchone()
    cur = dict(row) if row else {"ame": "", "ame_source": "", "ame_oikodomotexnikon": "",
                                 "search_oikodomotexnika": 0, "ika_user": "", "ika_pass": ""}
    conn.execute(
        "INSERT INTO efka_credentials(afm, ame, ame_source, ame_oikodomotexnikon, search_oikodomotexnika, "
        "ika_user, ika_pass, updated_at) VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(afm) DO UPDATE SET ame=excluded.ame, ame_source=excluded.ame_source, "
        "ame_oikodomotexnikon=excluded.ame_oikodomotexnikon, "
        "search_oikodomotexnika=excluded.search_oikodomotexnika, ika_user=excluded.ika_user, "
        "ika_pass=excluded.ika_pass, updated_at=excluded.updated_at",
        (afm, ame or cur["ame"], source if ame else cur["ame_source"],
         construction_ame or cur["ame_oikodomotexnikon"],
         int(search_oikodomotexnika) if search_oikodomotexnika is not None else cur["search_oikodomotexnika"],
         crypto.enc(user) if user else cur["ika_user"], crypto.enc(password) if password else cur["ika_pass"],
         db.utcnow()))
    return True


def employer_credentials(conn: sqlite3.Connection, afm: str) -> list[tuple[str, str]]:
    """Κωδικοί για την «Είσοδο Εργοδότη» του e-ΕΦΚΑ, με σειρά: κωδικοί ΙΚΑ ΕΡΓΟΔΟΤΗ (όπως το ScanMyData
    `kartela_ergodoti.py` — συμπληρώνονται στην ίδια φόρμα GSIS) και μετά οι κωδικοί TAXISnet της επιχείρησης (όπως το
    Tax Center `efka-employer-card-ssp.js`). Χωρίς διπλότυπα."""
    out: list[tuple[str, str]] = []
    for creds in (ika_credentials(conn, afm), taxis_credentials.get(conn, afm)):
        if creds and creds not in out:
            out.append(creds)
    return out


def employer_login_any(conn: sqlite3.Connection, afm: str, ame: str,
                       login_fn: Optional[Callable[..., dict[str, Any]]] = None) -> dict[str, Any]:
    """Σύνδεση εργοδότη δοκιμάζοντας κάθε σετ κωδικών του `employer_credentials` μέχρι να πετύχει."""
    login_fn = login_fn or employer_login
    last: dict[str, Any] = {"ok": False, "reason": "NoCredentials"}
    for user, password in employer_credentials(conn, afm):
        last = login_fn(user, password, afm, ame)
        if last.get("ok"):
            return last
    return last


def record_check(conn: sqlite3.Connection, afm: str, status: str, message: str) -> None:
    conn.execute("UPDATE efka_credentials SET check_status=?, check_message=?, checked_at=? WHERE afm=?",
                 (status, message, db.utcnow(), afm))


def status_map(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """{afm: {ame, construction_ame, search_oikodomotexnika, has_ika, check_status}} — ΧΩΡΙΣ αποκρυπτογράφηση."""
    return {r["afm"]: {"ame": r["ame"], "has_ika": bool(r["ika_user"] and r["ika_pass"]),
                       "construction_ame": r["ame_oikodomotexnikon"],
                       "search_oikodomotexnika": bool(r["search_oikodomotexnika"]),
                       "check_status": r["check_status"], "check_message": r["check_message"]}
            for r in conn.execute("SELECT * FROM efka_credentials")}


# ------------------------------------------------------------------ HTML/JSF βοηθοί
def _attr(tag: str, name: str) -> str:
    m = re.search(r"\b" + re.escape(name) + r'="([^"]*)"', tag, re.I)
    return _html.unescape(m.group(1)) if m else ""


def strip_tags(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def view_state(page: str) -> str:
    for name in ("jakarta.faces.ViewState", "javax.faces.ViewState"):
        m = (re.search(r'name="' + re.escape(name) + r'"[^>]*value="([^"]*)"', page)
             or re.search(r'value="([^"]*)"[^>]*name="' + re.escape(name) + '"', page)
             or re.search(r'<update id="[^"]*ViewState[^"]*"><!\[CDATA\[([^\]]*)\]\]>', page))
        if m:
            return _html.unescape(m.group(1))
    return ""


def form_of(page: str, form_id: str) -> Optional[dict[str, Any]]:
    """Πεδία (inputs + επιλεγμένες τιμές select) και κουμπιά της φόρμας `form_id` (port του `formOf` του Tax Center)."""
    at = page.find(f'id="{form_id}"')
    if at < 0:
        return None
    start = page.rfind("<form", 0, at)
    end = page.find("</form>", at)
    block = page[start:end if end >= 0 else None]
    fields: dict[str, str] = {}
    for m in re.finditer(r"<input\b[^>]*>", block, re.I):
        tag = m.group(0)
        name = _attr(tag, "name")
        if not name:
            continue
        kind = (_attr(tag, "type") or "text").lower()
        if kind in ("checkbox", "radio") and not re.search(r"\bchecked\b", tag, re.I):
            continue
        if kind in ("submit", "button"):
            continue
        fields[name] = _attr(tag, "value")
    for m in re.finditer(r'<select\b[^>]*name="([^"]*)"[^>]*>(.*?)</select>', block, re.I | re.S):
        opt = re.search(r"<option\b[^>]*\bselected\b[^>]*>", m.group(2), re.I)
        fields[m.group(1)] = _attr(opt.group(0), "value") if opt else ""
    buttons = [{"name": _attr(b.group(1), "name") or _attr(b.group(1), "id"), "title": _attr(b.group(1), "title"),
                "text": strip_tags(b.group(2))}
               for b in re.finditer(r"<button\b([^>]*)>(.*?)</button>", block, re.I | re.S)]
    first = re.search(r"<form\b[^>]*>", block, re.I)
    return {"action": _attr(first.group(0), "action") if first else "", "fields": fields, "buttons": buttons}


def extract_update(xml: str, update_id: str) -> str:
    m = re.search(r'<update id="' + re.escape(update_id) + r'"><!\[CDATA\[(.*?)\]\]></update>', xml, re.S)
    return m.group(1) if m else xml


def _gsis(http: _HyperHttp, user: str, password: str) -> dict[str, Any]:
    """GSIS oauth2: credentials + approval (== `gsisSubmitAndApprove` του runner)."""
    page = http.follow("POST", GSIS_CHECK, {"j_username": user, "j_password": password})
    if page["url"].endswith("authentication_error=true") or 'id="j_password"' in page["text"]:
        return {"ok": False, "reason": "InvalidCredentials"}
    if 'id="confirmationForm"' in page["text"] or "user_oauth_approval" in page["text"]:
        page = http.follow("POST", GSIS_AUTHORIZE, {"user_oauth_approval": "true", "scope.read": "true"})
    return {"ok": True, "page": page}


# ------------------------------------------------------------------ σύνδεση (services.e-efka.gov.gr)
def employer_login(user: str, password: str, afm: str, ame: str,
                   http_factory: Callable[[], _HyperHttp] = _HyperHttp) -> dict[str, Any]:
    """KeyCloak e-ΕΦΚΑ, ρόλος `external-employer` (ΑΦΜ + ΑΜΕ) — port του `efkaServicesLogin(isLegal=true)`."""
    if not ame:
        return {"ok": False, "reason": "NoAme"}
    return services_login(user, password, afm, ame=ame, http_factory=http_factory)


def _form_action_of(page: str, element_id: str) -> str:
    """Το action της <form> που περιέχει το στοιχείο `element_id` (runner `formActionOf`)."""
    e = re.escape(element_id)
    m = re.search(r'<form\b[^>]*action="([^"]*)"[^>]*>(?:(?!</form>).)*?id="' + e + '"', page, re.S | re.I)
    return _html.unescape(m.group(1)) if m else ""


def services_login(user: str, password: str, afm: str, amka: str = "", ame: str = "",
                   http_factory: Callable[[], _HyperHttp] = _HyperHttp) -> dict[str, Any]:
    """Και οι δύο ρόλοι του `efkaServicesLogin` (runner/Tax Center): με ΑΜΕ → `external-employer` («Είσοδος Εργοδότη»),
    αλλιώς → `external-non-employee` με ΑΦΜ + ΑΜΚΑ (φυσικά πρόσωπα). {ok, http, page, links} ή {ok: False, reason}."""
    legal = bool(ame)
    if not legal and not amka:
        return {"ok": False, "reason": "NoAmka"}
    http = http_factory()
    home = http.follow("GET", HOME)
    if legal:
        m = (re.search(r'id="social-taxisnet-employer"[^>]*href="([^"]*)"', home["text"], re.I)
             or re.search(r'href="([^"]*)"[^>]*id="social-taxisnet-employer"', home["text"], re.I))
        if not m:
            return {"ok": False, "reason": "HomeFormEmployer"}
        http.follow("GET", urljoin(home["url"], _html.unescape(m.group(1))))
    else:
        action = _form_action_of(home["text"], "social-external-non-employee")
        if not action:
            return {"ok": False, "reason": "HomeFormEmployer"}
        http.follow("POST", urljoin(home["url"], action), {})
    g = _gsis(http, user, password)
    if not g["ok"]:
        return g
    page = g["page"]
    role = (re.search(r'<form[^>]*id="kc-form-select-role"[^>]*action="([^"]*)"', page["text"], re.I)
            or re.search(r'<form[^>]*action="([^"]*)"[^>]*id="kc-form-select-role"', page["text"], re.I))
    if not role:
        return {"ok": False, "reason": "SelectRole"}
    land = http.follow("POST", urljoin(page["url"], _html.unescape(role.group(1))), {
        "role": "external-employer" if legal else "external-non-employee", "afm": afm, "amka": "" if legal else amka,
        "pa": "", "ame": ame if legal else "", "amoe": "",
        "authorizing-afm": "", "authorizing-contractor-afm": "", "submit-role-attribute": "Υποβολή"})
    if "eProfile" in land.get("url", ""):
        land = _skip_eprofile(http, land)               # υποχρεωτική «ενημέρωση στοιχείων» → «Παράλειψη»
    if 'id="viewsPanel"' not in land["text"] and "Καλώς ήρθατε" not in land["text"]:
        return {"ok": False, "reason": "LandPage"}
    links = {strip_tags(a.group(2)): urljoin(SVC, _html.unescape(a.group(1)))
             for a in re.finditer(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', land["text"], re.S | re.I)
             if strip_tags(a.group(2))}
    return {"ok": True, "http": http, "page": land, "links": links}


# ------------------------------------------------------------------ κινήσεις καρτέλας → ΑΠΔ ανά μήνα
_MONTHS_GEN = ("ιανουαρ", "φεβρουαρ", "μαρτ", "απριλ", "μαι", "ιουν", "ιουλ", "αυγουστ", "σεπτεμβρ", "οκτωβρ", "νοεμβρ",
               "δεκεμβρ")


def period_month(text: str) -> str:
    """'YYYY-MM' από τη στήλη «Περίοδος»: «09/2026», «2026-09», «01/09/2026 - 30/09/2026», «Σεπτέμβριος 2026», «202609»."""
    from .textutil import strip_accents
    t = (text or "").strip()
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", t)
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}"
    m = re.search(r"\b(\d{1,2})\s*[/.-]\s*(\d{4})\b", t)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{m.group(2)}-{int(m.group(1)):02d}"
    m = re.search(r"\b(\d{4})\s*[/.-]\s*(\d{1,2})\b", t)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])\b", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    low = strip_accents(t).lower()
    y = re.search(r"\b(20\d{2})\b", low)
    for i, stem in enumerate(_MONTHS_GEN):
        if stem in low and y:
            return f"{y.group(1)}-{i + 1:02d}"
    return ""


def _num(s: str) -> float:
    t = re.sub(r"[^\d,.-]", "", s or "")
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return float(t)
    except ValueError:
        return 0.0


def parse_lines(fragment: str) -> list[dict[str, Any]]:
    """Γραμμές `linesTable` (data-ri): [{date, kind, doc, period, ym, debit, credit}]."""
    out = []
    for tr in re.finditer(r'<tr\b[^>]*data-ri="\d+"[^>]*>(.*?)</tr>', fragment, re.S | re.I):
        cells = [strip_tags(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", tr.group(1), re.S | re.I)]
        if len(cells) < 6:
            continue
        out.append({"date": cells[0], "kind": cells[1], "doc": cells[2], "period": cells[3], "ym": period_month(cells[3]),
                    "debit": _num(cells[4]), "credit": _num(cells[5])})
    return out


def is_apd(line: dict[str, Any]) -> bool:
    kind = line["kind"]
    return kind.startswith(APD_KIND) or "ΑΠΔ" in kind.upper() or "Α.Π.Δ" in kind.upper()


def transactions(http: _HyperHttp, date_from: str, date_to: str) -> Optional[list[dict[str, Any]]]:
    """Όλες οι κινήσεις [date_from, date_to] (dd/mm/yyyy) της οικονομικής καρτέλας — με σελιδοποίηση. None = άλλαξε η σελίδα."""
    page = http.follow("GET", TX)["text"]
    form = form_of(page, "transactionsForm")
    if not form:
        return None
    show = next((b for b in form["buttons"] if b["text"] == "Εμφάνιση" or b["title"] == "Εμφάνιση"), None)
    if not show or not show["name"]:
        return None
    action = urljoin(TX, form["action"] or TX)
    fields = dict(form["fields"])
    fields.update({"dateFrom_input": date_from, "dateTo_input": date_to})
    resp = http.follow("POST", action, {
        **fields, "jakarta.faces.partial.ajax": "true", "jakarta.faces.source": show["name"],
        "jakarta.faces.partial.execute": "transactionsForm", "jakarta.faces.partial.render": "transactionsForm",
        show["name"]: show["name"]}, AJAX)["text"]
    fragment = extract_update(resp, "transactionsForm")
    lines = parse_lines(fragment)
    vs = view_state(resp) or fields.get("jakarta.faces.ViewState", "")
    pages = re.search(r"Σελίδα\s*(\d+)\s*από\s*(\d+)", strip_tags(fragment))
    total = int(pages.group(2)) if pages else 1
    per_page = len(lines) or 10
    for p in range(1, min(total, 60)):
        more = http.follow("POST", action, {
            "jakarta.faces.partial.ajax": "true", "jakarta.faces.source": "linesTable",
            "jakarta.faces.partial.execute": "linesTable", "jakarta.faces.partial.render": "linesTable",
            "jakarta.faces.behavior.event": "page", "jakarta.faces.partial.event": "page",
            "linesTable_pagination": "true", "linesTable_first": str(p * per_page), "linesTable_rows": str(per_page),
            "linesTable_encodeFeature": "true", "transactionsForm": "transactionsForm", "jakarta.faces.ViewState": vs},
            AJAX)["text"]
        vs = view_state(more) or vs
        batch = parse_lines(more)
        if not batch:
            break
        lines += batch
    return lines


def apd_entry(lines: list[dict[str, Any]], year: int, today: Optional[date] = None) -> dict[str, Any]:
    """Περίοδοι ΑΠΔ της χρήσης για τον πίνακα `obligation_checks`: μήνας με κίνηση ΑΠΔ = υποβλήθηκε (χρέωση = εισφορές)·
    μήνας του οποίου η προθεσμία πέρασε χωρίς κίνηση = «δεν υποβλήθηκε» (καμία ΑΠΔ στην καρτέλα)."""
    from .obligations import MONTHS_NOM, month_end, month_end_or_next
    today = today or date.today()
    by_month: dict[str, list[dict[str, Any]]] = {}
    for line in lines:
        if is_apd(line) and line["ym"].startswith(str(year)):
            by_month.setdefault(line["ym"], []).append(line)
    periods = []
    for m in range(1, 13):
        ym = f"{year}-{m:02d}"
        due = month_end_or_next(*((year + 1, 1) if m == 12 else (year, m + 1)))
        found = by_month.get(ym)
        label = f"{MONTHS_NOM[m - 1]} {year}"
        end = month_end(year, m).isoformat()
        if found:
            debit = round(sum(x["debit"] for x in found), 2)
            periods.append({"period": label, "status": "submitted", "period_end": end, "period_type": "month",
                            "detail": "ΑΠΔ στην οικονομική καρτέλα e-ΕΦΚΑ: " + ", ".join(
                                f"{x['date']} {x['doc']}".strip() for x in found),
                            "amounts": {"total_due": debit} if debit else {}})
        elif due < today:
            periods.append({"period": label, "status": "not_submitted", "period_end": end, "period_type": "month",
                            "detail": "Καμία κίνηση ΑΠΔ στην οικονομική καρτέλα e-ΕΦΚΑ"})
    statuses = {p["status"] for p in periods}
    return {"obligation": "ΑΠΔ", "label": "ΑΠΔ e-ΕΦΚΑ", "periods": periods,
            "status": "not_submitted" if "not_submitted" in statuses else ("submitted" if periods else "no_data")}


def check_apd(conn: sqlite3.Connection, afm: str, year: int, today: Optional[date] = None,
              login_fn: Callable[..., dict[str, Any]] = employer_login) -> dict[str, Any]:
    """Login εργοδότη (TAXISnet πελάτη + ΑΜΕ) → κινήσεις της χρήσης → `obligation_checks` (ΑΠΔ). {ok, reason, entry}."""
    from .easycheck import store_results
    today = today or date.today()
    if not employer_credentials(conn, afm):
        return {"ok": False, "reason": REASONS_EL["NoCredentials"], "entry": None}
    ame = get_ame(conn, afm)
    if not ame:
        found = retrieve_ame(conn, afm)
        ame = found.get("ame", "")
        if not ame:
            return {"ok": False, "reason": REASONS_EL["NoAme"], "entry": None}
    login = employer_login_any(conn, afm, ame, login_fn)
    if not login.get("ok"):
        msg = REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης στον e-ΕΦΚΑ.")
        record_check(conn, afm, "invalid" if login.get("reason") == "LandPage" else "error", msg)
        return {"ok": False, "reason": msg, "entry": None}
    end = today if year == today.year else date(year, 12, 31)
    # Η ΑΠΔ του Δεκεμβρίου υποβάλλεται τον Ιανουάριο — η καρτέλα ως τέλος Φεβρουαρίου του επόμενου έτους την πιάνει.
    if year < today.year:
        end = min(today, date(year + 1, 2, 28))
    lines = transactions(login["http"], f"01/01/{year}", end.strftime("%d/%m/%Y"))
    if lines is None:
        record_check(conn, afm, "error", REASONS_EL["NoTransactions"])
        return {"ok": False, "reason": REASONS_EL["NoTransactions"], "entry": None}
    entry = apd_entry(lines, year, today)
    store_results(conn, afm, year, [entry])
    record_check(conn, afm, "ok", f"{len(lines)} κινήσεις καρτέλας")
    return {"ok": True, "reason": "", "entry": entry}


def should_check_apd(conn: sqlite3.Connection, afm: str) -> bool:
    """Ο έλεγχος ΑΠΔ γίνεται για όποιον έχει ΑΜΕ/κωδικούς ΙΚΑ ή είναι γνωστός εργοδότης (στήλη «Μισθοδοσία» του Excel) —
    όχι για κάθε πελάτη (η ανάκτηση ΑΜΕ για 150+ πελάτες χωρίς προσωπικό θα ήταν άσκοπη)."""
    row = conn.execute("SELECT employer, activity_state FROM businesses WHERE afm=?", (afm,)).fetchone()
    if not row or row["employer"] == 0 or row["activity_state"] == "none":
        return False
    cred = conn.execute("SELECT ame, ika_user FROM efka_credentials WHERE afm=?", (afm,)).fetchone()
    return bool(row["employer"]) or bool(cred and (cred["ame"] or cred["ika_user"]))


def check_apd_entry(conn: sqlite3.Connection, afm: str, year: int) -> Optional[dict[str, Any]]:
    """Για τον έλεγχο υποβολών: η εγγραφή ΑΠΔ (ή εγγραφή σφάλματος με την αιτία) — None αν ο πελάτης δεν αφορά."""
    if not should_check_apd(conn, afm):
        return None
    try:
        res = check_apd(conn, afm, year)
    except Exception as exc:                       # noqa: BLE001 — δίκτυο / αλλαγή σελίδας e-ΕΦΚΑ
        log.warning("efka apd %s/%s: %s", afm, year, exc)
        res = {"ok": False, "reason": f"Ο έλεγχος ΑΠΔ δεν ολοκληρώθηκε ({str(exc)[:80]})."}
    if res["ok"]:
        return res["entry"]
    return {"obligation": "ΑΠΔ", "label": "ΑΠΔ e-ΕΦΚΑ", "status": "error", "periods": [], "detail": res["reason"]}


# ------------------------------------------------------------------ ανάκτηση ΑΜΕ (ePistErg) — best effort, καθαρό HTTP
def _eaccess_taxis(http: _HyperHttp, user: str, password: str, afm: str) -> dict[str, Any]:
    """eAccess με TAXISnet, καρτέλα «Επιχείρηση/Πολίτης» (ΑΦΜ) — `LoginEfkaOldWayTaxinet` του TaxSystem / `keaoLogin`."""
    r0 = http.follow("GET", APPS + "/eAccess/login.xhtml")
    gsis = next((u for u in re.findall(r"window\.location\s*=\s*'([^']+)'", r0["text"])
                 if u.startswith("https://oauth2.gsis.gr/")), "")
    if not gsis:
        return {"ok": False, "reason": "NoPistErg"}
    http.follow("GET", _html.unescape(gsis))
    g = _gsis(http, user, password)
    if not g["ok"]:
        return g
    page = g["page"]["text"]
    btn = next((re.search(r'id="([^"]*)"', b.group(1)).group(1)
                for b in re.finditer(r"<button\b([^>]*)>(.*?)</button>", page, re.S | re.I)
                if strip_tags(b.group(2)) == "Είσοδος" and re.search(r'id="([^"]*)"', b.group(1))), "")
    afm1 = (re.search(r'id="gsisTabView:afm1"[^>]*value="(\d{9})"', page) or
            re.search(r'name="gsisTabView:afm1"[^>]*value="(\d{9})"', page))
    if afm1 and afm1.group(1) != afm:
        return {"ok": False, "reason": "AfmMismatch"}                 # κωδικοί άλλου ΑΦΜ (runner: AfmMismatch)
    if not btn:
        return {"ok": False, "reason": "NoPistErg"}
    resp = http.follow("POST", APPS + "/eAccess/gsis/login.xhtml", {
        "javax.faces.partial.ajax": "true", "javax.faces.source": btn, "javax.faces.partial.execute": "mainForm",
        "javax.faces.partial.render": "mainForm", btn: btn, "mainForm": "mainForm", "gsisTabView:afm1": afm,
        # καρτέλα 1 = «Επιχείρηση/Πολίτης» (ΑΦΜ)· η 0 = «Ασφαλισμένος» θέλει ΑΜΚΑ και δεν ανακατευθύνει (ζωντανά 2026-10-22)
        "gsisTabView_activeIndex": "1", "javax.faces.ViewState": view_state(page)}, AJAX)
    redirect = re.search(r'<redirect url="([^"]+)"', resp["text"])
    if redirect:
        http.follow("GET", urljoin(APPS, _html.unescape(redirect.group(1))))
    return {"ok": True}


def _eaccess_ika(http: _HyperHttp, user: str, password: str) -> dict[str, Any]:
    """eAccess με κωδικούς ΙΚΑ εργοδότη (runner `efkaErgodLogin`)."""
    http.follow("GET", APPS + "/eAccess/login.xhtml")
    r = http.follow("POST", APPS + "/eAccess/j_security_check", {"j_username": user, "j_password": password})
    if f"Χρήστης: {user}" not in r["text"]:
        return {"ok": False, "reason": "InvalidCredentials"}
    return {"ok": True}


def parse_ames(page: str) -> list[dict[str, str]]:
    at = page.find('id="AMEsForm:AMEsResults_data"')
    if at < 0:
        return []
    out = []
    for tr in re.finditer(r'<tr\b[^>]*data-ri="(\d+)"[^>]*>(.*?)</tr>', page[at:], re.S | re.I):
        cells = [strip_tags(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", tr.group(2), re.S | re.I)]
        if cells and re.fullmatch(r"\d+", cells[0]):
            # στήλες όπως ο runner (efka-ame.js): ΑΜΕ · Επωνυμία · … · Κωδ. ΑΠΔ (4η)
            out.append({"ame": normalize_ame(cells[0]), "name": cells[1] if len(cells) > 1 else "",
                        "apd_code": cells[3] if len(cells) > 3 else "",
                        "username": cells[4] if len(cells) > 4 else ""})
    return out


def _skip_eprofile(http: _HyperHttp, page: dict[str, Any]) -> dict[str, Any]:
    """Υποχρεωτικό «ενημέρωση στοιχείων επικοινωνίας» (eProfile) → κουμπί «Παράλειψη»."""
    for _ in range(2):
        if "eProfile" not in page["url"]:
            return page
        m = re.search(r"<form\b[^>]*>.*?</form>", page["text"], re.S | re.I)
        btn = next((b for b in re.finditer(r"<button\b([^>]*)>(.*?)</button>", page["text"], re.S | re.I)
                    if strip_tags(b.group(2)) == "Παράλειψη"), None)
        if not m or not btn:
            return page
        form_id = _attr(re.search(r"<form\b[^>]*>", m.group(0)).group(0), "id")
        form = form_of(page["text"], form_id) if form_id else None
        name = _attr(btn.group(1), "name") or _attr(btn.group(1), "id")
        if not form or not name:
            return page
        page = http.follow("POST", urljoin(page["url"], form["action"] or page["url"]), {**form["fields"], name: name})
    return page


AME_KINDS = ("Κοινών Επιχειρήσεων", "Οικοδομοτεχνικών Έργων")
AME_HANDOFF_LABEL = "e-ΕΦΚΑ — ανάκτηση ΑΜΕ"


def retrieve_ame(conn: sqlite3.Connection, afm: str,
                 http_factory: Callable[[], _HyperHttp] = _HyperHttp,
                 include_oikodomotexnika: Optional[bool] = None) -> dict[str, Any]:
    """ΑΜΕ από την «Πιστοποίηση Εργοδοτών» με κωδικούς ΙΚΑ εργοδότη (αν υπάρχουν) ή TAXISnet του πελάτη· αποθηκεύεται.
    {ok, ame, reason, all}. Ίδια βήματα με τον runner `configs/efka-ame.js` (εκεί με browser), εδώ με καθαρό HTTP —
    ΕΠΑΛΗΘΕΥΤΗΚΕ ζωντανά 2026-10-22 με TAXISnet (καρτέλα eAccess `gsisTabView_activeIndex=1`). Η είσοδος με κωδικούς
    ΙΚΑ εργοδότη (`j_security_check`) δεν έχει δοκιμαστεί ζωντανά."""
    try:
        include_oikodomotexnika = (searches_oikodomotexnika(conn, afm) if include_oikodomotexnika is None
                                   else include_oikodomotexnika)
        ika = ika_credentials(conn, afm)
        taxis = taxis_credentials.get(conn, afm)
        attempts: list[tuple[str, tuple[str, str]]] = []
        if ika:
            attempts.append(("ika", ika))
        if taxis and taxis != ika:
            attempts.append(("taxis", taxis))
        if not attempts:
            return {"ok": False, "ame": "", "reason": REASONS_EL["NoCredentials"]}
        last_reason = "NoPistErg"
        stalled: Optional[tuple[_HyperHttp, str]] = None    # σύνδεση που πέρασε αλλά «κόλλησε» σε ενδιάμεση σελίδα
        for method, (user, password) in attempts:
            http = http_factory()
            res = (_eaccess_ika(http, user, password) if method == "ika"
                   else _eaccess_taxis(http, user, password, afm))
            if not res.get("ok"):
                last_reason = res.get("reason") or last_reason
                if last_reason == "NoPistErg" and getattr(http, "jar", None):
                    stalled = (http, getattr(http, "last_url", "") or APPS + "/eAccess/login.xhtml")
                continue
            page = _skip_eprofile(http, http.follow("GET", APPS + "/ePistErg"))
            if "ePistErg" in page["url"]:
                break
            last_reason = "NoPistErg"
            stalled = (http, page.get("url") or APPS + "/ePistErg")
        else:
            out = {"ok": False, "ame": "", "reason": REASONS_EL.get(last_reason, "Αποτυχία σύνδεσης e-ΕΦΚΑ.")}
            if stalled is not None:
                # Ο e-ΕΦΚΑ ζητά κάτι μετά τη σύνδεση (συνήθως επιβεβαίωση στοιχείων επικοινωνίας) — ο χρήστης συνεχίζει
                # στον browser ήδη συνδεδεμένος και μετά «Ξανά δοκιμή» (handoff.py, ρητό αίτημα 2026-10-29)
                from . import handoff
                out["handoff"] = handoff.remember(AME_HANDOFF_LABEL, afm, stalled[0], url=stalled[1],
                                                  reason=out["reason"])
            return out
        found = []
        construction = "Οικοδομοτεχνικών Έργων"
        for kind in AME_KINDS:
            if kind == construction and found and not include_oikodomotexnika:
                break
            link = next((a.group(1) for a in re.finditer(r'<a\b[^>]*href="([^"]*)"[^>]*>(.*?)</a>', page["text"],
                                                         re.S | re.I) if kind in strip_tags(a.group(2))), "")
            if not link and kind != AME_KINDS[0]:
                continue
            ames_page = http.follow("GET", urljoin(page["url"], _html.unescape(link)) if link
                                    else APPS + "/ePistErg/secure/AMEs.xhtml")
            found.extend({**a, "kind": kind} for a in parse_ames(ames_page["text"]))
            if found and not include_oikodomotexnika:
                break
    except Exception as exc:                      # noqa: BLE001 — δίκτυο / αλλαγή σελίδας: ποτέ crash
        log.warning("efka ame %s: %s", afm, exc)
        return {"ok": False, "ame": "", "reason": f"Η ανάκτηση ΑΜΕ δεν ολοκληρώθηκε ({str(exc)[:80]})."}
    if not found:
        return {"ok": False, "ame": "", "reason": REASONS_EL["AmeNotFound"]}
    # ΟΛΑ τα ΑΜΕ στο efka_ames (κάθε οικοδομοτεχνικό έργο έχει το δικό του — έλεγχος ΑΠΔ ανά ΑΜΕ). Το όνομα χρήστη
    # της λίστας μπαίνει μόνο αν δεν έχει ήδη οριστεί· ο κωδικός ΠΟΤΕ (δεν εμφανίζεται — τον δίνει ο χρήστης).
    known_users = {a["ame"]: a["ika_user"] for a in list_ames(conn, afm)}
    for item in found:
        upsert_ame(conn, afm, item["ame"], kind="oikodom" if item["kind"] == construction else "koinon",
                   name=item.get("name") or None, apd_code=item.get("apd_code") or None, source="efka",
                   user=item.get("username") if item.get("username") and not known_users.get(item["ame"]) else None)
    common_ame = next((item["ame"] for item in found if item["kind"] == AME_KINDS[0]), "")
    construction_ame = next((item["ame"] for item in found if item["kind"] == construction), "")
    primary_ame = common_ame or found[0]["ame"]
    set_(conn, afm, ame=primary_ame, source="efka")
    if include_oikodomotexnika:
        conn.execute("UPDATE efka_credentials SET ame_oikodomotexnikon=?, updated_at=? WHERE afm=?",
                     (construction_ame, db.utcnow(), afm))
    from . import handoff
    for h in handoff.pending(afm):                     # πέτυχε (π.χ. μετά από «Ξανά δοκιμή»): η εκκρεμότητα σβήνει
        if h["label"] == AME_HANDOFF_LABEL:
            handoff.forget(h["id"])
    return {"ok": True, "ame": primary_ame, "reason": "", "all": found}


# ------------------------------------------------------------------ πολλά ΑΜΕ ανά εργοδότη + ΑΠΔ e-ΕΦΚΑ / ΤΕΚΑ (2026-10-27)
# Ρητό αίτημα χρήστη: (α) ο έλεγχος ΑΠΔ ΑΝΑ ΑΜΕ — ένας εργοδότης μπορεί να έχει πολλά (ιδίως οικοδομοτεχνικά έργα, ένα
# ανά έργο), και η είσοδος σε κάθε ΑΜΕ γίνεται με ΔΙΑΦΟΡΕΤΙΚΟΥΣ κωδικούς ΙΚΑ· (β) ΧΩΡΙΣΤΑ η ΑΠΔ e-ΕΦΚΑ (ΙΚΑ) και η ΑΠΔ
# ΤΕΚΑ. Πηγή: TaxSystem Hyper.Server.Tax `GetAPD(APD | APD_Teka)` — σελίδα «submissions.xhtml» της ΑΠΔ:
#   νέα πύλη (TAXISnet/κωδικοί εργοδότη + ρόλος external-employer με ΑΜΕ): services.e-efka.gov.gr/ssp.efka.apd/secure/
#   και …/ssp.efka.apd.teka/secure/ · παλιά (eAccess, κωδικοί ΙΚΑ του ΑΜΕ): apps.e-efka.gov.gr/eAPDsso[TEKA]/faces/
#   secureAdmin/submissions.xhtml. Πίνακας `submissionTable_data`: td0 αρ. υποβολής · td1 είδος · td2 «Μ/ΕΕΕΕ - Μ/ΕΕΕΕ» ·
#   td3 κατάσταση (μετρά ΜΟΝΟ «ΥΠΟΒΟΛΗ», όπως το TaxSystem) · `resultTpte` = ταυτότητα πληρωμής. Σελιδοποίηση: PrimeFaces
#   partial-ajax (`submissionTable_first`/`_rows`). **ΔΕΝ έχει δοκιμαστεί ζωντανά από το TaxMatch η σελίδα υποβολών.**
APD_FUNDS = {
    "ΑΠΔ": ("ΑΠΔ e-ΕΦΚΑ (ΙΚΑ)", SVC + "ssp.efka.apd/secure/submissions.xhtml",
            APPS + "/eAPDsso/faces/secureAdmin/submissions.xhtml"),
    "ΑΠΔ_ΤΕΚΑ": ("ΑΠΔ ΤΕΚΑ", SVC + "ssp.efka.apd.teka/secure/submissions.xhtml",
                 APPS + "/eAPDssoTEKA/faces/secureAdmin/submissions.xhtml"),
}
AME_KIND_LABEL = {"koinon": "Κοινών Επιχειρήσεων", "oikodom": "Οικοδομοτεχνικών Έργων"}


def list_ames(conn: sqlite3.Connection, afm: str, active_only: bool = False) -> list[dict[str, Any]]:
    """Τα ΑΜΕ του εργοδότη (`efka_ames`) — ΧΩΡΙΣ κωδικούς· `ika_user` αποκρυπτογραφημένος για την οθόνη, `has_pass`."""
    rows = conn.execute("SELECT * FROM efka_ames WHERE afm=?" + (" AND active=1" if active_only else "")
                        + " ORDER BY kind, ame", (afm,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["ika_user"] = crypto.dec(r["ika_user"]) if r["ika_user"] else ""
        d["has_pass"] = bool(r["ika_pass"])
        d.pop("ika_pass", None)
        out.append(d)
    if not out:                                       # παλιά δεδομένα (πριν τη v19): το ΑΜΕ του efka_credentials
        main = get_ame(conn, afm)
        if main:
            out.append({"afm": afm, "ame": main, "kind": "koinon", "name": "", "apd_code": "",
                        "ika_user": ika_user(conn, afm), "has_pass": bool(ika_credentials(conn, afm)), "active": 1,
                        "source": "efka_credentials", "check_status": "", "check_message": ""})
    return out


def upsert_ame(conn: sqlite3.Connection, afm: str, ame: str, kind: Optional[str] = None, name: Optional[str] = None,
               user: Optional[str] = None, password: Optional[str] = None, apd_code: Optional[str] = None,
               source: str = "manual", active: Optional[bool] = None) -> bool:
    """Προσθήκη/ενημέρωση ΑΜΕ (κενά/None = μένουν ως έχουν). Κωδικοί ΙΚΑ του ΑΜΕ κρυπτογραφημένοι (enc:1:)."""
    ame = normalize_ame(ame)
    if not ame:
        return False
    row = conn.execute("SELECT * FROM efka_ames WHERE afm=? AND ame=?", (afm, ame)).fetchone()
    cur = dict(row) if row else {"kind": "koinon", "name": "", "apd_code": "", "ika_user": "", "ika_pass": "",
                                 "active": 1, "source": source}
    conn.execute(
        "INSERT INTO efka_ames(afm, ame, kind, name, apd_code, ika_user, ika_pass, active, source, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(afm, ame) DO UPDATE SET kind=excluded.kind, name=excluded.name, "
        "apd_code=excluded.apd_code, ika_user=excluded.ika_user, ika_pass=excluded.ika_pass, active=excluded.active, "
        "source=excluded.source, updated_at=excluded.updated_at",
        (afm, ame, kind or cur["kind"], name if name is not None else cur["name"],
         apd_code if apd_code is not None else cur["apd_code"],
         crypto.enc(user.strip()) if user and user.strip() else cur["ika_user"],
         crypto.enc(password) if password else cur["ika_pass"],
         int(active) if active is not None else cur["active"], cur.get("source") or source, db.utcnow()))
    return True


def remove_ame(conn: sqlite3.Connection, afm: str, ame: str) -> None:
    conn.execute("DELETE FROM efka_ames WHERE afm=? AND ame=?", (afm, normalize_ame(ame)))


def ame_credentials(conn: sqlite3.Connection, afm: str, ame: str) -> Optional[tuple[str, str]]:
    """Κωδικοί ΙΚΑ ΑΥΤΟΥ του ΑΜΕ· για το κύριο ΑΜΕ και οι κωδικοί ΙΚΑ εργοδότη του efka_credentials."""
    r = conn.execute("SELECT ika_user, ika_pass FROM efka_ames WHERE afm=? AND ame=?", (afm, normalize_ame(ame))).fetchone()
    if r and r["ika_user"] and r["ika_pass"]:
        return crypto.dec(r["ika_user"]), crypto.dec(r["ika_pass"])
    if normalize_ame(ame) == get_ame(conn, afm):
        return ika_credentials(conn, afm)
    return None


def _upper(s: str) -> str:
    from .textutil import strip_accents
    return strip_accents(s or "").upper()


def parse_submissions(page: str) -> dict[str, Any]:
    """Σελίδα (ή partial-response) υποβολών ΑΠΔ → {rows: [{id, kind, period, yms, status}], payment_id, pages}."""
    m = re.search(r'<update id="submissionTable"><!\[CDATA\[(.*?)\]\]></update>', page, re.S)
    if m:
        body = m.group(1)
    else:
        at = page.find('id="submissionTable_data"')
        body = page[at:] if at >= 0 else ""
    rows = []
    for tr in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", body, re.S | re.I):
        cells = [strip_tags(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", tr.group(1), re.S | re.I)]
        if len(cells) < 4 or not re.search(r"\d{1,2}/\d{4}", cells[2]):
            continue
        yms: list[str] = []
        for mo, yr in re.findall(r"(\d{1,2})/(\d{4})", cells[2]):
            ym = f"{yr}-{int(mo):02d}"
            if ym not in yms:
                yms.append(ym)
        # το κελί του αριθμού περιέχει και το script του PrimeFaces («82624495 $(function(){…») — μόνο ο αριθμός
        rows.append({"id": (cells[0].split() or [""])[0], "kind": cells[1], "period": cells[2], "yms": yms,
                     "status": cells[3]})
    to = re.search(r'id="resultTpte"[^>]*>(.*?)</', page, re.S)
    pages = re.search(r"Σελίδα\s*\d+\s*από\s*(\d+)", strip_tags(page))
    return {"rows": rows, "payment_id": strip_tags(to.group(1)) if to else "",
            "pages": int(pages.group(1)) if pages else 1}


def apd_submissions(http: _HyperHttp, url: str, year: int, max_pages: int = 30) -> Optional[dict[str, Any]]:
    """Όλες οι υποβολές ΑΠΔ (με σελιδοποίηση) ώσπου να περάσουμε σε παλαιότερο έτος. None = δεν φόρτωσε ο πίνακας."""
    page = http.follow("GET", url)
    text = page["text"]
    if 'id="submissionTable' not in text:
        return None
    first = parse_submissions(text)
    rows, vs = list(first["rows"]), view_state(text)
    prefix = "jakarta" if "jakarta.faces.ViewState" in text else "javax"
    size = len(first["rows"]) or 10
    for i in range(1, min(first["pages"], max_pages)):
        if rows and all(ym < f"{year}-01" for r in rows[-size:] for ym in (r["yms"] or ["9999"])):
            break                                     # η σελίδα είναι ήδη σε παλαιότερο έτος (TaxSystem: MinYearCnt)
        resp = http.follow("POST", page["url"], {
            f"{prefix}.faces.partial.ajax": "true", f"{prefix}.faces.source": "submissionTable",
            f"{prefix}.faces.partial.execute": "submissionTable", f"{prefix}.faces.partial.render": "submissionTable",
            "submissionTable": "submissionTable", "submissionTable_pagination": "true",
            "submissionTable_first": str(i * size), "submissionTable_rows": str(size),
            "submissionTable_encodeFeature": "true", "submissionShow": "submissionShow", "submissionTable_selection": "",
            f"{prefix}.faces.ViewState": vs})
        more = parse_submissions(resp["text"])
        if not more["rows"]:
            break
        rows += more["rows"]
        vs = view_state(resp["text"]) or vs
    return {"rows": rows, "payment_id": first["payment_id"]}


def submissions_entry(key: str, ame: str, rows: list[dict[str, Any]], year: int, payment_id: str = "",
                      today: Optional[date] = None) -> dict[str, Any]:
    """Εγγραφή `obligation_checks` «<ΑΠΔ|ΑΠΔ_ΤΕΚΑ>#<ΑΜΕ>»: μήνας με υποβολή «ΥΠΟΒΟΛΗ» = υποβλήθηκε· ληγμένος μήνας χωρίς
    = δεν υποβλήθηκε. Για ΤΕΚΑ χωρίς ΚΑΜΙΑ ΑΠΔ ποτέ = «δεν αφορά» (χωρίς εργαζόμενους στο ΤΕΚΑ)."""
    from .obligations import MONTHS_NOM, month_end, month_end_or_next
    today = today or date.today()
    label = f"{APD_FUNDS[key][0]} · ΑΜΕ {ame}"
    if key == "ΑΠΔ_ΤΕΚΑ" and not rows:
        return {"obligation": f"{key}#{ame}", "label": label, "periods": [], "status": "not_obligated",
                "detail": "Καμία ΑΠΔ ΤΕΚΑ στον e-ΕΦΚΑ — χωρίς εργαζόμενους ασφαλισμένους στο ΤΕΚΑ"}
    by_month: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if "ΥΠΟΒΟΛΗ" not in _upper(r["status"]):
            continue
        for ym in r["yms"]:
            by_month.setdefault(ym, []).append(r)
    periods = []
    for m in range(1, 13):
        ym = f"{year}-{m:02d}"
        due = month_end_or_next(*((year + 1, 1) if m == 12 else (year, m + 1)))
        found = by_month.get(ym)
        base = {"period": f"{MONTHS_NOM[m - 1]} {year}", "period_end": month_end(year, m).isoformat(),
                "period_type": "month"}
        if found:
            periods.append({**base, "status": "submitted", "amounts": {"payment_id": payment_id} if payment_id else {},
                            "detail": "Υποβολή " + ", ".join(f"{x['id']} ({x['kind']})" for x in found)})
        elif due < today:
            detail = "Καμία υποβολή ΑΠΔ για τον μήνα"
            if key == "ΑΠΔ_ΤΕΚΑ" and by_month and ym > max(by_month):
                # ζωντανά 2026-10-27: ΑΠΔ ΤΕΚΑ ως τον Μάιο και μετά τίποτα — συνήθως αποχώρησε ο εργαζόμενος του ΤΕΚΑ
                detail = (f"Καμία ΑΠΔ ΤΕΚΑ μετά το {max(by_month)[5:]}/{max(by_month)[:4]} — αν δεν απασχολεί πια "
                          "εργαζόμενους ασφαλισμένους στο ΤΕΚΑ, δεν οφείλεται (σημειώστε την ως ολοκληρωμένη)")
            periods.append({**base, "status": "not_submitted", "detail": detail})
    statuses = {p["status"] for p in periods}
    return {"obligation": f"{key}#{ame}", "label": label, "periods": periods,
            "status": "not_submitted" if "not_submitted" in statuses else ("submitted" if periods else "no_data")}


def _login_for_ame(conn: sqlite3.Connection, afm: str, ame: str,
                   http_factory: Callable[[], _HyperHttp]) -> dict[str, Any]:
    """Σύνδεση για ΕΝΑ ΑΜΕ στη νέα πύλη (services.e-efka.gov.gr, «Είσοδος Εργοδότη» → GSIS → ρόλος external-employer
    με ΑΥΤΟ το ΑΜΕ): πρώτα οι κωδικοί του ΑΜΕ (αν δόθηκαν), μετά οι TAXISnet του πελάτη. Η παλιά πύλη ΑΠΔ
    (apps.e-efka.gov.gr/eAPDsso, με eAccess) ΑΝΑΚΑΤΕΥΘΥΝΕΙ πλέον στη νέα — ζωντανά 2026-10-27 — άρα δεν χρησιμοποιείται·
    η σελίδα εισόδου δεν έχει άλλη επιλογή εργοδότη από το TAXISnet. {ok, http, portal: 'new', reason}."""
    attempts: list[tuple[str, tuple[str, str]]] = []
    own = ame_credentials(conn, afm, ame)
    if own:
        attempts.append(("new", own))
    taxis = taxis_credentials.get(conn, afm)
    if taxis and ("new", taxis) not in attempts:
        attempts.append(("new", taxis))
    if not attempts:
        return {"ok": False, "reason": REASONS_EL["NoCredentials"]}
    last = "Αποτυχία σύνδεσης στον e-ΕΦΚΑ."
    for portal, (user, password) in attempts:
        try:
            if portal == "new":
                res = services_login(user, password, afm, ame=ame, http_factory=http_factory)
                if res.get("ok"):
                    return {"ok": True, "http": res["http"], "portal": "new"}
            else:
                http = http_factory()
                res = _eaccess_ika(http, user, password)
                if res.get("ok"):
                    return {"ok": True, "http": http, "portal": "old"}
            last = REASONS_EL.get(res.get("reason") or "", last)
        except Exception as exc:                       # noqa: BLE001 — δίκτυο/σελίδα: επόμενη προσπάθεια
            last = f"Η σύνδεση δεν ολοκληρώθηκε ({str(exc)[:80]})."
    return {"ok": False, "reason": last}


def check_apd_entries(conn: sqlite3.Connection, afm: str, year: int, today: Optional[date] = None,
                      http_factory: Callable[[], _HyperHttp] = _HyperHttp) -> list[dict[str, Any]]:
    """ΑΠΔ e-ΕΦΚΑ ΚΑΙ ΑΠΔ ΤΕΚΑ για ΚΑΘΕ ενεργό ΑΜΕ του εργοδότη → `obligation_checks` ως «ΑΠΔ#<ΑΜΕ>» /
    «ΑΠΔ_ΤΕΚΑ#<ΑΜΕ>» (το Ημερολόγιο τα ενώνει ανά κανόνα: deadlines._merge_per_ame). Χωρίς ΑΜΕ: ανάκτηση από την
    «Πιστοποίηση Εργοδοτών»· αν δεν φορτώσει η σελίδα υποβολών ΑΠΔ, η παλιά μέθοδος (οικονομική καρτέλα εργοδότη)."""
    from .easycheck import store_results
    if not should_check_apd(conn, afm):
        return []
    today = today or date.today()
    ames = list_ames(conn, afm, active_only=True)
    if not ames and employer_credentials(conn, afm):
        found = retrieve_ame(conn, afm)
        ames = list_ames(conn, afm, active_only=True) if found.get("ok") else []
    if not ames:
        return [{"obligation": "ΑΠΔ", "label": "ΑΠΔ e-ΕΦΚΑ", "status": "error", "periods": [],
                 "detail": REASONS_EL["NoAme"]}]
    entries: list[dict[str, Any]] = []
    for a in ames:
        ame = a["ame"]
        try:
            login = _login_for_ame(conn, afm, ame, http_factory)
        except Exception as exc:                       # noqa: BLE001
            login = {"ok": False, "reason": f"Η σύνδεση δεν ολοκληρώθηκε ({str(exc)[:80]})."}
        if not login["ok"]:
            conn.execute("UPDATE efka_ames SET check_status='error', check_message=? WHERE afm=? AND ame=?",
                         (login["reason"], afm, ame))
            entries.append({"obligation": f"ΑΠΔ#{ame}", "label": f"ΑΠΔ e-ΕΦΚΑ · ΑΜΕ {ame}", "status": "error",
                            "periods": [], "detail": login["reason"]})
            continue
        messages = []
        teka = conn.execute("SELECT teka FROM businesses WHERE afm=?", (afm,)).fetchone()
        for key, (label, new_url, old_url) in APD_FUNDS.items():
            if key == "ΑΠΔ_ΤΕΚΑ" and not (teka and teka["teka"]):
                continue                               # ΤΕΚΑ μόνο αν έχει επιλεγεί στο Προφίλ
            try:
                found = apd_submissions(login["http"], new_url if login["portal"] == "new" else old_url, year)
            except Exception as exc:                   # noqa: BLE001
                log.warning("apd %s %s/%s: %s", key, afm, ame, exc)
                found = None
            if found is None and key == "ΑΠΔ" and login["portal"] == "new":
                # παλιά μέθοδος (ζωντανά επαληθευμένη 2026-10-22): κινήσεις ΑΠΔ στην οικονομική καρτέλα εργοδότη
                end = min(today, date(year + 1, 2, 28)) if year < today.year else today
                lines = transactions(login["http"], f"01/01/{year}", end.strftime("%d/%m/%Y"))
                if lines is not None:
                    legacy = apd_entry(lines, year, today)
                    entries.append({**legacy, "obligation": f"ΑΠΔ#{ame}", "label": f"{label} · ΑΜΕ {ame}"})
                    messages.append(f"{label}: οικονομική καρτέλα")
                    continue
            if found is None:
                # η σύνδεση πέτυχε αλλά η σελίδα δεν ήρθε — συχνά ο e-ΕΦΚΑ ζητά επιβεβαίωση στοιχείων επικοινωνίας:
                # ο χρήστης μπορεί να συνεχίσει στον browser από εδώ (handoff.py)
                from . import handoff
                handoff.remember(f"{label} · ΑΜΕ {ame}", afm, login["http"],
                                 new_url if login["portal"] == "new" else old_url)
                entries.append({"obligation": f"{key}#{ame}", "label": f"{label} · ΑΜΕ {ame}", "status": "error",
                                "periods": [], "detail": "Δεν φόρτωσε η σελίδα υποβολών ΑΠΔ μετά τη σύνδεση (ίσως ζητά "
                                                         "επιβεβαίωση στοιχείων) — «Συνέχεια στον browser»."})
                messages.append(f"{label}: σφάλμα")
                continue
            entries.append(submissions_entry(key, ame, found["rows"], year, found["payment_id"], today))
            messages.append(f"{label}: {len(found['rows'])} υποβολές")
        conn.execute("UPDATE efka_ames SET check_status='ok', check_message=? WHERE afm=? AND ame=?",
                     ("; ".join(messages), afm, ame))
    # οι παλιές εγγραφές «ΑΠΔ» (ένα ΑΜΕ, πριν τη v19) αντικαθίστανται από τις ανά ΑΜΕ
    if any(e["status"] != "error" for e in entries):
        conn.execute("DELETE FROM obligation_checks WHERE afm=? AND year=? AND obligation='ΑΠΔ'", (afm, year))
    store_results(conn, afm, year, entries)
    return entries
