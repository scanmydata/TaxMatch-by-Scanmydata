"""Αρχείο καταγραφής.

Το ιστορικό έφυγε από την οθόνη, οπότε αυτό εδώ είναι πλέον το μόνο μέρος όπου
μένει τι έγινε και πότε. Γράφεται σε ώρα Ελλάδας — ένας λογιστής που στέλνει το
αρχείο για υποστήριξη δεν πρέπει να χρειάζεται να μεταφράζει UTC.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .crypto import SecretRedactingFilter

_ATHENS = None


def _athens_tz():
    """Η ζώνη Ελλάδας, με πτώση πίσω στην τοπική ώρα.

    Τα Windows δεν έχουν βάση ζωνών: το ZoneInfo δουλεύει μόνο αν υπάρχει το
    πακέτο tzdata. Αν λείπει, η τοπική ώρα του μηχανήματος είναι ούτως ή άλλως
    ελληνική στην πράξη — καλύτερο από το να μη γράφεται τίποτα.
    """
    global _ATHENS
    if _ATHENS is None:
        try:
            from zoneinfo import ZoneInfo

            _ATHENS = ZoneInfo("Europe/Athens")
        except Exception:  # noqa: BLE001 — δεν αξίζει να πέσει το logging
            _ATHENS = False
    return _ATHENS or None


class GreekFormatter(logging.Formatter):
    """ηη/μμ/εεεε ωω:λλ:δδ, ώρα Ελλάδας."""

    _LEVELS = {
        "DEBUG": "ΛΕΠΤ",
        "INFO": "ΠΛΗΡ",
        "WARNING": "ΠΡΟΣ",
        "ERROR": "ΣΦΑΛ",
        "CRITICAL": "ΚΡΙΣ",
    }

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, tz=timezone.utc)
        athens = _athens_tz()
        moment = moment.astimezone(athens) if athens else moment.astimezone()
        return moment.strftime(datefmt or "%d/%m/%Y %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        record.levelshort = self._LEVELS.get(record.levelname, record.levelname[:4])
        return super().format(record)


def format_local(iso_utc: str, fmt: str = "%d/%m/%Y %H:%M") -> str:
    """Ένα αποθηκευμένο UTC timestamp (`db.utcnow()`) σε τοπική ώρα Ελλάδας, για την οθόνη.

    Ίδια λογική ζώνης με το `GreekFormatter` παραπάνω, ώστε ό,τι δείχνει το UI να συμφωνεί με το αρχείο
    καταγραφής. `''` αν το timestamp είναι κενό/άκυρο (π.χ. δεν έχει τρέξει ποτέ έλεγχος ακόμη).
    """
    if not iso_utc:
        return ""
    try:
        moment = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    athens = _athens_tz()
    return (moment.astimezone(athens) if athens else moment.astimezone()).strftime(fmt)


def log_dir(data_dir: Path) -> Path:
    return data_dir / "logs"


def current_log(data_dir: Path) -> Path:
    return log_dir(data_dir) / "taxmatch.log"


def setup(data_dir: Path, *, verbose: bool = False) -> Path:
    """Στήνει το αρχείο καταγραφής. Επιστρέφει τη διαδρομή του.

    Καλείται μία φορά στην εκκίνηση. Ένα δεύτερο κάλεσμα δεν προσθέτει δεύτερο
    handler — αλλιώς κάθε γραμμή θα γραφόταν δύο φορές.
    """
    target = current_log(data_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for existing in root.handlers:
        if isinstance(existing, RotatingFileHandler):
            base = getattr(existing, "baseFilename", "")
            if base and os.path.abspath(base) == os.path.abspath(target):
                return target

    handler = RotatingFileHandler(
        target, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(
        GreekFormatter("%(asctime)s  %(levelshort)s  %(name)s  %(message)s")
    )
    # Το φίλτρο είναι υποχρεωτικό εδώ, όχι προαιρετικό: σε αντίθεση με το stderr
    # (που σε παραθυρική εφαρμογή δεν το βλέπει κανείς), αυτό το αρχείο μένει
    # στον δίσκο και είναι ακριβώς αυτό που θα στείλει ο λογιστής όταν ζητήσει
    # υποστήριξη. Ένα κλειδί που ξέφυγε εδώ, ξέφυγε για πάντα.
    handler.addFilter(SecretRedactingFilter())
    root.addHandler(handler)

    # Τα urllib3/httpx λένε πολλά και τίποτα χρήσιμο για τον λογιστή.
    for noisy in ("httpx", "httpcore", "urllib3", "requests", "feedparser"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info("── Εκκίνηση εφαρμογής (TaxMatch)")
    return target
