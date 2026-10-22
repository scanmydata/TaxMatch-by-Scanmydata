"""Πρότυπα Word του γραφείου για τη δέουσα επιμέλεια (πίνακας `aml_templates`, db.py v8).

Έξι θέσεις: {Δήλωση παροχής στοιχείων, Ιδιωτικό συμφωνητικό, Αξιολόγηση κινδύνου} × {φυσικά, νομικά πρόσωπα}. Για κάθε
θέση υπάρχει ΠΡΟΕΠΙΛΕΓΜΕΝΟ πρότυπο (δικό μας κείμενο, `default_template`) και το γραφείο μπορεί να ανεβάσει δικό του .docx.
Τα πεδία γράφονται `${ονομα}` ή `{{ονομα}}`. Γίνονται δεκτά και τα ονόματα πεδίων που χρησιμοποιεί το taxis.com.gr
(`${meponimia_etairias}`, `${mafm_etairias}`, `${mtitlos1}` …) ώστε να λειτουργούν πρότυπα που το γραφείο έχει ήδη
προσαρμόσει εκεί (`TAXIS_ALIASES`).

Χωρίς εξωτερική βιβλιοθήκη: ένα .docx είναι zip με XML. Σε κάθε παράγραφο που περιέχει πεδίο, το κείμενο των runs
ενώνεται στο πρώτο run (το Word σπάει συχνά ένα `${πεδίο}` σε πολλά runs) — χάνεται μόνο η εσωτερική μορφοποίηση
ΕΚΕΙΝΗΣ της παραγράφου. Ποτέ κωδικοί TAXISnet ή άλλα μυστικά ως πεδία.
"""
from __future__ import annotations

import html
import io
import re
import sqlite3
import zipfile
from datetime import date
from typing import Any, Optional

from .. import db, office as office_mod, settings_store
from . import model, store
from .content import (
    ASSESSMENT_LABEL, CLIENT_KIND_LABEL, FEE_PAYMENTS, FILE_STATUS_LABEL, MEASURES, TX_LABELS, UBO_LABEL, documents_for,
)

DOC_TYPES = (("declaration", "Δήλωση παροχής στοιχείων"), ("agreement", "Ιδιωτικό συμφωνητικό"),
             ("assessment", "Αξιολόγηση κινδύνου"))
AUDIENCES = (("natural", "Φυσικά πρόσωπα (ιδιώτες, συνταξιούχοι, αγρότες, ελ. επαγγελματίες)"),
             ("legal", "Νομικά πρόσωπα και οντότητες"))
DOC_TYPE_LABEL = dict(DOC_TYPES)
AUDIENCE_LABEL = dict(AUDIENCES)
SLOTS = [f"{d}:{a}" for d, _ in DOC_TYPES for a, _ in AUDIENCES]
MAX_UBOS = 7
_WEEKDAYS = ("Δευτέρα", "Τρίτη", "Τετάρτη", "Πέμπτη", "Παρασκευή", "Σάββατο", "Κυριακή")


def audience_for(kind: str) -> str:
    return "natural" if kind in ("individual", "sole") else "legal"


def slot_label(slot: str) -> str:
    d, a = slot.split(":")
    return f"{DOC_TYPE_LABEL[d]} — {AUDIENCE_LABEL[a].split(' (')[0]}"


# ------------------------------------------------------------------ πεδία
FIELD_HELP: tuple[tuple[str, str], ...] = (
    ("today", "Σημερινή ημερομηνία (ηη/μμ/εεεε)"), ("weekday", "Ημέρα εβδομάδας"), ("city", "Τόπος υπογραφής"),
    ("office_line1…office_line4", "Στοιχεία γραφείου (καρτέλα Μεθοδολογία)"), ("officer", "Υπεύθυνος συμμόρφωσης"),
    ("client_name", "Επωνυμία / ονοματεπώνυμο"), ("client_afm", "ΑΦΜ"), ("client_doy", "ΔΟΥ"),
    ("client_address", "Διεύθυνση"), ("client_legal_form", "Νομική μορφή"), ("client_kind", "Είδος πελάτη"),
    ("client_kad", "Κύριος ΚΑΔ και περιγραφή"), ("client_start", "Έναρξη εργασιών"), ("relationship_start", "Έναρξη σχέσης"),
    ("purpose", "Σκοπός και φύση της σχέσης"), ("pep", "Δήλωση ΠΕΠ"), ("fee_payment", "Τρόπος εξόφλησης αμοιβής"),
    ("rep_name / rep_afm / rep_role / rep_id / rep_address / rep_phone / rep_email", "Νόμιμος εκπρόσωπος ή ο ίδιος ο πελάτης (φυσικό πρόσωπο)"),
    ("rep_father / rep_birth / rep_birthplace / rep_nationality", "Πατρώνυμο, γέννηση, υπηκοότητα"),
    ("client_gemi", "Αρ. ΓΕΜΗ"),
    ("ubo_table", "Πραγματικοί δικαιούχοι (μία γραμμή ανά δικαιούχο)"),
    ("ubo1_name … ubo7_pep", "Δικαιούχος Ν: _name _afm _id _birth _nationality _address _percent _control _pep"),
    ("ubo_status", "Κατάσταση ελέγχου δικαιούχων"),
    ("tx_table", "Συναλλαγές/προέλευση κεφαλαίων (μία γραμμή ανά ρεύμα)"),
    ("tx1_amount … tx8_justification", "Συναλλαγή Ν: _amount _channel _frequency _activity _country _justification"),
    ("office_name / office_afm / office_doy / office_address / office_city / office_phone / office_email / office_oee / "
     "office_accountant / office_iban", "Παράμετροι γραφείου (Ρυθμίσεις → Γραφείο)"),
    ("fee_table / fee_monthly / fee_yearly / fee_vat_rate / fee_monthly_with_vat", "Αμοιβές πελάτη (καρτέλα πελάτη → Αμοιβές)"),
    ("fee1_description … fee8_amount", "Αμοιβή Ν: _description _period _amount (γραμμή πίνακα)"),
    ("docs_list / docs_missing", "Έγγραφα φακέλου / εκκρεμή έγγραφα"),
    ("docs_checklist", "Έγγραφα φακέλου με κουτάκι ☐ (☒ όσα παραλήφθηκαν)"), ("file_status", "Κατάσταση φακέλου"),
    ("risk_category", "Κατηγορία κινδύνου"), ("risk_dd", "Είδος δέουσας επιμέλειας"),
    ("risk_score_a / risk_score_b", "Βαθμολογία Μοντέλου Α / Β"), ("risk_model", "Μοντέλο γραφείου"),
    ("risk_factors", "Παράγοντες που συντρέχουν"), ("risk_overrides", "Περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ»"),
    ("risk_notes", "Σημειώσεις μοντέλου"), ("risk_measures", "Μέτρα"), ("risk_justification", "Αιτιολογία"),
    ("risk_escalation", "Αιτιολογία αύξησης κατάταξης"), ("assessed_on", "Ημερομηνία αξιολόγησης"),
    ("assessment_kind", "Είδος αξιολόγησης"), ("next_review", "Επόμενη επανεξέταση"),
    ("review_months", "Διάστημα επανεξέτασης (μήνες)"), ("assessor", "Αξιολογητής"), ("approved_by", "Έγκριση ανώτερου στελέχους"),
)

# Και τα 6 πρότυπα του taxis ελέγχθηκαν με `render(data, build_fields(...))` (τελευταίο: «Αξιολόγηση κινδύνου — Φυσικά
# πρόσωπα», 2026-09-24): 0 άγνωστα πεδία όταν ο πελάτης έχει αξιολόγηση (τα `*_kin` έρχονται από αυτήν).
#: Πεδία του taxis.com.gr -> δικά μας (ό,τι δεν αντιστοιχεί μένει κενό)
TAXIS_ALIASES = {
    "hmer": "today", "hmera": "weekday", "mstart_date": "today",
    "mtitlos1": "office_line1", "mtitlos2": "office_line2", "mtitlos3": "office_line3", "mtitlos4": "office_line4",
    "meponimia_etairias": "client_name", "mafm_etairias": "client_afm", "mdoy_etairias": "client_doy",
    "perigrafi_nomikis_morfis": "client_legal_form", "modos_etairias": "client_address", "mpoli_etairias": "city",
    "mepaggelma_etairias": "client_kad", "mdate_sistasis": "client_start", "mpep_perigrafi": "pep",
    "mnom_ekp_eponimo": "rep_name", "mnom_ekp_afm": "rep_afm", "idiotita_nom_ekpr": "rep_role",
    "mnom_ekp_taytotita": "rep_id", "mnom_ekp_odos": "rep_address", "mnom_ekp_tel": "rep_phone",
    "mnom_ekp_kinito": "rep_phone", "dilisi_dikaioyxon": "ubo_status",
    "titlos_kin": "risk_category", "perig_kin": "risk_dd", "anal_kin": "risk_justification",
    "energy_kin": "risk_measures", "periodos_kin": "review_period", "totalsum": "risk_score_a",
    "sumtotpel": "risk_score_a", "pinakas_poso": "tx_table", "mgemi": "client_gemi",
    "memail_etairias": "rep_email", "memail_id": "rep_email", "mtel_etairias": "rep_phone", "mtel_id": "rep_phone",
    "mkinito_etairias": "rep_phone", "mkinito_id": "rep_phone", "modos_id": "rep_address", "mtaytotita": "rep_id",
    "mpatronimo": "rep_father", "mim_genisi": "rep_birth", "mtop_genisi": "rep_birthplace", "mipikoos": "rep_nationality",
    "monoma": "rep_name", "epopt_kin": "risk_measures", "xarakt_kin": "risk_factors",
}
for _i in range(1, MAX_UBOS + 1):
    for _theirs, _ours in (("eponimo", "name"), ("afm", "afm"), ("taytotita", "id"), ("date_genisi", "birth"),
                           ("ypikootita", "nationality"), ("xora", "nationality"), ("odos", "address"),
                           ("pososto", "percent"), ("elenxos", "control"),
                           # στα πρότυπα του taxis η στήλη «Πολιτικό πρόσωπο» έχει (παράξενα) αυτό το πεδίο
                           ("date_epikairopoihsi", "pep")):
        TAXIS_ALIASES[f"mdikaioyxos{_i}_{_theirs}"] = f"ubo{_i}_{_ours}"

#: Πεδία του taxis που ΣΚΟΠΙΜΑ μένουν κενά: στήλες του πίνακα «πόντων ανά συναλλαγή» της δικής τους μεθόδου (εμείς
#: βάζουμε όλο τον πίνακα στο `tx_table` της πρώτης στήλης), στοιχεία που δεν τηρούμε χωριστά (αριθμός/ΤΚ/νομός — είναι
#: μέσα στη διεύθυνση), κύκλος εργασιών και η εικόνα υπογραφής. Δεν αναφέρονται ως «άγνωστα».
TAXIS_BLANK = {"met_pliromis", "sixnotita", "drast", "xora", "po", "plir", "sixn", "dr", "xor", "summ",
               "pinakas_poso_anal", "mar_etairias", "mtk_etairias", "mnomos_etairias", "mtk_id", "mnomos_id", "mpoli_id",
               "mxora_id", "mkiklos_ergasion_tel_xrisis", "midiot", "pinakas_ubo", "afm_ubo", "tayt_ubo", "date_ubor",
               "xora_ubo", "xora_pontos_ubo", "drast_ubo", "dr_pontos_ubo", "pep_ubo", "pep_pontos_ubo", "sum_ubo",
               "totalsum_ubo", "totpep"}


def _gr(iso: str) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(iso or "")


def build_fields(conn: sqlite3.Connection, afm: str, assessment_id: Optional[int] = None) -> dict[str, str]:
    """Όλα τα πεδία για έναν πελάτη (και για μια αξιολόγηση: την ορισμένη ή την τελευταία)."""
    b = conn.execute("SELECT * FROM businesses WHERE afm=?", (afm,)).fetchone()
    b = dict(b) if b else {"afm": afm}
    p = store.get_profile(conn, afm)
    legal_form = b.get("legal_form", "") or ""
    kind = store.effective_kind(p, legal_form, b.get("activity_state", "") or "")
    today = date.today()
    office = office_mod.office_lines(conn)          # δική του κεφαλίδα (Μεθοδολογία) ή από τις παραμέτρους γραφείου
    params = office_mod.get_office(conn)
    f: dict[str, str] = {
        "today": today.strftime("%d/%m/%Y"), "weekday": _WEEKDAYS[today.weekday()],
        "city": settings_store.get(conn, "aml_office_city") or params["office_city"],
        "officer": settings_store.get(conn, "aml_officer") or params["office_accountant"],
        "client_name": b.get("name", "") or "", "client_afm": afm, "client_doy": b.get("doy", "") or "",
        "client_address": b.get("address", "") or "", "client_legal_form": legal_form or CLIENT_KIND_LABEL.get(kind, ""),
        "client_kind": CLIENT_KIND_LABEL.get(kind, ""),
        "client_kad": f"{b.get('kad_main_code', '') or ''} {b.get('kad_main_desc', '') or ''}".strip(),
        "client_start": _gr(b.get("start_date", "") or ""), "relationship_start": _gr(p["relationship_start"]),
        "purpose": p["purpose"], "pep": model.PEP_LABEL.get(p["pep_status"], ""),
        "fee_payment": dict(FEE_PAYMENTS).get(p["fee_payment"], ""),
        "ubo_status": " — ".join(x for x in (UBO_LABEL.get(p["ubo_state"], ""), p["ubo_notes"]) if x),
        "file_status": FILE_STATUS_LABEL.get(p["file_status"], ""),
    }
    for i in range(4):
        f[f"office_line{i + 1}"] = office[i] if i < len(office) else ""
    f.update(params)                                # office_name, office_afm, office_iban …
    f.update(office_mod.fee_fields(conn, afm))      # fee_table, fee_monthly, fee1_description …
    rep = p["rep"]
    f.update({"rep_name": rep.get("name", ""), "rep_afm": rep.get("afm", ""), "rep_role": rep.get("role", ""),
              "rep_id": rep.get("id_number", ""), "rep_address": rep.get("address", ""), "rep_phone": rep.get("phone", ""),
              "rep_email": rep.get("email", ""), "rep_father": rep.get("father_name", ""),
              "rep_birth": rep.get("birth_date", ""), "rep_birthplace": rep.get("birth_place", ""),
              "rep_nationality": rep.get("nationality", ""), "client_gemi": b.get("ar_gemi", "") or ""})
    ubo_lines = []
    for i in range(1, MAX_UBOS + 1):
        u = p["ubos"][i - 1] if i <= len(p["ubos"]) else {}
        pep = model.PEP_LABEL.get(u.get("pep", ""), "") if u else ""
        vals = {"name": u.get("name", ""), "afm": u.get("afm", ""), "id": u.get("id_number", ""),
                "birth": u.get("birth_date", ""), "nationality": u.get("nationality", ""), "address": u.get("address", ""),
                "percent": u.get("percent", ""), "control": u.get("control", ""), "pep": pep}
        for k, v in vals.items():
            f[f"ubo{i}_{k}"] = v
        if u:
            ubo_lines.append(" · ".join(x for x in (vals["name"], f"ΑΦΜ {vals['afm']}" if vals["afm"] else "",
                                                     f"{vals['percent']}%" if vals["percent"] else "", vals["control"],
                                                     vals["nationality"], pep) if x))
    f["ubo_table"] = "\n".join(ubo_lines)
    f["tx_table"] = "\n".join(
        " · ".join(x for x in (TX_LABELS[k].get(t.get(k, ""), "") for k in ("amount", "channel", "frequency", "activity", "country")) if x)
        + (f" — {t['justification']}" if t.get("justification") else "") for t in p["transactions"])
    for i in range(1, MAX_TX + 1):
        t = p["transactions"][i - 1] if i <= len(p["transactions"]) else {}
        for k in ("amount", "channel", "frequency", "activity", "country"):
            f[f"tx{i}_{k}"] = TX_LABELS[k].get(t.get(k, ""), "") if t else ""
        f[f"tx{i}_justification"] = t.get("justification", "") if t else ""
    docs = documents_for(legal_form, kind)
    f["docs_list"] = "\n".join(t for _k, t in docs)
    missing = set(store.missing_documents(p, legal_form, b.get("activity_state", "") or ""))
    f["docs_missing"] = "\n".join(t for k, t in docs if k in missing) or "Κανένα"
    f["docs_checklist"] = "\n".join(("☐ " if k in missing else "☒ ") + t for k, t in docs)

    a = store.get_assessment(conn, assessment_id) if assessment_id else store.latest(conn, afm)
    if a:
        r = model.compute(a["factors"], a["overrides"])
        cat = a["final_category"]
        f.update({
            "risk_category": model.CATEGORY_LABEL[cat], "risk_dd": model.DD_LABEL[cat], "risk_model": a["model"],
            "risk_score_a": f"{r.sum_a:+d}", "risk_score_b": f"{r.total_b}/100",
            "risk_factors": "\n".join(f"{x.text} ({x.ref})" for x in model.FACTORS if x.key in set(a["factors"])),
            "risk_overrides": "\n".join(f"{model.OVERRIDES_BY_KEY[o].text} ({model.OVERRIDES_BY_KEY[o].ref})"
                                        for o in a["overrides"]) or "Καμία",
            "risk_notes": "\n".join(r.notes), "risk_measures": MEASURES[cat], "risk_justification": a["justification"],
            "risk_escalation": a["escalation_note"], "assessed_on": _gr(a["assessed_on"]),
            "assessment_kind": ASSESSMENT_LABEL.get(a["kind"], ""), "next_review": _gr(a["next_review"]),
            "review_months": str(store.review_months(conn, cat)), "assessor": a["assessor"], "approved_by": a["approved_by"],
        })
        f["review_period"] = f"{f['review_months']} μήνες (επόμενη: {f['next_review']})"
    return f


# ------------------------------------------------------------------ συμπλήρωση .docx
_PARA = re.compile(r"<w:p[ >].*?</w:p>", re.S)
_TEXT = re.compile(r"(<w:t(?: [^>]*)?>)(.*?)(</w:t>)", re.S)
_FIELD = re.compile(r"\$\{\s*(\w+)\s*\}|\{\{\s*(\w+)\s*\}\}")
_PARTS = re.compile(r"word/(document|header\d*|footer\d*)\.xml$")
_ROW = re.compile(r"<w:tr[ >].*?</w:tr>", re.S)
#: Πεδία «γραμμής» (δικαιούχος Ν, συναλλαγή Ν): μια γραμμή πίνακα που έχει ΜΟΝΟ τέτοια πεδία, όλα κενά, αφαιρείται.
_ROW_FIELD = re.compile(r"(ubo|tx|fee)\d+_\w+")


def _substitute(text: str, fields: dict[str, str], unknown: set[str]) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(2)
        key = name if name in fields else TAXIS_ALIASES.get(name, name)
        if key in fields:
            return fields[key]
        if name not in TAXIS_BLANK:
            unknown.add(name)
        return ""
    return _FIELD.sub(repl, text)


def _drop_empty_rows(xml: str, fields: dict[str, str]) -> str:
    """Γραμμές πίνακα για δικαιούχο/συναλλαγή Ν που δεν υπάρχει: αφαιρούνται, ώστε ένας πελάτης με 2 δικαιούχους να
    μη βγάζει 5 κενές γραμμές. Ισχύει και για τα πρότυπα του taxis (`mdikaioyxos3_…` → `ubo3_…`)."""
    def row(m: re.Match) -> str:
        r = m.group(0)
        if "<w:tbl" in r:                                  # ένθετος πίνακας: το regex δεν ξέρει πού κλείνει — άθικτο
            return r
        text = "".join(html.unescape(x.group(2)) for x in _TEXT.finditer(r))
        names = [TAXIS_ALIASES.get(n, n) for n in (a or b for a, b in _FIELD.findall(text))]
        if names and all(_ROW_FIELD.fullmatch(n) and not fields.get(n) for n in names):
            return ""
        return r
    return _ROW.sub(row, xml)


def _align_left(p: str) -> str:
    """Λίστα πολλών γραμμών σε πλήρως στοιχισμένη παράγραφο: το Word «τεντώνει» κάθε γραμμή που τελειώνει σε αλλαγή
    γραμμής σε όλο το πλάτος — αριστερή στοίχιση για αυτή την παράγραφο. Το <w:jc> μπαίνει πριν από το <w:rPr> της
    παραγράφου (το σχήμα OOXML ορίζει σειρά στοιχείων)."""
    head = re.match(r"<w:p[^>]*>(<w:pPr>.*?</w:pPr>|<w:pPr/>)?", p, re.S)
    ppr = head.group(1) or ""
    if "<w:jc " in ppr:
        new_ppr = re.sub(r'<w:jc w:val="(both|distribute)"/>', '<w:jc w:val="left"/>', ppr, count=1)
    elif ppr in ("", "<w:pPr/>"):
        new_ppr = '<w:pPr><w:jc w:val="left"/></w:pPr>'
    else:
        cut = ppr.find("<w:rPr>") if "<w:rPr>" in ppr else ppr.rfind("</w:pPr>")
        new_ppr = ppr[:cut] + '<w:jc w:val="left"/>' + ppr[cut:]
    start = head.end() - len(ppr)
    return p[:start] + new_ppr + p[head.end():]


def _fill_part(xml: str, fields: dict[str, str], unknown: set[str]) -> str:
    xml = _drop_empty_rows(xml, fields)

    def para(m: re.Match) -> str:
        p = m.group(0)
        runs = list(_TEXT.finditer(p))
        if not runs:
            return p
        joined = "".join(html.unescape(r.group(2)) for r in runs)
        if "${" not in joined and "{{" not in joined:
            return p
        new = _substitute(joined, fields, unknown)
        # Κενό πεδίο δίπλα σε διαχωριστικό («${rep_phone} · ${rep_email}» χωρίς τηλέφωνο): χωρίς ξεκρέμαστα « · » / « / ».
        new = re.sub(r"^[ \t]*[·/][ \t]*|[ \t]*[·/][ \t]*$", "", new)
        new = re.sub(r"[ \t]*·[ \t]*(?:·[ \t]*)+", " · ", new)
        if "\n" in new:
            p = _align_left(p)
            runs = list(_TEXT.finditer(p))
        esc = html.escape(new, quote=False).replace("\n", '</w:t><w:br/><w:t xml:space="preserve">')
        out, last = [], 0
        for i, r in enumerate(runs):
            out.append(p[last:r.start()])
            out.append('<w:t xml:space="preserve">' + (esc if i == 0 else "") + "</w:t>")
            last = r.end()
        out.append(p[last:])
        return "".join(out)
    return _PARA.sub(para, xml)


def render(docx: bytes, fields: dict[str, str]) -> tuple[bytes, list[str]]:
    """Συμπληρώνει τα πεδία. Επιστρέφει (νέο .docx, άγνωστα πεδία που έμειναν κενά)."""
    unknown: set[str] = set()
    src = zipfile.ZipFile(io.BytesIO(docx))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for item in src.infolist():
            data = src.read(item.filename)
            if _PARTS.match(item.filename):
                data = _fill_part(data.decode("utf-8"), fields, unknown).encode("utf-8")
            out.writestr(item, data)
    return buf.getvalue(), sorted(unknown - {n for n in unknown if n.startswith("image_")})


def validate_docx(data: bytes) -> None:
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        z.getinfo("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("Το αρχείο δεν είναι έγγραφο Word (.docx).") from exc


# ------------------------------------------------------------------ αποθήκευση προτύπων
def get_template(conn: sqlite3.Connection, slot: str) -> tuple[bytes, str, bool]:
    """(περιεχόμενο, όνομα αρχείου, είναι του γραφείου;) — αλλιώς το προεπιλεγμένο."""
    row = conn.execute("SELECT filename, content FROM aml_templates WHERE slot=?", (slot,)).fetchone()
    if row:
        return bytes(row["content"]), row["filename"], True
    d, a = slot.split(":")
    return default_template(d, a), f"Πρότυπο {slot_label(slot)}.docx", False


def template_info(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    return {r["slot"]: {"filename": r["filename"], "uploaded_at": r["uploaded_at"]}
            for r in conn.execute("SELECT slot, filename, uploaded_at FROM aml_templates")}


def save_template(conn: sqlite3.Connection, slot: str, filename: str, data: bytes) -> None:
    if slot not in SLOTS:
        raise ValueError("Άγνωστη θέση προτύπου.")
    validate_docx(data)
    conn.execute("INSERT INTO aml_templates(slot, filename, content, uploaded_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(slot) DO UPDATE SET filename=excluded.filename, content=excluded.content, "
                 "uploaded_at=excluded.uploaded_at", (slot, filename, data, db.utcnow()))


def reset_template(conn: sqlite3.Connection, slot: str) -> None:
    conn.execute("DELETE FROM aml_templates WHERE slot=?", (slot,))


def generate(conn: sqlite3.Connection, doc_type: str, afm: str, assessment_id: Optional[int] = None) -> tuple[bytes, list[str], str]:
    """Το έγγραφο του πελάτη από το πρότυπο της σωστής θέσης (φυσικό/νομικό πρόσωπο κατά το είδος του)."""
    b = conn.execute("SELECT legal_form, activity_state FROM businesses WHERE afm=?", (afm,)).fetchone()
    kind = store.effective_kind(store.get_profile(conn, afm), b["legal_form"] if b else "", b["activity_state"] if b else "")
    slot = f"{doc_type}:{audience_for(kind)}"
    template, _name, _custom = get_template(conn, slot)
    data, unknown = render(template, build_fields(conn, afm, assessment_id))
    return data, unknown, slot


# ------------------------------------------------------------------ προεπιλεγμένα πρότυπα (δικό μας κείμενο)
# Μορφοποίηση (2026-10-22, αίτημα χρήστη «καλύτερα πρότυπα»): πραγματικά στυλ Word (Τίτλος/Επικεφαλίδα — φαίνονται στο
# παράθυρο «Στυλ» και στην πλοήγηση), κεφαλίδα με τα στοιχεία του γραφείου και υποσέλιδο «Σελίδα Χ από Υ» σε ΚΑΘΕ σελίδα,
# στοιχεία πελάτη σε πίνακα ετικέτας/τιμής, δικαιούχοι και συναλλαγές σε πίνακες με μία γραμμή ανά εγγραφή (οι κενές
# γραμμές αφαιρούνται κατά τη συμπλήρωση — `_drop_empty_rows`), μπλοκ υπογραφών σε δύο στήλες. Μόνο απλό OOXML: ανοίγει
# σε Word, LibreOffice και Google Docs και ο χρήστης το αλλάζει ελεύθερα («Επεξεργασία στο Word»).
_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
_R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
_TEXT_WIDTH = 9638                                     # A4 (11906) μείον περιθώρια 2×1134 twips
_ACCENT, _ACCENT_LIGHT, _LABEL_FILL, _MUTED = "1F4E79", "2E75B6", "EAF1F8", "595959"
MAX_TX = 8                                             # γραμμές του πίνακα συναλλαγών στα πρότυπα (tx1_… tx8_…)


def _run(text: str, *, bold: bool = False, italic: bool = False, size: int = 0, color: str = "") -> str:
    props = ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "") + (f'<w:color w:val="{color}"/>' if color else "") \
        + (f'<w:sz w:val="{size * 2}"/><w:szCs w:val="{size * 2}"/>' if size else "")
    rpr = f"<w:rPr>{props}</w:rPr>" if props else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{html.escape(text, quote=False)}</w:t></w:r>'


def _p(text: str = "", *, bold: bool = False, italic: bool = False, size: int = 0, center: bool = False,
       style: str = "", color: str = "", align: str = "", after: Optional[int] = None, keep_next: bool = False) -> str:
    """Μία παράγραφος. Τα πεδία ${…} μένουν ΜΕΣΑ σε ένα run, ώστε η συμπλήρωση να μην αλλάζει τη μορφή."""
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + ("<w:keepNext/>" if keep_next else "") \
        + (f'<w:spacing w:after="{after}"/>' if after is not None else "") \
        + (f'<w:jc w:val="{"center" if center else align}"/>' if center or align else "")
    runs = _run(text, bold=bold, italic=italic, size=size, color=color) if text else ""
    return f'<w:p>{f"<w:pPr>{ppr}</w:pPr>" if ppr else ""}{runs}</w:p>'


def _h(text: str, level: int = 1) -> str:
    return _p(text, style=f"Heading{level}")


def _cell(content: str, width: int, *, fill: str = "", bold: bool = False, color: str = "", size: int = 0,
          center: bool = False) -> str:
    shd = f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>' if fill else ""
    paras = "".join(_p(line, bold=bold, color=color, size=size, center=center, align="left", after=0)
                    for line in content.split("\n"))
    return f'<w:tc><w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shd}</w:tcPr>{paras}</w:tc>'


def _table(rows: list[str], widths: list[int], *, borders: bool = True) -> str:
    b = "single" if borders else "nil"
    edges = "".join(f'<w:{e} w:val="{b}" w:sz="4" w:space="0" w:color="A6BCD4"/>'
                    for e in ("top", "left", "bottom", "right", "insideH", "insideV"))
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    return (f'<w:tbl><w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/><w:tblBorders>{edges}</w:tblBorders>'
            '<w:tblLayout w:type="fixed"/><w:tblCellMar><w:top w:w="40" w:type="dxa"/><w:left w:w="90" w:type="dxa"/>'
            '<w:bottom w:w="40" w:type="dxa"/><w:right w:w="90" w:type="dxa"/></w:tblCellMar></w:tblPr>'
            f'<w:tblGrid>{grid}</w:tblGrid>{"".join(rows)}</w:tbl>' + _p(after=60))


def _kv(pairs: list[tuple[str, str]], label_width: int = 3100) -> str:
    """Πίνακας «ετικέτα | τιμή» — τα στοιχεία πελάτη/εκπροσώπου διαβάζονται με μια ματιά."""
    w = [label_width, _TEXT_WIDTH - label_width]
    return _table([f"<w:tr>{_cell(k, w[0], fill=_LABEL_FILL, bold=True, color=_ACCENT)}{_cell(v, w[1])}</w:tr>"
                   for k, v in pairs], w)


def _grid(headers: list[str], rows: list[list[str]], widths: list[int]) -> str:
    """Πίνακας με σκιασμένη γραμμή επικεφαλίδων (επαναλαμβάνεται σε κάθε σελίδα)."""
    head = ('<w:tr><w:trPr><w:tblHeader/></w:trPr>'
            + "".join(_cell(h, w, fill=_ACCENT, bold=True, color="FFFFFF", size=9) for h, w in zip(headers, widths))
            + "</w:tr>")
    body = ["<w:tr>" + "".join(_cell(c, w, size=9) for c, w in zip(r, widths)) + "</w:tr>" for r in rows]
    return _table([head] + body, widths)


def _box(lines: list[tuple[str, bool]], fill: str = _LABEL_FILL) -> str:
    """Σκιασμένο πλαίσιο (αποτέλεσμα αξιολόγησης, σημείωση εμπιστευτικότητας)."""
    paras = "".join(_p(t, bold=b, color=_ACCENT if b else "", align="left", after=40) for t, b in lines)
    cell = f'<w:tc><w:tcPr><w:tcW w:w="{_TEXT_WIDTH}" w:type="dxa"/><w:shd w:val="clear" w:color="auto" w:fill="{fill}"/></w:tcPr>{paras}</w:tc>'
    return _table([f"<w:tr>{cell}</w:tr>"], [_TEXT_WIDTH])


def _signatures(left: tuple[str, str], right: tuple[str, str]) -> str:
    """Δύο στήλες υπογραφών χωρίς περιγράμματα: τίτλος, κενό, γραμμή, όνομα."""
    half = _TEXT_WIDTH // 2

    def col(title: str, name: str) -> str:
        return (f'<w:tc><w:tcPr><w:tcW w:w="{half}" w:type="dxa"/></w:tcPr>'
                + _p(title, bold=True, center=True, after=600) + _p("……………………………………", center=True, after=0)
                + _p(name, center=True, color=_MUTED, after=0) + "</w:tc>")
    return _p(after=200) + _table([f"<w:tr><w:trPr><w:cantSplit/></w:trPr>{col(*left)}{col(*right)}</w:tr>"],
                                  [half, half], borders=False)


def _ubo_grid() -> str:
    widths = [2600, 1250, 1250, 900, 1900, 1738]
    rows = [[f"${{ubo{i}_name}}", f"${{ubo{i}_afm}}", f"${{ubo{i}_id}}", f"${{ubo{i}_percent}}",
             f"${{ubo{i}_control}}", f"${{ubo{i}_pep}}"] for i in range(1, MAX_UBOS + 1)]
    return _grid(["Ονοματεπώνυμο", "ΑΦΜ", "Ταυτότητα", "%", "Έλεγχος / μέσω", "ΠΕΠ"], rows, widths)


def _tx_grid() -> str:
    widths = [1500, 1700, 1300, 1900, 1300, 1938]
    rows = [[f"${{tx{i}_amount}}", f"${{tx{i}_channel}}", f"${{tx{i}_frequency}}", f"${{tx{i}_activity}}",
             f"${{tx{i}_country}}", f"${{tx{i}_justification}}"] for i in range(1, MAX_TX + 1)]
    return _grid(["Ποσό (ετήσιο)", "Μέσο πληρωμής", "Συχνότητα", "Δραστηριότητα", "Χώρα", "Αιτιολόγηση / προέλευση"],
                 rows, widths)


def _fee_article() -> list[str]:
    """Άρθρο «Αμοιβή»: οι αμοιβές του πελάτη (καρτέλα πελάτη → Αμοιβές), μία γραμμή ανά υπηρεσία."""
    widths = [5338, 2100, 2200]
    rows = [[f"${{fee{i}_description}}", f"${{fee{i}_period}}", f"${{fee{i}_amount}}"] for i in range(1, 9)]
    return [_p("Η αμοιβή του Γραφείου για τις παραπάνω υπηρεσίες ορίζεται ως εξής (ποσά προ ΦΠΑ):", keep_next=True),
            _grid(["Υπηρεσία", "Περιοδικότητα", "Ποσό"], rows, widths),
            _p("Μηνιαία επαναλαμβανόμενη αμοιβή: ${fee_monthly} πλέον ΦΠΑ ${fee_vat_rate} (${fee_monthly_with_vat}). "
               "Τρόπος εξόφλησης: ${fee_payment} · IBAN γραφείου: ${office_iban}.")]


def _client_table(audience: str) -> str:
    rows = [("Επωνυμία / ονοματεπώνυμο", "${client_name}"), ("ΑΦΜ / ΔΟΥ", "${client_afm} / ${client_doy}"),
            ("Νομική μορφή / είδος", "${client_legal_form} · ${client_kind}"),
            ("Διεύθυνση", "${client_address}"), ("Κύρια δραστηριότητα (ΚΑΔ)", "${client_kad}")]
    if audience == "legal":
        rows += [("Αρ. Γ.Ε.ΜΗ.", "${client_gemi}"), ("Έναρξη εργασιών", "${client_start}")]
    else:
        rows += [("Πατρώνυμο", "${rep_father}"), ("Αρ. ταυτότητας / διαβατηρίου", "${rep_id}"),
                 ("Ημ/νία και τόπος γέννησης", "${rep_birth} · ${rep_birthplace}"), ("Υπηκοότητα", "${rep_nationality}"),
                 ("Τηλέφωνο / email", "${rep_phone} · ${rep_email}")]
    rows.append(("Έναρξη συνεργασίας", "${relationship_start}"))
    return _kv(rows)


def _rep_table() -> str:
    return _kv([("Ονοματεπώνυμο", "${rep_name}"), ("ΑΦΜ / ιδιότητα", "${rep_afm} · ${rep_role}"),
                ("Αρ. ταυτότητας / διαβατηρίου", "${rep_id}"), ("Διεύθυνση", "${rep_address}"),
                ("Τηλέφωνο / email", "${rep_phone} · ${rep_email}")])


_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles {_W}><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri" '
    'w:eastAsia="Calibri"/><w:sz w:val="21"/><w:szCs w:val="21"/><w:lang w:val="el-GR"/></w:rPr></w:rPrDefault>'
    '<w:pPrDefault><w:pPr><w:spacing w:after="100" w:line="264" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
    '<w:pPr><w:jc w:val="both"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:next w:val="Normal"/>'
    '<w:qFormat/><w:pPr><w:spacing w:before="120" w:after="40"/><w:jc w:val="center"/></w:pPr>'
    f'<w:rPr><w:b/><w:color w:val="{_ACCENT}"/><w:sz w:val="30"/><w:szCs w:val="30"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Subtitle"><w:name w:val="Subtitle"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:spacing w:after="240"/><w:jc w:val="center"/></w:pPr>'
    f'<w:rPr><w:i/><w:color w:val="{_MUTED}"/><w:sz w:val="19"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="220" w:after="100"/>'
    f'<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="2" w:color="{_ACCENT_LIGHT}"/></w:pBdr><w:jc w:val="left"/>'
    f'<w:outlineLvl w:val="0"/></w:pPr><w:rPr><w:b/><w:color w:val="{_ACCENT}"/><w:sz w:val="23"/><w:szCs w:val="23"/>'
    '</w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/><w:basedOn w:val="Normal"/>'
    '<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="160" w:after="60"/><w:jc w:val="left"/>'
    f'<w:outlineLvl w:val="1"/></w:pPr><w:rPr><w:b/><w:color w:val="{_ACCENT}"/></w:rPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Header"><w:name w:val="header"/><w:basedOn w:val="Normal"/>'
    '<w:pPr><w:spacing w:after="0"/><w:jc w:val="center"/></w:pPr></w:style>'
    '<w:style w:type="paragraph" w:styleId="Footer"><w:name w:val="footer"/><w:basedOn w:val="Normal"/>'
    f'<w:pPr><w:spacing w:after="0"/><w:jc w:val="left"/></w:pPr><w:rPr><w:color w:val="{_MUTED}"/><w:sz w:val="16"/></w:rPr></w:style>'
    '</w:styles>'
)


def _header_xml() -> str:
    """Στοιχεία γραφείου σε κάθε σελίδα, με λεπτή γραμμή από κάτω (ό,τι γράφει η «Μεθοδολογία» → Στοιχεία γραφείου)."""
    lines = [_p("${office_line1}", style="Header", bold=True, color=_ACCENT, size=11)] + \
        [_p(f"${{office_line{i}}}", style="Header", color=_MUTED, size=9) for i in (2, 3, 4)]
    lines[-1] = lines[-1].replace('<w:pStyle w:val="Header"/>',
                                  f'<w:pStyle w:val="Header"/><w:pBdr><w:bottom w:val="single" w:sz="8" w:space="4" '
                                  f'w:color="{_ACCENT_LIGHT}"/></w:pBdr><w:spacing w:after="160"/>', 1)
    return f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:hdr {_W}>{"".join(lines)}</w:hdr>'


def _footer_xml(title: str) -> str:
    """Τίτλος εγγράφου και πελάτης αριστερά, «Σελίδα Χ από Υ» δεξιά — σε ΧΩΡΙΣΤΕΣ παραγράφους: η συμπλήρωση ενώνει τα
    runs μιας παραγράφου με πεδίο ${…} στο πρώτο run, άρα δεν πρέπει να μοιράζεται παράγραφο με τα πεδία σελίδας."""
    def field(instr: str) -> str:
        return f'<w:fldSimple w:instr=" {instr} "><w:r><w:rPr><w:b/></w:rPr><w:t>1</w:t></w:r></w:fldSimple>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<w:ftr {_W}><w:p><w:pPr><w:pStyle w:val="Footer"/><w:pBdr><w:top w:val="single" w:sz="4" w:space="4" '
            f'w:color="A6BCD4"/></w:pBdr></w:pPr>{_run(title + " · ${client_name}")}</w:p>'
            f'<w:p><w:pPr><w:pStyle w:val="Footer"/><w:jc w:val="right"/></w:pPr>{_run("Σελίδα ")}{field("PAGE")}'
            f'{_run(" από ")}{field("NUMPAGES")}</w:p></w:ftr>')


def _docx(blocks: list[str], title: str = "") -> bytes:
    body = "".join(blocks)
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document {_W} {_R}><w:body>'
                f'{body}<w:sectPr><w:headerReference w:type="default" r:id="rId2"/>'
                '<w:footerReference w:type="default" r:id="rId3"/><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/>'
                '</w:sectPr></w:body></w:document>')
    ct = "application/vnd.openxmlformats-officedocument.wordprocessingml"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   f'<Override PartName="/word/document.xml" ContentType="{ct}.document.main+xml"/>'
                   f'<Override PartName="/word/styles.xml" ContentType="{ct}.styles+xml"/>'
                   f'<Override PartName="/word/header1.xml" ContentType="{ct}.header+xml"/>'
                   f'<Override PartName="/word/footer1.xml" ContentType="{ct}.footer+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{rel}/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{rel}/styles" Target="styles.xml"/>'
                   f'<Relationship Id="rId2" Type="{rel}/header" Target="header1.xml"/>'
                   f'<Relationship Id="rId3" Type="{rel}/footer" Target="footer1.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", _STYLES)
        z.writestr("word/header1.xml", _header_xml())
        z.writestr("word/footer1.xml", _footer_xml(title))
    return buf.getvalue()


def default_template(doc_type: str, audience: str) -> bytes:
    from .documents import ENGAGEMENT_CLAUSES
    legal = audience == "legal"
    n = iter(range(1, 20))                                   # αρίθμηση ενοτήτων (διαφέρει ανά φυσικό/νομικό πρόσωπο)
    if doc_type == "declaration":
        blocks = [
            _p("ΔΗΛΩΣΗ ΠΑΡΟΧΗΣ ΣΤΟΙΧΕΙΩΝ ΠΕΛΑΤΗ", style="Title"),
            _p("Πρόληψη και καταστολή της νομιμοποίησης εσόδων από εγκληματικές δραστηριότητες — ν. 4557/2018",
               style="Subtitle"),
            _p("${city}, ${weekday} ${today}", align="right"),
            _p("Το γραφείο, ως υπόχρεο πρόσωπο του ν. 4557/2018, οφείλει να ταυτοποιεί τους πελάτες του, να εξακριβώνει "
               "τους πραγματικούς δικαιούχους και να γνωρίζει τον σκοπό και τη φύση της συνεργασίας (άρθρα 12–13). "
               "Παρακαλούμε ελέγξτε τα στοιχεία, συμπληρώστε ό,τι λείπει και υπογράψτε."),
            _h(f"{next(n)}. Στοιχεία πελάτη"), _client_table(audience),
        ]
        if legal:
            blocks += [_h(f"{next(n)}. Νόμιμος εκπρόσωπος"), _rep_table(),
                       _h(f"{next(n)}. Πραγματικοί δικαιούχοι"),
                       _p("Φυσικά πρόσωπα που κατέχουν άμεσα ή έμμεσα ποσοστό άνω του 25% ή ασκούν έλεγχο με άλλο τρόπο "
                          "(άρθρο 3 παρ. 17 ν. 4557/2018).", italic=True, color=_MUTED),
                       _ubo_grid(), _p("Κατάσταση ελέγχου: ${ubo_status}")]
        else:
            blocks += [_h(f"{next(n)}. Πραγματικός δικαιούχος"),
                       _p("☐ Ενεργώ για δικό μου λογαριασμό — πραγματικός δικαιούχος είμαι ο ίδιος / η ίδια."),
                       _p("☐ Ενεργώ για λογαριασμό τρίτου: ……………………………………………………………………")]
        blocks += [
            _h(f"{next(n)}. Πολιτικώς εκτεθειμένα πρόσωπα (ΠΕΠ)"),
            _p("Δηλώνω αν ο ίδιος / η ίδια" + (", ο νόμιμος εκπρόσωπος ή πραγματικός δικαιούχος" if legal else "")
               + " είναι πολιτικώς εκτεθειμένο πρόσωπο, μέλος της οικογένειας ή στενός συνεργάτης τέτοιου προσώπου "
               "(άρθρο 3 παρ. 9–11, άρθρο 18):"),
            _p("☐ ΟΧΙ          ☐ ΝΑΙ — αξίωμα / σχέση: ……………………………………………………"),
            _p("Καταγραφή γραφείου: ${pep}", italic=True, color=_MUTED),
            _h(f"{next(n)}. Σκοπός της συνεργασίας και προέλευση κεφαλαίων"),
            _kv([("Σκοπός και φύση της σχέσης", "${purpose}"), ("Τρόπος εξόφλησης αμοιβής", "${fee_payment}")]),
            _p("Κύρια ρεύματα συναλλαγών / εσόδων:", bold=True, keep_next=True), _tx_grid(),
            _h(f"{next(n)}. Έγγραφα που προσκομίζονται"), _p("${docs_checklist}"),
            _h(f"{next(n)}. Δεσμεύσεις και προσωπικά δεδομένα"),
            _p("Δηλώνω ότι τα στοιχεία είναι αληθή και πλήρη και ότι θα ενημερώνω εγγράφως το γραφείο για κάθε μεταβολή "
               "τους (ιδίως εκπροσώπησης και πραγματικών δικαιούχων). Γνωρίζω ότι, αν δεν παρασχεθούν τα στοιχεία που "
               "απαιτεί ο νόμος, το γραφείο δεν μπορεί να συνάψει ή να συνεχίσει τη συνεργασία (άρθρο 13 παρ. 2) και ότι "
               "εκπληρώνει τις νόμιμες υποχρεώσεις του, περιλαμβανομένης της αναφοράς στην Αρχή Καταπολέμησης της "
               "Νομιμοποίησης Εσόδων (άρθρο 22)."),
            _p("Τα προσωπικά δεδομένα τηρούνται για πέντε (5) έτη μετά τη λήξη της συνεργασίας, αποκλειστικά για τους "
               "σκοπούς του ν. 4557/2018 (άρθρο 30) και σύμφωνα με τον Γενικό Κανονισμό Προστασίας Δεδομένων."),
            _signatures(("Ο / Η δηλών / ούσα", "${rep_name}" if legal else "${client_name}"),
                        ("Για το γραφείο", "${officer}")),
        ]
        title = "Δήλωση παροχής στοιχείων"
    elif doc_type == "agreement":
        party = ("η εταιρεία με την επωνυμία «${client_name}», ΑΦΜ ${client_afm}, ΔΟΥ ${client_doy}, με έδρα ${client_address}, "
                 "νόμιμα εκπροσωπούμενη από ${rep_name} (ΑΦΜ ${rep_afm}, ${rep_role})") if legal else \
                ("ο/η ${client_name}, ΑΦΜ ${client_afm}, ΔΟΥ ${client_doy}, κάτοικος ${client_address}, "
                 "αρ. ταυτότητας ${rep_id}")
        articles = [("Αντικείμενο", "Το Γραφείο αναλαμβάνει την παροχή των λογιστικών και φοροτεχνικών υπηρεσιών: "
                                   "…………………………………………………………………………………………………"),
                    ("Αμοιβή", None),
                    ("Υποχρεώσεις του Εντολέα", "Ο Εντολέας παραδίδει εγκαίρως πλήρη και ακριβή στοιχεία και ενημερώνει "
                                              "για κάθε μεταβολή που επηρεάζει τις υποχρεώσεις του.")] \
            + list(ENGAGEMENT_CLAUSES) \
            + [("Διάρκεια – καταγγελία", "Η σύμβαση ισχύει από ${relationship_start} για αόριστο χρόνο και καταγγέλλεται "
                                        "από κάθε μέρος με έγγραφη προειδοποίηση τριάντα (30) ημερών.")]
        blocks = [
            _p("ΙΔΙΩΤΙΚΟ ΣΥΜΦΩΝΗΤΙΚΟ", style="Title"),
            _p("παροχής λογιστικών – φοροτεχνικών υπηρεσιών", style="Subtitle"),
            _p("Στην πόλη ${city}, σήμερα ${weekday} ${today}, οι παρακάτω συμβαλλόμενοι:"),
            _kv([("Α. Το Γραφείο", "${office_line1}\n${office_line2}\n${office_line4}"),
                 ("Β. Ο Εντολέας", party)], label_width=2200),
            _p("συμφωνούν και αποδέχονται αμοιβαία τα εξής:"),
        ] + [x for i, (t, c) in enumerate(articles, 1) for x in (_h(f"Άρθρο {i} — {t}", 2), *(_fee_article() if c is None else [_p(c)]))] + [
            _p("Το παρόν συντάχθηκε σε δύο (2) όμοια αντίτυπα και κάθε συμβαλλόμενος έλαβε από ένα.", after=0),
            _p("ΥΠΟΔΕΙΓΜΑ — προσαρμόστε το ή ανεβάστε το δικό σας συμφωνητικό.", italic=True, color=_MUTED, size=9),
            _signatures(("Για το Γραφείο", "${officer}"), ("Ο Εντολέας", "${rep_name}" if legal else "${client_name}")),
        ]
        title = "Ιδιωτικό συμφωνητικό"
    else:
        blocks = [
            _p("ΕΣΩΤΕΡΙΚΗ ΑΞΙΟΛΟΓΗΣΗ ΚΙΝΔΥΝΟΥ ΠΕΛΑΤΗ", style="Title"),
            _p("Δέουσα επιμέλεια βάσει κινδύνου — άρθρα 5, 13 και 15–18 ν. 4557/2018", style="Subtitle"),
            _box([("ΕΜΠΙΣΤΕΥΤΙΚΟ — μόνο για εσωτερική χρήση", True),
                  ("Δεν γνωστοποιείται στον πελάτη ή σε τρίτους (άρθρο 27 ν. 4557/2018). Τηρείται για πέντε έτη μετά τη "
                   "λήξη της συνεργασίας (άρθρο 30).", False)], fill="FBEFEF"),
            _kv([("Είδος αξιολόγησης", "${assessment_kind}"), ("Ημερομηνία", "${assessed_on}"),
                 ("Μοντέλο γραφείου", "${risk_model}"), ("Αξιολογητής", "${assessor}")]),
            _h(f"{next(n)}. Στοιχεία πελάτη"), _client_table(audience),
        ]
        if legal:
            blocks += [_h(f"{next(n)}. Νόμιμος εκπρόσωπος"), _rep_table(),
                       _h(f"{next(n)}. Πραγματικοί δικαιούχοι"), _ubo_grid(), _p("Κατάσταση ελέγχου: ${ubo_status}")]
        blocks += [
            _h(f"{next(n)}. Πολιτικώς εκτεθειμένα πρόσωπα"), _p("${pep}"),
            _h(f"{next(n)}. Σκοπός, συναλλαγές και προέλευση κεφαλαίων"), _p("${purpose}"), _tx_grid(),
            _h(f"{next(n)}. Παράγοντες κινδύνου (Παραρτήματα Ι–ΙΙ)"), _p("${risk_factors}"),
            _p("Περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ»: ${risk_overrides}"),
            _h(f"{next(n)}. Αποτέλεσμα"),
            _box([("ΚΑΤΑΤΑΞΗ: ${risk_category} → ${risk_dd}", True),
                  ("Βαθμολογία: Μοντέλο Α ${risk_score_a} · Μοντέλο Β ${risk_score_b}", False),
                  ("${risk_escalation}", False), ("${risk_notes}", False)]),
            _h("Μέτρα δέουσας επιμέλειας", 2), _p("${risk_measures}"),
            _h("Αιτιολογία", 2), _p("${risk_justification}"),
            _h("Εκκρεμή έγγραφα", 2), _p("${docs_missing}"),
            _kv([("Επόμενη επανεξέταση", "${next_review} — κάθε ${review_months} μήνες και εκτάκτως σε κάθε μεταβολή")]),
            _signatures(("Ο αξιολογητής", "${assessor}"), ("Έγκριση ανώτερου στελέχους", "${approved_by}")),
        ]
        title = "Αξιολόγηση κινδύνου"
    return _docx(blocks, title)
