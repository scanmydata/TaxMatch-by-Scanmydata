"""Συμπλήρωση των νέων του taxheaven που ΔΕΝ πρόλαβε το RSS (ρητό αίτημα 2026-11-04 «καλύτερη ανάκτηση, ειδικά από το
taxheaven»).

Το `soft_new.xml` κρατά ΜΟΝΟ τα 15 τελευταία νέα — επαληθεύτηκε ζωντανά (2026-11-04): σε ημέρα με πολλά νέα, ή όταν ο
έλεγχος δεν έτρεξε για μια-δυο μέρες, τα παλαιότερα έφευγαν από το feed πριν τα δούμε (στη βάση του χρήστη έλειπαν 33
από τα 126 άρθρα μεταξύ 74917 και 75043). Τα άρθρα έχουν ΣΥΝΕΧΟΜΕΝΟ αριθμό (`/news/<id>/<slug>`) και το
`/news/<id>/x` ανακατευθύνει στο σωστό slug — άρα τα κενά στην αρίθμηση των πρόσφατων άρθρων είναι ακριβώς ό,τι
χάθηκε. Κάποιοι αριθμοί δεν αντιστοιχούν σε άρθρο (404) — τους θυμόμαστε ώστε να μη ζητούνται ξανά.
Μόνο δημόσιες σελίδες, χωρίς σύνδεση.
"""
from __future__ import annotations

import html
import logging
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import requests

from .. import settings_store
from ..http import DEFAULT_TIMEOUT
from .rss_fetch import FeedItem, store_articles

log = logging.getLogger(__name__)

SOURCE_ID = "taxheaven_new"
NEWS_URL = "https://www.taxheaven.gr/news/{id}/x"
MAX_FETCH = 40                  # αιτήματα ανά έλεγχο — τα υπόλοιπα κενά στον επόμενο
WINDOW_DAYS = 14                # μόνο κενά ανάμεσα σε πρόσφατα άρθρα (το παλιό παρελθόν δεν ενδιαφέρει)
_MISSING_KEY = "taxheaven_missing_ids"
_ID_RE = re.compile(r"taxheaven\.gr/news/(\d+)")


def _meta(page: str, prop: str) -> str:
    m = re.search(r'<meta[^>]+(?:property|name)="' + re.escape(prop) + r'"[^>]+content="([^"]*)"', page)
    return html.unescape(m.group(1)).strip() if m else ""


def parse_page(page: str, url: str) -> Optional[FeedItem]:
    """Τίτλος / ημερομηνία / περιγραφή από τη σελίδα του άρθρου (og:title, article:published_time, description)."""
    title = _meta(page, "og:title")
    if not title:
        m = re.search(r"<title>(.*?)</title>", page, re.S)
        title = html.unescape(m.group(1)).strip() if m else ""
    if not title or title.lower() == "taxheaven":
        return None
    published = None
    raw = _meta(page, "article:published_time")
    if raw:
        try:
            published = datetime.fromisoformat(raw).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            published = None
    return FeedItem(title=title, url=_meta(page, "og:url") or url, published_at=published,
                    summary=_meta(page, "description"), category="", guid=url)


def missing_ids(conn: sqlite3.Connection, latest: Optional[int] = None) -> list[int]:
    """Αριθμοί άρθρων ανάμεσα στο παλαιότερο πρόσφατο (τελευταίες `WINDOW_DAYS`) και στο νεότερο που ΔΕΝ έχουμε —
    νεότερα πρώτα."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    have, recent = set(), []
    for (url, when) in conn.execute("SELECT url, COALESCE(published_at, fetched_at) FROM articles "
                                    "WHERE url LIKE '%taxheaven.gr/news/%'"):
        m = _ID_RE.search(url or "")
        if m:
            have.add(int(m.group(1)))
            if (when or "") >= cutoff:
                recent.append(int(m.group(1)))
    if latest:
        recent.append(latest)
    if not recent:
        return []
    known_missing = {int(x) for x in (settings_store.get(conn, _MISSING_KEY) or "").split(",") if x.isdigit()}
    lo, hi = min(recent), max(recent)
    return [i for i in range(hi - 1, lo, -1) if i not in have and i not in known_missing]


def backfill(conn: sqlite3.Connection, session: requests.Session, max_fetch: int = MAX_FETCH,
             sleep: Callable[[float], None] = time.sleep,
             on_progress: Optional[Callable[[str], None]] = None) -> dict[str, int]:
    """Κατεβάζει τα άρθρα που λείπουν και τα αποθηκεύει όπως το RSS (ίδιο dedup / ίδια ροή προς το LLM)."""
    ids = missing_ids(conn)
    items: list[FeedItem] = []
    gone: list[int] = []
    to_fetch = ids[:max_fetch]
    for idx, i in enumerate(to_fetch, 1):
        if on_progress:
            on_progress(f"Συμπλήρωση Taxheaven ({idx}/{len(to_fetch)})…")
        url = NEWS_URL.format(id=i)
        try:
            resp = session.get(url, timeout=DEFAULT_TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            log.warning("taxheaven %s: %s", i, exc)
            break                                       # δίκτυο — ξανά στον επόμενο έλεγχο
        if resp.status_code == 404:
            gone.append(i)
            continue
        if resp.status_code != 200:
            break
        item = parse_page(resp.text, resp.url)
        if item is None:
            gone.append(i)
        else:
            items.append(item)
        sleep(0.3)
    if gone:
        prev = [x for x in (settings_store.get(conn, _MISSING_KEY) or "").split(",") if x.isdigit()]
        settings_store.set_value(conn, _MISSING_KEY, ",".join((prev + [str(g) for g in gone])[-500:]))
    new = store_articles(conn, SOURCE_ID, items) if items else 0
    return {"missing": len(ids), "fetched": len(items), "new": new}
