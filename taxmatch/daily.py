"""Headless καθημερινός έλεγχος (χωρίς UI) — αυτό εκτελεί το Windows Task Scheduler."""
from __future__ import annotations

import logging
import sys

from . import config, crypto, logs, pipeline


def main() -> int:
    config.load_env()
    logs.setup(config.data_dir())
    log = logging.getLogger("taxmatch.daily")
    log.info("── Έναρξη καθημερινού ελέγχου (προγραμματισμένος)")
    try:
        stats = pipeline.run_pipeline("scheduled")
    except pipeline.AlreadyRunning:
        log.info("Ένας έλεγχος τρέχει ήδη — παραλείπεται.")
        return 0
    except crypto.KeyfileLocked:
        log.error("Ο φάκελος δεδομένων προστατεύεται με κύριο κωδικό· ο προγραμματισμένος έλεγχος δεν μπορεί να "
                  "ξεκλειδώσει χωρίς να ανοίξει η εφαρμογή. Ανοίξτε το TaxMatch και δώστε τον κωδικό, ή ορίστε τη "
                  "μεταβλητή TAXMATCH_MASTER_PASSWORD για τον λογαριασμό που τρέχει το task.")
        return 3
    except Exception:
        log.exception("Ο καθημερινός έλεγχος απέτυχε")
        return 1
    for err in stats.get("errors", []):
        log.warning("%s", err)
    log.info("Ολοκλήρωση: %s", {k: v for k, v in stats.items() if k != "ingest"})
    return 1 if stats.get("errors") and stats.get("ingest") and all("error" in v for v in stats["ingest"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
