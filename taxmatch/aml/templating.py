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

from .. import db, settings_store
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
    ("office_line1…office_line4", "Στοιχεία γραφείου (Μεθοδολογία & ρυθμίσεις)"), ("officer", "Υπεύθυνος συμμόρφωσης"),
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
    ("docs_list / docs_missing", "Έγγραφα φακέλου / εκκρεμή έγγραφα"), ("file_status", "Κατάσταση φακέλου"),
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
    office = [ln.strip() for ln in settings_store.get(conn, "aml_office_info").splitlines() if ln.strip()]
    f: dict[str, str] = {
        "today": today.strftime("%d/%m/%Y"), "weekday": _WEEKDAYS[today.weekday()],
        "city": settings_store.get(conn, "aml_office_city"), "officer": settings_store.get(conn, "aml_officer"),
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
    docs = documents_for(legal_form, kind)
    f["docs_list"] = "\n".join(t for _k, t in docs)
    missing = set(store.missing_documents(p, legal_form, b.get("activity_state", "") or ""))
    f["docs_missing"] = "\n".join(t for k, t in docs if k in missing) or "Κανένα"

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


def _fill_part(xml: str, fields: dict[str, str], unknown: set[str]) -> str:
    def para(m: re.Match) -> str:
        p = m.group(0)
        runs = list(_TEXT.finditer(p))
        if not runs:
            return p
        joined = "".join(html.unescape(r.group(2)) for r in runs)
        if "${" not in joined and "{{" not in joined:
            return p
        new = _substitute(joined, fields, unknown)
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
def _p(text: str, *, bold: bool = False, size: int = 0, center: bool = False) -> str:
    props = ""
    if bold or size:
        props = "<w:rPr>" + ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size * 2}"/>' if size else "") + "</w:rPr>"
    ppr = '<w:pPr><w:jc w:val="center"/></w:pPr>' if center else ""
    return f'<w:p>{ppr}<w:r>{props}<w:t xml:space="preserve">{html.escape(text, quote=False)}</w:t></w:r></w:p>'


def _docx(blocks: list[str]) -> bytes:
    body = "".join(blocks)
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>'
                f'{body}<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1134" w:right="1134" w:bottom="1134" w:left="1134" w:header="567" w:footer="567" w:gutter="0"/>'
                '</w:sectPr></w:body></w:document>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault>'
              '<w:rPr><w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:cs="Calibri"/><w:sz w:val="21"/><w:lang w:val="el-GR"/>'
              '</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80"/></w:pPr></w:pPrDefault></w:docDefaults></w:styles>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                   '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
                   '</Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                   '</Relationships>')
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                   '</Relationships>')
        z.writestr("word/document.xml", document)
        z.writestr("word/styles.xml", styles)
    return buf.getvalue()


def _office_header() -> list[str]:
    return [_p("${office_line1}", bold=True), _p("${office_line2}"), _p("${office_line3}"), _p("${office_line4}"), _p("")]


def _client_block(audience: str) -> list[str]:
    rows = ["Επωνυμία / ονοματεπώνυμο: ${client_name}", "ΑΦΜ: ${client_afm} — ΔΟΥ: ${client_doy}",
            "Νομική μορφή / είδος: ${client_legal_form} (${client_kind})", "Διεύθυνση: ${client_address}",
            "Κύρια δραστηριότητα: ${client_kad}", "Έναρξη σχέσης: ${relationship_start}"]
    out = [_p("1. Στοιχεία πελάτη", bold=True, size=12)] + [_p(r) for r in rows]
    if audience == "legal":
        out += [_p("2. Νόμιμος εκπρόσωπος", bold=True, size=12),
                _p("${rep_name} — ΑΦΜ ${rep_afm} — ιδιότητα: ${rep_role}"),
                _p("Αρ. ταυτότητας/διαβατηρίου: ${rep_id} — Διεύθυνση: ${rep_address} — Τηλ.: ${rep_phone}"),
                _p("3. Πραγματικοί δικαιούχοι (φυσικά πρόσωπα με >25% ή έλεγχο — άρθρο 3 παρ. 17 ν. 4557/2018)", bold=True, size=12),
                _p("${ubo_table}"), _p("Κατάσταση ελέγχου: ${ubo_status}")]
    else:
        out += [_p("2. Πραγματικός δικαιούχος", bold=True, size=12),
                _p("Ο πελάτης δηλώνει ότι ενεργεί για δικό του λογαριασμό και ότι πραγματικός δικαιούχος είναι ο ίδιος. "
                   "Αν ενεργεί για λογαριασμό τρίτου, το δηλώνει εδώ: ______________________")]
    return out


def default_template(doc_type: str, audience: str) -> bytes:
    from .documents import ENGAGEMENT_CLAUSES
    legal = audience == "legal"
    if doc_type == "declaration":
        blocks = _office_header() + [
            _p("ΔΗΛΩΣΗ ΠΑΡΟΧΗΣ ΣΤΟΙΧΕΙΩΝ ΠΕΛΑΤΗ", bold=True, size=16, center=True),
            _p("Πρόληψη νομιμοποίησης εσόδων από εγκληματικές δραστηριότητες — ν. 4557/2018", center=True),
            _p("${city}, ${weekday} ${today}"),
            _p("Το γραφείο, ως υπόχρεο πρόσωπο του ν. 4557/2018, οφείλει να ταυτοποιεί τους πελάτες του, να εξακριβώνει "
               "τους πραγματικούς δικαιούχους και να γνωρίζει τον σκοπό της συνεργασίας (άρθρα 12–13). Παρακαλούμε "
               "ελέγξτε, συμπληρώστε ό,τι λείπει και υπογράψτε."),
        ] + _client_block(audience) + [
            _p(("4" if legal else "3") + ". Πολιτικώς εκτεθειμένα πρόσωπα (άρθρο 3 παρ. 9–11, άρθρο 18)", bold=True, size=12),
            _p("Δήλωση: ${pep}"),
            _p("Ο/Η δηλών/ούσα βεβαιώνει αν ο ίδιος" + (", ο νόμιμος εκπρόσωπος ή πραγματικός δικαιούχος" if legal else "")
               + " είναι πολιτικώς εκτεθειμένο πρόσωπο, μέλος οικογένειας ή στενός συνεργάτης τέτοιου προσώπου."),
            _p(("5" if legal else "4") + ". Σκοπός της σχέσης και προέλευση κεφαλαίων", bold=True, size=12),
            _p("${purpose}"), _p("Κύρια ρεύματα συναλλαγών/εσόδων:"), _p("${tx_table}"),
            _p("Τρόπος εξόφλησης αμοιβής γραφείου: ${fee_payment}"),
            _p(("6" if legal else "5") + ". Δεσμεύσεις", bold=True, size=12),
            _p("Δηλώνω ότι τα στοιχεία είναι αληθή και πλήρη, ότι θα ενημερώνω εγγράφως το γραφείο για κάθε μεταβολή τους "
               "(ιδίως εκπροσώπησης και πραγματικών δικαιούχων) και ότι, αν δεν παρασχεθούν τα στοιχεία που απαιτεί ο "
               "νόμος, το γραφείο δεν μπορεί να συνάψει ή να συνεχίσει τη συνεργασία (άρθρο 13 παρ. 2). Γνωρίζω ότι το "
               "γραφείο εκπληρώνει τις νόμιμες υποχρεώσεις του, περιλαμβανομένης της αναφοράς στην Αρχή Καταπολέμησης "
               "της Νομιμοποίησης Εσόδων (άρθρο 22)."),
            _p("Προσωπικά δεδομένα: τηρούνται για 5 έτη μετά τη λήξη της συνεργασίας, αποκλειστικά για τους σκοπούς του "
               "ν. 4557/2018 (άρθρο 30) και σύμφωνα με τον ΓΚΠΔ."),
            _p("Έγγραφα που προσκομίζονται:", bold=True), _p("${docs_list}"),
            _p(""), _p("Για τον πελάτη: ${client_name}" + (" — ο/η νόμιμος εκπρόσωπος ${rep_name}" if legal else "")),
            _p("Υπογραφή: ______________________"), _p(""), _p("Για το γραφείο: ${officer}"), _p("Υπογραφή: ______________________"),
        ]
    elif doc_type == "agreement":
        party = ("η εταιρεία ${client_name}, με ΑΦΜ ${client_afm}, ΔΟΥ ${client_doy}, έδρα ${client_address}, νόμιμα "
                 "εκπροσωπούμενη από ${rep_name} (ΑΦΜ ${rep_afm}, ${rep_role})") if legal else \
                ("ο/η ${client_name}, με ΑΦΜ ${client_afm}, ΔΟΥ ${client_doy}, κάτοικος ${client_address}")
        blocks = _office_header() + [
            _p("ΙΔΙΩΤΙΚΟ ΣΥΜΦΩΝΗΤΙΚΟ ΠΑΡΟΧΗΣ ΛΟΓΙΣΤΙΚΩΝ – ΦΟΡΟΤΕΧΝΙΚΩΝ ΥΠΗΡΕΣΙΩΝ", bold=True, size=14, center=True),
            _p("${city}, ${weekday} ${today}, μεταξύ:"),
            _p("Α) του γραφείου ${office_line1} (${office_line4}), εφεξής «το Γραφείο», και"),
            _p(f"Β) {party}, εφεξής «ο Εντολέας», συμφωνούνται τα εξής:"),
            _p("Άρθρο 1. Αντικείμενο", bold=True),
            _p("Το Γραφείο παρέχει τις λογιστικές και φοροτεχνικές υπηρεσίες: ________________________________"),
            _p("Άρθρο 2. Αμοιβή", bold=True),
            _p("Αμοιβή: ________ € ανά ________, πλέον ΦΠΑ. Τρόπος εξόφλησης: ${fee_payment}."),
            _p("Άρθρο 3. Υποχρεώσεις του Εντολέα", bold=True),
            _p("Ο Εντολέας παραδίδει εγκαίρως πλήρη και ακριβή στοιχεία και ενημερώνει για κάθε μεταβολή που επηρεάζει "
               "τις υποχρεώσεις του."),
        ] + [x for i, (title, text) in enumerate(ENGAGEMENT_CLAUSES, 4)
             for x in (_p(f"Άρθρο {i}. {title}", bold=True), _p(text))] + [
            _p(f"Άρθρο {4 + len(ENGAGEMENT_CLAUSES)}. Διάρκεια – καταγγελία", bold=True),
            _p("Η σύμβαση ισχύει από ${relationship_start} για αόριστο χρόνο και καταγγέλλεται από κάθε μέρος με έγγραφη "
               "προειδοποίηση 30 ημερών."),
            _p("Η παρούσα συντάχθηκε σε δύο αντίτυπα. ΥΠΟΔΕΙΓΜΑ — προσαρμόστε το ή ανεβάστε το δικό σας συμφωνητικό."),
            _p(""), _p("Για το Γραφείο                                        Ο Εντολέας"),
            _p("______________________                                ______________________"),
        ]
    else:
        blocks = _office_header() + [
            _p("ΕΣΩΤΕΡΙΚΗ ΑΞΙΟΛΟΓΗΣΗ ΚΙΝΔΥΝΟΥ ΠΕΛΑΤΗ", bold=True, size=16, center=True),
            _p("ΕΜΠΙΣΤΕΥΤΙΚΟ — μόνο για εσωτερική χρήση. Δεν γνωστοποιείται στον πελάτη ή σε τρίτους (άρθρο 27 ν. 4557/2018).",
               bold=True),
            _p("Αξιολόγηση: ${assessment_kind} — ${assessed_on} — Μοντέλο γραφείου ${risk_model}"),
        ] + _client_block(audience) + [
            _p("Δήλωση ΠΕΠ: ${pep}"),
            _p("Συναλλαγές / προέλευση κεφαλαίων", bold=True, size=12), _p("${tx_table}"),
            _p("Παράγοντες κινδύνου που συντρέχουν (Παραρτήματα Ι–ΙΙ)", bold=True, size=12), _p("${risk_factors}"),
            _p("Περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ»: ${risk_overrides}"),
            _p("Βαθμολογία: Μοντέλο Α ${risk_score_a} · Μοντέλο Β ${risk_score_b}"),
            _p("ΚΑΤΑΤΑΞΗ: ${risk_category} → ${risk_dd}", bold=True, size=12),
            _p("${risk_escalation}"), _p("${risk_notes}"),
            _p("Μέτρα δέουσας επιμέλειας", bold=True), _p("${risk_measures}"),
            _p("Αιτιολογία", bold=True), _p("${risk_justification}"),
            _p("Εκκρεμή έγγραφα: ${docs_missing}"),
            _p("Επόμενη επανεξέταση: ${next_review} (κάθε ${review_months} μήνες και εκτάκτως σε κάθε μεταβολή)"),
            _p(""), _p("Αξιολογητής: ${assessor}   Υπογραφή: ______________"),
            _p("Έγκριση ανώτερου στελέχους: ${approved_by}   Υπογραφή: ______________"),
        ]
    return _docx(blocks)
