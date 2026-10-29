"""Headless προγραμματισμένη εκτέλεση (χωρίς UI) — αυτό εκτελεί το Windows Task Scheduler. Τρέχει όσες εργασίες του
χρονοπρογραμματισμού οφείλονται σήμερα (νέα, myDATA, έλεγχος υποβολών, οφειλές, αιτήματα, ανανέωση στοιχείων, email)
— βλ. scheduled_jobs.py. `--daily --all`: όλες οι ενεργές, ανεξάρτητα από τη συχνότητα («Εκτέλεση τώρα»)."""
from __future__ import annotations

import logging
import sys

from . import config, crypto, logs


def main(force: bool = False) -> int:
    config.load_env()
    logs.setup(config.data_dir())
    log = logging.getLogger("taxmatch.daily")
    log.info("── Έναρξη προγραμματισμένης εκτέλεσης%s", " (όλες οι ενεργές εργασίες)" if force else "")
    from . import db, scheduled_jobs
    try:
        conn = db.connect()
    except crypto.KeyfileLocked:
        log.error("Ο φάκελος δεδομένων προστατεύεται με κύριο κωδικό· ο προγραμματισμένος έλεγχος δεν μπορεί να "
                  "ξεκλειδώσει χωρίς να ανοίξει η εφαρμογή. Ανοίξτε το TaxMatch και δώστε τον κωδικό, ή ορίστε τη "
                  "μεταβλητή TAXMATCH_MASTER_PASSWORD για τον λογαριασμό που τρέχει το task.")
        return 3
    try:
        results = scheduled_jobs.run_due(conn, force=force)
    except crypto.KeyfileLocked:
        log.error("Κλειδωμένος φάκελος δεδομένων (κύριος κωδικός) — οι εργασίες δεν εκτελέστηκαν.")
        return 3
    finally:
        conn.close()
    log.info("Ολοκλήρωση: %s", {k: v[1] for k, v in results.items()} or "καμία εργασία δεν οφειλόταν σήμερα")
    return 0 if all(ok for ok, _t in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main("--all" in sys.argv))
