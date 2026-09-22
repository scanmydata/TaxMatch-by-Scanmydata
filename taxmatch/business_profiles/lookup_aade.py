"""Στοιχεία πελάτη από το Μητρώο ΑΑΔΕ (myAADE / TAXISnet) — ΔΟΥ, κατάσταση, διεύθυνση, καθεστώς ΦΠΑ,
κατηγορία βιβλίων. Pure-HTTP port του e3/checks/aade_profile.py του ScanMyData (login μέσω GSIS OAM,
μετά GET στα webresources/infomytaxisnet του saadeapps3/comregistry).

Αλλαγές από το πρωτότυπο: αφαιρέθηκε το CLI (ο κωδικός δεν πρέπει να περνά ως όρισμα γραμμής εντολών)
και το `logging.basicConfig` κατά το import. Τα credentials δίνονται από τις κρυπτογραφημένες Ρυθμίσεις.

Το πεδίο διεύθυνσης είναι best-effort εικασία (βλ. `_guess_address`)· επιστρέφονται πάντα και όλα τα
tags (`all_tags`) ώστε να διορθωθεί χωρίς νέα ζωντανή κλήση.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode, urljoin, urlsplit

import requests

from ..textutil import strip_accents

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
AADE = "https://www1.aade.gr"

# Επιβεβαιωμένα tags διεύθυνσης (ζωντανά, 2026-09-10): «dieyaskhshsdrasthrio» = διεύθυνση άσκησης
# δραστηριότητας (η έδρα), «dieykatoikias» = κατοικία (fallback για ΙΔΙΩΤΗ χωρίς επιχείρηση).
_ADDRESS_TAG_PRIORITY = ("dieyaskhshsdrasthrio", "dieykatoikias")
_ADDRESS_TAG_HINTS = (
    "dieyaskhsh", "dieykatoikia", "dieuthins", "dieythins", "odos",
    "arithmos", "poli", "polh", "tk", "postal", "address", "perioxi", "dimos",
)


class _HyperHttp:
    """Ελάχιστο cookie jar + redirect follower (ίδιο με τα υπόλοιπα pure-HTTP scripts του ScanMyData)."""

    def __init__(self, timeout: float = 60.0):
        self.jar: Dict[str, Dict[str, str]] = {}
        self.timeout = timeout

    @staticmethod
    def _host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def set_cookie(self, host: str, name: str, value: str) -> None:
        self.jar.setdefault(host, {})[name] = value

    def _store(self, url: str, resp: requests.Response) -> None:
        host = self._host(url)
        self.jar.setdefault(host, {})
        try:
            raws = resp.raw.headers.getlist("Set-Cookie")
        except Exception:
            sc = resp.headers.get("Set-Cookie")
            raws = [sc] if sc else []
        for raw in raws:
            kv = raw.split(";", 1)[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                self.jar[host][k.strip()] = v.strip()

    def _cookie(self, url: str) -> str:
        host = self._host(url)
        parts: List[str] = []
        for h, kv in self.jar.items():
            if host == h or host.endswith(h) or h.endswith("aade.gr") or h.endswith("gsis.gr"):
                parts.extend(f"{k}={v}" for k, v in kv.items())
        return "; ".join(parts)

    def _once(self, method: str, url: str, form: Optional[Dict[str, str]] = None) -> requests.Response:
        headers = {"User-Agent": UA, "Accept-Language": "el-GR,el;q=0.9,en;q=0.8"}
        ck = self._cookie(url)
        if ck:
            headers["Cookie"] = ck
        data = None
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
            data = urlencode(form).encode("utf-8")
        resp = requests.request(method, url, headers=headers, data=data, allow_redirects=False, timeout=self.timeout)
        self._store(url, resp)
        return resp

    def follow(self, method: str, url: str, form: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        res = self._once(method, url, form)
        loc = res.headers.get("Location")
        cur = url
        hops = 0
        while loc and 300 <= res.status_code < 400 and hops < 25:
            cur = urljoin(cur, loc)
            res = self._once("GET", cur)
            loc = res.headers.get("Location")
            hops += 1
        return {"url": cur, "status": res.status_code, "text": res.text}


def _tag(xml: str, name: str) -> str:
    m = re.search(r"<" + re.escape(name) + r">([^<]*)</" + re.escape(name) + r">", xml, re.I)
    return m.group(1).strip() if m else ""


def _parse_all_tags(xml: str) -> Dict[str, str]:
    """Κάθε απλό <tag>value</tag> ζεύγος (πρώτη εμφάνιση), lowercased key."""
    out: Dict[str, str] = {}
    for m in re.finditer(r"<([A-Za-z_][\w.-]*)>([^<]*)</\1>", xml):
        key, val = m.group(1).strip().lower(), m.group(2).strip()
        if val and key not in out:
            out[key] = val
    return out


def _guess_address(tags: Dict[str, str]) -> str:
    for key in _ADDRESS_TAG_PRIORITY:
        if tags.get(key):
            return tags[key]
    hits = [k for k in tags if any(h in k for h in _ADDRESS_TAG_HINTS)]
    parts = [tags[k] for k in hits if tags[k]]
    return " ".join(dict.fromkeys(parts)) if parts else ""


def aade_login(username: str, password: str) -> Dict[str, Any]:
    """Login μέσω GSIS OAM. Επιστρέφει {ok, http, page} ή {ok: False, reason}."""
    http = _HyperHttp()
    log.info("[aade-login] GET protected home -> OAM login")
    home = http.follow("GET", AADE + "/taxisnet/info/protected/home.htm")
    m = re.search(r'name="request_id"[^>]*value="([^"]*)"', home["text"], re.I) or \
        re.search(r'value="([^"]*)"[^>]*name="request_id"', home["text"], re.I)
    req_id = m.group(1) if m else ""
    if not req_id:
        return {"ok": False, "reason": "NoRequestId"}
    auth = http.follow("POST", "https://login.gsis.gr/oam/server/auth_cred_submit", {
        "username": username, "password": password, "request_id": req_id, "btn_login": "",
    })
    if (re.search(r"An incorrect Username or Password|Καθορίστηκε λανθασμένο όνομα χρήστη ή κωδικός"
                  r"|κλειδωμένος ή απενεργοποιημένος|auth_fail_exception", auth["text"], re.I)
            or (re.search(r'name="username"', auth["text"], re.I) and re.search(r'name="password"', auth["text"], re.I))):
        return {"ok": False, "reason": "InvalidCredentials"}
    http.set_cookie("www1.aade.gr", "gr.taxisnet.infrastructure.common.web.ActorRoleCookieResolver.ACTOR_ROLE", "SELF_SERVICE")
    http.set_cookie("www1.aade.gr", "OAMAuthnHintCookie", "1")
    http.follow("GET", AADE + "/webtax/incomefp/")
    http.follow("GET", AADE + "/webtax/incomefp/login.done")
    check = http.follow("GET", AADE + "/taxisnet/info/protected/home.htm")
    if re.search(r'name="request_id"', check["text"], re.I) and re.search(r'name="password"', check["text"], re.I):
        return {"ok": False, "reason": "NotLoggedIn"}
    return {"ok": True, "http": http, "page": check}


REASONS_EL = {
    "NoRequestId": "Δεν φόρτωσε η σελίδα σύνδεσης της ΑΑΔΕ.",
    "InvalidCredentials": "Λάθος ή κλειδωμένος λογαριασμός TAXISnet.",
    "NotLoggedIn": "Η σύνδεση στη ΑΑΔΕ δεν ολοκληρώθηκε.",
    "NoAfm": "Δεν βρέθηκε ΑΦΜ.",
    "NoRegistry": "Δεν βρέθηκε εγγραφή στο Μητρώο ΑΑΔΕ για αυτό το ΑΦΜ (ή δεν υπάρχει πρόσβαση).",
}
# Το Μητρώο (myAADE) δείχνει ΜΟΝΟ τα στοιχεία του ΑΦΜ με το οποίο έγινε η σύνδεση (επαληθεύτηκε ζωντανά με 2 λογαριασμούς).
REASON_OFFICE_NOREGISTRY = ("Ο λογαριασμός γραφείου βλέπει μόνο το δικό του Μητρώο — για τα στοιχεία του πελάτη "
                            "χρειάζονται οι δικοί του κωδικοί TAXISnet (ή ΓΕΜΗ).")


def _parse_date(text: str) -> Optional[date]:
    """'30/01/2026' -> date."""
    try:
        return datetime.strptime((text or "").strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def parse_activities(payload: str) -> List[Dict[str, Any]]:
    """JSON του `getMhtrwoDrastiriothtesEpixeir` -> [{code, descr, is_main, ceased}]. Ανεκτικό: άκυρο JSON -> []."""
    try:
        data = json.loads(payload or "[]")
    except ValueError:
        return []
    out: List[Dict[str, Any]] = []
    for row in data if isinstance(data, list) else []:
        if not isinstance(row, dict) or not str(row.get("kwdikos") or "").strip():
            continue
        out.append({
            "code": str(row["kwdikos"]).strip(),
            "descr": str(row.get("drasthriothta") or "").strip(),
            # «ΚΥΡΙΑ» = κύρια δραστηριότητα· οι υπόλοιπες («ΔΕΥΤΕΡΕΥΟΥΣΑ», «ΒΟΗΘΗΤΙΚΗ»…) όχι
            "is_main": strip_accents(str(row.get("eidos") or "")).upper().startswith("ΚΥΡΙΑ"),
            "ceased": bool(row.get("hmdiakophs")),
        })
    return out


def build_profile(target: str, login_afm: str, userdata: str, fysiko: str, epix: str, activities: str = "[]",
                  today: Optional[date] = None) -> Dict[str, Any]:
    """Καθαρή ερμηνεία των απαντήσεων του Μητρώου (χωρίς δίκτυο). Κανόνες (επαληθευμένοι σε ζωντανούς λογαριασμούς):

    * Η «κατάσταση επιχείρησης» της ΑΑΔΕ γράφει ΕΝΕΡΓΗ ΚΑΙ ΜΕΤΑ τη διακοπή (είναι η τελευταία κατάσταση *πριν* τη
      διακοπή)· μόνο το `hmdiakophs` λέει αν η επιχείρηση έκλεισε.
    * Φυσικό πρόσωπο με κλειστή επιχείρηση είναι ΙΔΙΩΤΗΣ· νομικό πρόσωπο μένει νομικό ακόμη και διακομμένο.
    * Η ΔΟΥ και η διεύθυνση είναι της επιχείρησης όσο ζει, αλλιώς του φυσικού προσώπου.
    """
    today = today or date.today()
    has_fysiko = f"<afm>{target}</afm>" in fysiko
    has_epix = "<hmenarxhs>" in epix
    if not has_fysiko and not has_epix:
        return {"ok": False, "reason": "NoRegistry", "afm": target}

    cease_date = _tag(epix, "hmdiakophs") if has_epix else ""
    cease_dt = _parse_date(cease_date)
    ceased = bool(cease_date) and (cease_dt is None or cease_dt <= today)
    business_open = has_epix and not ceased
    if not has_epix:
        state = "none"                                   # φυσικό πρόσωπο χωρίς επιχείρηση
    else:
        state = "ceased" if ceased else "active"

    if has_fysiko:
        kind = "ΑΤΟΜΙΚΗ ΕΠΙΧΕΙΡΗΣΗ" if business_open else "ΙΔΙΩΤΗΣ"
    else:
        kind = "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ"

    same_account = target == login_afm
    if kind == "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ":
        name = (_tag(userdata, "longepwnymia") or _tag(userdata, "onomatepwnymo")) if same_account else ""
    elif same_account:
        name = _tag(userdata, "onomatepwnymo")
    else:
        name = _tag(fysiko, "epwnymoa")
    name = re.sub(r"\s+", " ", name).strip()

    fysiko_tags, epix_tags = _parse_all_tags(fysiko), _parse_all_tags(epix)
    all_tags = {**fysiko_tags, **epix_tags}
    # Νομική μορφή: αν το Μητρώο έχει tag «...morfh...» (νομικά πρόσωπα)· αλλιώς το είδος (ΑΤΟΜΙΚΗ ΕΠΙΧΕΙΡΗΣΗ/ΙΔΙΩΤΗΣ)
    form_tag = next((v for k, v in epix_tags.items() if "morfh" in k), "")
    legal_form = form_tag or ("" if kind == "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ" else kind)

    acts = parse_activities(activities) if has_epix else []
    live = [a for a in acts if not a["ceased"]]
    kads = [{"code": a["code"], "descr": a["descr"], "is_main": a["is_main"]} for a in (live or ([] if ceased else acts))]

    # «Ενεργός» αφορά τον ΠΕΛΑΤΗ: όποιος έκλεισε την ατομική του εξακολουθεί να υποβάλλει Ε1, άρα κρίνεται από την
    # κατάσταση του φυσικού προσώπου· το νομικό πρόσωπο από τη διακοπή της επιχείρησης.
    if has_fysiko:
        client_active = bool(re.search(r"ΚΑΝΟΝΙΚΗ", strip_accents(_tag(fysiko, "katastashforologoumenoy")).upper()))
    else:
        client_active = not ceased
    # Το προσωπικό μέρος (ταυτότητα, ημ. γέννησης, γονείς…) ΔΕΝ αποθηκεύεται· μόνο ό,τι αφορά την επιχείρηση.
    raw = {"kind": kind, "business_state": state, "business": epix_tags if has_epix else {},
           "person": {k: fysiko_tags[k] for k in ("katastashforologoumenoy", "armodiadoy") if k in fysiko_tags},
           "activities": acts}
    return {
        "ok": True,
        "afm": target,
        "name": name,
        "kind": kind,
        "legal_form": legal_form,
        "business_state": state,                          # active | ceased | none
        "cease_date": cease_date,
        "cease_reason": _tag(epix, "aitiadiakophs") if has_epix else "",
        "business_start": _tag(epix, "hmenarxhs") if has_epix else "",
        "doy": (_tag(epix, "doydescription") if business_open else "") or _tag(fysiko, "armodiadoy")
               or _tag(epix, "doydescription"),
        "active": client_active,
        "address": _guess_address(all_tags if business_open else fysiko_tags or all_tags),
        "kads": kads,
        "all_tags": all_tags if has_epix else fysiko_tags,   # για interpret_vat_profile (ΦΠΑ/βιβλία)
        "raw": raw,
    }


def fetch_company_profile(username: str, password: str, afm: Optional[str] = None,
                          login_fn=aade_login) -> Dict[str, Any]:
    """Login + Μητρώο (φυσικού + επιχείρησης + δραστηριοτήτων/ΚΑΔ) για `afm` (κενό = ο ΑΦΜ του λογαριασμού)."""
    login = login_fn(username, password)
    if not login.get("ok"):
        return {"ok": False, "reason": login.get("reason")}
    http: _HyperHttp = login["http"]
    reg = urljoin(AADE, "/saadeapps3/comregistry")
    w = reg + "/webresources/infomytaxisnet"

    http.follow("GET", reg + "/")
    userdata = http.follow("GET", w + "/getuserdata/username")["text"]
    login_afm = _tag(userdata, "afm")
    target = (afm or "").strip() or login_afm
    if not target:
        return {"ok": False, "reason": "NoAfm"}

    fysiko = http.follow("GET", w + "/getMhtrwoFusikou/" + target)["text"]
    epix = http.follow("GET", w + "/getMhtrwoEpixeirhshs/" + target)["text"]
    activities = "[]"
    if "<hmenarxhs>" in epix:
        try:
            activities = http.follow("GET", w + "/getMhtrwoDrastiriothtesEpixeir/" + target)["text"]
        except requests.RequestException:            # οι ΚΑΔ είναι επιπλέον· μην χαλάς όλο το lookup
            log.warning("οι δραστηριότητες (ΚΑΔ) δεν ανακτήθηκαν για το ΑΦΜ %s", target)
    return build_profile(target, login_afm, userdata, fysiko, epix, activities)
