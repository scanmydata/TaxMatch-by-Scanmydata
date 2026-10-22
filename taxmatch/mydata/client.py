"""HTTP client για το myDATA REST API — port του `timologio/mydata/client.py` (Timologio Downloader).

Αμετάβλητος κανόνας (ίδιος με εκεί): αυτό είναι το ΜΟΝΟ σημείο όπου στέλνονται τα διαπιστευτήρια myDATA, και μόνο
προς `AADE_HOST`. Το `pdfs.py` έχει δικό του Session χωρίς auth headers — διαρροή κλειδιού σε πάροχο είναι δομικά
αδύνατη.
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

import requests

log = logging.getLogger(__name__)

MYDATA_BASE = "https://mydatapi.aade.gr/myDATA"
URL_REQUEST_DOCS = f"{MYDATA_BASE}/RequestDocs"
URL_REQUEST_TRANSMITTED_DOCS = f"{MYDATA_BASE}/RequestTransmittedDocs"
URL_REQUEST_MY_EXPENSES = f"{MYDATA_BASE}/RequestMyExpenses"
AADE_HOST = "mydatapi.aade.gr"

#: Αναμονές σε «πολλά αιτήματα» (429) — η ΑΑΔΕ ζητά από 10″ μέχρι ~4½′ (μετρημένο στο timologio).
RATE_LIMIT_DELAYS: tuple[float, ...] = (10, 30, 60, 120, 300)
TRANSIENT_DELAYS: tuple[float, ...] = (5, 15, 30)
MAX_WAIT = 300.0


class MydataError(Exception):
    message_el = "Σφάλμα myDATA"


class AuthError(MydataError):
    """Λάθος κλειδί → HTTP 403 με ΚΕΝΟ σώμα (μετρημένο)."""
    message_el = "Άκυρο όνομα χρήστη ή κλειδί API myDATA"


class RateLimitError(MydataError):
    message_el = "Προσωρινός περιορισμός αιτημάτων από την ΑΑΔΕ"

    def __init__(self, retry_after: Optional[float] = None) -> None:
        super().__init__("rate limited")
        self.retry_after = retry_after


class TransientError(MydataError):
    message_el = "Η ΑΑΔΕ δεν απάντησε σωστά"


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def cursors(root: ET.Element) -> dict[str, str]:
    """nextPartitionKey/nextRowKey (ή το legacy nextPartitionToken)."""
    out = {"nextPartitionKey": "", "nextRowKey": "", "nextPartitionToken": ""}
    for elem in root.iter():
        name, text = _local(elem.tag), (elem.text or "").strip()
        if text and name in out and not out[name]:
            out[name] = text
    return out


class MydataClient:
    def __init__(self, user: str, key: str, *, timeout: float = 60.0, retry: bool = True,
                 sleep: Callable[[float], None] = time.sleep, session: Optional[requests.Session] = None) -> None:
        if not user or not key:
            raise AuthError("λείπουν διαπιστευτήρια")
        self._headers = {"aade-user-id": user, "Ocp-Apim-Subscription-Key": key}
        self._session = session or requests.Session()
        self._timeout, self._retry, self._sleep = timeout, retry, sleep

    def close(self) -> None:
        self._session.close()

    def _get_once(self, url: str, params: dict[str, str]) -> bytes:
        if urlsplit(url).netloc != AADE_HOST:
            raise ValueError(f"Τα διαπιστευτήρια myDATA δεν στέλνονται στο {url!r}")
        try:
            resp = self._session.get(url, params=params, headers=self._headers, timeout=self._timeout)
        except requests.Timeout as exc:
            raise TransientError("timeout") from exc
        except requests.RequestException as exc:
            raise TransientError(str(exc)[:200]) from exc
        if resp.status_code in (401, 403):
            raise AuthError("HTTP 403")
        if resp.status_code == 429:
            ra = resp.headers.get("Retry-After")
            raise RateLimitError(float(ra) if ra and ra.replace(".", "", 1).isdigit() else None)
        if resp.status_code >= 500:
            raise TransientError(f"HTTP {resp.status_code}")
        if resp.status_code != 200:
            raise TransientError(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content

    def get(self, url: str, params: dict[str, str]) -> bytes:
        rate = list(RATE_LIMIT_DELAYS) if self._retry else []
        transient = list(TRANSIENT_DELAYS) if self._retry else []
        while True:
            try:
                return self._get_once(url, params)
            except RateLimitError as exc:
                if not rate:
                    raise
                delay = rate.pop(0)
                if exc.retry_after:
                    delay = min(max(delay, exc.retry_after), MAX_WAIT)
                log.warning("myDATA 429 — αναμονή %.0fs", delay)
                self._sleep(delay)
            except TransientError:
                if not transient:
                    raise
                self._sleep(transient.pop(0))

    def fetch_pages(self, incoming: bool, date_from: str, date_to: str) -> list[bytes]:
        """Όλες οι σελίδες XML ενός διαστήματος (dd/mm/yyyy). incoming=True → RequestDocs (λήψεις του πελάτη),
        False → RequestTransmittedDocs (ό,τι διαβίβασε ο ίδιος: εκδοθέντα ΚΑΙ καταχωρήσεις λήπτη 14.x)."""
        url = URL_REQUEST_DOCS if incoming else URL_REQUEST_TRANSMITTED_DOCS
        params = {"mark": "0", "dateFrom": date_from, "dateTo": date_to}
        pages: list[bytes] = []
        seen: set[str] = set()
        while True:
            payload = self.get(url, dict(params))
            pages.append(payload)
            c = cursors(ET.fromstring(payload))
            if c["nextPartitionKey"] or c["nextRowKey"]:
                sig = c["nextPartitionKey"] + "|" + c["nextRowKey"]
                if sig in seen:
                    break
                seen.add(sig)
                params.pop("nextPartitionToken", None)
                params.update(nextPartitionKey=c["nextPartitionKey"], nextRowKey=c["nextRowKey"])
                continue
            if c["nextPartitionToken"] and c["nextPartitionToken"] not in seen:
                seen.add(c["nextPartitionToken"])
                params.pop("nextPartitionKey", None)
                params.pop("nextRowKey", None)
                params["nextPartitionToken"] = c["nextPartitionToken"]
                continue
            break
        return pages

    def check(self, date_from: str, date_to: str) -> bool:
        """Έλεγχος κωδικών με το μικρό `RequestMyExpenses` (σύνολα, χωρίς pagination). 200 = δεκτοί· 403 → AuthError."""
        self.get(URL_REQUEST_MY_EXPENSES, {"dateFrom": date_from, "dateTo": date_to})
        return True
