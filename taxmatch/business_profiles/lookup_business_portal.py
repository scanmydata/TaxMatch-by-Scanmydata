"""Lookup επιχείρησης από το Business Portal (ΓΕΜΗ) opendata API.

Port (απλοποιημένο) του e3/checks/fetch_business_partners.py του ScanMyData: κρατήθηκαν ο throttling
(8 αιτήματα/λεπτό ανά key), η cache 15' και το retry/backoff. Αφαιρέθηκε η λογική εταίρων (δεν χρειάζεται εδώ).

ΣΗΜΕΙΩΣΗ: το ακριβές σχήμα των πεδίων ΚΑΔ της απάντησης δεν είχε επαληθευτεί με ζωντανό key όταν γράφτηκε
ο κώδικας. Ο parser είναι σκόπιμα ανεκτικός (`extract_kads`) και το ωμό JSON αποθηκεύεται στο
`businesses.lookup_raw` ώστε να διορθωθεί χωρίς νέα κλήση αν το σχήμα διαφέρει.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from collections import deque
from typing import Any, Optional

import requests

from ..http import make_session
from ..textutil import strip_accents

log = logging.getLogger(__name__)

API_URL = "https://opendata-api.businessportal.gr/api/opendata/v1/companies/{ar_gemi}"
SEARCH_URL = "https://opendata-api.businessportal.gr/api/opendata/v1/companies"

try:
    _MAX_RPM = int(os.getenv("BUSINESS_PORTAL_MAX_RPM", "8"))
except ValueError:
    _MAX_RPM = 8
_WINDOW = 60.0
_CACHE_TTL = float(os.getenv("BUSINESS_PORTAL_CACHE_TTL", "900"))

_rate_lock = threading.Lock()
_calls: "deque[float]" = deque()
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, dict]] = {}


def _acquire(sleep=time.sleep, now=time.monotonic) -> None:
    """Μπλοκάρει μέχρι να επιτρέπεται νέο αίτημα (≤ _MAX_RPM ανά λεπτό, κοινό ανάμεσα στα threads)."""
    while True:
        with _rate_lock:
            t = now()
            while _calls and t - _calls[0] > _WINDOW:
                _calls.popleft()
            if len(_calls) < _MAX_RPM:
                _calls.append(t)
                return
            wait = _WINDOW - (t - _calls[0]) + 0.05
        sleep(min(wait, 5.0))


def _cache_get(key: str) -> Optional[dict]:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.monotonic() - entry[0] <= _CACHE_TTL:
            return entry[1]
        _cache.pop(key, None)
        return None


def _cache_put(key: str, data: dict) -> None:
    with _cache_lock:
        _cache[key] = (time.monotonic(), data)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ------------------------------------------------------------------ parsing

def _text(v: Any) -> str:
    """Τιμή πεδίου -> κείμενο (τα lookups συχνά επιστρέφουν {id, descr})."""
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("descr", "description", "name", "title", "value", "label"):
            if v.get(k):
                return str(v[k]).strip()
        return ""
    if isinstance(v, (list, tuple)):
        return _text(v[0]) if v else ""
    return str(v).strip()


def _first(d: dict, keys: list[str]) -> str:
    for k in keys:
        t = _text(d.get(k))
        if t:
            return t
    return ""


_CODE_RE = re.compile(r"^\d{2}(?:[.\s]?\d{1,2}){0,3}$")
_CODE_KEYS = ("code", "kadCode", "activityCode", "kad", "id", "activityId", "codeId")
_DESCR_KEYS = ("descr", "description", "activityDescr", "activityDescription", "name", "title", "label")
_MAIN_WORDS = ("ΚΥΡΙ", "MAIN", "PRIMARY", "ΠΡΩΤΕΥ")


def _kad_from_element(el: Any) -> Optional[dict]:
    if isinstance(el, str):
        m = re.match(r"^\s*(\d[\d.]{3,})\s*[-–:]?\s*(.*)$", el)
        return {"code": m.group(1), "descr": m.group(2).strip(), "is_main": False} if m else None
    if not isinstance(el, dict):
        return None
    flat: dict = {}
    for k, v in el.items():                     # ένα επίπεδο φωλιάς: {"activity": {"id":..., "descr":...}}
        if isinstance(v, dict):
            flat.update({kk: vv for kk, vv in v.items() if kk not in flat})
        flat.setdefault(k, v)
    code = ""
    for k in _CODE_KEYS:
        c = str(flat.get(k) or "").strip()
        if c and _CODE_RE.match(c):
            code = c
            break
    if not code:
        return None
    descr = _first(flat, list(_DESCR_KEYS))
    kind = strip_accents(" ".join(str(flat.get(k) or "") for k in ("type", "kind", "activityType", "typeDescr", "role"))).upper()
    is_main = bool(flat.get("isMain") or flat.get("main") or flat.get("primary")) or any(w in kind for w in _MAIN_WORDS)
    return {"code": code, "descr": descr, "is_main": is_main}


def extract_kads(data: Any, _depth: int = 0) -> list[dict]:
    """Βρίσκει λίστες με κλειδί που μοιάζει «activities/kad» οπουδήποτε (βάθος ≤ 4) και τις κάνει [{code, descr, is_main}]."""
    found: list[dict] = []
    if _depth > 4:
        return found
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, list) and re.search(r"activit|kad", k, re.I):
                for el in v:
                    entry = _kad_from_element(el)
                    if entry:
                        found.append(entry)
            elif isinstance(v, (dict, list)):
                found.extend(extract_kads(v, _depth + 1))
    elif isinstance(data, list):
        for el in data:
            found.extend(extract_kads(el, _depth + 1))
    seen: set[str] = set()
    unique = []
    for e in found:
        digits = re.sub(r"\D", "", e["code"])
        if digits not in seen:
            seen.add(digits)
            unique.append(e)
    if unique and not any(e["is_main"] for e in unique):
        unique[0]["is_main"] = True      # χωρίς ένδειξη: το πρώτο θεωρείται κύριο (η διόρθωση γίνεται στο UI)
    return unique


def _address(c: dict) -> str:
    raw = c.get("address")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    a = raw if isinstance(raw, dict) else {}
    parts = [_first(a, ["street", "addressStreet", "streetName"]) or _first(c, ["street", "coStreet"]),
             _first(a, ["streetNumber", "addressNumber"]) or _first(c, ["streetNumber", "coStreetNumber"]),
             _first(a, ["city", "addressCity"]) or _first(c, ["city", "coCity"]),
             _first(a, ["zipCode", "postalCode"]) or _first(c, ["zipCode", "coZipCode"])]
    return " ".join(p for p in parts if p)


def parse_company(data: Any) -> dict:
    """Ωμή απάντηση API -> {ar_gemi, name, legal_form, status, address, kads}. Ανεκτικό σε διαφορετικά σχήματα."""
    if not isinstance(data, dict):
        return {}
    company = data.get("company") if isinstance(data.get("company"), dict) else data
    if not (company.get("coNameEl") or company.get("afm") or company.get("arGemi")) and data.get("searchResults"):
        company = data["searchResults"][0] or {}
    return {
        "ar_gemi": _first(company, ["arGemi", "arGEMI"]),
        "name": _first(company, ["coNameEl", "coName", "name", "companyName", "coTitlesEl", "coTitleEl"]),
        "legal_form": _first(company, ["legalType", "legalTypeLabel", "legalForm", "legalFormLabel", "coLegalType",
                                       "companyLegalForm"]),
        "status": _first(company, ["status", "statusDescr", "coStatus", "companyStatus"]),
        "address": _address(company),
        "kads": extract_kads(company),
    }


# ------------------------------------------------------------------ HTTP

def _get(session: requests.Session, url: str, api_key: str, params: Optional[dict] = None) -> requests.Response:
    _acquire()
    resp = session.get(url, headers={"accept": "application/json", "api_key": api_key}, params=params, timeout=60)
    if resp.status_code in (401, 403):
        raise PermissionError("Άκυρο API key του Business Portal")
    resp.raise_for_status()
    return resp


class NotFound(Exception):
    pass


def lookup(afm: str, api_key: str, session: Optional[requests.Session] = None) -> dict:
    """Επιστρέφει {'company': parse_company(...), 'raw': <ωμό JSON>}. Ρίχνει NotFound/PermissionError/RequestException."""
    if not api_key:
        raise PermissionError("Δεν έχει οριστεί API key Business Portal")
    cached = _cache_get(afm)
    if cached is not None:
        return cached
    s = session or make_session()
    search = _get(s, SEARCH_URL, api_key, {"afm": afm.zfill(9), "resultsSortBy": "+arGemi",
                                          "resultsOffset": 0, "resultsSize": 10}).json()
    results = search.get("searchResults") or []
    if not results:
        raise NotFound("Δεν βρέθηκε επιχείρηση στο ΓΕΜΗ για αυτό το ΑΦΜ")
    first = results[0]
    company = parse_company(first)
    raw: dict = {"search": first}
    if not company["kads"] and company["ar_gemi"]:          # η αναζήτηση δεν έφερε ΚΑΔ -> πλήρης εγγραφή
        detail = _get(s, API_URL.format(ar_gemi=company["ar_gemi"]), api_key).json()
        raw["detail"] = detail
        full = parse_company(detail)
        company = {k: (full.get(k) or company.get(k)) for k in company}
    out = {"company": company, "raw": raw}
    _cache_put(afm, out)
    return out
