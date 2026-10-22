"""Λήψη myDATA στον καθημερινό έλεγχο (Task Scheduler, `taxmatch --daily`) — ρητό αίτημα χρήστη 2026-10-22.

«Έξυπνα», όπως το Timologio Downloader: για κάθε πελάτη με κωδικούς myDATA ζητούνται ΜΟΝΟ ο τρέχων και ο
προηγούμενος μήνας (νέα/καθυστερημένα παραστατικά) και όσοι από τους τελευταίους κλειστούς λείπουν
(`store.months_to_refresh`) — έσοδα ΚΑΙ έξοδα, με ένα αίτημα ανά κατεύθυνση. Από αυτά βγαίνουν μόνα τους, στο
Ημερολόγιο, οι παρακρατούμενοι φόροι, το τέλος ανθεκτικότητας (8.2), το ψηφιακό τέλος συναλλαγής και τα VIES/Intrastat.
Μόνο στοιχεία — ΟΧΙ PDF (αυτά κατεβαίνουν από την καρτέλα του πελάτη).
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable, Optional

from . import credentials, names, store

log = logging.getLogger(__name__)


def run(conn: sqlite3.Connection, should_cancel: Optional[Callable[[], bool]] = None,
        on_each: Optional[Callable[[str, int, int], None]] = None, vies_limit: int = 50) -> dict[str, int]:
    afms = sorted(credentials.with_credentials(conn))
    stats = {"clients": len(afms), "ok": 0, "failed": 0, "docs": 0, "names": 0}
    for i, afm in enumerate(afms, 1):
        if should_cancel and should_cancel():
            break
        try:
            out = store.sync_range(conn, afm, store.months_to_refresh(conn, afm), should_cancel=should_cancel)
        except Exception as exc:                          # noqa: BLE001 — ένας πελάτης δεν σταματά τους υπόλοιπους
            log.warning("myDATA %s: %s", afm, str(exc)[:200])
            out = {"ok": False}
        if out.get("ok"):
            stats["ok"] += 1
            stats["docs"] += out.get("n", 0)
        else:
            stats["failed"] += 1
        if on_each:
            on_each(afm, i, len(afms))
    try:
        stats["names"] = names.resolve_via_vies(conn, limit=vies_limit, should_cancel=should_cancel)
    except Exception as exc:                              # noqa: BLE001
        log.info("VIES επωνυμίες: %s", exc)
    return stats
