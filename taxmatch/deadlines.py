"""Ενοποιημένη προβολή προθεσμιών: γενικό ημερολόγιο (taxheaven) + προθεσμίες άρθρων που ταίριαξαν με πελάτες."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Optional


def events_between(conn: sqlite3.Connection, start: date, end: date, afm: Optional[str] = None,
                   include_news: bool = True) -> list[dict[str, Any]]:
    """Γεγονότα με due_date στο [start, end]. kind: 'general' | 'news' (το news έχει n_clients = πόσους πελάτες αφορά,
    ή τη συγκεκριμένη επιχείρηση αν δοθεί `afm`)."""
    s, e = start.isoformat(), end.isoformat()
    events: list[dict[str, Any]] = []
    for r in conn.execute("SELECT id, title, due_date, source_url FROM obligations_general "
                          "WHERE due_date BETWEEN ? AND ? ORDER BY due_date, id", (s, e)):
        events.append({"kind": "general", "id": r["id"], "title": r["title"], "date": r["due_date"],
                       "url": r["source_url"], "n_clients": None})
    if include_news:
        sql = ("SELECT a.id, a.title, a.deadline, a.url, COUNT(DISTINCT m.afm) AS n FROM articles a "
               "JOIN matches m ON m.article_id=a.id WHERE a.deadline BETWEEN ? AND ?")
        args: list[Any] = [s, e]
        if afm:
            sql += " AND m.afm=?"
            args.append(afm)
        sql += " GROUP BY a.id ORDER BY a.deadline, a.id"
        for r in conn.execute(sql, args):
            events.append({"kind": "news", "id": r["id"], "title": r["title"], "date": r["deadline"],
                           "url": r["url"], "n_clients": r["n"]})
    events.sort(key=lambda ev: (ev["date"], 0 if ev["kind"] == "news" else 1))
    return events
