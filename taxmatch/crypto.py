"""Κρυπτογράφηση credentials at-rest — ίδιο σχήμα με το Timologio Downloader (timologio/crypto.py), ώστε οι δύο
εφαρμογές να μπαίνουν στη σουίτα ScanMyData με την ίδια αποθήκευση.

* Fernet, versioned prefix ``enc:1:``· ό,τι δεν έχει prefix επιστρέφεται ως έχει από το ``dec()``.
* Το **κλειδί δεδομένων** παράγεται τυχαία μία φορά ανά φάκελο δεδομένων και ζει στο ``.enckey`` — ποτέ στον κώδικα.
* Το ``.enckey`` έχει δύο μορφές:

  ``ακάλυπτο``      σκέτο κλειδί Fernet (ACL μόνο για τον χρήστη Windows). Προστατεύει από κλεμμένο αντίγραφο
                    μόνο της βάσης.
  ``προστατευμένο`` (με κύριο κωδικό) το κλειδί δεδομένων τυλίγεται με δεύτερο κλειδί από τον κωδικό μέσω Argon2id.
                    Χωρίς τον κωδικό ο φάκελος είναι άχρηστος ακόμη κι αν αντιγραφεί ολόκληρος.

  Το κλειδί δεδομένων μένει ίδιο όταν αλλάζει ο κωδικός — αλλιώς κάθε αλλαγή θα ξανακρυπτογραφούσε όλη τη βάση.
* ΠΑΛΙΑ αρχεία του TaxMatch (``taxmatch-key: dpapi1`` / ``plain1``) διαβάζονται κανονικά (DPAPI για τον ίδιο χρήστη
  Windows)· τα δεδομένα δεν χάνονται. Η προστασία με κύριο κωδικό τα μετατρέπει στη νέα μορφή.
* ``TAXMATCH_ENC_KEY`` (env) παρακάμπτει το αρχείο (CI/tests)· ``TAXMATCH_MASTER_PASSWORD`` ξεκλειδώνει χωρίς
  διαδραστικό prompt (headless).
"""
from __future__ import annotations

import base64
import ctypes
import logging
import os
import re
import secrets
import subprocess
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger(__name__)

PREFIX = "enc:1:"
_ENV_KEY = "TAXMATCH_ENC_KEY"
_ENV_PASSWORD = "TAXMATCH_MASTER_PASSWORD"
_MAGIC = "taxmatch-key: 2"                    # προστατευμένη μορφή
_MAGIC_DPAPI = "taxmatch-key: dpapi1"         # παλιά μορφή TaxMatch (μόνο ανάγνωση)
_MAGIC_PLAIN = "taxmatch-key: plain1"         # παλιά μορφή TaxMatch (μόνο ανάγνωση)

# Argon2id: προφίλ OWASP για interactive login (64 MiB / 3 περάσματα)
_ARGON_MEMORY_KIB = 65536
_ARGON_TIME = 3
_ARGON_LANES = 4
_SALT_BYTES = 16

#: Το κλειδί αφού ξεκλειδωθεί μία φορά (ανά διεργασία) — ο χρήστης δίνει τον κωδικό μία φορά ανά εκκίνηση.
_UNLOCKED: dict[str, bytes] = {}
_fernet_cache: dict[str, Fernet] = {}

#: Ίδιο όριο με το timologio downloader (gui/unlock.py) — αρκετό να αντέξει brute force, όχι τόσο ώστε να
#: αποθαρρύνει σε κανονική χρήση.
MIN_PASSWORD_LENGTH = 10


class KeyfileLocked(Exception):
    """Το `.enckey` είναι προστατευμένο και δεν δόθηκε κωδικός."""

    message_el = "Ο φάκελος δεδομένων προστατεύεται με κύριο κωδικό."


class WrongPassword(Exception):
    """Ο κωδικός δεν ξεκλειδώνει το `.enckey`."""

    message_el = "Λάθος κωδικός."


class KeyUnavailable(Exception):
    """Το `.enckey` υπάρχει αλλά δεν ξεκλειδώνει (άλλος χρήστης Windows/άλλο μηχάνημα/λάθος κλειδί)."""

    message_el = (
        "Δεν είναι δυνατή η ανάγνωση των αποθηκευμένων κωδικών (άλλος χρήστης Windows ή μηχάνημα). "
        "Ξαναδώστε τα credentials στις Ρυθμίσεις."
    )


class SecretRedactingFilter(logging.Filter):
    """Κόβει credentials από τα logs (το αρχείο καταγραφής είναι αυτό που στέλνει ο χρήστης για υποστήριξη)."""

    _PATTERNS = [
        (re.compile(r"\bgsk_[A-Za-z0-9]{10,}\b"), "<redacted-key>"),                   # Groq
        (re.compile(r"\bsk-or-[A-Za-z0-9_\-]{10,}\b"), "<redacted-key>"),              # OpenRouter
        (re.compile(r"(?i)\b[0-9a-f]{32}\b"), "<redacted-key>"),                       # ΓΕΜΗ / hex keys
        (re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]+"), r"\1<redacted>"),
        (re.compile(r"(?i)((?:api[_-]?key|password|passwd|pass|pwd|taxis_pass)\s*[:=]\s*)\S+"), r"\1<redacted>"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        red = msg
        for pattern, repl in self._PATTERNS:
            red = pattern.sub(repl, red)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


# ---------------------------------------------------------------- DPAPI (μόνο για ανάγνωση παλιών αρχείων)

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    src = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _DataBlob()
    if not crypt32.CryptUnprotectData(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("DPAPI αποτυχία")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _lock_down(path: Path) -> None:
    """Στα Windows δεν υπάρχει chmod 0600 -> ACL μόνο για τον τρέχοντα χρήστη (best effort)."""
    try:
        if os.name == "nt":
            import getpass

            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
                           check=False, capture_output=True, timeout=10)
        else:
            os.chmod(path, 0o600)
    except Exception:  # pragma: no cover - best effort, δεν μπλοκάρει
        log.debug("Δεν μπόρεσα να περιορίσω τα δικαιώματα του %s", path)


# ---------------------------------------------------------------- μορφή αρχείου

def _parse(raw: bytes) -> dict[str, str] | None:
    """Τα πεδία του προστατευμένου αρχείου, ή None αν δεν είναι αυτής της μορφής."""
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        return None
    if not text.startswith(_MAGIC):
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        key, _, value = line.partition(":")
        if value:
            fields[key.strip()] = value.strip()
    return fields


def _kek(password: str, salt: bytes, kdf: str) -> bytes:
    """Το κλειδί που τυλίγει το κλειδί δεδομένων, παραγόμενο από τον κωδικό."""
    if kdf == "argon2id":
        from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

        raw = Argon2id(salt=salt, length=32, iterations=_ARGON_TIME, lanes=_ARGON_LANES,
                       memory_cost=_ARGON_MEMORY_KIB).derive(password.encode())
    elif kdf == "scrypt":
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        raw = Scrypt(salt=salt, length=32, n=2 ** 16, r=8, p=1).derive(password.encode())
    else:
        raise ValueError(f"Άγνωστη συνάρτηση παραγωγής κλειδιού: {kdf}")
    return base64.urlsafe_b64encode(raw)


def _preferred_kdf() -> str:
    """Argon2id όπου υπάρχει (OpenSSL 3.2+), αλλιώς scrypt· το αρχείο γράφει ποιο χρησιμοποιήθηκε."""
    try:
        _kek("δοκιμή", b"\x00" * _SALT_BYTES, "argon2id")
    except (UnsupportedAlgorithm, ImportError):
        log.info("Το Argon2id δεν υποστηρίζεται εδώ — χρήση scrypt.")
        return "scrypt"
    return "argon2id"


def _write_atomic(path: Path, payload: bytes) -> None:
    """Γράψιμο μέσω προσωρινού αρχείου — το `.enckey` είναι το μοναδικό αντίγραφο του κλειδιού."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_bytes(payload)
    _lock_down(tmp)
    os.replace(tmp, path)
    _lock_down(path)


def _write_protected(path: Path, data_key: bytes, password: str) -> None:
    salt = secrets.token_bytes(_SALT_BYTES)
    kdf = _preferred_kdf()
    wrapped = Fernet(_kek(password, salt, kdf)).encrypt(data_key)
    lines = [_MAGIC, f"kdf: {kdf}", f"salt: {base64.urlsafe_b64encode(salt).decode()}", f"wrapped: {wrapped.decode()}"]
    if kdf == "argon2id":
        lines[1:1] = [f"memory_kib: {_ARGON_MEMORY_KIB}", f"time: {_ARGON_TIME}", f"lanes: {_ARGON_LANES}"]
    _write_atomic(path, ("\n".join(lines) + "\n").encode("ascii"))


def _unwrap(fields: dict[str, str], password: str) -> bytes:
    try:
        salt = base64.urlsafe_b64decode(fields["salt"])
        return Fernet(_kek(password, salt, fields.get("kdf", "argon2id"))).decrypt(fields["wrapped"].encode())
    except (InvalidToken, KeyError, ValueError) as exc:
        raise WrongPassword from exc


def _read_legacy(raw: bytes) -> bytes | None:
    """Παλιές μορφές TaxMatch (dpapi1/plain1). None αν δεν είναι τέτοιο αρχείο."""
    try:
        lines = raw.decode("utf-8").strip().splitlines()
    except UnicodeDecodeError:
        return None
    if not lines:
        return None
    payload = lines[1] if len(lines) > 1 else ""
    if lines[0] == _MAGIC_DPAPI:
        try:
            return _dpapi_unprotect(base64.b64decode(payload))
        except Exception as exc:
            raise KeyUnavailable(str(exc)) from exc
    if lines[0] == _MAGIC_PLAIN:
        return payload.encode()
    return None


# ---------------------------------------------------------------- δημόσιο API (διαδρομές)

def _keyfile() -> Path:
    return config.data_dir() / ".enckey"


def keyfile_path() -> Path:
    """Δημόσιο ισοδύναμο του `_keyfile()` — για τη σελίδα Ρυθμίσεων/ξεκλειδώματος (`web/views.py`)."""
    return _keyfile()


def is_protected(enckey_path: Path) -> bool:
    """Χρειάζεται κύριος κωδικός για να ανοίξει αυτός ο φάκελος δεδομένων;"""
    if not enckey_path.exists():
        return False
    return _parse(enckey_path.read_bytes()) is not None


def unlock(enckey_path: Path, password: str) -> None:
    """Ξεκλειδώνει μία φορά για όλη τη διεργασία. Λάθος κωδικός -> WrongPassword."""
    fields = _parse(enckey_path.read_bytes())
    if fields is None:
        return
    _UNLOCKED[str(enckey_path.resolve())] = _unwrap(fields, password)
    _fernet_cache.clear()


def forget() -> None:
    """Ξεχνά το ξεκλειδωμένο κλειδί (κλείδωμα, δοκιμές)."""
    _UNLOCKED.clear()
    _fernet_cache.clear()


def load_or_create_key(enckey_path: Path, password: str | None = None) -> bytes:
    env = os.environ.get(_ENV_KEY)
    if env:
        return env.strip().encode()

    if enckey_path.exists():
        raw = enckey_path.read_bytes()
        fields = _parse(raw)
        if fields is None:
            legacy = _read_legacy(raw)
            return legacy if legacy is not None else raw.strip()
        slot = str(enckey_path.resolve())
        # Κωδικός που δόθηκε ρητά ελέγχεται ΠΑΝΤΑ απέναντι στο αρχείο, ποτέ από την cache — αλλιώς η «επιβεβαίωση
        # τρέχοντος κωδικού» στην αλλαγή/αφαίρεση προστασίας θα δεχόταν οποιονδήποτε κωδικό.
        if password:
            key = _unwrap(fields, password)
            _UNLOCKED[slot] = key
            return key
        cached = _UNLOCKED.get(slot)
        if cached:
            return cached
        env_password = os.environ.get(_ENV_PASSWORD)
        if not env_password:
            raise KeyfileLocked(KeyfileLocked.message_el)
        key = _unwrap(fields, env_password)
        _UNLOCKED[slot] = key
        return key

    key = Fernet.generate_key()
    if password:
        _write_protected(enckey_path, key, password)
        _UNLOCKED[str(enckey_path.resolve())] = key
    else:
        _write_atomic(enckey_path, key)
    log.info("Δημιουργήθηκε νέο κλειδί κρυπτογράφησης: %s", enckey_path)
    return key


def set_password(enckey_path: Path, password: str, current: str | None = None) -> None:
    """Βάζει ή αλλάζει τον κύριο κωδικό. Το κλειδί δεδομένων δεν αλλάζει (μετατρέπει και τα παλιά αρχεία)."""
    if not password:
        raise ValueError("Ο κωδικός δεν μπορεί να είναι κενός.")
    data_key = load_or_create_key(enckey_path, current)
    _write_protected(enckey_path, data_key, password)
    _UNLOCKED[str(enckey_path.resolve())] = data_key
    _fernet_cache.clear()


def remove_password(enckey_path: Path, current: str) -> None:
    """Επιστρέφει το αρχείο σε ακάλυπτη μορφή, αφού επιβεβαιώσει τον κωδικό."""
    data_key = load_or_create_key(enckey_path, current)
    _write_atomic(enckey_path, data_key)
    _UNLOCKED[str(enckey_path.resolve())] = data_key
    _fernet_cache.clear()


# ---------------------------------------------------------------- enc / dec (ό,τι χρησιμοποιεί η εφαρμογή)

def _fernet() -> Fernet:
    cache_key = os.getenv(_ENV_KEY) or str(_keyfile())
    f = _fernet_cache.get(cache_key)
    if f is None:
        f = _fernet_cache[cache_key] = Fernet(load_or_create_key(_keyfile()))
    return f


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
