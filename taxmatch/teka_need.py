"""Ανάγκη υποβολής ΑΠΔ ΤΕΚΑ ανά μήνα από την ΕΡΓΑΝΗ ΙΙ — port του TaxSystem `efka-teka-apd-need.js` + `lib/ergani-http.js`
(2026-11-18, ρητό αίτημα «νέα διαδικασία ελέγχου υπαγωγής στην ΑΠΔ ΤΕΚΑ για καλύτερη δημιουργία υποχρεώσεων»).

Ερώτημα: τον μήνα M απασχολείται (έστω για μέρος του μήνα) εργαζόμενος που υπάγεται στο ΤΕΚΑ; Αν ναι, οφείλεται ΑΠΔ
ΤΕΚΑ για τον M (προθεσμία μαζί με την ΑΠΔ e-ΕΦΚΑ).

ΕΡΓΑΝΗ ΙΙ (eservices.yeka.gr), ΜΟΝΟ αναζητήσεις/εξαγωγές — καμία «Εισαγωγή», καμία ενέργεια στην καρτέλα εργαζομένου:
* σύνδεση ASP.NET `login.aspx` με τους κωδικούς ΟΠΣ e-ΕΦΚΑ (ΙΚΑ) του εργοδότη (εργοδότης ΜΕ ΑΜΕ ΔΕΝ μπαίνει με TAXISnet)·
* «Στοιχεία Προσωπικού» (`Mitroa/ErgazomenosSearch.aspx`, εξαγωγή Excel): ΑΦΜ, ονοματεπώνυμο, Ημ/νία Από / Έως·
* «Αναγγελίες» (`Anaggelies/AnaggeliesNewSearch.aspx`): αιτία λήξης (αποχώρηση, απόλυση, λήξη ορισμένου χρόνου …)·
* καρτέλα εργαζομένου (GET `Mitroa/Ergazomenos.aspx?ergodotiId=…&afm=…`): ΑΜΚΑ, ημ/νία γέννησης και «Φορείς
  Επικουρικής Ασφάλισης» — «(ΤΕΚΑ)» = ο εργοδότης τον έχει δηλώσει στο ΤΕΚΑ.

Υπαγωγή ανά εργαζόμενο (προτεραιότητα του runner, χωρίς τις πηγές που δεν μεταφέρθηκαν): καρτέλα ΕΡΓΑΝΗ (ΤΕΚΑ ή όχι),
και κανόνας «απασχόληση στον ίδιο εργοδότη ΠΡΙΝ το 2022 ⇒ ασφαλισμένος πριν το 2022 ⇒ όχι ΤΕΚΑ» (ΤΕΚΑ = πρώτη ασφάλιση από
1/1/2022). Αποτέλεσμα μήνα: REQUIRED (≥1 ενεργός υπαγόμενος) · POSSIBLY_REQUIRED (κανείς, αλλά ≥1 άγνωστος) ·
NOT_REQUIRED · NO_EMPLOYEES. ΔΕΝ μεταφέρθηκαν: η «Υπηρεσία Επιβεβαίωσης Υπαγωγής ΤΕΚΑ» (eCheckTEKA — ο runner τη βρήκε
να απορρίπτει αιτήματα ανάλογα με την προέλευση) και η ανάγνωση των αναλυτικών ΑΠΔ ανά εργαζόμενο.
"""
from __future__ import annotations

import html as _html
import io
import json
import logging
import re
import sqlite3
from calendar import monthrange
from datetime import date
from typing import Any, Optional
from urllib.parse import quote, urljoin

import requests

from . import db

log = logging.getLogger(__name__)

HOST = "https://eservices.yeka.gr/"
C = "ctl00$ctl00$ContentHolder$ContentHolder$"
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
STATUS_LABEL = {"REQUIRED": "Απαιτείται ΑΠΔ ΤΕΚΑ", "POSSIBLY_REQUIRED": "Πιθανώς απαιτείται ΑΠΔ ΤΕΚΑ (άγνωστη υπαγωγή)",
                "NOT_REQUIRED": "Δεν απαιτείται ΑΠΔ ΤΕΚΑ", "NO_EMPLOYEES": "Κανείς ενεργός εργαζόμενος"}


class ErganiError(Exception):
    pass


def _text(s: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", s or "")).replace("\xa0", " ").split())


def form_fields(page: str) -> dict[str, str]:
    """Τιμές φόρμας ASP.NET όπως τις στέλνει ο browser (χωρίς κουμπιά / ατσεκάριστα checkbox)."""
    out: dict[str, str] = {}
    for m in re.finditer(r"<input\b[^>]*>", page, re.I):
        tag = m.group(0)
        name = (re.search(r'name="([^"]+)"', tag) or [None, None])[1]
        if not name:
            continue
        typ = ((re.search(r'type="([^"]+)"', tag, re.I) or [None, ""])[1]).lower()
        if typ in ("submit", "button", "image", "file"):
            continue
        if typ in ("checkbox", "radio") and "checked" not in tag.lower():
            continue
        value = (re.search(r'value="([^"]*)"', tag) or [None, None])[1]
        out[name] = _html.unescape(value) if value is not None else ("on" if typ == "checkbox" else "")
    for m in re.finditer(r'<select\b[^>]*name="([^"]+)"[^>]*>(.*?)</select>', page, re.S | re.I):
        sel = re.search(r'<option[^>]*selected[^>]*value="([^"]*)"', m.group(2), re.I)
        out[m.group(1)] = sel.group(1) if sel else ""
    return out


def dmy(s: Any) -> Optional[str]:
    """'d/m/yyyy' (ή ημερομηνία Excel) → 'yyyy-mm-dd'· κενό → None."""
    if hasattr(s, "strftime"):
        return s.strftime("%Y-%m-%d")
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", str(s or ""))
    return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}" if m else None


def xlsx_rows(content: bytes) -> list[dict[str, Any]]:
    """Εξαγωγή Excel του grid → [{επικεφαλίδα: τιμή}] (πρώτη μη κενή γραμμή = επικεφαλίδες)."""
    import warnings
    import openpyxl
    # ΟΧΙ read_only: η εξαγωγή δηλώνει <dimension ref="A1"/> και το read_only το εμπιστεύεται (έβγαζε 0 γραμμές)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                 # «Workbook contains no default style»
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    rows = [list(r) for r in wb.worksheets[0].iter_rows(values_only=True)]
    rows = [r for r in rows if any(v not in (None, "") for v in r)]
    if not rows:
        return []
    head = [str(h or "").strip() for h in rows[0]]
    return [{head[i]: (v if v is not None else "") for i, v in enumerate(r) if i < len(head)} for r in rows[1:]]


class Ergani:
    """ΕΡΓΑΝΗ ΙΙ — σύνδεση και ΜΟΝΟ αναζητήσεις. Τα cookies μένουν στη μνήμη του session."""

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self.s = session or requests.Session()
        self.s.headers.setdefault("User-Agent", _UA)
        self.base = HOST
        self.ergodoti_id: Optional[str] = None

    def url(self, path: str) -> str:
        return urljoin(self.base, path)

    def login(self, user: str, password: str) -> dict[str, Any]:
        r = self.s.get(HOST + "login.aspx", timeout=30)
        p = C + "SiteLogin$"
        r = self.s.post(r.url, data={**form_fields(r.text), p + "UserName": user, p + "Password": password,
                                     p + "Login": "Είσοδος"}, timeout=30)
        if re.search(r"login\.aspx", r.url, re.I) or "SiteLogin$Password" in r.text:
            alert = re.search(r'alert\("([^"]{5,400})"\)', r.text)
            return {"ok": False, "reason": "ErganiLoginFailed", "message": alert.group(1) if alert else ""}
        self.base = r.url.rsplit("/", 1)[0] + "/"       # κρατάμε το /(S(<session>))/
        return {"ok": True}

    def search(self, path: str, control: str, criteria: Optional[dict[str, str]] = None,
               excel: bool = True) -> tuple[str, Optional[list[dict[str, Any]]]]:
        x = self.s.get(self.url(path), timeout=60)
        f = form_fields(x.text)
        for k, v in (criteria or {}).items():
            f[C + control + "$" + k] = v
        x = self.s.post(x.url, data={**f, C + control + "$SearchControlSearchButton": "Αναζήτηση"}, timeout=90)
        grid = re.search(r"__doPostBack\(&#39;([^&]*\$Grid\$Grid)&#39;,&#39;ExcelExport\$1&#39;\)", x.text)
        if not excel or not grid:
            return x.text, (None if grid else [])
        res = self.s.post(x.url, data={**form_fields(x.text), "__EVENTTARGET": grid.group(1),
                                       "__EVENTARGUMENT": "ExcelExport$1"}, headers={"Referer": x.url}, timeout=120)
        if res.status_code != 200 or not res.content.startswith(b"PK"):
            raise ErganiError(f"ΕΡΓΑΝΗ: η εξαγωγή Excel απέτυχε ({res.status_code})")
        return x.text, xlsx_rows(res.content)

    def personnel(self) -> list[dict[str, Any]]:
        page, rows = self.search("Mitroa/ErgazomenosSearch.aspx", "ErgazomenosSearchControl")
        m = re.search(r"Select\(\d+,\s*&#39;(\d+)\|", page)
        self.ergodoti_id = m.group(1) if m else None
        return [{"afm": str(r.get("ΑΦΜ") or "").strip(), "name": r.get("Όνομα") or "", "surname": r.get("Επώνυμο") or "",
                 "branch": r.get("ΑΑ Παραρτηματος") or "", "from": dmy(r.get("Ημ/νία Από")), "to": dmy(r.get("Ημ/νία Έως"))}
                for r in rows or []]

    def declarations(self) -> list[dict[str, Any]]:
        _page, rows = self.search("Anaggelies/AnaggeliesNewSearch.aspx", "AnaggeliesNewSearchControl")
        return [{"status": r.get("") or r.get("Κατάσταση") or "", "type": r.get("Τύπος Αναγγελίας") or "",
                 "afm": str(r.get("ΑΦΜ Εργαζόμενου") or "").strip(), "submitted": dmy(r.get("Ημ/νία Υποβολής")),
                 "protocol": r.get("Αριθμός Πρωτοκόλλου") or ""} for r in rows or []]

    def employee_card(self, afm: str) -> Optional[dict[str, Any]]:
        """Καρτέλα εργαζομένου — ΜΟΝΟ GET (η σελίδα έχει κουμπιά ενεργειών· ποτέ POST εκεί)."""
        if not self.ergodoti_id:
            raise ErganiError("ΕΡΓΑΝΗ: άγνωστο ergodotiId")
        r = self.s.get(self.url(f"Mitroa/Ergazomenos.aspx?ergodotiId={quote(self.ergodoti_id)}&afm={quote(afm)}"),
                       timeout=60)
        fields: dict[str, str] = {}
        for m in re.finditer(r"<label[^>]*>((?:(?!</label>).)*?)</label>\s*<span[^>]*>(.*?)</span>", r.text, re.S):
            k, v = _text(m.group(1)), _text(m.group(2))
            if k and k not in fields:
                fields[k] = v
        if fields.get("ΑΦΜ") != afm:
            return None
        aux = fields.get("Φορείς Επικουρικής Ασφάλισης", "")
        return {"amka": fields.get("ΑΜΚΑ", ""), "birth": dmy(fields.get("Ημ/νία Γέννησης")),
                "relation": fields.get("Σχέση Απασχόλησης", ""), "auxiliary": aux,
                "teka": bool(re.search(r"\(ΤΕΚΑ\)|ΚΕΦΑΛΑΙΟΠΟΙΗΤΙΚΗΣ", aux, re.I))}

    def logout(self) -> None:
        try:
            h = self.s.get(self.url("Default.aspx"), timeout=30)
            self.s.post(h.url, data={**form_fields(h.text), "__EVENTTARGET": "ctl00$ctl00$ContentHolder$Logout$ctl00",
                                     "__EVENTARGUMENT": ""}, timeout=30)
        except requests.RequestException:
            pass


def end_kind(t: str) -> str:
    """Αιτία λήξης από τον τύπο αναγγελίας ΕΡΓΑΝΗ."""
    if re.search("Καταγγελία", t, re.I):
        return "ΣΥΝΤΑΞΙΟΔΟΤΗΣΗ (καταγγελία)" if re.search("Συνταξιοδότηση", t, re.I) else "ΑΠΟΛΥΣΗ (καταγγελία σύμβασης)"
    for pat, label in (("Συνταξιοδότηση", "ΣΥΝΤΑΞΙΟΔΟΤΗΣΗ"), ("Οικειοθελής", "ΟΙΚΕΙΟΘΕΛΗΣ ΑΠΟΧΩΡΗΣΗ"),
                       ("Ορισμένου Χρόνου", "ΛΗΞΗ ΣΥΜΒΑΣΗΣ ΟΡΙΣΜΕΝΟΥ ΧΡΟΝΟΥ"), ("Δοκιμαστικής", "ΛΥΣΗ ΔΟΚΙΜΑΣΤΙΚΗΣ ΠΕΡΙΟΔΟΥ"),
                       ("Εθελούσια", "ΕΘΕΛΟΥΣΙΑ ΕΞΟΔΟΣ"), ("Θανάτου", "ΘΑΝΑΤΟΣ"), ("Μεταβίβαση|Δανεισμ", "ΜΕΤΑΒΙΒΑΣΗ/ΔΑΝΕΙΣΜΟΣ")):
        if re.search(pat, t, re.I):
            return label
    return re.sub(r"^ΨΗΦΙΑΚΗ ΑΝΑΓΓΕΛΙΑ ΛΗΞΗΣ ΕΡΓΑΣΙΑΣ\s*-\s*", "", t, flags=re.I) or "ΛΗΞΗ"


def assess(ym: str, personnel: list[dict[str, Any]], declarations: list[dict[str, Any]],
           cards: dict[str, Optional[dict[str, Any]]]) -> dict[str, Any]:
    """Καθαρή λογική (χωρίς δίκτυο): ποιοι απασχολούνται τον μήνα `ym` ('YYYY-MM'), κινήσεις, υπαγωγή ΤΕΚΑ, αποτέλεσμα."""
    y, m = int(ym[:4]), int(ym[5:7])
    first, last = f"{ym}-01", f"{ym}-{monthrange(y, m)[1]:02d}"
    emps: dict[str, dict[str, Any]] = {}
    for p in personnel:
        if p.get("afm") and p.get("from") and p["from"] <= last and (not p.get("to") or p["to"] >= first):
            e = emps.setdefault(p["afm"], {"afm": p["afm"], "surname": p.get("surname", ""), "name": p.get("name", ""),
                                           "periods": []})
            e["periods"].append({"from": p["from"], "to": p.get("to")})
    ends = [d for d in declarations if re.search("ΛΗΞΗΣ", d.get("type", ""), re.I)
            and not re.search("ανακληθεί|Cancelled", d.get("status", ""), re.I)]
    movements = []
    for e in emps.values():
        hired = next((x["from"] for x in e["periods"] if x["from"] >= first), None)
        if hired:
            movements.append({"type": "ΠΡΟΣΛΗΨΗ", "afm": e["afm"], "surname": e["surname"], "date": hired})
        endp = next((x for x in e["periods"] if x.get("to") and x["to"] <= last), None)
        if endp:
            d = sorted((x for x in ends if x["afm"] == e["afm"] and (x.get("submitted") or "") >= endp["from"]),
                       key=lambda x: x.get("submitted") or "", reverse=True)
            kind = end_kind(d[0]["type"]) if d else "ΛΗΞΗ (χωρίς αναγγελία στην ΕΡΓΑΝΗ ΙΙ)"
            movements.append({"type": kind, "afm": e["afm"], "surname": e["surname"], "date": endp["to"]})
        card = cards.get(e["afm"]) or {}
        e.update({k: card.get(k) for k in ("amka", "birth", "auxiliary") if card.get(k)})
        evidence = []
        if card and (card.get("auxiliary") or card.get("teka")):   # κενός επικουρικός = καμία ένδειξη (όπως ο runner)
            evidence.append({"source": f"ΕΡΓΑΝΗ (επικουρικός: {card.get('auxiliary') or '—'})", "teka": bool(card.get("teka"))})
        early = sorted(p["from"] for p in personnel if p.get("afm") == e["afm"] and p.get("from")
                       and p["from"] < "2022-01-01")
        if early:
            evidence.append({"source": f"ΕΡΓΑΝΗ: απασχόληση από {early[0]} (ασφάλιση πριν το 2022)", "teka": False})
        top = evidence[0] if evidence else None
        e["teka"] = ("SUBJECT" if top["teka"] else "NOT_SUBJECT") if top else "UNKNOWN"
        e["teka_source"] = top["source"] if top else ""
        if len({x["teka"] for x in evidence}) > 1:
            e["conflict"] = " · ".join(f"{x['source']}: {'ΤΕΚΑ' if x['teka'] else 'όχι ΤΕΚΑ'}" for x in evidence)
        if e["teka"] == "UNKNOWN" and e.get("birth"):
            e["hint"] = ("πιθανή υπαγωγή (πρώτη ασφάλιση πιθανόν από 2022)" if 2022 - int(e["birth"][:4]) <= 22
                         else "μάλλον όχι")
    rows = sorted(emps.values(), key=lambda e: (e["surname"], e["name"]))
    subj = [e for e in rows if e["teka"] == "SUBJECT"]
    unk = [e for e in rows if e["teka"] == "UNKNOWN"]
    # Απόλυση εργαζομένου του ΤΕΚΑ τον ΠΡΟΗΓΟΥΜΕΝΟ μήνα: η αποζημίωση δηλώνεται συχνά στην ΑΠΔ του επόμενου (ζωντανά: απόλυση
    # 28/04, αναγγελία 04/05, ΑΠΔ ΤΕΚΑ Μαΐου υποβλήθηκε ενώ κανείς ενεργός του Μαΐου δεν ήταν στο ΤΕΚΑ) — «πιθανώς».
    prev_first = f"{y - 1}-12-01" if m == 1 else f"{y}-{m - 1:02d}-01"
    severance = []
    for p in personnel:
        card = cards.get(p.get("afm") or "") or {}
        if p.get("afm") in emps or not card.get("teka") or not p.get("to") or not prev_first <= p["to"] < first:
            continue
        d = sorted((x for x in ends if x["afm"] == p["afm"] and (x.get("submitted") or "") >= p.get("from", "")),
                   key=lambda x: x.get("submitted") or "", reverse=True)
        if d and end_kind(d[0]["type"]).startswith("ΑΠΟΛΥΣΗ"):
            severance.append({"afm": p["afm"], "surname": p.get("surname", ""), "date": p["to"]})
    status = ("REQUIRED" if subj else "POSSIBLY_REQUIRED" if unk or severance
              else "NO_EMPLOYEES" if not rows else "NOT_REQUIRED")
    return {"month": ym, "status": status, "employees": rows, "movements": movements, "severance": severance,
            "teka_count": len(subj), "unknown_count": len(unk), "conflicts": sum(1 for e in rows if e.get("conflict"))}


def _credentials(conn: sqlite3.Connection, afm: str) -> list[tuple[str, str]]:
    """Κωδικοί ΟΠΣ e-ΕΦΚΑ (ΙΚΑ) του εργοδότη και, χωριστά, όσων ΑΜΕ έχουν δικούς τους — η ΕΡΓΑΝΗ δεν δέχεται TAXISnet."""
    from . import crypto, efka
    out: list[tuple[str, str]] = []
    creds = efka.ika_credentials(conn, afm)
    if creds:
        out.append(creds)
    for r in conn.execute("SELECT ika_user, ika_pass FROM efka_ames WHERE afm=? AND ika_user<>'' AND ika_pass<>''", (afm,)):
        c = (crypto.dec(r["ika_user"]), crypto.dec(r["ika_pass"]))
        if c not in out:
            out.append(c)
    return out


def has_credentials(conn: sqlite3.Connection, afm: str) -> bool:
    return bool(_credentials(conn, afm))


def months_to_check(today: Optional[date] = None) -> list[str]:
    """Τρέχων και προηγούμενος μήνας (η ΑΠΔ του προηγούμενου λήγει στο τέλος του τρέχοντος)."""
    t = today or date.today()
    prev = date(t.year - 1, 12, 1) if t.month == 1 else date(t.year, t.month - 1, 1)
    return [f"{prev.year}-{prev.month:02d}", f"{t.year}-{t.month:02d}"]


def check_client(conn: sqlite3.Connection, afm: str, months: Optional[list[str]] = None,
                 session_factory=requests.Session) -> dict[str, Any]:
    """Σύνδεση στην ΕΡΓΑΝΗ με κάθε σετ κωδικών ΙΚΑ του πελάτη, εκτίμηση ανά μήνα και αποθήκευση (`teka_need`).
    Αν κάποιος μήνας βγει REQUIRED, ανάβει το «ΑΠΔ ΤΕΚΑ» στο Προφίλ (για να γίνεται και ο έλεγχος υποβολής)."""
    months = months or months_to_check()
    creds = _credentials(conn, afm)
    if not creds:
        return {"ok": False, "reason": "Χωρίς κωδικούς ΟΠΣ e-ΕΦΚΑ (ΙΚΑ) εργοδότη — η ΕΡΓΑΝΗ δεν δέχεται TAXISnet για εργοδότη με ΑΜΕ."}
    personnel: list[dict[str, Any]] = []
    declarations: list[dict[str, Any]] = []
    cards: dict[str, Optional[dict[str, Any]]] = {}
    reason, logged = "", False
    for user, password in creds:
        e = Ergani(session_factory())
        try:
            login = e.login(user, password)
            if not login["ok"]:
                reason = login.get("message") or "Λάθος κωδικοί ΟΠΣ e-ΕΦΚΑ (ΙΚΑ) στην ΕΡΓΑΝΗ."
                continue
            logged = True
            pers = e.personnel()
            personnel += pers
            declarations += e.declarations()
            last = max(months)
            y0, m0 = int(min(months)[:4]), int(min(months)[5:7])
            # και όσοι έφυγαν τον μήνα ΠΡΙΝ από τον πρώτο ελεγχόμενο (αποζημίωση απόλυσης — βλ. `assess`)
            first = f"{y0 - 1}-12-01" if m0 == 1 else f"{y0}-{m0 - 1:02d}-01"
            for p in pers:
                if p["afm"] and p["afm"] not in cards and p.get("from") and p["from"] <= f"{last}-31" \
                        and (not p.get("to") or p["to"] >= first):
                    try:
                        cards[p["afm"]] = e.employee_card(p["afm"])
                    except (requests.RequestException, ErganiError) as exc:
                        log.info("ΕΡΓΑΝΗ καρτέλα %s: %s", afm, exc)
                        cards[p["afm"]] = None
        except (requests.RequestException, ErganiError) as exc:
            reason = f"ΕΡΓΑΝΗ: {exc}"
        finally:
            e.logout()
    if not logged:
        return {"ok": False, "reason": reason or "Αποτυχία σύνδεσης στην ΕΡΓΑΝΗ."}
    out = {"ok": True, "months": {}}
    now = db.utcnow()
    for ym in months:
        rep = assess(ym, personnel, declarations, cards)
        out["months"][ym] = rep
        conn.execute("INSERT INTO teka_need(afm, ym, status, result_json, checked_at) VALUES (?,?,?,?,?) "
                     "ON CONFLICT(afm, ym) DO UPDATE SET status=excluded.status, result_json=excluded.result_json, "
                     "checked_at=excluded.checked_at",
                     (afm, ym, rep["status"], json.dumps(rep, ensure_ascii=False), now))
    if any(r["status"] == "REQUIRED" for r in out["months"].values()):
        conn.execute("UPDATE businesses SET teka=1, employer=1 WHERE afm=? AND coalesce(teka,0)=0", (afm,))
    return out


def stored(conn: sqlite3.Connection, afm: Optional[str] = None) -> dict[str, dict[str, str]]:
    """{ΑΦΜ: {ΥΥΥΥ-ΜΜ: status}} από τους αποθηκευμένους ελέγχους."""
    out: dict[str, dict[str, str]] = {}
    sql, args = "SELECT afm, ym, status FROM teka_need", ()
    if afm:
        sql, args = sql + " WHERE afm=?", (afm,)
    for r in conn.execute(sql, args):
        out.setdefault(r["afm"], {})[r["ym"]] = r["status"]
    return out
