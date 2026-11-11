"""Μέλη και εκπρόσωποι εταιρειών (όχι ατομικών επιχειρήσεων) — ρητό αίτημα χρήστη 2026-10-22: «αν θέλει ο χρήστης να
αντλεί και τα μέλη, που μπορεί να προέλθουν και από το Excel, και να αντλούνται και γι' αυτά οφειλές και έντυπα».

Πηγές:
* **Μητρώο ΑΑΔΕ** (ίδιο login/εφαρμογή με την ανάκτηση στοιχείων — comregistry): `getMelh/{afm}` (μέλη: επωνυμία,
  ΑΦΜ, είδος συμμετοχής, ποσοστό) και `getSxeseisEpixeirhshs/{afm}` (εκπρόσωποι/διαχειριστές) — ΜΟΝΟ ενεργά (χωρίς
  `hmdiakophs`). Από 2026-11-11 ΠΡΩΤΑ η σελίδα `displayRegistryInfo.htm` με ΟΛΟ το ιστορικό (port του ScanMyData
  `members_registry`) — τα JSON μένουν εφεδρικά. Χρειάζονται οι κωδικοί TAXISnet της εταιρείας.
* **Excel**: στη γραμμή του μέλους «ΑΦΜ εταιρείας», ή στη γραμμή της εταιρείας «Μέλη» / «ΑΦΜ μελών» (λίστα).

Οφειλές/έντυπα ενός μέλους αντλούνται με τους ΔΙΚΟΥΣ ΤΟΥ κωδικούς — γι' αυτό το μέλος γίνεται πελάτης («Προσθήκη ως
πελάτη», μόνο ρητά) και οι κωδικοί του έρχονται από το Excel ή τη δική του καρτέλα. Τρίτοι που δεν είναι πελάτες
δεν αποθηκεύονται πέρα από όνομα/ΑΦΜ/ιδιότητα (ελαχιστοποίηση).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urljoin

from .textutil import strip_accents
from . import db
from .business_profiles import credentials, lookup_aade
from .identifiers import normalize_afm

ROLE_FIELDS = ("eidossymmetoxhs", "eidossxeshs")


def _rows(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "[]")
    except ValueError:
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _year(value: str) -> Optional[int]:
    """«10/05/1997» ή «1997-05-10» → 1997."""
    m = re.search(r"(\d{4})", value or "")
    return int(m.group(1)) if m else None


def _iso(value: str) -> str:
    m = re.match(r"\s*(\d{1,2})/(\d{1,2})/(\d{4})", value or "")
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else (value or "")


def _span_in_year(span: dict[str, str], year: int) -> bool:
    start, end = _year(span.get("since", "")), _year(span.get("until", ""))
    return (start is None or start <= year) and (end is None or end >= year)


def snapshot(conn: sqlite3.Connection, company: str, tax_year: Optional[int] = None) -> None:
    """Μητρώο μελών ανά χρήση. Μέλη ΜΕ ημερομηνίες (Μητρώο ΑΑΔΕ) μπαίνουν σε ΚΑΘΕ χρήση όπου ήταν ενεργά, με τους
    ρόλους που είχαν τότε· μέλη χωρίς ημερομηνίες (Excel/χειροκίνητα/παλιές αντλήσεις) γράφονται μόνο στην τρέχουσα
    χρήση (στιγμιότυπο), οπότε τα παλιότερα έτη τους δεν αλλάζουν."""
    this_year = date.today().year
    now = db.utcnow()
    rows = [dict(r) for r in conn.execute("SELECT * FROM company_members WHERE company_afm=?", (company,))]
    dated = [r for r in rows if _rows(r.get("spans_json") or "[]")]
    undated = [r for r in rows if not _rows(r.get("spans_json") or "[]")]
    if dated:
        marks = ",".join("?" * len(dated))
        conn.execute(f"DELETE FROM company_member_history WHERE company_afm=? AND member_afm IN ({marks})",
                     (company, *[r["member_afm"] for r in dated]))
    for r in dated:
        spans = _rows(r["spans_json"])
        first = min((_year(s.get("since", "")) or this_year) for s in spans)
        for y in range(min(first, this_year), this_year + 1):
            live = [s for s in spans if _span_in_year(s, y)]
            if not live:
                continue
            names = list(dict.fromkeys(s["role"] for s in live if s.get("role")))
            names.sort(key=lambda x: 2 if "διαχειριστ" in _low(x) else 1 if "εκπροσωπ" in _low(x) else 0)
            roles = " · ".join(names)
            since = min((s.get("since", "") for s in live), key=lambda v: _iso(v) or "9999")
            ends = [s.get("until", "") for s in live]
            until = "" if any(not e for e in ends) else max(ends, key=_iso)
            share = next((s.get("share", "") for s in live if s.get("share")), "") or r["share"]
            conn.execute("INSERT OR REPLACE INTO company_member_history(company_afm, tax_year, member_afm, name, role, "
                         "share, since, until, source, snapshot_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (company, y, r["member_afm"], r["name"], roles or r["role"], share, since, until, r["source"], now))
    if tax_year is None or tax_year == this_year:
        conn.execute("DELETE FROM company_member_history WHERE company_afm=? AND tax_year=? AND member_afm NOT IN "
                     "(SELECT member_afm FROM company_members WHERE company_afm=? AND spans_json<>'[]')",
                     (company, this_year, company))
        for r in undated:
            if r["until"]:
                continue                                # αποχώρησε — μένει μόνο στα παλιότερα στιγμιότυπα
            conn.execute("INSERT OR REPLACE INTO company_member_history(company_afm, tax_year, member_afm, name, role, "
                         "share, since, until, source, snapshot_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (company, this_year, r["member_afm"], r["name"], r["role"], r["share"], r["since"], "",
                          r["source"], now))
    elif tax_year:
        # ρητό στιγμιότυπο παλιότερης χρήσης (π.χ. από tests/μετάβαση): τα μέλη χωρίς ημερομηνίες όπως είναι τώρα
        for r in undated:
            if not r["until"]:
                conn.execute("INSERT OR REPLACE INTO company_member_history(company_afm, tax_year, member_afm, name, "
                             "role, share, since, until, source, snapshot_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (company, tax_year, r["member_afm"], r["name"], r["role"], r["share"], r["since"], "",
                              r["source"], now))


def member_years(conn: sqlite3.Connection, company: str) -> list[int]:
    """Χρήσεις με διαθέσιμο μητρώο μελών (ενημερώνει πρώτα το τρέχον)."""
    snapshot(conn, company)
    return [r[0] for r in conn.execute("SELECT DISTINCT tax_year FROM company_member_history "
                                       "WHERE company_afm=? ORDER BY tax_year DESC", (company,))]


def parse_members(melh: str, sxeseis: str) -> list[dict[str, Any]]:
    """Μέλη + σχέσεις του Μητρώου → ένα ανά ΑΦΜ: {afm, name, role, share, since, until, spans}. ΚΡΑΤΑ και όσους
    αποχώρησαν (`hmdiakophs`) — χρειάζονται για το μητρώο παλιότερων χρήσεων· `until` κενό = ενεργό σήμερα, και τότε
    `role`/`share` είναι μόνο οι τρέχουσες ιδιότητες."""
    out: dict[str, dict[str, Any]] = {}
    for r, afm_key, role_key in [(r, "afmmelous", "eidossymmetoxhs") for r in _rows(melh)] + \
                                [(r, "afmsxeshs", "eidossxeshs") for r in _rows(sxeseis)]:
        afm = normalize_afm(_s(r.get(afm_key)))
        if not afm:
            continue
        m = out.setdefault(afm, {"afm": afm, "name": _s(r.get("epwnymia")), "spans": []})
        share = _s(r.get("percentage"))
        m["spans"].append({"role": _s(r.get(role_key)), "since": _s(r.get("hmenarxhs")),
                           "until": _s(r.get("hmdiakophs")), "share": share.replace(".", ",") + "%" if share else ""})
        m["name"] = m["name"] or _s(r.get("epwnymia"))
    for m in out.values():
        _summarise(m)
    return list(out.values())


# ---------------------------------------------------------------- πλήρες ιστορικό: σελίδα Μητρώου (port ScanMyData)
# `members_registry.parse_registry_html` του ScanMyData (ρητό αίτημα 2026-11-11 «τα μέλη σωστά ανά έτος βάσει του
# module που έχουμε στο ScanMyData»): η σελίδα «Στοιχεία Νομικού Προσώπου» έχει ΟΛΟ το ιστορικό μελών και σχέσεων —
# και όσους αποχώρησαν — ενώ το JSON `getMelh` δίνει μόνο τα τρέχοντα (γι' αυτό κανένας πελάτης δεν είχε αποχωρήσαντα).
REGISTRY_PAGE = "/taxisnet/info/protected/displayRegistryInfo.htm"
_PARTICIPATION_ROLE = {"ο.μ.": "Ομόρρυθμο Μέλος", "ε.μ.": "Ετερόρρυθμο Μέλος", "ομ": "Ομόρρυθμο Μέλος",
                       "εμ": "Ετερόρρυθμο Μέλος"}


def _low(s: Any) -> str:
    return strip_accents(str(s or "")).lower()


def _cell_text(raw: str) -> str:
    import html as _html
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", raw or "")).replace("\xa0", " ").split())


def _row_cells(row_html: str) -> list[str]:
    """ΟΛΑ τα κελιά, ΚΑΙ τα κενά (αλλιώς η κενή «Ημ/νία διακοπής» μετατοπίζει τις στήλες)."""
    return [_cell_text(c) for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S | re.I)]


def _tables_by_header(page: str) -> list[tuple[list[str], list[list[str]]]]:
    out = []
    for m in re.finditer(r"<tr[^>]*class=\"[^\"]*tblHeader[^\"]*\"[^>]*>(.*?)</tr>", page or "", re.S | re.I):
        end = page.find("</table>", m.end())
        seg = page[m.end(): end if end >= 0 else len(page)]
        rows = [_row_cells(r) for r in re.findall(r"<tr[^>]*>(.*?)</tr>", seg, re.S | re.I)]
        out.append(([_low(c) for c in _row_cells(m.group(1))], [r for r in rows if any(r)]))
    return out


def _col(headers: list[str], *tokens: str) -> int:
    return next((i for i, h in enumerate(headers) if all(t in h for t in tokens)), -1)


def parse_registry_html(page: str, own_afm: str = "") -> list[dict[str, Any]]:
    """Σελίδα Μητρώου → ένα ανά ΑΦΜ {afm, name, role, share, since, until, spans} (ίδια μορφή με `parse_members`).
    Κάθε γραμμή της σελίδας = μία ΠΕΡΙΟΔΟΣ· μέλη από «Στοιχεία Μελών» (Ο.Μ./Ε.Μ./εταίροι) και από τις «Σχέσεις» ο
    διαχειριστής και ο νόμιμος εκπρόσωπος. Κενή λίστα = η σελίδα δεν είχε τους πίνακες (π.χ. φυσικό πρόσωπο)."""
    out: dict[str, dict[str, Any]] = {}
    for headers, rows in _tables_by_header(page):
        i_afm, i_name = _col(headers, "αφμ"), _col(headers, "επωνυμο")
        i_from, i_to = _col(headers, "εναρξης"), _col(headers, "διακοπης")
        if i_afm < 0 or i_from < 0 or i_to < 0:
            continue
        is_members = _col(headers, "αφμ", "μελους") >= 0 or _col(headers, "συμμετοχης") >= 0
        is_rel = _col(headers, "σχετιζομενος") >= 0 or _col(headers, "ειδος", "σχεσης") >= 0
        i_kind, i_pct = _col(headers, "ειδος"), _col(headers, "ποσοστο")
        for r in rows:
            def g(i: int) -> str:
                return r[i] if 0 <= i < len(r) else ""
            afm = normalize_afm(g(i_afm))
            if not afm or afm == own_afm:
                continue
            kind = g(i_kind)
            if is_members:
                role = _PARTICIPATION_ROLE.get(_low(kind).replace(" ", ""), kind)
            elif is_rel:
                if not any(k in _low(kind) for k in ("διαχειριστ", "εκπροσωπ")):
                    continue
                role = kind
            else:
                continue
            share = g(i_pct) if is_members else ""
            if share and "%" not in share:
                share = share.replace(".", ",") + "%"
            m = out.setdefault(afm, {"afm": afm, "name": g(i_name), "spans": []})
            span = {"role": role, "since": g(i_from), "until": g(i_to), "share": share}
            if span not in m["spans"]:
                m["spans"].append(span)
            m["name"] = m["name"] or g(i_name)
    for m in out.values():
        _summarise(m)
    return list(out.values())


def _summarise(m: dict[str, Any]) -> None:
    """Τρέχουσες ιδιότητες/ποσοστό και συνολικό διάστημα από τις περιόδους· ο διαχειριστής μετά τη συμμετοχή
    («Ομόρρυθμο Μέλος · Διαχειριστής», όπως το `_join_roles` του ScanMyData)."""
    spans = m["spans"]
    live = [s for s in spans if not s["until"]]
    use = live or spans
    roles = list(dict.fromkeys(s["role"] for s in use if s["role"]))
    roles.sort(key=lambda x: 2 if "διαχειριστ" in _low(x) else 1 if "εκπροσωπ" in _low(x) else 0)
    m["role"] = " · ".join(roles)
    m["share"] = next((s["share"] for s in use if s["share"]), "")
    m["since"] = min((s["since"] for s in spans if s["since"]), key=_iso, default="")
    m["until"] = "" if live else max((s["until"] for s in spans), key=_iso, default="")


def fetch_from_registry(http, afm: str) -> list[dict[str, Any]]:
    """Με ήδη συνδεδεμένο http (aade_login της εταιρείας): πρώτα η σελίδα Μητρώου με ΟΛΟ το ιστορικό, εφεδρικά τα
    JSON `getMelh`/`getSxeseisEpixeirhshs` (μόνο τρέχοντα)."""
    try:
        page = http.follow("GET", urljoin(lookup_aade.AADE, REGISTRY_PAGE)).get("text") or ""
        if "tblHeader" not in page:                     # μερικές φορές το session «κολλά» μόνο μετά το home.htm
            http.follow("GET", urljoin(lookup_aade.AADE, "/taxisnet/info/protected/home.htm"))
            page = http.follow("GET", urljoin(lookup_aade.AADE, REGISTRY_PAGE)).get("text") or ""
        found = parse_registry_html(page, afm)
        if found:
            return found
    except Exception as exc:                           # noqa: BLE001 — εφεδρικά το JSON
        import logging
        logging.getLogger(__name__).info("Μητρώο (σελίδα ιστορικού) %s: %s", afm, exc)
    reg = urljoin(lookup_aade.AADE, "/saadeapps3/comregistry")
    w = reg + "/webresources/infomytaxisnet"
    http.follow("GET", reg + "/")
    return parse_members(http.follow("GET", f"{w}/getMelh/{afm}")["text"],
                         http.follow("GET", f"{w}/getSxeseisEpixeirhshs/{afm}")["text"])


def store(conn: sqlite3.Connection, company: str, members: Iterable[dict[str, Any]], source: str) -> int:
    """Αποθηκεύει (ή ενημερώνει) μέλη και ξαναχτίζει το μητρώο ανά χρήση. ΤΙΠΟΤΑ δεν σβήνεται: όσοι αποχώρησαν
    κρατούν ημερομηνία λήξης. Από ΑΑΔΕ: κενή απάντηση = πιθανή αστοχία, δεν αλλάζει τίποτα· μέλος της ΑΑΔΕ που δεν
    εμφανίζεται πια σημειώνεται ως αποχωρήσαν σήμερα. Επιστρέφει πλήθος ΕΝΕΡΓΩΝ μελών."""
    members = [m for m in members if m.get("afm") and m["afm"] != company]
    now = db.utcnow()
    if source == "aade":
        if not members:
            snapshot(conn, company)
            return 0
        marks = ",".join("?" * len(members))
        conn.execute(f"UPDATE company_members SET until=?, updated_at=? WHERE company_afm=? AND source='aade' "
                     f"AND until='' AND member_afm NOT IN ({marks})",
                     (date.today().strftime("%d/%m/%Y"), now, company, *[m["afm"] for m in members]))
    for m in members:
        spans = json.dumps(m.get("spans") or [], ensure_ascii=False)
        conn.execute("INSERT INTO company_members(company_afm, member_afm, name, role, share, since, until, spans_json, "
                     "source, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(company_afm, member_afm) DO UPDATE SET "
                     "name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE company_members.name END, "
                     "role=CASE WHEN excluded.role<>'' THEN excluded.role ELSE company_members.role END, "
                     "share=CASE WHEN excluded.share<>'' THEN excluded.share ELSE company_members.share END, "
                     "since=CASE WHEN excluded.since<>'' THEN excluded.since ELSE company_members.since END, "
                     "until=CASE WHEN excluded.source='aade' THEN excluded.until ELSE company_members.until END, "
                     "spans_json=CASE WHEN excluded.spans_json<>'[]' THEN excluded.spans_json "
                     "ELSE company_members.spans_json END, "
                     "source=CASE WHEN company_members.source IN ('excel','manual') THEN company_members.source "
                     "ELSE excluded.source END, updated_at=excluded.updated_at",
                     (company, m["afm"], m.get("name", ""), m.get("role", ""), m.get("share", ""), m.get("since", ""),
                      m.get("until", ""), spans, source, now))
    snapshot(conn, company)
    return sum(1 for m in members if not m.get("until"))


def fetch_for_company(conn: sqlite3.Connection, afm: str,
                      login_fn: Callable[[str, str], dict] = lookup_aade.aade_login) -> dict[str, Any]:
    creds = credentials.get(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Δεν υπάρχουν κωδικοί TAXISnet της εταιρείας.", "n": 0}
    login = login_fn(*creds)
    if not login.get("ok"):
        return {"ok": False, "reason": lookup_aade.REASONS_EL.get(login.get("reason") or "", "Αποτυχία σύνδεσης."), "n": 0}
    found = fetch_from_registry(login["http"], afm)
    if not found:
        return {"ok": False, "n": 0, "reason": "Το Μητρώο ΑΑΔΕ δεν επέστρεψε μέλη — τα αποθηκευμένα δεν άλλαξαν."}
    return {"ok": True, "reason": "", "n": store(conn, afm, found, "aade"),
            "left": sum(1 for m in found if m.get("until"))}


def company_members(conn: sqlite3.Connection, company: str, tax_year: Optional[int] = None) -> list[dict[str, Any]]:
    """Μέλη με επιχειρηματική κατάσταση, γνωστές άλλες συμμετοχές, κωδικούς και οφειλές."""
    from .obligations import is_legal_entity

    year = tax_year or date.today().year
    if year == date.today().year:
        snapshot(conn, company, year)
    out = []
    for r in conn.execute("SELECT m.*, b.afm AS client_afm, b.email, b.activity_state, b.legal_form AS member_legal_form "
                          "FROM company_member_history m LEFT JOIN businesses b "
                          "ON b.afm=m.member_afm WHERE m.company_afm=? AND m.tax_year=? ORDER BY m.role DESC, m.name",
                          (company, year)):
        d = dict(r)
        d["is_client"] = bool(r["client_afm"])
        since_y, until_y = _year(d.get("since") or ""), _year(d.get("until") or "")
        moves = []
        if since_y == year:
            moves.append("Εισήλθε " + d["since"])
        if until_y == year:
            moves.append("Αποχώρησε " + d["until"])
        # αλλαγές ΙΔΙΟΤΗΤΑΣ μέσα στη χρήση (π.χ. μέλος από 2019, διαχειριστής από 05/07/2021) — όπως οι σημειώσεις
        # «Έναρξη/Αποχώρηση» του ScanMyData, χωρίς να φαίνεται ως νέα είσοδος στην εταιρεία
        spans_row = conn.execute("SELECT spans_json FROM company_members WHERE company_afm=? AND member_afm=?",
                                 (company, r["member_afm"])).fetchone()
        for s in _rows(spans_row["spans_json"] if spans_row else "[]"):
            if _year(s.get("since", "")) == year and s.get("since") != d.get("since") and s.get("role"):
                moves.append(f"{s['role']} από {s['since']}")
            if _year(s.get("until", "")) == year and s.get("until") != d.get("until") and s.get("role"):
                moves.append(f"{s['role']} έως {s['until']}")
        d["movement"] = " · ".join(dict.fromkeys(moves))
        d["has_creds"] = credentials.get(conn, r["member_afm"]) is not None if d["is_client"] else False
        state, legal_form = r["activity_state"] or "", r["member_legal_form"] or ""
        if not d["is_client"]:
            d["own_business"] = "Άγνωστο"
        elif state == "none":
            d["own_business"] = "Όχι"
        elif state == "ceased":
            d["own_business"] = "Διακοπή"
        elif state == "active" and not is_legal_entity(legal_form):
            d["own_business"] = "Ναι — ενεργή"
        elif state == "active":
            d["own_business"] = "Νομικό πρόσωπο"
        else:
            d["own_business"] = "Άγνωστο"
        other_companies = conn.execute(
            "SELECT DISTINCT h.company_afm, COALESCE(b.name, '') AS name FROM company_member_history h "
            "LEFT JOIN businesses b ON b.afm=h.company_afm WHERE h.member_afm=? AND h.tax_year=? "
            "AND h.company_afm<>? ORDER BY name, h.company_afm",
            (r["member_afm"], year, company)).fetchall()
        d["other_companies"] = " · ".join(f"{item['name'] or item['company_afm']} ({item['company_afm']})"
                           for item in other_companies) or "Δεν υπάρχουν καταχωρισμένες"
        d["debts"] = {x["source"]: x for x in (dict(y) for y in conn.execute(
            "SELECT source, total, status, synced_at FROM debt_source_sync WHERE afm=?", (r["member_afm"],)))}
        out.append(d)
    return out


def companies_of(conn: sqlite3.Connection, member: str) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT m.company_afm, m.role, b.name FROM company_members m LEFT JOIN "
                                          "businesses b ON b.afm=m.company_afm WHERE m.member_afm=? AND m.until=''", (member,))]


def add_as_client(conn: sqlite3.Connection, company: str, member_afm: str) -> bool:
    """Το μέλος γίνεται πελάτης (για να έχει δικούς του κωδικούς/οφειλές/έντυπα). False αν ήταν ήδη."""
    from .business_profiles import service
    row = conn.execute("SELECT name FROM company_members WHERE company_afm=? AND member_afm=?",
                       (company, member_afm)).fetchone()
    return service.add(conn, member_afm, row["name"] if row else "", source="member")


def sync_member_clients(conn: sqlite3.Connection, company: str,
                        rg_lookup: Optional[Callable[[str], dict[str, Any]]] = None, check_business: bool = True) -> list[str]:
    """Κάθε μέλος της εταιρείας γίνεται ΚΑΙ εγγραφή στους πελάτες (εμφανίζεται κάτω από την εταιρεία στη λίστα,
    ανοίγει κανονικά — ρητό αίτημα χρήστη 2026-10-23) και εξετάζεται αν έχει ατομική επιχείρηση. Επιστρέφει τους νέους.
    Ατομική επιχείρηση μέλους χωρίς δικούς του κωδικούς: «Αναζήτηση Βασικών Στοιχείων Μητρώου» (RgWsPublic2) με τους
    ειδικούς κωδικούς του γραφείου — το Μητρώο TAXISnet δείχνει μόνο τον ΑΦΜ του λογαριασμού. Χωρίς αυτούς: άγνωστο."""
    from .business_profiles import service
    added = []
    rows = conn.execute("SELECT member_afm FROM company_members WHERE company_afm=? AND until=''",
                        (company,)).fetchall()
    for r in rows:
        if add_as_client(conn, company, r["member_afm"]):
            added.append(r["member_afm"])
    lookup = (rg_lookup or _rg_lookup(conn)) if check_business else None
    for r in rows:
        afm = r["member_afm"]
        if lookup is None or credentials.get(conn, afm):
            continue                                # με δικούς του κωδικούς: η κανονική ανάκτηση ξέρει περισσότερα
        res = lookup(afm)
        if not res.get("ok"):
            continue
        from .business_profiles.rgwspublic import business_state
        state = business_state(res)
        conn.execute("UPDATE businesses SET activity_state=CASE WHEN ?<>'' THEN ? ELSE activity_state END, "
                     "cease_date=?, start_date=CASE WHEN ?<>'' THEN ? ELSE start_date END, "
                     "trade_name=CASE WHEN ?<>'' THEN ? ELSE trade_name END, "
                     "name=CASE WHEN name='' OR name=afm THEN ? ELSE name END, updated_at=? WHERE afm=?",
                     (state, state, res.get("stop_date") or "", res.get("start_date") or "", res.get("start_date") or "",
                      res.get("trade_name") or "", res.get("trade_name") or "", res.get("name") or "", db.utcnow(), afm))
        if state == "active" and res.get("kads"):
            service.set_kads(conn, afm, res["kads"])
    return added


def _rg_lookup(conn: sqlite3.Connection) -> Optional[Callable[[str], dict[str, Any]]]:
    from . import settings_store
    from .business_profiles import rgwspublic
    user, password = settings_store.get(conn, "rg_user"), settings_store.get(conn, "rg_pass")
    if not (user and password):
        return None
    return lambda afm: rgwspublic.lookup(afm, user, password)


def parent_company(conn: sqlite3.Connection) -> dict[str, str]:
    """ΑΦΜ μέλους → ΑΦΜ εταιρείας στην οποία εμφανίζεται (λίστα πελατών). Μέλος σε πολλές: η πρώτη αλφαβητικά."""
    out: dict[str, str] = {}
    for r in conn.execute("SELECT m.member_afm, m.company_afm FROM company_members m JOIN businesses b ON "
                          "b.afm=m.member_afm JOIN businesses c ON c.afm=m.company_afm ORDER BY m.until<>'', m.company_afm"):
        if r["member_afm"] != r["company_afm"]:
            out.setdefault(r["member_afm"], r["company_afm"])
    return out


def member_roles(conn: sqlite3.Connection) -> dict[str, str]:
    """ΑΦΜ μέλους → ρόλος (όπως στο Μητρώο ΑΑΔΕ, π.χ. «ΜΕΛΟΣ · ΔΙΑΧΕΙΡΙΣΤΗΣ · ΝΟΜΙΜΟΣ ΕΚΠΡΟΣΩΠΟΣ»)."""
    out: dict[str, str] = {}
    for r in conn.execute("SELECT member_afm, role FROM company_members WHERE coalesce(role,'')<>'' AND until='' "
                          "ORDER BY company_afm"):
        out.setdefault(r["member_afm"], r["role"])
    return out


def short_role(role: str) -> str:
    """Σύντομη μορφή για τη στήλη «Είδος» της λίστας πελατών: ο σημαντικότερος ρόλος."""
    r = strip_accents(role or "").upper()
    if not r:
        return ""
    if "ΝΟΜΙΜ" in r and "ΕΚΠΡΟΣ" in r:
        return "Νόμιμος εκπρόσωπος"
    if "ΔΙΑΧΕΙΡΙΣΤ" in r:
        return "Διαχειριστής"
    if "ΜΕΤΟΧ" in r:
        return "Μέτοχος"
    if "ΕΤΑΙΡ" in r:
        return "Εταίρος"
    return "Μέλος"


def legal_representative(conn: sqlite3.Connection, company: str) -> Optional[dict[str, Any]]:
    """Ο νόμιμος εκπρόσωπος της εταιρείας από τα (ενεργά) μέλη του Μητρώου — αλλιώς ο διαχειριστής."""
    rows = [dict(r) for r in conn.execute("SELECT * FROM company_members WHERE company_afm=? AND until=''",
                                          (company,))]
    for wanted in ("Νόμιμος εκπρόσωπος", "Διαχειριστής"):
        hit = next((r for r in rows if short_role(r.get("role", "")) == wanted), None)
        if hit:
            return hit
    return None


def sync_aml_rep(conn: sqlite3.Connection, company: str) -> bool:
    """Δέουσα επιμέλεια ↔ πελάτες (ρητό αίτημα 2026-10-29): ο νόμιμος εκπρόσωπος του φακέλου συμπληρώνεται/ενημερώνεται
    από τα μέλη της ανάκτησης στοιχείων — και, αν ο εκπρόσωπος είναι ο ίδιος πελάτης, από τα στοιχεία του (email,
    τηλέφωνο, διεύθυνση). Ό,τι έγραψε ο χρήστης για ΑΛΛΟ πρόσωπο δεν αλλάζει· κενά πεδία μόνο συμπληρώνονται.
    True αν άλλαξε κάτι."""
    from .aml import store as aml_store
    rep_member = legal_representative(conn, company)
    if not rep_member:
        return False
    profile = aml_store.get_profile(conn, company)
    rep = dict(profile["rep"])
    if rep.get("afm") and rep["afm"] != rep_member["member_afm"]:
        return False                                    # ο χρήστης όρισε άλλον εκπρόσωπο — δεν τον πατάμε
    client = conn.execute("SELECT name, email, mobile, phone, address FROM businesses WHERE afm=?",
                          (rep_member["member_afm"],)).fetchone()
    found = {"afm": rep_member["member_afm"], "name": (client["name"] if client else "") or rep_member.get("name", ""),
             "role": short_role(rep_member.get("role", "")),
             "email": client["email"] if client else "", "phone": (client["mobile"] or client["phone"]) if client else "",
             "address": client["address"] if client else ""}
    changed = False
    for key, value in found.items():
        if value and (not rep.get(key) or key in ("afm", "name")) and rep.get(key) != value:
            rep[key] = value
            changed = True
    if changed:
        aml_store.save_profile(conn, company, pep_status=profile["pep_status"],
                               relationship_start=profile["relationship_start"], relationship_end=profile["relationship_end"],
                               purpose=profile["purpose"], kyc=profile["kyc"], notes=profile["notes"], rep=rep)
    return changed


def remove(conn: sqlite3.Connection, company: str, member_afm: str) -> None:
    conn.execute("DELETE FROM company_members WHERE company_afm=? AND member_afm=?", (company, member_afm))
    conn.execute("DELETE FROM company_member_history WHERE company_afm=? AND member_afm=? AND tax_year=?",
                 (company, member_afm, date.today().year))
    snapshot(conn, company)


def parse_afm_list(value: Any) -> list[str]:
    """«123456789, 987654321» / «123456789;987654321» / ένας ΑΦΜ ανά γραμμή → έγκυροι ΑΦΜ."""
    out = []
    for part in re.split(r"[,;/\n\s]+", str(value or "")):
        afm = normalize_afm(part)
        if afm and afm not in out:
            out.append(afm)
    return out


def from_import(conn: sqlite3.Connection, rows: Iterable[Any]) -> int:
    """Σύνδεση μελών από το Excel: `member_of` (ΑΦΜ εταιρειών στη γραμμή του μέλους) και `member_afms` (ΑΦΜ μελών
    στη γραμμή της εταιρείας). Μόνο για εταιρείες που είναι πελάτες."""
    names = {r.afm: r.name for r in rows}
    n = 0
    clients = {r["afm"] for r in conn.execute("SELECT afm FROM businesses")}
    for r in rows:
        pairs = [(c, r.afm) for c in getattr(r, "member_of", [])] + [(r.afm, m) for m in getattr(r, "member_afms", [])]
        for company, member in pairs:
            if company in clients and company != member:
                n += store(conn, company, [{"afm": member, "name": names.get(member, "")}], "excel")
                # και εγγραφή στους πελάτες (χωρίς δίκτυο εδώ — η ατομική επιχείρηση ελέγχεται με την ανανέωση στοιχείων)
                sync_member_clients(conn, company, check_business=False)
    return n


def legal_entity(conn: sqlite3.Connection, afm: str) -> Optional[bool]:
    from .obligations import is_legal_entity
    row = conn.execute("SELECT legal_form, activity_state FROM businesses WHERE afm=?", (afm,)).fetchone()
    if not row:
        return None
    return is_legal_entity(row["legal_form"] or "")
