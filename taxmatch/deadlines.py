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
                       "url": r["source_url"], "n_clients": None, "description": r["description"] or "",
                       "conditional": False})
        general_by_day.setdefault(r["due_date"], []).append(normalize_text(r["title"]))

    if include_rules:
        business = None
        if afm:
            row = conn.execute(_BUSINESS_SQL + " WHERE afm=?", (afm,)).fetchone()
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
    if not afm:                                          # γενική προβολή: πόσοι πελάτες είναι υπόχρεοι σε κάθε γεγονός
        businesses = _businesses(conn)
        for ev in events:
            if ev["kind"] in ("rule", "general"):
                people = _liable(ev, businesses)
                if people is not None:
                    ev["n_clients"] = sum(1 for c in people if c["status"] == "yes")
                    ev["n_maybe"] = sum(1 for c in people if c["status"] == "maybe")
    order = {"news": 0, "aml": 1, "rule": 2, "general": 3}
    events.sort(key=lambda ev: (ev["date"], order[ev["kind"]], ev["title"]))
    return events


# ------------------------------------------------------------------ ποιους πελάτες αφορά ένα γεγονός
_BUSINESS_SQL = ("SELECT afm, name, legal_form, vat_subject, vat_period_type, books_category, activity_state, cease_date "
                 "FROM businesses")
_RANK = {"yes": 0, "maybe": 1, "no": 2}


def _businesses(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(_BUSINESS_SQL + " ORDER BY name")]


def rules_for_event(ev: dict[str, Any]) -> list[str]:
    """Ποιοι κανόνες αντιστοιχούν σε ένα γεγονός. Για γεγονός του γενικού ημερολογίου (taxheaven) γίνεται από τον τίτλο,
    με τους ίδιους όρους που κρύβουν τον κανόνα όταν υπάρχει το πραγματικό γεγονός — αλλιώς [] (άγνωστο ποιον αφορά)."""
    if ev["kind"] == "rule":
        return [ev["id"]]
    if ev["kind"] != "general":
        return []
    title = normalize_text(ev["title"])
    month = date.fromisoformat(ev["date"]).month
    ids = [r.id for r in obligations.RULES
           if r.hide_if_feed_terms and all(t in title for t in r.hide_if_feed_terms) and month in r.due_months]
    if "vat_monthly" in ids and "vat_quarterly" in ids:
        if "τριμην" in title:
            ids.remove("vat_monthly")
        elif "μηνιαι" in title:
            ids.remove("vat_quarterly")
    if any(i in ids for i in ("vies", "intrastat", "oss", "ioss")):      # «Δήλωση ΦΠΑ OSS» δεν είναι η περιοδική ΦΠΑ
        ids = [i for i in ids if i not in ("vat_monthly", "vat_quarterly")]
    return ids


def _liable(ev: dict[str, Any], businesses: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    ids = rules_for_event(ev)
    if not ids:
        return None
    out = []
    for b in businesses:
        status, reason = min((obligations.liability(i, b) for i in ids), key=lambda x: _RANK[x[0]])
        if status != "no":
            out.append({"afm": b["afm"], "name": b["name"] or b["afm"], "status": status, "reason": reason})
    out.sort(key=lambda c: (_RANK[c["status"]], c["name"]))
    return out


def clients_for_event(conn: sqlite3.Connection, ev: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    """Πελάτες που αφορά το γεγονός: [{afm, name, status 'yes'|'maybe', reason}] — ή None όταν δεν μπορούμε να το
    ξέρουμε (γεγονός του γενικού ημερολογίου που δεν αντιστοιχεί σε κανόνα)."""
    if ev["kind"] == "news":
        return clients_for_article(conn, ev["id"])
    if ev["kind"] == "aml":
        row = conn.execute("SELECT name FROM businesses WHERE afm=?", (ev["afm"],)).fetchone()
        return [{"afm": ev["afm"], "name": (row["name"] if row else "") or ev["afm"], "status": "yes", "reason": ""}]
    return _liable(ev, _businesses(conn))


def clients_for_article(conn: sqlite3.Connection, article_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT m.afm, b.name, m.matched_reason, m.confidence FROM matches m "
                        "JOIN businesses b ON b.afm=m.afm WHERE m.article_id=? ORDER BY m.confidence DESC, b.name",
                        (article_id,))
    return [{"afm": r["afm"], "name": r["name"] or r["afm"], "status": "yes" if r["confidence"] >= 1.0 else "maybe",
             "reason": r["matched_reason"] or ""} for r in rows]
