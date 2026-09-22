"""Πλήρες φορολογικό ημερολόγιο taxheaven — από τη σελίδα, όχι από το `soft_dat.xml`.

Το `soft_dat.xml` είναι κυλιόμενο παράθυρο ~6 εβδομάδων γύρω από το "τώρα" (επαληθεύτηκε 2026-09-22: 60 εγγραφές,
16/9–30/10/2026, καμία για Ιούλιο). Η ίδια η σελίδα `taxheaven.gr/calendar?m=..&y=..` όμως δείχνει ΟΛΕΣ τις
υποχρεώσεις του μήνα (π.χ. 90 για Ιούλιο 2026 — επαληθεύτηκε) μέσα σε ένα ενσωματωμένο JS αντικείμενο
`var calendarData = {events: [...], month: '..', year: '..'}`. Το `events` είναι έγκυρο JSON (τα `month`/`year`
γύρω του όχι — είναι literal του JS, με μονά quotes)· το εξάγουμε με regex και το κάνουμε `json.loads` μόνο αυτό.

Πιο εύθραυστο από ένα RSS feed — αν αλλάξει η σελίδα τους, το regex δεν θα ταιριάξει και το μήνα θα μείνει με ό,τι
είχε ήδη (`fetch_month` σηκώνει `ValueError`, το `ingest()` το καταγράφει σαν σφάλμα πηγής χωρίς να ρίξει τα άλλα).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from .. import db
from ..http import DEFAULT_TIMEOUT, make_session
from .rss_fetch import FeedItem, store_obligations

log = logging.getLogger(__name__)

CALENDAR_URL = "https://www.taxheaven.gr/calendar"
_DATA_RE = re.compile(r"var\s+calendarData\s*=\s*\{\s*events:\s*(\[.*?\])\s*,\s*month:\s*'(\d+)'\s*,\s*year:\s*'(\d+)'\s*\}\s*;",
                      re.S)

# Πόσο μπροστά/πίσω από «τώρα» θεωρείται «κοντινό» μήνα (ξανακατεβαίνει σε κάθε έλεγχο — μπορεί να προστεθούν
# νέες υποχρεώσεις/παρατάσεις). Πιο μακρινός μήνας: μία φορά αρκεί, δεν αλλάζει το παρελθόν.
ROLLING_BACK, ROLLING_FORWARD = 1, 3
STALE_AFTER = timedelta(hours=12)


def _month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def rolling_months(today: Optional[date] = None) -> list[tuple[int, int]]:
    """[(year, month), …] από `ROLLING_BACK` μήνες πριν έως `ROLLING_FORWARD` μετά το σημερινό μήνα."""
    today = today or date.today()
    out = []
    for delta in range(-ROLLING_BACK, ROLLING_FORWARD + 1):
        total = today.year * 12 + (today.month - 1) + delta
        out.append((total // 12, total % 12 + 1))
    return out


def is_rolling(year: int, month: int, today: Optional[date] = None) -> bool:
    return (year, month) in rolling_months(today)


def fetch_month(session: Optional[requests.Session], year: int, month: int) -> list[FeedItem]:
    """Οι υποχρεώσεις ενός μήνα, ό,τι δείχνει η ίδια η σελίδα ημερολογίου. `ValueError` αν η σελίδα άλλαξε μορφή."""
    s = session or make_session()
    resp = s.get(CALENDAR_URL, params={"m": f"{month:02d}", "y": year}, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    m = _DATA_RE.search(resp.text)
    if not m:
        raise ValueError("δεν βρέθηκε calendarData στη σελίδα (άλλαξε η μορφή της;)")
    try:
        events = json.loads(m.group(1))
    except ValueError as exc:
        raise ValueError(f"calendarData δεν είναι έγκυρο JSON: {exc}") from exc
    items: list[FeedItem] = []
    for ev in events:
        title = (ev.get("Title") or "").strip()
        raw_date = (ev.get("Date") or "").strip()          # 'MM/DD/YYYY'
        url = (ev.get("url") or "").strip()
        if not title or not url:
            continue
        try:
            due = datetime.strptime(raw_date, "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        items.append(FeedItem(title=title, url=url, published_at=None, summary="", category="", guid=url, due_date=due))
    return items


def ensure_month_synced(conn: sqlite3.Connection, session: Optional[requests.Session], year: int, month: int) -> bool:
    """Κατεβάζει τον μήνα αν δεν έχει ξαναγίνει ποτέ, ή αν είναι «κοντινός» και μπαγιάτικος. True αν έγινε λήψη.
    Δεν ρίχνει εξαίρεση προς τα έξω (σφάλμα δικτύου/μορφής): η σελίδα δείχνει ό,τι υπάρχει ήδη στη βάση."""
    key = _month_key(year, month)
    row = conn.execute("SELECT synced_at FROM calendar_sync WHERE year_month=?", (key,)).fetchone()
    if row is not None:
        stale = is_rolling(year, month) and row["synced_at"] < (datetime.now(timezone.utc) - STALE_AFTER).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not stale:
            return False
    try:
        items = fetch_month(session, year, month)
        new = store_obligations(conn, items)
    except (requests.RequestException, ValueError) as exc:
        log.warning("Ανανέωση ημερολογίου %s απέτυχε: %s", key, exc)
        return False
    conn.execute("INSERT INTO calendar_sync(year_month, synced_at, event_count) VALUES (?,?,?) "
                 "ON CONFLICT(year_month) DO UPDATE SET synced_at=excluded.synced_at, event_count=excluded.event_count",
                 (key, db.utcnow(), len(items)))
    log.info("Ημερολόγιο %s: %d υποχρεώσεις (%d νέες)", key, len(items), new)
    return True


def sync_rolling_window(conn: sqlite3.Connection, session: Optional[requests.Session] = None) -> dict[str, int]:
    """Καλείται από το `ingest()` σε κάθε έλεγχο: οι κοντινοί μήνες ξαναφρεσκάρονται, οι μακρινοί μόνο αν λείπουν."""
    s = session or make_session()
    fetched = new_months = errors = 0
    for year, month in rolling_months():
        try:
            if ensure_month_synced(conn, s, year, month):
                new_months += 1
            fetched += 1
        except Exception:                                   # δεν πρέπει ΠΟΤΕ να σταματήσει το ingest
            log.exception("sync_rolling_window: μήνας %s-%s", year, month)
            errors += 1
    return {"months": fetched, "refreshed": new_months, "errors": errors}
