"""Έλεγχος υποβολής δήλωσης INTRASTAT στην ΕΛ.ΣΤΑΤ (eurostat.statistics.gr) — port του runner
`recerse-engineer/runner/configs/intrastat-check.js` (εκεί με browser, ΖΩΝΤΑΝΑ επαληθευμένο) σε καθαρό HTTP.

Η εφαρμογή της ΕΛ.ΣΤΑΤ είναι παλιό ASP με μία συνάρτηση `post(φόρμα, στοιχείο, τιμή)`: βάζει την `τιμή` στο
στοιχείο Νο `στοιχείο` της φόρμας Νο `φόρμα` και την υποβάλλει (επαληθεύτηκε στον κώδικα της σελίδας εισόδου,
2026-10-27). Άρα κάθε «κλικ» = POST της φόρμας με όλα τα πεδία της, και ένα από αυτά αλλαγμένο — `_post`.

Ροή: login.aspx (io1 = email, io2 = κωδικός) → history.aspx («Αρχείο Δηλώσεων»: ο λογιστής βλέπει ΟΛΕΣ τις επιχειρήσεις
που διαχειρίζεται, link = το ΑΦΜ) → επιλογή έτους (select io2) → πίνακας «Πρωτόκολλο | * | Ροή | Γραμμές | Τιμ.Αξία |
Στατ.Αξία | Δημιουργία | Μεταβολή» — πρωτόκολλο `YYYYMM/αύξων`, άρα ο μήνας είναι μέσα στο πρωτόκολλο.

Κωδικοί: ΕΝΑΣ λογαριασμός του γραφείου (Ρυθμίσεις → `intrastat_user` / `intrastat_pass`), όχι ανά πελάτη.
ΖΩΝΤΑΝΑ ✓ (2026-10-29, λογαριασμός γραφείου): σύνδεση, αρχείο 2025/2026, δηλώσεις + βεβαιώσεις σε PDF. Η σελίδα
γράφει HTML χωρίς `</form>` και με href χωρίς εισαγωγικά — ο parser τα ανέχεται. Μόνο ανάγνωση, ποτέ υποβολή.
"""
from __future__ import annotations

import html as _html
import logging
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urljoin

from . import settings_store
from .business_profiles.lookup_aade import _HyperHttp  # noqa: PLC2701 — ίδιος http client με τις άλλες πύλες

log = logging.getLogger(__name__)

BASE = "https://eurostat.statistics.gr/"
FLOWS = {"arrivals": "Άφιξη", "dispatches": "Αποστολή"}
REASONS_EL = {"NoCredentials": "Δεν έχουν οριστεί οι κωδικοί Intrastat του γραφείου (Ρυθμίσεις).",
              "LoginFailed": "Η ΕΛ.ΣΤΑΤ απέρριψε τους κωδικούς Intrastat του γραφείου.",
              "NoLoginForm": "Δεν φόρτωσε η σελίδα εισόδου της ΕΛ.ΣΤΑΤ.",
              "AfmNotManaged": "Το ΑΦΜ δεν εμφανίζεται στο «Αρχείο Δηλώσεων» του λογαριασμού Intrastat του γραφείου.",
              "NoYearSelect": "Δεν φόρτωσε το αρχείο δηλώσεων της επιχείρησης (άλλαξε η σελίδα;).",
              "YearNotAvailable": "Το έτος δεν υπάρχει στο αρχείο δηλώσεων της επιχείρησης."}

_FORM_OPEN = re.compile(r"<form\b([^>]*)>", re.I)
_FIELD = re.compile(r"<(input|select|textarea|button)\b([^>]*)>(.*?</\1>)?", re.S | re.I)


def _attr(tag: str, name: str) -> str:
    m = re.search(r'\b' + name + r'\s*=\s*("([^"]*)"|\'([^\']*)\'|([^\s>]+))', tag, re.I)
    return _html.unescape(next(g for g in (m.group(2), m.group(3), m.group(4)) if g is not None)) if m else ""


def _text(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def forms(page: str) -> list[dict[str, Any]]:
    """Οι φόρμες της σελίδας με τα στοιχεία τους ΜΕ ΤΗ ΣΕΙΡΑ του `form.elements` (όπως τα αριθμεί το `post()`)."""
    out = []
    page = page or ""
    starts = list(_FORM_OPEN.finditer(page))
    for k, fm in enumerate(starts):
        # Το «Αρχείο Δηλώσεων» (history) ΔΕΝ κλείνει τη φόρμα (`</form>` λείπει — ζωντανά 2026-10-29)· ο browser την
        # κλείνει στο τέλος της σελίδας. Άρα: μέχρι το `</form>`, την επόμενη `<form` ή το τέλος.
        stop = starts[k + 1].start() if k + 1 < len(starts) else len(page)
        close = re.search(r"</form\s*>", page[fm.end():stop], re.I)
        body = page[fm.end():fm.end() + close.start()] if close else page[fm.end():stop]
        elements = []
        for f in _FIELD.finditer(body):
            kind, attrs, inner = f.group(1).lower(), f.group(2), f.group(3) or ""
            itype = _attr(attrs, "type").lower() if kind == "input" else kind
            value = _attr(attrs, "value")
            if kind == "select":
                opts = re.findall(r"<option\b([^>]*)>", inner, re.I)
                chosen = next((o for o in opts if re.search(r"\bselected\b", o, re.I)), opts[0] if opts else "")
                value = _attr(chosen, "value")
            elif kind == "textarea":
                value = _html.unescape(re.sub(r"</textarea>$", "", inner, flags=re.I))
            checked = bool(re.search(r"\bchecked\b", attrs, re.I))
            elements.append({"name": _attr(attrs, "name"), "type": itype, "value": value, "checked": checked})
        out.append({"action": _attr(fm.group(1), "action"), "elements": elements})
    return out


def _post(http: _HyperHttp, page_url: str, page: str, seq: int = 0, element: int = -1, value: Any = None,
          fields: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """Το `post(seq, element, value)` της σελίδας: τιμή στο στοιχείο `element` της φόρμας `seq` και υποβολή της."""
    fs = forms(page)
    if seq >= len(fs):
        raise RuntimeError("Η σελίδα της ΕΛ.ΣΤΑΤ δεν έχει την αναμενόμενη φόρμα.")
    form = fs[seq]
    data: dict[str, str] = {}
    for i, el in enumerate(form["elements"]):
        if not el["name"] or el["type"] in ("button", "submit", "reset", "image"):
            continue
        if el["type"] in ("checkbox", "radio") and not el["checked"]:
            continue
        data[el["name"]] = str(value) if i == element and element > -1 else el["value"]
    if element > -1 and element < len(form["elements"]) and form["elements"][element]["name"]:
        data[form["elements"][element]["name"]] = str(value)
    data.update(fields or {})
    return http.follow("POST", urljoin(page_url, form["action"] or page_url), data)


def _post_args(href: str) -> list[str]:
    m = re.search(r"post\(([^)]*)\)", href or "")
    return [a.strip().strip("'\"") for a in m.group(1).split(",")] if m else []


def login(http: _HyperHttp, user: str, password: str) -> dict[str, Any]:
    page = http.follow("GET", BASE + "login.aspx")
    if 'name="io1"' not in page["text"]:
        return {"ok": False, "reason": "NoLoginForm"}
    res = _post(http, page["url"], page["text"], 0, fields={"io1": user, "io2": password})
    if "login" in res["url"].lower() and re.search(r'name="io2"[^>]*type="password"|type="password"[^>]*name="io2"',
                                                    res["text"], re.I):
        return {"ok": False, "reason": "LoginFailed"}
    return {"ok": True}


def open_archive(http: _HyperHttp, afm: str, year: int) -> dict[str, Any]:
    """«Αρχείο Δηλώσεων» της επιχείρησης για το έτος → {ok, page, url} ή {ok: False, reason}."""
    hist = http.follow("GET", BASE + "history.aspx")
    link = next((_attr(a.group(1), "href") for a in re.finditer(r"<a\b([^>]*)>(.*?)</a>", hist["text"], re.S | re.I)
                 if _attr(a.group(1), "href").startswith("javascript:post") and _text(a.group(2)) == afm), "")
    args = _post_args(link)
    if len(args) < 3:
        return {"ok": False, "reason": "AfmNotManaged"}
    page = _post(http, hist["url"], hist["text"], int(args[0]), int(args[1]), args[2])
    fs = forms(page["text"])
    sel = next(((i, j) for i, f in enumerate(fs) for j, e in enumerate(f["elements"])
                if e["name"] == "io2" and e["type"] == "select"), None)
    if sel is None:
        return {"ok": False, "reason": "NoYearSelect"}
    if not re.search(r'<option[^>]*value="?' + str(year) + r'\b', page["text"]):
        return {"ok": False, "reason": "YearNotAvailable"}
    page = _post(http, page["url"], page["text"], sel[0], sel[1], str(year))
    return {"ok": True, "page": page["text"], "url": page["url"]}


def parse_archive(page: str) -> list[dict[str, str]]:
    """Γραμμές του αρχείου: πρώτο κελί = πρωτόκολλο `YYYYMM/αύξων` (όπως ο runner, θεσιακά — 8 στήλες)."""
    if re.search(r"Δεν βρέθηκαν Δηλώσεις", page or "", re.I):
        return []
    rows = []
    for tr in re.findall(r"<tr\b.*?</tr>", page or "", re.S | re.I):
        cells = [_text(c) for c in re.findall(r"<td\b[^>]*>(.*?)</td>", tr, re.S | re.I)]
        if len(cells) < 3 or not re.match(r"^\d{6}/", cells[0].replace(" ", "")):
            continue
        # href ΧΩΡΙΣ εισαγωγικά στη σελίδα της ΕΛ.ΣΤΑΤ (`href=javascript:post(1,0,20260911912)` — ζωντανά)
        link = re.search(r"""href=["']?(javascript:post\(\s*1\s*,[^"'\s>]*)""", tr, re.I)
        rows.append({"protocol": cells[0].replace(" ", ""), "type": cells[1], "flow": cells[2],
                     "lines": cells[3] if len(cells) > 3 else "", "invoice_value": cells[4] if len(cells) > 4 else "",
                     "stat_value": cells[5] if len(cells) > 5 else "", "created": cells[6] if len(cells) > 6 else "",
                     "modified": cells[7] if len(cells) > 7 else "", "link": _html.unescape(link.group(1)) if link else ""})
    return rows


def _flow_of(text: str) -> str:
    t = (text or "").lower()
    return "arrivals" if "άφιξ" in t or "αφιξ" in t else ("dispatches" if "αποστολ" in t else "")


def entry(rows: list[dict[str, str]], year: int, liable: dict[str, set[str]], today: Optional[date] = None
          ) -> dict[str, Any]:
    """Εγγραφή `obligation_checks` «INTRASTAT»: κάθε μήνας με δήλωση = υποβλήθηκε (ροές, πρωτόκολλα)· μήνας που ΠΡΕΠΕΙ
    (`liable`: 'YYYY-MM' → ροές, από τα κατώφλια/myDATA) και έχει λήξει (26η του επόμενου, μαζί με το VIES) χωρίς τη
    ροή = δεν υποβλήθηκε."""
    from .obligations import _due_vies
    today = today or date.today()
    by_month: dict[str, dict[str, list[str]]] = {}
    for r in rows:
        ym = f"{r['protocol'][:4]}-{r['protocol'][4:6]}"
        if not ym.startswith(str(year)):
            continue
        by_month.setdefault(ym, {}).setdefault(_flow_of(r["flow"]) or "other", []).append(r["protocol"])
    periods = []
    for m in range(1, 13):
        ym = f"{year}-{m:02d}"
        y2, m2 = (year + 1, 1) if m == 12 else (year, m + 1)
        due = _due_vies(y2, m2)
        found, need = by_month.get(ym, {}), liable.get(ym, set())
        missing = [f for f in need if f not in found]
        if found and not missing:
            detail = "; ".join(f"{FLOWS.get(k, k)}: {', '.join(v)}" for k, v in found.items())
            periods.append({"period": ym, "status": "submitted", "detail": f"Υποβλήθηκε — {detail}",
                            "period_end": f"{ym}-28", "period_type": "month"})
        elif missing and due < today:
            periods.append({"period": ym, "status": "not_submitted",
                            "detail": "Δεν βρέθηκε δήλωση " + " / ".join(FLOWS[f] for f in missing)
                                      + " στο αρχείο της ΕΛ.ΣΤΑΤ (υπέρβαση κατωφλίου στο myDATA)",
                            "period_end": f"{ym}-28", "period_type": "month"})
        elif missing:
            periods.append({"period": ym, "status": "pending", "detail": f"Προθεσμία {due.strftime('%d/%m/%Y')}",
                            "period_end": f"{ym}-28", "period_type": "month"})
    status = ("not_submitted" if any(p["status"] == "not_submitted" for p in periods)
              else "submitted" if periods else "not_obligated")
    return {"obligation": "INTRASTAT", "label": "Δήλωση Intrastat (ΕΛ.ΣΤΑΤ)", "status": status, "periods": periods,
            "detail": "" if periods else "Καμία δήλωση Intrastat στο αρχείο της ΕΛ.ΣΤΑΤ για τη χρήση."}


def liable_months(conn: sqlite3.Connection, afm: str, year: int) -> dict[str, set[str]]:
    """'YYYY-MM' → ροές που ξεπερνούν το κατώφλι (mydata.analysis.intrastat) — μόνο για μήνες που έχουν κατέβει."""
    from .mydata import analysis
    from .mydata.store import synced_months
    synced = synced_months(conn, afm).get(afm, set())
    cache: dict[str, dict[str, float]] = {}
    out: dict[str, set[str]] = {}
    for m in range(1, 13):
        ym = f"{year}-{m:02d}"
        if ym not in synced:
            continue
        res = analysis.intrastat(conn, afm, ym, synced, cache)
        flows = {f for f in FLOWS if res.get(f)}
        if flows:
            out[ym] = flows
    return out


def wanted(conn: sqlite3.Connection, afm: str, year: int) -> bool:
    """Αφορά τον πελάτη: σημαία Intrastat (Excel/Προφίλ), υπέρβαση κατωφλίου στο myDATA ή προηγούμενος έλεγχος."""
    b = conn.execute("SELECT intrastat FROM businesses WHERE afm=?", (afm,)).fetchone()
    if b and b["intrastat"] == 0:
        return False
    if b and b["intrastat"]:
        return True
    if conn.execute("SELECT 1 FROM obligation_checks WHERE afm=? AND obligation='INTRASTAT' AND status IN "
                    "('submitted','not_submitted')", (afm,)).fetchone():
        return True
    return bool(liable_months(conn, afm, year))


def credentials(conn: sqlite3.Connection) -> Optional[tuple[str, str]]:
    user, password = settings_store.get(conn, "intrastat_user"), settings_store.get(conn, "intrastat_pass")
    return (user, password) if user and password else None


def save_pdfs(http: _HyperHttp, archive: dict[str, Any], afm: str, rows: list[dict[str, str]], folder: Path) -> list[str]:
    """PDF ανά υποβληθείσα δήλωση: η «Προβολή δήλωσης» και η «Βεβαίωση παραλαβής» (σελίδες HTML της ΕΛ.ΣΤΑΤ → PDF),
    όπως ο runner. Επιστρέφει τα αρχεία που γράφτηκαν."""
    from .aml.documents import write_pdf
    from .reports import ensure_qt
    ensure_qt()
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    page, url = archive["page"], archive["url"]
    for r in rows:
        args = _post_args(r.get("link", ""))
        if len(args) < 3:
            continue
        base = f"INTRASTAT_{afm}_{r['protocol'].replace('/', '_')}_{'AFIKSI' if _flow_of(r['flow']) == 'arrivals' else 'APOSTOLI'}"
        if (folder / f"{base}_ΔΗΛΩΣΗ.pdf").exists():
            continue                                    # μία φορά ανά δήλωση (όπως τα PDF της ΑΑΔΕ)
        try:
            view = _post(http, url, page, int(args[0]), int(args[1]), args[2])
            write_pdf(view["text"], folder / f"{base}_ΔΗΛΩΣΗ.pdf", f"Intrastat {r['protocol']}")
            saved.append(str(folder / f"{base}_ΔΗΛΩΣΗ.pdf"))
            cert = next((_attr(a.group(1), "href") for a in re.finditer(r"<a\b([^>]*)>(.*?)</a>", view["text"], re.S | re.I)
                         if _text(a.group(2)) == "Παραλαβή Βεβαίωσης"), "")
            cargs = _post_args(cert)
            if len(cargs) >= 3:
                bev = _post(http, view["url"], view["text"], int(cargs[0]), int(cargs[1]), cargs[2])
                write_pdf(bev["text"], folder / f"{base}_ΒΕΒΑΙΩΣΗ.pdf", f"Βεβαίωση Intrastat {r['protocol']}")
                saved.append(str(folder / f"{base}_ΒΕΒΑΙΩΣΗ.pdf"))
        except Exception as exc:                        # noqa: BLE001 — το PDF είναι επιπλέον
            log.warning("intrastat pdf %s %s: %s", afm, r["protocol"], exc)
    return saved


def attach_pdfs(e: dict[str, Any], afm: str, folder: Path) -> None:
    """Τα PDF (δήλωση + βεβαίωση) που ήδη υπάρχουν στον φάκελο → `amounts` της περιόδου τους, ώστε να ανοίγουν από
    την υποχρέωση (στήλη «PDF»). Ό,τι κατέβηκε σε προηγούμενο έλεγχο μένει συνδεδεμένο και χωρίς νέα λήψη."""
    if not folder.exists():
        return
    for p in e["periods"]:
        files = sorted(str(f) for f in folder.glob(f"INTRASTAT_{afm}_{p['period'].replace('-', '')}_*.pdf"))
        if files:
            decl = [f for f in files if f.endswith("_ΔΗΛΩΣΗ.pdf")]
            p["amounts"] = {**(p.get("amounts") or {}), "submission_pdf": (decl or files)[0], "submission_pdfs": files}


def check_entries(conn: sqlite3.Connection, afm: str, year: int, want_pdf: bool = False,
                  http_factory: Callable[[], _HyperHttp] = _HyperHttp) -> list[dict[str, Any]]:
    """Έλεγχος υποβολής Intrastat της χρήσης → εγγραφές `obligation_checks` (σφάλμα = status 'error', όχι εξαίρεση)."""
    creds = credentials(conn)
    if not creds or not wanted(conn, afm, year):
        return []
    try:
        http = http_factory()
        lg = login(http, *creds)
        if not lg["ok"]:
            raise RuntimeError(REASONS_EL.get(lg["reason"], lg["reason"]))
        archive = open_archive(http, afm, year)
        if not archive["ok"]:
            if archive["reason"] == "YearNotAvailable":
                return [entry([], year, liable_months(conn, afm, year))]
            raise RuntimeError(REASONS_EL.get(archive["reason"], archive["reason"]))
        rows = parse_archive(archive["page"])
        e = entry(rows, year, liable_months(conn, afm, year))
        from . import config
        folder = config.data_dir() / "submissions" / afm / str(year)
        if want_pdf and rows:
            save_pdfs(http, archive, afm, [r for r in rows if r["protocol"].startswith(str(year))], folder)
        attach_pdfs(e, afm, folder)
        return [e]
    except Exception as exc:                            # noqa: BLE001 — δίκτυο / αλλαγή σελίδας
        log.warning("intrastat %s/%s: %s", afm, year, exc)
        return [{"obligation": "INTRASTAT", "label": "Δήλωση Intrastat (ΕΛ.ΣΤΑΤ)", "status": "error", "periods": [],
                 "detail": f"Ο έλεγχος δεν ολοκληρώθηκε ({str(exc)[:160]})."}]
