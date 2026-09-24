"""Ενοποιημένη προβολή προθεσμιών: γενικό ημερολόγιο (taxheaven), κανόνες (ΦΠΑ/VIES/…), και προθεσμίες άρθρων που ταίριαξαν."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Any, Optional

from . import obligations
from .ingestion.filters import normalize_text


def events_between(conn: sqlite3.Connection, start: date, end: date, afm: Optional[str] = None,
                   include_news: bool = True, include_rules: bool = True, include_conditional: bool = True,
                   include_aml: bool = False) -> list[dict[str, Any]]:
    """Γεγονότα με ημερομηνία στο [start, end]. kind:
    * 'general' — γεγονός του ημερολογίου taxheaven (πραγματική ημερομηνία)·
    * 'rule'    — υπολογισμένη κανονική προθεσμία (προσαρμοσμένη στον πελάτη αν δοθεί `afm`)· `conditional`=True όταν
                  ισχύει μόνο υπό προϋποθέσεις που δεν γνωρίζουμε (π.χ. VIES: μόνο αν υπήρξαν ενδοκοινοτικές συναλλαγές)·
    * 'news'    — προθεσμία από άρθρο που ταίριαξε (n_clients = πόσους πελάτες αφορά, ή τη συγκεκριμένη επιχείρηση)·
    * 'aml'     — επανεξέταση δέουσας επιμέλειας πελάτη (`include_aml`, μόνο στο native GUI — το web δεν τα δείχνει)."""
    s, e = start.isoformat(), end.isoformat()
    events: list[dict[str, Any]] = []
    general_by_day: dict[str, list[str]] = {}
    for r in conn.execute("SELECT id, title, due_date, source_url, description FROM obligations_general "
                          "WHERE due_date BETWEEN ? AND ? ORDER BY due_date, id", (s, e)):
        events.append({"kind": "general", "id": r["id"], "title": r["title"], "date": r["due_date"],
                       "url": r["source_url"], "n_clients": None, "description": "", "conditional": False})
        general_by_day.setdefault(r["due_date"], []).append(normalize_text(r["title"]))

    if include_rules:
        business = None
        if afm:
            row = conn.execute("SELECT vat_subject, vat_period_type FROM businesses WHERE afm=?", (afm,)).fetchone()
            business = dict(row) if row else None
        for oc in obligations.occurrences(start, end, business):
            if oc.conditional and not include_conditional:
                continue
            terms = obligations.RULES_BY_ID[oc.rule_id].hide_if_feed_terms
            same_day = general_by_day.get(oc.date.isoformat(), [])
            if terms and any(all(t in title for t in terms) for title in same_day):
                continue                                   # το feed έχει ήδη το πραγματικό γεγονός
            events.append({"kind": "rule", "id": oc.rule_id, "title": oc.title, "date": oc.date.isoformat(),
                           "url": oc.source_url, "n_clients": None, "description": oc.description,
                           "conditional": oc.conditional})

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
                           "url": r["url"], "n_clients": r["n"], "description": "", "conditional": False})
    if include_aml:
        from .aml import model as aml_model, store as aml_store
        for r in aml_store.reviews_between(conn, start, end, afm=afm):
            events.append({"kind": "aml", "id": r["afm"], "title": f"Επανεξέταση δέουσας επιμέλειας: {r['name'] or r['afm']}",
                           "date": r["next_review"], "url": "", "n_clients": 1, "afm": r["afm"], "conditional": False,
                           "description": f"Τρέχουσα κατάταξη: {aml_model.CATEGORY_LABEL.get(r['final_category'], '')}. "
                                          "Νέα αξιολόγηση από «Δέουσα επιμέλεια» ή την καρτέλα του πελάτη."})
    order = {"news": 0, "aml": 1, "rule": 2, "general": 3}
    events.sort(key=lambda ev: (ev["date"], order[ev["kind"]], ev["title"]))
    return events
