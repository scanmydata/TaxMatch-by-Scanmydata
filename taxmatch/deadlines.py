"""Ενοποιημένη προβολή προθεσμιών: γενικό ημερολόγιο (taxheaven), κανόνες (ΦΠΑ/VIES/…), και προθεσμίες άρθρων που ταίριαξαν."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, timedelta
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
    # Γεγονότα του γενικού ημερολογίου που αντιστοιχούν σε κανόνα (και λίγο έξω από το διάστημα, ώστε ένας κανόνας στις
    # 30/10 να «βλέπει» το γεγονός του taxheaven στις 2/11): ο κανόνας κρύβεται — το πραγματικό γεγονός υπερισχύει.
    covered: list[tuple[str, date]] = []
    seen_titles: set[tuple[str, str]] = set()
    margin = timedelta(days=_SAME_DEADLINE_DAYS)
    for r in conn.execute("SELECT id, title, due_date, source_url, description FROM obligations_general "
                          "WHERE due_date BETWEEN ? AND ? ORDER BY due_date, id",
                          ((start - margin).isoformat(), (end + margin).isoformat())):
        if (r["due_date"], r["title"]) in seen_titles:
            continue                                   # το taxheaven έχει κατά καιρούς το ίδιο γεγονός δύο φορές
        seen_titles.add((r["due_date"], r["title"]))
        rules = classify_general(r["title"])
        d = date.fromisoformat(r["due_date"])
        covered += [(rid, d) for rid in rules]
        if not (s <= r["due_date"] <= e):
            continue
        events.append({"kind": "general", "id": r["id"], "title": r["title"], "date": r["due_date"],
                       "url": r["source_url"], "n_clients": None, "description": r["description"] or "",
                       "conditional": False, "rules": rules,
                       "ref_months": {rid: nearest_ref(rid, d) for rid in rules}})

    business = None
    if include_rules:
        if afm:
            row = conn.execute(_BUSINESS_SQL + " WHERE afm=?", (afm,)).fetchone()
            if row:
                from .mydata import analysis as mydata_analysis
                months = mydata_analysis.month_flags(conn).get(afm, {})
                business = {**dict(row), "mydata": months, "mydata_ever": _ever(months)}
            else:
                business = None
        if business is not None:
            business.update(_checks_for(conn, afm))
        for oc in obligations.occurrences(start, end, business):
            if oc.conditional and not include_conditional:
                continue
            if any(rid == oc.rule_id and abs((d - oc.date).days) <= _SAME_DEADLINE_DAYS for rid, d in covered):
                continue                                   # το feed έχει ήδη το πραγματικό γεγονός (ίδια προθεσμία)
            ev = {"kind": "rule", "id": oc.rule_id, "title": oc.title, "date": oc.date.isoformat(),
                  "url": oc.source_url, "n_clients": None, "description": oc.description,
                  "conditional": oc.conditional, "ref_month": oc.ref_month}
            if business is not None:
                ev["submission"] = obligations.submission(oc.rule_id, business, oc.ref_month)
            events.append(ev)

    if afm and business is not None:
        # Καρτέλα πελάτη: από το γενικό ημερολόγιο ΜΟΝΟ ό,τι αντιστοιχεί σε κανόνα για τον οποίο είναι υπόχρεος (πριν
        # έβγαιναν ~90 γεγονότα τον μήνα που δεν τον αφορούν και υπόχρεος ακόμη κι όταν το myDATA έλεγε «όχι»).
        kept = []
        for ev in events:
            if ev["kind"] != "general":
                kept.append(ev)
                continue
            best = None
            for rid in ev.get("rules") or []:
                ref = ev["ref_months"][rid]
                status, reason = obligations.liability(rid, business, ref)
                if status != "no" and (best is None or _RANK[status] < _RANK[best[1]]):
                    best = (rid, status, reason, ref)
            if best and (best[1] == "yes" or include_conditional):
                ev.update(conditional=best[1] == "maybe", ref_month=best[3], rule_id=best[0],
                          submission=obligations.submission(best[0], business, best[3]))
                kept.append(ev)
        events = kept

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
                    subs = [c["submission"]["status"] for c in people if c["status"] == "yes"]
                    ev["n_submitted"] = sum(1 for x in subs if x in ("submitted", "done"))
                    ev["n_missing"] = sum(1 for x in subs if x == "not_submitted")
    order = {"news": 0, "aml": 1, "rule": 2, "general": 3}
    events.sort(key=lambda ev: (ev["date"], order[ev["kind"]], ev["title"]))
    return events


# ------------------------------------------------------------------ ποιους πελάτες αφορά ένα γεγονός
_BUSINESS_SQL = ("SELECT afm, name, legal_form, vat_subject, vat_period_type, vat_period_source, books_category, "
                 "activity_state, cease_date, employer, intrastat "
                 "FROM businesses")
#: Ίδια προθεσμία αν κανόνας και γεγονός του taxheaven απέχουν έως τόσες ημέρες (Σαββατοκύριακα, παρατάσεις λίγων ημερών).
_SAME_DEADLINE_DAYS = 6
_RANK = {"yes": 0, "maybe": 1, "no": 2}


def _businesses(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    from .mydata import analysis as mydata_analysis
    flags = mydata_analysis.month_flags(conn)          # ανά μήνα: VIES / Intrastat / παρακράτηση από το myDATA
    checks = _checks_for(conn)
    return [{**dict(r), "mydata": flags.get(r["afm"], {}), "mydata_ever": _ever(flags.get(r["afm"], {})),
             **checks.get(r["afm"], {"checks": {}, "done": set()})}
            for r in conn.execute(_BUSINESS_SQL + " ORDER BY name")]


def _ever(months: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Σημαίες myDATA «έχει εμφανιστεί ποτέ» (σε όσους μήνες έχουν κατέβει) — {} αν δεν έχει κατέβει τίποτα."""
    if not months:
        return {}
    keys = {k for flags in months.values() for k in flags}
    return {k: any(flags.get(k) for flags in months.values()) for k in keys}


def _checks_for(conn: sqlite3.Connection, afm: Optional[str] = None) -> dict[str, Any]:
    """Αποτελέσματα ελέγχου υποβολών + χειροκίνητα «ολοκληρώθηκε», στη μορφή που διαβάζουν τα
    `obligations.liability/submission`: {'checks': {'years': {(έντυπο, χρήση): status}, 'periods': {(έντυπο, 'YYYY-MM'):
    {status, amounts}}}, 'done': {(κανόνας, 'YYYY-MM')}}. Με `afm` επιστρέφει ΤΟΝ πελάτη, αλλιώς ΑΦΜ -> αυτό."""
    out: dict[str, dict[str, Any]] = {}
    where, args = (" WHERE afm=?", (afm,)) if afm else ("", ())
    for r in conn.execute("SELECT afm, year, obligation, period, period_end, status, amounts_json, detail, checked_at "
                          "FROM obligation_checks" + where, args):
        c = out.setdefault(r["afm"], {"checks": {"years": {}, "periods": {}}, "done": set()})["checks"]
        years = c["years"]
        key = (r["obligation"], r["year"])
        if r["status"] == "not_obligated" or key not in years or years[key] == "submitted":
            years[key] = r["status"]
        if r["period_end"]:
            c["periods"][(r["obligation"], r["period_end"][:7])] = {
                "status": r["status"], "amounts": json.loads(r["amounts_json"] or "{}"), "obligation": r["obligation"],
                "period": r["period"], "detail": r["detail"], "checked_at": r["checked_at"]}
    for r in conn.execute("SELECT afm, rule_id, ref_month, done_at FROM obligation_done" + where, args):
        entry = out.setdefault(r["afm"], {"checks": {"years": {}, "periods": {}}, "done": set()})
        entry["done"].add((r["rule_id"], r["ref_month"]))
        entry.setdefault("done_at", {})[(r["rule_id"], r["ref_month"])] = r["done_at"]
    if afm:
        return out.get(afm, {"checks": {"years": {}, "periods": {}}, "done": set()})
    return out


def mark_done(conn: sqlite3.Connection, afm: str, rule_id: str, ref_month: str, done: bool = True, note: str = "") -> None:
    """Χειροκίνητο «ολοκληρώθηκε» για μια προθεσμία πελάτη (ή αναίρεση)."""
    from . import db
    if done:
        conn.execute("INSERT OR REPLACE INTO obligation_done(afm, rule_id, ref_month, done_at, note) VALUES (?,?,?,?,?)",
                     (afm, rule_id, ref_month, db.utcnow(), note))
    else:
        conn.execute("DELETE FROM obligation_done WHERE afm=? AND rule_id=? AND ref_month=?", (afm, rule_id, ref_month))


# ------------------------------------------------------------------ τίτλοι του ημερολογίου ΑΑΔΕ/taxheaven -> κανόνες
def _compact(title: str) -> str:
    """normalize_text + ένωση συντομογραφιών με τελείες: «Φ.Π.Α.» → «φ π α» → «φπα», «Α.Π.Δ.» → «απδ»."""
    t = normalize_text(title)
    return re.sub(r"\b(\w)( \w\b)+", lambda m: m.group(0).replace(" ", ""), t)


#: (κανόνας, όλα αυτά, τουλάχιστον ένα από αυτά, κανένα από αυτά) — επί του `_compact(τίτλου)`. Επαληθεύτηκαν στους
#: πραγματικούς τίτλους Σεπ.–Νοε. 2026 (π.χ. «Υποβολή δήλωσης Φ.Π.Α. … διπλογραφικά βιβλία, μηνός Σεπτεμβρίου»,
#: «Υποβολή Α.Π.Δ. e-Ε.Φ.Κ.Α. μηνός Σεπτεμβρίου…», «…παρακρατούμενου φόρου, … περ. δ’ της παρ. 1 του άρθ. 64…»).
_VAT_NOT = ("δηλωσης επιλογης", "ειδικ", "επιστροφ", "εκτακτ", "παρεπιδημ", "αναφορ", "oss", "τελων")
_TITLE_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]] = [
    ("vat_monthly", ("δηλωσ", "φπα"), ("διπλογραφ",), _VAT_NOT),
    ("vat_quarterly", ("δηλωσ", "φπα"), ("απλογραφ", "τριμην"), _VAT_NOT),
    ("vies", (), ("ανακεφαλαιωτικ", "vies"), ()),
    ("intrastat", ("intrastat",), (), ()),
    ("oss", ("oss",), (), ("import",)),
    ("ioss", (), ("import oss", "ioss"), ()),
    ("apd", ("απδ", "εφκα"), (), ("ετων", "τεκα")),
    ("withholding_fees", ("παρακρατ",), (" 64", "εργοληπτ"), ("μισθωτ", "γενικης κυβερνησης", "προσωπικων εταιρειων")),
    ("fmy_wages", ("παρακρατ", "μισθωτη εργασια"), (), ("ναυτικ",)),
    ("climate_fee", ("ανθεκτικοτητ",), (), ()),
    ("digital_fee", ("ψηφιακ", "τελους συναλλαγ"), (), ()),
    ("ergani_e4", (), ("ε4", "ετησιου πινακα προσωπικου"), ()),
    ("income_tax_return", ("δηλωσ", "φορολογιας εισοδηματος"), (), ("τροποποιητικ", "μεταφορας")),
]


def classify_general(title: str) -> list[str]:
    t = _compact(title)
    out = [rid for rid, every, some, never in _TITLE_RULES
           if all(w in t for w in every) and (not some or any(w in t for w in some)) and not any(w in t for w in never)]
    # Γενικός τίτλος («Υποβολή δήλωσης ΦΠΑ …» χωρίς είδος βιβλίων/περίοδο): αφορά και τις δύο περιοδικές.
    if ("δηλωσ" in t and "φπα" in t and not any(w in t for w in _VAT_NOT) and not out):
        out = ["vat_monthly", "vat_quarterly"]
    # Απλογραφικά «α) τριμήνου … και β) μηνός …»: το (β) αφορά λίγους Β κατηγορίας με μηνιαία περίοδο — δεν ενώνεται με
    # τη μηνιαία των διπλογραφικών (θα έβγαζε υπόχρεους και τους Γ κατηγορίας δύο φορές).
    return out


def nearest_ref(rule_id: str, d: date) -> str:
    """Μήνας αναφοράς ('YYYY-MM') της προθεσμίας του κανόνα που πέφτει πιο κοντά στην ημερομηνία `d` — π.χ. ΑΠΔ στις
    2/11 = Σεπτέμβριος (η κανονική λήξη 31/10 μετατέθηκε), όχι Οκτώβριος."""
    rule = obligations.RULES_BY_ID[rule_id]
    best: Optional[tuple[int, str]] = None
    y, m = d.year, d.month
    for back in range(0, 3):
        cy, cm = y, m - back
        while cm < 1:
            cy, cm = cy - 1, cm + 12
        if cm not in rule.due_months:
            continue
        gap = abs((rule.due(cy, cm) - d).days)
        if best is None or gap < best[0]:
            best = (gap, obligations.ref_month(cy, cm, rule.ref_offset))
    if best is None:
        due = d
        return obligations.ref_month(due.year, due.month, rule.ref_offset)
    return best[1]


def rules_for_event(ev: dict[str, Any]) -> list[str]:
    """Ποιοι κανόνες αντιστοιχούν σε ένα γεγονός. Για γεγονός του γενικού ημερολογίου (taxheaven) γίνεται από τον τίτλο
    (`classify_general`) — αλλιώς [] (άγνωστο ποιον αφορά)."""
    if ev["kind"] == "rule":
        return [ev["id"]]
    if ev["kind"] != "general":
        return []
    return ev.get("rules") if ev.get("rules") is not None else classify_general(ev["title"])


def _liable(ev: dict[str, Any], businesses: list[dict[str, Any]]) -> Optional[list[dict[str, Any]]]:
    ids = rules_for_event(ev)
    if not ids:
        return None
    # Μήνας αναφοράς ανά κανόνα: το γεγονός κανόνα τον φέρει· για γεγονός του γενικού ημερολογίου βγαίνει από την ημερομηνία.
    due = date.fromisoformat(ev["date"])
    refs = {i: ev.get("ref_month") or (ev.get("ref_months") or {}).get(i) or nearest_ref(i, due) for i in ids}
    out = []
    for b in businesses:
        rid, (status, reason) = min(((i, obligations.liability(i, b, refs[i])) for i in ids),
                                    key=lambda x: _RANK[x[1][0]])
        if status != "no":
            out.append({"afm": b["afm"], "name": b["name"] or b["afm"], "status": status, "reason": reason,
                        "rule_id": rid, "ref_month": refs[rid],
                        "submission": obligations.submission(rid, b, refs[rid])})
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


def client_year(conn: sqlite3.Connection, afm: str, year: int) -> list[dict[str, Any]]:
    """Όλες οι περιοδικές υποχρεώσεις του πελάτη με περίοδο μέσα στη φορολογική χρήση `year` (και η δήλωση εισοδήματος
    της χρήσης, που λήγει την επόμενη χρονιά): προθεσμία, υπόχρεος (αιτιολογία), κατάσταση υποβολής, ποσά.
    Για την καρτέλα πελάτη → «Προθεσμίες & υποβολές» (χωρισμένα ανά χρήση — ρητό αίτημα χρήστη 2026-10-22)."""
    from .mydata import analysis as mydata_analysis
    row = conn.execute(_BUSINESS_SQL + " WHERE afm=?", (afm,)).fetchone()
    if not row:
        return []
    months = mydata_analysis.month_flags(conn).get(afm, {})
    business = {**dict(row), "mydata": months, "mydata_ever": _ever(months), **_checks_for(conn, afm)}
    out = []
    for oc in obligations.occurrences(date(year, 1, 1), date(year + 1, 8, 31), business):
        if obligations.check_year(oc.rule_id, oc.ref_month) != year:
            continue
        status, reason = obligations.liability(oc.rule_id, business, oc.ref_month)
        if status == "no":
            continue
        out.append({"rule_id": oc.rule_id, "title": oc.title, "date": oc.date.isoformat(), "ref_month": oc.ref_month,
                    "status": status, "reason": reason, "url": oc.source_url, "description": oc.description,
                    "submission": obligations.submission(oc.rule_id, business, oc.ref_month)})
    out.sort(key=lambda r: (r["date"], r["title"]))
    return out

