"""Λήψη RSS (άρθρα + ημερολόγιο), dedup βάσει URL hash, αποθήκευση στη βάση."""
from __future__ import annotations

import calendar as _cal
import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests

from .. import db
from ..http import DEFAULT_TIMEOUT, make_session
from ..textutil import strip_tags
from . import filters
from .sources import Source

log = logging.getLogger(__name__)

SUMMARY_MAX = 4000


@dataclass
class FeedItem:
    title: str
    url: str
    published_at: Optional[str]      # ISO UTC (Ζ)
    summary: str
    category: str
    guid: str
    due_date: Optional[str] = None   # μόνο για calendar feeds: ημερομηνία στη ζώνη του feed


def url_hash(url: str) -> str:
    parts = urlsplit((url or "").strip())
    norm = urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), parts.query, ""))
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


def _iso_utc(entry) -> Optional[str]:
    st = entry.get("published_parsed") or entry.get("updated_parsed")
    if not st:
        return None
    return datetime.fromtimestamp(_cal.timegm(st), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _local_date(entry) -> Optional[str]:
    """Η ημερομηνία ΩΣ ΓΡΑΜΜΕΝΗ στο feed (π.χ. 'Mon, 21 Sep 2026 00:00:00 +0300' -> 2026-09-21).
    Δεν μετατρέπεται σε UTC: θα μετακινούσε τη λήξη στην προηγούμενη μέρα."""
    raw = entry.get("published") or entry.get("updated")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        return None


def parse_feed(content: bytes, calendar: bool = False) -> list[FeedItem]:
    parsed = feedparser.parse(content)
    items: list[FeedItem] = []
    for e in parsed.entries:
        title = strip_tags(e.get("title", ""))
        link = (e.get("link") or e.get("id") or "").strip()
        if not title or not link:
            continue
        cats = ", ".join(t.get("term", "") for t in e.get("tags", []) if t.get("term"))
        items.append(FeedItem(
            title=title,
            url=link,
            published_at=_iso_utc(e),
            summary=strip_tags(e.get("summary", ""))[:SUMMARY_MAX],
            category=cats,
            guid=(e.get("id") or link).strip(),
            due_date=_local_date(e) if calendar else None,
        ))
    return items


def fetch_feed(source: Source, session: Optional[requests.Session] = None) -> list[FeedItem]:
    s = session or make_session()
    resp = s.get(source.url, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    items = parse_feed(resp.content, calendar=(source.kind == "calendar"))
    if source.max_items and len(items) > source.max_items:
        items = sorted(items, key=lambda i: i.published_at or "", reverse=True)[:source.max_items]
    return items


def store_articles(conn: sqlite3.Connection, source_id: str, items: list[FeedItem], keywords: bool = False) -> int:
    """INSERT OR IGNORE βάσει url_hash. Επιστρέφει πόσα ήταν νέα.

    * `keywords`: γενικό portal — άρθρα χωρίς φορολογικές λέξεις-κλειδιά μπαίνουν ως 'skipped' (δεν πάνε στο LLM).
    * Ίδιο θέμα που υπάρχει ήδη από άλλη πηγή (ίδιος/σχεδόν ίδιος τίτλος, τελευταίες 14 μέρες) μπαίνει ως 'duplicate'
      με `duplicate_of` — δεν αναλύεται ξανά και δεν παράγει διπλά matches."""
    now = db.utcnow()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = [(r["id"], r["title_key"]) for r in conn.execute(
        "SELECT id, title_key FROM articles WHERE title_key != '' AND duplicate_of IS NULL "
        "AND COALESCE(published_at, fetched_at) >= ?", (cutoff,))]
    new = 0
    conn.execute("BEGIN")
    try:
        for it in items:
            key = filters.title_key(it.title)
            dup = filters.find_duplicate(key, recent)
            status = "pending"
            if dup is not None:
                status = "duplicate"
            elif keywords and not filters.is_tax_relevant(it.title, it.summary):
                status = "skipped"
            cur = conn.execute(
                "INSERT OR IGNORE INTO articles(source,title,url,url_hash,published_at,fetched_at,category,raw_summary,"
                "title_key,duplicate_of,extraction_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, it.title, it.url, url_hash(it.url), it.published_at, now, it.category, it.summary, key, dup, status))
            if cur.rowcount:
                new += 1
                if dup is None:
                    recent.append((cur.lastrowid, key))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return new


def store_obligations(conn: sqlite3.Connection, items: list[FeedItem]) -> int:
    """Upsert στο ημερολόγιο υποχρεώσεων βάσει guid. Επιστρέφει πόσες ήταν νέες."""
    now = db.utcnow()
    new = 0
    conn.execute("BEGIN")
    try:
        for it in items:
            if not it.due_date:
                continue
            existing = conn.execute("SELECT id FROM obligations_general WHERE guid=?", (it.guid,)).fetchone()
            if existing:
                conn.execute("UPDATE obligations_general SET title=?, due_date=?, source_url=?, description=?, fetched_at=? "
                             "WHERE id=?", (it.title, it.due_date, it.url, it.summary, now, existing["id"]))
            else:
                conn.execute("INSERT INTO obligations_general(guid,title,due_date,source_url,description,fetched_at) "
                             "VALUES (?,?,?,?,?,?)", (it.guid, it.title, it.due_date, it.url, it.summary, now))
                new += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return new


def ingest(conn: sqlite3.Connection, sources: list[Source], session: Optional[requests.Session] = None,
           on_progress: Optional[Callable[[str], None]] = None) -> dict[str, dict]:
    """Ένα feed που αποτυγχάνει δεν σταματά τα υπόλοιπα. Επιστρέφει {source_id: {new, fetched | error}}."""
    s = session or make_session()
    stats: dict[str, dict] = {}
    for src in sources:
        if on_progress:
            on_progress(f"Λήψη: {src.name}")
        try:
            if src.scrape:
                from . import taxheaven_calendar                    # lazy: αποφυγή κυκλικού import
                stats[src.id] = taxheaven_calendar.sync_rolling_window(conn, s)
                continue
            items = fetch_feed(src, s)
            new = store_obligations(conn, items) if src.kind == "calendar" else store_articles(conn, src.id, items, keywords=src.keywords)
            stats[src.id] = {"fetched": len(items), "new": new}
        except Exception as exc:  # δίκτυο, XML, κ.λπ.
            log.warning("Αποτυχία πηγής %s: %s", src.id, exc)
            stats[src.id] = {"error": str(exc)[:300]}
    return stats
