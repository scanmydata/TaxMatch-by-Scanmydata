"""Headless καθημερινός έλεγχος (χωρίς UI) — αυτό εκτελεί το Windows Task Scheduler."""
from __future__ import annotations

import logging
import sys

from . import config, logging_setup, pipeline


def main() -> int:
    config.load_env()
    logging_setup.setup("daily.log")
    log = logging.getLogger("taxmatch.daily")
    log.info("Έναρξη καθημερινού ελέγχου")
    try:
        stats = pipeline.run_pipeline("scheduled")
    except pipeline.AlreadyRunning:
        log.info("Ένας έλεγχος τρέχει ήδη — παραλείπεται.")
        return 0
    except Exception:
        log.exception("Ο καθημερινός έλεγχος απέτυχε")
        return 1
    for err in stats.get("errors", []):
        log.warning("%s", err)
    log.info("Ολοκλήρωση: %s", {k: v for k, v in stats.items() if k != "ingest"})
    return 1 if stats.get("errors") and stats.get("ingest") and all("error" in v for v in stats["ingest"].values()) else 0


if __name__ == "__main__":
    sys.exit(main())
