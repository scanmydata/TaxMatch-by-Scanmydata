"""Επωνυμία και διεύθυνση από ΑΦΜ μέσω VIES (μητρώο ΦΠΑ της ΕΕ) — χωρίς API key.

Ίδια προσέγγιση με το timologio downloader του ScanMyData (REST αντί για SOAP): δωρεάν δημόσια υπηρεσία,
ευγενικός throttle ≤ 1 αίτημα/δευτ. κοινός σε όλα τα threads, και ΠΟΤΕ εξαίρεση προς τον καλούντα — το VIES
πέφτει τακτικά για συντήρηση και η αποτυχία του δεν πρέπει να χαλά την προσθήκη πελάτη.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import requests

from ..http import make_session

log = logging.getLogger(__name__)

VIES_REST = "https://ec.europa.eu/taxation_customs/vies/rest-api/ms/{cc}/vat/{vat}"
MIN_INTERVAL = 1.0
TIMEOUT = 20

_lock = threading.Lock()
_last_call = 0.0


@dataclass
class ViesResult:
    valid: bool = False
    name: str = ""
    address: str = ""
    error: str = ""          # μη κενό = δεν πήραμε απάντηση (δίκτυο/HTTP), όχι «άκυρο ΑΦΜ»


def clean_name(name: Optional[str]) -> str:
    """Το VIES επιστρέφει συχνά πολλαπλές επωνυμίες με «||» (κρατάμε την πρώτη)· «---» σημαίνει «δεν δίνεται»."""
    if not name:
        return ""
    text = str(name).split("||")[0]
    text = " ".join(text.split())
    return "" if (not text or text.strip("-") == "") else text


def clean_address(address: Optional[str]) -> str:
    if not address:
        return ""
    text = " ".join(str(address).replace("\n", " ").split())
    return "" if text.strip("-") == "" else text


def _throttle(sleep=time.sleep, now=time.monotonic) -> None:
    global _last_call
    with _lock:
        wait = MIN_INTERVAL - (now() - _last_call)
        if wait > 0:
            sleep(wait)
        _last_call = now()


def lookup_live(afm: str, session: Optional[requests.Session] = None) -> ViesResult:
    digits = re.sub(r"\D", "", afm or "")
    if len(digits) != 9:
        return ViesResult(error="Το ΑΦΜ πρέπει να έχει 9 ψηφία")
    _throttle()
    s = session or make_session(retries=1)
    try:
        resp = s.get(VIES_REST.format(cc="EL", vat=digits), timeout=TIMEOUT, headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        log.debug("VIES: δίκτυο %s: %s", digits, exc)
        return ViesResult(error="Το VIES δεν απάντησε")
    if resp.status_code != 200:
        return ViesResult(error=f"VIES HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        return ViesResult(error="Μη αναμενόμενη απάντηση VIES")
    if not data.get("isValid"):
        return ViesResult(valid=False)
    return ViesResult(valid=True, name=clean_name(data.get("name")), address=clean_address(data.get("address")))


#: Σημείο που καλούν οι υπόλοιπες μονάδες· τα tests το αντικαθιστούν ώστε να μη γίνεται ποτέ πραγματική κλήση.
lookup = lookup_live
