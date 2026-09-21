"""Κρυπτογράφηση credentials at-rest (ίδιο pattern με το crypto.py του ScanMyData).

* Fernet (AES-128-CBC + HMAC), prefix ``enc:1:`` για μελλοντικό rotation· ό,τι δεν έχει prefix
  επιστρέφεται ως έχει από το ``dec()``.
* Το κλειδί δεδομένων παράγεται τυχαία μία φορά και ζει στο ``<data dir>/.enckey`` — ποτέ στον κώδικα.
* Στα Windows το κλειδί τυλίγεται με DPAPI (CryptProtectData): ξεκλειδώνει ΜΟΝΟ για τον ίδιο
  χρήστη Windows στο ίδιο μηχάνημα. Ένα αντιγραμμένο φάκελος δεδομένων είναι άχρηστος αλλού.
  Εκτός Windows (dev/tests) αποθηκεύεται ακάλυπτο — προστατεύει μόνο από αντίγραφο της βάσης.
* ``TAXMATCH_ENC_KEY`` (env) παρακάμπτει το αρχείο (CI/tests).
"""
from __future__ import annotations

import base64
import ctypes
import logging
import os
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger(__name__)

PREFIX = "enc:1:"
_ENV_KEY = "TAXMATCH_ENC_KEY"
_MAGIC_DPAPI = "taxmatch-key: dpapi1"
_MAGIC_PLAIN = "taxmatch-key: plain1"

_fernet_cache: dict[str, Fernet] = {}


class KeyUnavailable(Exception):
    """Το `.enckey` υπάρχει αλλά δεν ξεκλειδώνει (άλλος χρήστης Windows/άλλο μηχάνημα)."""

    message_el = (
        "Δεν είναι δυνατή η ανάγνωση των αποθηκευμένων κωδικών (άλλος χρήστης Windows ή μηχάνημα). "
        "Ξαναδώστε τα credentials στις Ρυθμίσεις."
    )


# ---------------------------------------------------------------- DPAPI

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    src, _keep = _blob(data)
    out = _DataBlob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI αποτυχία")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------- key file

def _keyfile() -> Path:
    return config.data_dir() / ".enckey"


def _load_or_create_key() -> bytes:
    env = os.getenv(_ENV_KEY)
    if env:
        return env.encode()
    path = _keyfile()
    if path.exists():
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        magic, payload = (lines[0], lines[1] if len(lines) > 1 else "")
        if magic == _MAGIC_DPAPI:
            try:
                return _dpapi(base64.b64decode(payload), protect=False)
            except Exception as exc:
                raise KeyUnavailable(str(exc)) from exc
        if magic == _MAGIC_PLAIN:
            return payload.encode()
        raise KeyUnavailable("άγνωστη μορφή .enckey")
    key = Fernet.generate_key()
    if os.name == "nt":
        content = f"{_MAGIC_DPAPI}\n{base64.b64encode(_dpapi(key, protect=True)).decode()}\n"
    else:
        content = f"{_MAGIC_PLAIN}\n{key.decode()}\n"
    path.write_text(content, encoding="utf-8")
    if os.name != "nt":
        os.chmod(path, 0o600)
    return key


def _fernet() -> Fernet:
    cache_key = os.getenv(_ENV_KEY) or str(_keyfile())
    f = _fernet_cache.get(cache_key)
    if f is None:
        f = _fernet_cache[cache_key] = Fernet(_load_or_create_key())
    return f


# ---------------------------------------------------------------- public API

def enc(plain: Optional[str]) -> str:
    if not plain:
        return ""
    return PREFIX + _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def dec(value: Optional[str]) -> str:
    if not value:
        return ""
    if not value.startswith(PREFIX):
        return value
    try:
        return _fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise KeyUnavailable("άκυρο κλειδί για τα αποθηκευμένα δεδομένα") from exc
