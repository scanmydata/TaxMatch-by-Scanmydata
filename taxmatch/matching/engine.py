"""Εφαρμογή του matching πάνω στη βάση: άρθρα (με extracted scope) × πελάτες -> πίνακας `matches`."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .. import db
from ..business_profiles import service as clients
from .match import match_business

MATCH_WINDOW_DAYS = 45
_REMATCH_LOCK = threading.Lock()


def rematch(conn: sqlite3.Connection, window_days: int = MATCH_WINDOW_DAYS) -> dict[str, int]:
    """Ξαναϋπολογίζει τα matches για πρόσφατα άρθρα. Idempotent: προσθέτει νέα, ενημερώνει αιτιολογία/βεβαιότητα
    των υπαρχόντων (το feedback μένει) και αφαιρεί όσα δεν ισχύουν πια ΜΟΝΟ αν δεν έχουν feedback.
    Το lock σειριοποιεί τα rematch μέσα στο ίδιο process (ουρά ανάκτησης + έλεγχος + επεξεργασία πελάτη)."""
    with _REMATCH_LOCK:
        return _rematch(conn, window_days)


def _rematch(conn: sqlite3.Connection, window_days: int) -> dict[str, int]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    businesses = clients.for_matching(conn)
    arts = conn.execute("SELECT id, extracted_json FROM articles WHERE extraction_status='done' "
                        "AND COALESCE(published_at, fetched_at) >= ?", (cutoff,)).fetchall()
    existing: dict[tuple[int, str], sqlite3.Row] = {
        (r["article_id"], r["afm"]): r for r in conn.execute(
            "SELECT article_id, afm, matched_reason, confidence, user_feedback FROM matches WHERE article_id IN "
            "(SELECT id FROM articles WHERE COALESCE(published_at, fetched_at) >= ?)", (cutoff,))}
    now = db.utcnow()
    added = updated = removed = 0
    wanted: set[tuple[int, str]] = set()
    conn.execute("BEGIN")
    try:
        for art in arts:
            try:
                extracted = json.loads(art["extracted_json"] or "{}")
            except ValueError:
                continue
            for b in businesses:
                m = match_business(extracted, b)
                if not m:
                    continue
                key = (art["id"], b["afm"])
                wanted.add(key)
                prev = existing.get(key)
                if prev is None:
                    conn.execute("INSERT INTO matches(article_id, afm, matched_reason, confidence, created_at) "
                                 "VALUES (?,?,?,?,?)", (art["id"], b["afm"], m.reason, m.confidence, now))
                    added += 1
                elif prev["matched_reason"] != m.reason or prev["confidence"] != m.confidence:
                    conn.execute("UPDATE matches SET matched_reason=?, confidence=? WHERE article_id=? AND afm=?",
                                 (m.reason, m.confidence, art["id"], b["afm"]))
                    updated += 1
        for key, prev in existing.items():
            if key not in wanted and prev["user_feedback"] is None:
                conn.execute("DELETE FROM matches WHERE article_id=? AND afm=?", key)
                removed += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return {"added": added, "updated": updated, "removed": removed}


def set_feedback(conn: sqlite3.Connection, match_id: int, value: Optional[int]) -> bool:
    """value: 1 (👍), -1 (👎) ή None (καθαρισμός)."""
    if value not in (1, -1, None):
        raise ValueError("feedback must be 1, -1 or None")
    cur = conn.execute("UPDATE matches SET user_feedback=?, feedback_at=? WHERE id=?",
                       (value, db.utcnow() if value is not None else None, match_id))
    return cur.rowcount > 0


def digest(conn: sqlite3.Connection, days: int = 7, afm: Optional[str] = None,
           feedback: Optional[str] = None, limit: int = 500) -> list[dict[str, Any]]:
    """Matches για το Dashboard/Νέα: ταξινομημένα με πλησιέστερη προθεσμία πρώτη, μετά νεότερα. Εκπρόθεσμα στο τέλος
    της ομάδας με προθεσμία (η προθεσμία που πέρασε είναι λιγότερο επείγουσα από αυτήν που έρχεται)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sql = ("SELECT m.id AS match_id, m.afm, m.matched_reason, m.confidence, m.user_feedback, m.created_at AS matched_at, "
           "b.name AS business_name, a.id AS article_id, a.title, a.url, a.source, a.published_at, a.deadline, "
           "a.extracted_json FROM matches m JOIN articles a ON a.id=m.article_id JOIN businesses b ON b.afm=m.afm "
           "WHERE COALESCE(a.published_at, a.fetched_at) >= ?")
    args: list[Any] = [cutoff]
    if afm:
        sql += " AND m.afm=?"
        args.append(afm)
    if feedback == "up":
        sql += " AND m.user_feedback=1"
    elif feedback == "down":
        sql += " AND m.user_feedback=-1"
    elif feedback == "none":
        sql += " AND m.user_feedback IS NULL"
    rows = []
    for r in conn.execute(sql + " LIMIT ?", args + [limit]):
        d = dict(r)
        try:
            ex = json.loads(d.pop("extracted_json") or "{}")
        except ValueError:
            ex = {}
        d.update(summary=ex.get("summary", ""), action_required=ex.get("action_required"), topic=ex.get("topic", ""))
        rows.append(d)
    rows.sort(key=lambda d: d["published_at"] or "", reverse=True)     # δευτερεύον: νεότερα πρώτα (stable sort)
    rows.sort(key=lambda d: _urgency(d["deadline"], d["confidence"]))
    return rows


def _urgency(deadline: Optional[str], confidence: float, today: Optional[str] = None) -> tuple:
    """Μελλοντική προθεσμία (πλησιέστερη πρώτη) < χωρίς προθεσμία < προθεσμία που έληξε."""
    today = today or datetime.now().date().isoformat()
    if deadline and deadline >= today:
        return (0, deadline, -confidence)
    if not deadline:
        return (1, "", -confidence)
    return (2, deadline, -confidence)


def digest_articles(conn: sqlite3.Connection, days: int = 7) -> list[dict[str, Any]]:
    """Όπως το digest(), αλλά ομαδοποιημένο ανά άρθρο: ένα άρθρο «για όλους» είναι ΜΙΑ κάρτα με λίστα πελατών."""
    groups: dict[int, dict[str, Any]] = {}
    for r in digest(conn, days=days, limit=20000):
        g = groups.get(r["article_id"])
        if g is None:
            g = groups[r["article_id"]] = {
                "article_id": r["article_id"], "title": r["title"], "url": r["url"], "source": r["source"],
                "published_at": r["published_at"], "deadline": r["deadline"], "summary": r["summary"],
                "action_required": r["action_required"], "topic": r["topic"], "matches": [],
            }
        g["matches"].append({k: r[k] for k in ("match_id", "afm", "business_name", "matched_reason",
                                               "confidence", "user_feedback")})
    out = list(groups.values())
    for g in out:
        g["n_matches"] = len(g["matches"])
        g["n_uncertain"] = sum(1 for m in g["matches"] if m["confidence"] < 1.0)
        g["max_confidence"] = max(m["confidence"] for m in g["matches"])
    out.sort(key=lambda g: g["published_at"] or "", reverse=True)
    out.sort(key=lambda g: _urgency(g["deadline"], g["max_confidence"]))
    return out
