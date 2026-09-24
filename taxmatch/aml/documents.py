"""Έγγραφα δέουσας επιμέλειας σε PDF (QTextDocument + QPdfWriter, όπως το εγχειρίδιο — καμία εξωτερική βιβλιοθήκη):

* Δήλωση παροχής στοιχείων πελάτη — προσυμπληρωμένη από όσα ξέρει η εφαρμογή, για υπογραφή από τον πελάτη.
* Ερωτηματολόγιο γνωριμίας πελάτη (KYC) — ανά είδος πελάτη, για να το συμπληρώσει ο πελάτης.
* Έκθεση αξιολόγησης κινδύνου — από αποθηκευμένη αξιολόγηση (υπογράφεται από τον αξιολογητή).
* Ρήτρες σύμβασης/επιστολής ανάθεσης για τον ν. 4557/2018 — ΥΠΟΔΕΙΓΜΑ προς έλεγχο από νομικό.
* Φάκελος υπόθεσης — εσωτερική καταγγελία ή αναφορά ύποπτης συναλλαγής, με χρονολόγιο και απόφαση.

Τα κείμενα είναι δικά μας (όχι αντιγραφή τρίτων προτύπων) με παραπομπές στα άρθρα του ν. 4557/2018. Ποτέ κωδικοί
TAXISnet ή άλλα μυστικά σε έγγραφα.
"""
from __future__ import annotations

import html
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .. import settings_store
from . import cases as cases_mod, model, store
from .content import (
    ASSESSMENT_LABEL, CASE_CHANNELS, CASE_KIND_LABEL, CASE_STATUS_LABEL, CLIENT_KIND_LABEL, FEE_PAYMENTS, KYC_ITEMS,
    LAW_VERSION, MEASURES, RED_FLAG_LABEL, TX_LABELS, UBO_LABEL, WHISTLE_CATEGORIES, documents_for, required_documents,
)

_CSS = """
body { font-family: 'Segoe UI', Arial, sans-serif; font-size: 10pt; color: #111; }
h1 { font-size: 16pt; margin: 0 0 2px 0; } h2 { font-size: 12pt; margin: 14px 0 4px 0; border-bottom: 1px solid #999; }
.muted { color: #555; font-size: 8.5pt; } table { border-collapse: collapse; width: 100%; }
td, th { border: 1px solid #999; padding: 4px 6px; vertical-align: top; } th { background: #e8eef4; text-align: left; }
.blank { height: 22px; } .box { border: 1px solid #999; padding: 6px; } .warn { color: #a00; font-weight: bold; }
"""
_BOX = "☐"
_CHECK = "☒"


def _e(value: Any) -> str:
    return html.escape(str(value or ""))


def _gr(iso: str) -> str:
    try:
        return date.fromisoformat(str(iso)[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return str(iso or "")


def _page(title: str, subtitle: str, body: str) -> str:
    return (f"<html><head><meta charset='utf-8'><style>{_CSS}</style></head><body>"
            f"<h1>{_e(title)}</h1><div class='muted'>{_e(subtitle)}</div>{body}"
            f"<p class='muted'>Εκδόθηκε {date.today():%d/%m/%Y} · {_e(LAW_VERSION)}</p></body></html>")


def _rows(pairs: Iterable[tuple[str, Any]]) -> str:
    return "<table>" + "".join(f"<tr><th width='34%'>{_e(k)}</th><td>{_e(v) or '&nbsp;'}</td></tr>" for k, v in pairs) + "</table>"


def _client(conn: sqlite3.Connection, afm: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    b = conn.execute("SELECT * FROM businesses WHERE afm=?", (afm,)).fetchone()
    b = dict(b) if b else {"afm": afm, "name": "", "legal_form": "", "address": "", "doy": "", "activity_state": "",
                           "kad_main_code": "", "kad_main_desc": ""}
    profile = store.get_profile(conn, afm)
    kind = store.effective_kind(profile, b.get("legal_form", ""), b.get("activity_state", ""))
    return b, profile, kind


# ------------------------------------------------------------------ Δήλωση παροχής στοιχείων
def declaration_html(conn: sqlite3.Connection, afm: str) -> str:
    b, p, kind = _client(conn, afm)
    legal = kind in ("company", "partnership", "public", "trust")
    pep_rows = "".join(f"<tr><td>{_CHECK if p['pep_status'] == key else _BOX} {_e(label)}</td></tr>"
                       for key, label in model.PEP_STATUS if key != "unknown")
    ubo = ("<h2>3. Πραγματικοί δικαιούχοι (φυσικά πρόσωπα με >25% ή έλεγχο — άρθρο 3 παρ. 17)</h2>"
           "<table><tr><th>Ονοματεπώνυμο</th><th>ΑΦΜ</th><th>Ιθαγένεια</th><th>Ποσοστό / τρόπος ελέγχου</th><th>ΠΕΠ;</th></tr>"
           + "<tr><td class='blank'></td><td></td><td></td><td></td><td></td></tr>" * 4 + "</table>"
           + (f"<p class='muted'>Σημείωση γραφείου: {_e(UBO_LABEL.get(p['ubo_state'], ''))} {_e(p['ubo_notes'])}</p>"
              if p["ubo_state"] or p["ubo_notes"] else "")) if legal else \
        "<h2>3. Πραγματικός δικαιούχος</h2><p>Δηλώνω ότι ενεργώ για δικό μου λογαριασμό και ότι πραγματικός δικαιούχος " \
        f"είμαι ο ίδιος/η ίδια. {_BOX} Ενεργώ για λογαριασμό τρίτου: ______________________________</p>"
    tx = "".join(f"<tr><td>{_e(TX_LABELS['amount'].get(t['amount'], ''))}</td><td>{_e(TX_LABELS['channel'].get(t['channel'], ''))}</td>"
                 f"<td>{_e(TX_LABELS['frequency'].get(t['frequency'], ''))}</td><td>{_e(TX_LABELS['country'].get(t['country'], ''))}</td></tr>"
                 for t in p["transactions"]) or "<tr><td class='blank'></td><td></td><td></td><td></td></tr>" * 2
    body = (
        "<p>Σύμφωνα με τον ν. 4557/2018 (άρθρα 13 και 18) το γραφείο οφείλει να ταυτοποιεί τους πελάτες του, να "
        "εξακριβώνει τους πραγματικούς δικαιούχους και να γνωρίζει τον σκοπό της συνεργασίας. Παρακαλούμε ελέγξτε, "
        "συμπληρώστε και υπογράψτε.</p>"
        "<h2>1. Στοιχεία πελάτη</h2>" + _rows([
            ("Επωνυμία / ονοματεπώνυμο", b["name"]), ("ΑΦΜ", b["afm"]), ("ΔΟΥ", b.get("doy", "")),
            ("Είδος", CLIENT_KIND_LABEL.get(kind, "")), ("Νομική μορφή", b.get("legal_form", "")),
            ("Διεύθυνση έδρας/κατοικίας", b.get("address", "")),
            ("Κύρια δραστηριότητα (ΚΑΔ)", f"{b.get('kad_main_code', '')} {b.get('kad_main_desc', '')}".strip()),
            ("Αρ. ταυτότητας/διαβατηρίου" + (" νόμιμου εκπροσώπου" if legal else ""), ""),
            ("Τηλέφωνο / email", ""),
        ])
        + ("<h2>2. Νόμιμος εκπρόσωπος</h2>" + _rows([("Ονοματεπώνυμο", ""), ("ΑΦΜ", ""), ("Ιδιότητα", ""),
                                                      ("Αρ. ταυτότητας/διαβατηρίου", "")]) if legal else "")
        + ubo
        + "<h2>4. Πολιτικώς εκτεθειμένο πρόσωπο (άρθρο 3 παρ. 9–11, άρθρο 18)</h2>"
          "<p>Είμαι (ή κάποιος πραγματικός δικαιούχος είναι) πολιτικώς εκτεθειμένο πρόσωπο, μέλος οικογένειας ή στενός "
          "συνεργάτης τέτοιου προσώπου:</p><table>" + pep_rows + "</table>"
        + "<h2>5. Σκοπός και φύση της συνεργασίας — προέλευση κεφαλαίων</h2>"
          f"<div class='box'>{_e(p['purpose']) or '<br><br>'}</div>"
          "<p>Κύρια ρεύματα συναλλαγών/εσόδων:</p><table><tr><th>Ποσό (ετήσιο)</th><th>Μέσο πληρωμής</th><th>Συχνότητα</th>"
          f"<th>Χώρα</th></tr>{tx}</table>"
          f"<p>Τρόπος εξόφλησης αμοιβής γραφείου: {_e(dict(FEE_PAYMENTS).get(p['fee_payment'], '')) or '________________'}</p>"
        + "<h2>6. Δεσμεύσεις</h2><p>Δηλώνω ότι τα στοιχεία είναι αληθή και πλήρη, ότι θα ενημερώνω το γραφείο για κάθε "
          "μεταβολή τους (ιδίως εκπροσώπησης, μετόχων/εταίρων και πραγματικών δικαιούχων) και ότι γνωρίζω πως, αν δεν "
          "παρασχεθούν τα στοιχεία που απαιτεί ο νόμος, το γραφείο δεν μπορεί να συνάψει ή να συνεχίσει τη συνεργασία "
          "(άρθρο 13 παρ. 2). Ενημερώθηκα ότι τα προσωπικά δεδομένα τηρούνται για 5 έτη μετά τη λήξη της συνεργασίας "
          "αποκλειστικά για την πρόληψη του ξεπλύματος (άρθρο 30 ν. 4557/2018 και ΓΚΠΔ).</p>"
          "<br><table><tr><td width='50%'>Τόπος / ημερομηνία<br><br></td><td>Υπογραφή (και σφραγίδα)<br><br><br></td></tr></table>"
    )
    return _page("ΔΗΛΩΣΗ ΠΑΡΟΧΗΣ ΣΤΟΙΧΕΙΩΝ ΠΕΛΑΤΗ", "Πρόληψη νομιμοποίησης εσόδων από εγκληματικές δραστηριότητες", body)


# ------------------------------------------------------------------ Ερωτηματολόγιο
_Q_COMMON = (
    "Ποιες υπηρεσίες ζητάτε από το γραφείο και για ποιον σκοπό;",
    "Ποια είναι η κύρια δραστηριότητα και από πού προέρχονται τα έσοδα/κεφάλαιά σας;",
    "Ποιο είναι το εκτιμώμενο ετήσιο ύψος εσόδων/συναλλαγών;",
    "Με ποιους τρόπους εισπράττετε και πληρώνετε (τράπεζα, κάρτα, μετρητά, κρυπτοστοιχεία, άλλο);",
    "Έχετε συναλλαγές με χώρες εκτός ΕΕ; Αν ναι, με ποιες και γιατί;",
    "Εσείς ή κάποιος συνδεδεμένος με εσάς είστε πολιτικώς εκτεθειμένο πρόσωπο, μέλος οικογένειας ή στενός συνεργάτης;",
    "Υπάρχουν πληρωμές από ή προς τρίτα πρόσωπα για λογαριασμό σας; Αν ναι, ποια σχέση έχετε μαζί τους;",
    "Έχει προηγηθεί συνεργασία με άλλο λογιστικό γραφείο; Για ποιο λόγο αλλάζετε;",
)
_Q_BY_KIND = {
    "company": ("Ποιοι είναι οι μέτοχοι/εταίροι και με ποια ποσοστά; Υπάρχουν νομικά πρόσωπα στη μετοχική σύνθεση;",
                "Υπάρχουν μετοχές στον κομιστή ή μέτοχοι για λογαριασμό τρίτων (nominees);",
                "Ανήκει η εταιρεία σε όμιλο; Έχει θυγατρικές/υποκαταστήματα στο εξωτερικό;"),
    "partnership": ("Ποιοι είναι οι εταίροι (ομόρρυθμοι/ετερόρρυθμοι) και με ποια ποσοστά;",
                    "Ποιος διαχειρίζεται και εκπροσωπεί την εταιρεία;"),
    "sole": ("Από πότε ασκείτε τη δραστηριότητα και σε ποιο χώρο;",
             "Εισπράττετε μετρητά από τους πελάτες σας; Περίπου τι ποσοστό των εσόδων;"),
    "individual": ("Ποια είναι η κύρια πηγή εισοδήματος (μισθός, σύνταξη, ενοίκια, επενδύσεις, άλλο);",
                   "Σκοπεύετε σε μεγάλες συναλλαγές (π.χ. αγορά/πώληση ακινήτου, δωρεά, γονική παροχή);"),
    "public": ("Ποια πράξη ορίζει τον εκπρόσωπο και το εύρος εξουσιοδότησής του;",),
    "trust": ("Ποιοι είναι ο ιδρυτής, ο διαχειριστής, ο προστάτης και οι δικαιούχοι;",
              "Ποιο δίκαιο διέπει τη σύσταση και πού τηρείται η περιουσία;"),
}


def questionnaire_html(conn: sqlite3.Connection, afm: str) -> str:
    b, p, kind = _client(conn, afm)
    qs = list(_Q_COMMON) + list(_Q_BY_KIND.get(kind, ()))
    body = (f"<p>Πελάτης: <b>{_e(b['name'])}</b> — ΑΦΜ {_e(b['afm'])} — {_e(CLIENT_KIND_LABEL.get(kind, ''))}</p>"
            "<p>Το ερωτηματολόγιο βοηθά το γραφείο να εκτιμήσει τον κίνδυνο της συνεργασίας, όπως ορίζει ο "
            "ν. 4557/2018 (άρθρο 13). Απαντήστε όσο πιο συγκεκριμένα γίνεται.</p>"
            + "".join(f"<h2>{i}. {_e(q)}</h2><div class='box'><br><br><br></div>" for i, q in enumerate(qs, 1))
            + "<h2>Έγγραφα που παρακαλούμε να προσκομίσετε</h2><table>"
            + "".join(f"<tr><td>{_CHECK if k in p['docs'] else _BOX} {_e(t)}"
                      f"{'' if k in required_documents(b.get('legal_form', ''), kind) else ' (αν ζητηθεί)'}</td></tr>"
                      for k, t in documents_for(b.get("legal_form", ""), kind))
            + "</table><br><table><tr><td width='50%'>Ημερομηνία<br><br></td><td>Υπογραφή<br><br><br></td></tr></table>")
    return _page("ΕΡΩΤΗΜΑΤΟΛΟΓΙΟ ΓΝΩΡΙΜΙΑΣ ΠΕΛΑΤΗ (KYC)", "Δέουσα επιμέλεια — ν. 4557/2018", body)


# ------------------------------------------------------------------ Έκθεση αξιολόγησης
def assessment_html(conn: sqlite3.Connection, assessment_id: int) -> str:
    a = store.get_assessment(conn, assessment_id)
    if not a:
        raise KeyError(assessment_id)
    b, p, kind = _client(conn, a["afm"])
    res = model.compute(a["factors"], a["overrides"])
    chosen = set(a["factors"])
    rows = []
    for axis, label in model.AXES:
        rows.append(f"<tr><th colspan='4'>{_e(label)} — Μοντέλο Β: {res.subs_b.get(axis, 0)}/25</th></tr>")
        for f in model.FACTORS:
            if f.axis == axis and f.key in chosen:
                rows.append(f"<tr><td>{_e(f.text)}</td><td>{_e(f.ref)}</td><td>{f.a:+d}</td>"
                            f"<td>{'—' if f.b is None else f.b}</td></tr>")
    ovs = "".join(f"<li>{_e(model.OVERRIDES_BY_KEY[o].text)} ({_e(model.OVERRIDES_BY_KEY[o].ref)})</li>" for o in a["overrides"])
    missing = store.missing_documents(p, b.get("legal_form", ""), b.get("activity_state", ""))
    doc_labels = dict(documents_for(b.get("legal_form", ""), kind))
    cat = a["final_category"]
    body = (
        _rows([("Πελάτης", a["client_name"] or b["name"]), ("ΑΦΜ", a["afm"]), ("Είδος πελάτη", CLIENT_KIND_LABEL.get(kind, "")),
               ("Είδος αξιολόγησης", ASSESSMENT_LABEL.get(a["kind"], "")), ("Ημερομηνία", _gr(a["assessed_on"])),
               ("ΠΕΠ", model.PEP_LABEL.get(p["pep_status"], "")),
               ("Πραγματικοί δικαιούχοι", f"{UBO_LABEL.get(p['ubo_state'], '')} {p['ubo_notes']}".strip())])
        + f"<h2>Αποτέλεσμα: {_e(model.CATEGORY_LABEL[cat])} → {_e(model.DD_LABEL[cat])}</h2>"
        f"<p>Μοντέλο Α: {res.sum_a:+d} ({_e(model.CATEGORY_LABEL[res.cat_a])}) · Μοντέλο Β: {res.total_b}/100 "
        f"({_e(model.CATEGORY_LABEL[res.cat_b])}) · Μοντέλο γραφείου: {a['model']}"
        + (f"<br><span class='warn'>Η κατάταξη αυξήθηκε από τον αξιολογητή: {_e(a['escalation_note'])}</span>"
           if a["escalation_note"] else "") + "</p>"
        + (f"<p><b>Περιπτώσεις «ΥΠΕΡΙΣΧΥΕΙ»:</b></p><ul>{ovs}</ul>" if ovs else "")
        + "<h2>Παράγοντες που συντρέχουν</h2><table><tr><th>Παράγοντας</th><th>Διάταξη</th><th>Α</th><th>Β</th></tr>"
        + ("".join(rows) or "<tr><td colspan='4'>—</td></tr>") + "</table>"
        + "".join(f"<p class='warn'>{_e(n)}</p>" for n in res.notes)
        + f"<h2>Μέτρα δέουσας επιμέλειας</h2><p>{_e(MEASURES[cat])}</p>"
        + f"<h2>Αιτιολογία</h2><div class='box'>{_e(a['justification']) or '&nbsp;'}</div>"
        + "<h2>Πληρότητα φακέλου</h2><p>" + ("Όλα τα υποχρεωτικά έγγραφα έχουν παραληφθεί." if not missing else
                                              "Εκκρεμούν: " + _e(", ".join(doc_labels.get(k, k) for k in missing)))
        + f"</p><p>Επόμενη επανεξέταση: <b>{_gr(a['next_review'])}</b></p>"
        + f"<br><table><tr><td width='50%'>Αξιολογητής: {_e(a['assessor'])}<br><br>Υπογραφή:</td>"
          f"<td>Έγκριση ανώτερου στελέχους: {_e(a['approved_by'])}<br><br>Υπογραφή:</td></tr></table>"
    )
    return _page("ΕΚΘΕΣΗ ΑΞΙΟΛΟΓΗΣΗΣ ΚΙΝΔΥΝΟΥ ΠΕΛΑΤΗ", "Εσωτερικό έγγραφο — ατομικός φάκελος πελάτη", body)


# ------------------------------------------------------------------ Ρήτρες σύμβασης
#: Ρήτρες ν. 4557/2018 για σύμβαση/επιστολή ανάθεσης — χρησιμοποιούνται στο PDF και στο προεπιλεγμένο πρότυπο Word.
ENGAGEMENT_CLAUSES = (
    ("Ταυτοποίηση", "Ο εντολέας παρέχει, πριν από την έναρξη και καθ' όλη τη διάρκεια της συνεργασίας, τα στοιχεία "
     "και έγγραφα ταυτοποίησης του ίδιου, των εκπροσώπων και των πραγματικών δικαιούχων του (άρθρα 13–14 ν. 4557/2018)."),
    ("Επικαιροποίηση", "Ο εντολέας ενημερώνει εγγράφως το γραφείο για κάθε μεταβολή των στοιχείων αυτών εντός 30 "
     "ημερών και προσκομίζει επικαιροποιημένα στοιχεία όταν του ζητηθούν (άρθρο 13 παρ. 7)."),
    ("Προέλευση κεφαλαίων", "Ο εντολέας παρέχει, όταν ζητηθεί, πληροφορίες για τον σκοπό και την οικονομική "
     "αιτιολογία συναλλαγών και για την προέλευση κεφαλαίων (άρθρα 13 παρ. 1γ–δ και 16)."),
    ("Μη παροχή στοιχείων", "Αν δεν παρασχεθούν τα στοιχεία που απαιτεί ο νόμος, το γραφείο δικαιούται να μη συνάψει "
     "ή να καταγγείλει αζημίως τη σύμβαση (άρθρο 13 παρ. 2)."),
    ("Νόμιμες υποχρεώσεις γραφείου", "Ο εντολέας αναγνωρίζει ότι το γραφείο είναι υπόχρεο πρόσωπο του ν. 4557/2018 "
     "και εκπληρώνει τις νόμιμες υποχρεώσεις του, που δεν συνιστούν παραβίαση του επαγγελματικού απορρήτου ή της "
     "σύμβασης (άρθρο 22)."),
    ("Προσωπικά δεδομένα", "Τα δεδομένα που συλλέγονται για τη δέουσα επιμέλεια τηρούνται για 5 έτη μετά τη λήξη της "
     "συνεργασίας και χρησιμοποιούνται μόνο για την πρόληψη του ξεπλύματος (άρθρο 30 και ΓΚΠΔ)."),
    ("Εξόφληση αμοιβής", "Η αμοιβή εξοφλείται μέσω τραπεζικού λογαριασμού του ίδιου του εντολέα· πληρωμή από τρίτο "
     "πρόσωπο γίνεται δεκτή μόνο με προηγούμενη έγγραφη αιτιολόγηση."),
)


def engagement_clauses_html(conn: sqlite3.Connection, afm: str) -> str:
    b, _p, _kind = _client(conn, afm)
    body = (f"<p>Εντολέας: <b>{_e(b['name'])}</b> — ΑΦΜ {_e(b['afm'])}. Υπόδειγμα ρητρών για ενσωμάτωση στη σύμβαση ή "
            "την επιστολή ανάθεσης λογιστικών-φοροτεχνικών υπηρεσιών.</p>"
            + "".join(f"<h2>Άρθρο {i}. {_e(t)}</h2><p>{_e(c)}</p>" for i, (t, c) in enumerate(ENGAGEMENT_CLAUSES, 1))
            + "<p class='warn'>ΥΠΟΔΕΙΓΜΑ — να ελεγχθεί από νομικό σύμβουλο πριν χρησιμοποιηθεί.</p>"
            + "<br><table><tr><td width='50%'>Για το γραφείο<br><br><br></td><td>Ο εντολέας<br><br><br></td></tr></table>")
    return _page("ΡΗΤΡΕΣ ΣΥΜΒΑΣΗΣ — ΠΡΟΛΗΨΗ ΞΕΠΛΥΜΑΤΟΣ (ν. 4557/2018)", "Παράρτημα σύμβασης/επιστολής ανάθεσης", body)


# ------------------------------------------------------------------ Φάκελος υπόθεσης
def case_file_html(conn: sqlite3.Connection, case_id: int) -> str:
    c = cases_mod.get(conn, case_id)
    if not c:
        raise KeyError(case_id)
    channel = dict(CASE_CHANNELS).get(c["channel"], "")
    rows: list[tuple[str, Any]] = [("Αρ. υπόθεσης", c["id"]), ("Είδος", CASE_KIND_LABEL.get(c["kind"], "")),
                                   ("Παραλαβή/εντοπισμός", _gr(c["received_on"])), ("Τρόπος", channel),
                                   ("Υπεύθυνος χειρισμού", c["handler"]),
                                   ("Κατάσταση", CASE_STATUS_LABEL.get(c["status"], ""))]
    if c["kind"] == "whistle":
        rows += [("Κατηγορία παράβασης", dict(WHISTLE_CATEGORIES).get(c["category"], "")),
                 ("Καταγγέλλων", "ΑΝΩΝΥΜΗ" if c["anonymous"] else c["reporter"]),
                 ("Βεβαίωση παραλαβής", f"{_gr(c['ack_on']) or 'όχι ακόμη'} (προθεσμία {_gr(c.get('ack_due', ''))})"),
                 ("Ενημέρωση καταγγέλλοντος", f"{_gr(c['feedback_on']) or 'όχι ακόμη'} (προθεσμία {_gr(c.get('feedback_due', ''))})")]
        if c["afm"]:
            rows.append(("Σχετικός πελάτης", f"{c['client_name']} ({c['afm']})"))
    else:
        rows += [("Πελάτης", f"{c['client_name']} ({c['afm']})"), ("Αναφέρων υπάλληλος", c["reporter"]),
                 ("Συναλλαγή", c["tx_description"]), ("Ποσό", c["tx_amount"]), ("Ημ/νία συναλλαγής", _gr(c["tx_date"]))]
    flags = "".join(f"<li>{_e(RED_FLAG_LABEL.get(f, f))}</li>" for f in c["red_flags"])
    history = ""
    if c["afm"]:
        latest = store.latest(conn, c["afm"])
        if latest:
            history = (f"<h2>Προφίλ κινδύνου πελάτη</h2><p>Τελευταία αξιολόγηση {_gr(latest['assessed_on'])}: "
                       f"<b>{_e(model.CATEGORY_LABEL[latest['final_category']])}</b> — "
                       f"{_e(model.DD_LABEL[latest['final_category']])}.</p>")
    decision = cases_mod.outcome_label(c)
    body = (
        "<p class='warn'>ΕΜΠΙΣΤΕΥΤΙΚΟ" + (" — απαγορεύεται η γνωστοποίηση στον πελάτη ή σε τρίτους (άρθρο 27 ν. 4557/2018)."
                                          if c["kind"] == "suspicion" else
                                          " — προστασία ταυτότητας καταγγέλλοντος και απαγόρευση αντιποίνων (ν. 4990/2022).")
        + "</p>" + _rows(rows)
        + f"<h2>Περιγραφή</h2><div class='box'>{_e(c['description'])}</div>"
        + (f"<h2>Ενδείξεις (red flags)</h2><ul>{flags}</ul>" if flags else "")
        + history
        + f"<h2>Ενέργειες διερεύνησης</h2><div class='box'>{_e(c['actions']) or '&nbsp;'}</div>"
        + f"<h2>{'Απόφαση για αναφορά στην Αρχή' if c['kind'] == 'suspicion' else 'Αποτέλεσμα'}</h2>"
        + _rows([("Απόφαση", decision), ("Ημερομηνία απόφασης", _gr(c["decided_on"])), ("Αιτιολογία", c["decision_reason"])]
                + ([("Αρ. πρωτοκόλλου Αρχής", c["authority_ref"])] if c["kind"] == "suspicion" else []))
        + ("<p class='muted'>Η αναφορά υποβάλλεται στην Αρχή Καταπολέμησης της Νομιμοποίησης Εσόδων (Μονάδα Α' — FIU) με "
           "τον τρόπο που ορίζει η Αρχή, αμελλητί, πριν από την εκτέλεση της συναλλαγής όπου είναι εφικτό (άρθρο 22). "
           "Τηρείται 5 έτη μαζί με τα συνοδευτικά έγγραφα (άρθρο 30).</p>" if c["kind"] == "suspicion" else "")
        + "<br><table><tr><td width='50%'>Υπεύθυνος συμμόρφωσης<br><br><br></td><td>Ημερομηνία<br><br><br></td></tr></table>"
    )
    title = "ΦΑΚΕΛΟΣ ΕΣΩΤΕΡΙΚΗΣ ΑΝΑΦΟΡΑΣ ΥΠΟΠΤΗΣ ΣΥΝΑΛΛΑΓΗΣ" if c["kind"] == "suspicion" else "ΦΑΚΕΛΟΣ ΕΣΩΤΕΡΙΚΗΣ ΚΑΤΑΓΓΕΛΙΑΣ"
    return _page(title, f"Υπόθεση #{c['id']} — {settings_store.get(conn, 'aml_officer') or ''}", body)


# ------------------------------------------------------------------ PDF
def write_pdf(html_text: str, target: Path, title: str) -> Path:
    """Απαιτεί ενεργό QGuiApplication (η εφαρμογή το έχει πάντα· στα tests αρκεί offscreen)."""
    from PySide6.QtCore import QMarginsF, QSizeF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    writer = QPdfWriter(str(target))
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    writer.setPageMargins(QMarginsF(16, 14, 16, 14), QPageLayout.Unit.Millimeter)
    writer.setTitle(title)
    writer.setCreator("TaxMatch by ScanMyData")
    writer.setResolution(96)
    doc = QTextDocument()
    doc.setHtml(html_text)
    doc.setPageSize(QSizeF(writer.pageLayout().paintRectPixels(writer.resolution()).size()))
    doc.print_(writer)
    return target
