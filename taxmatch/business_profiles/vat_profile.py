"""Ερμηνεία των tags του Μητρώου ΑΑΔΕ σε καθεστώς ΦΠΑ / κατηγορία βιβλίων / περίοδο ΦΠΑ.

Port της λογικής `_ar_detect_vat_profile_for_afm` + `_ar_books_category_letter` του app.py του ScanMyData
(εκεί επαληθεύτηκε σε πραγματικές εταιρείες — βλ. σχόλια στον αρχικό κώδικα). Αντί για JSON store
αποθηκεύει σε στήλες του πίνακα `businesses`.
"""
from __future__ import annotations

from typing import Optional

from ..textutil import strip_accents

_BOOKS_TO_PERIOD = {"Β": "quarterly", "Γ": "monthly"}


def books_category_letter(raw: Optional[str]) -> str:
    """'Γ-ΔΙΠΛΟΓΡΑΦΙΚΑ' / 'Β-ΑΠΛΟΓΡΑΦΙΚΑ ΜΕ ΜΗΝΙΑΙΑ ΠΕΡΙΟΔΟ ΦΠΑ' / 'G' / 'b' -> 'Β' | 'Γ' | ''."""
    s = str(raw or "").strip().upper()
    if s.startswith(("Γ", "G")):
        return "Γ"
    if s.startswith(("Β", "B")):
        return "Β"
    return ""


def interpret_vat_profile(all_tags: dict) -> dict:
    """{vat_subject: True|False|None, books_category: 'Β'|'Γ'|'', books_category_raw, vat_period_type}."""
    ypagwgh = str(all_tags.get("ypagwghfpa") or "").strip().upper()
    vat_subject: Optional[bool] = True if ypagwgh == "NAI" else (False if ypagwgh == "OXI" else None)
    # Το «Υπαγωγή ΦΠΑ: ΝΑΙ» μόνο του παραπλανά: επιχειρήσεις με καθεστώς απαλλαγής ή ειδικό καθεστώς μικρών
    # επιχειρήσεων φέρουν ΝΑΙ αλλά ΔΕΝ είναι πρακτικά υπόχρεες. Το όνομα του tag «Καθεστώς ΦΠΑ» δεν είναι
    # επιβεβαιωμένο, οπότε ψάχνουμε στις ΤΙΜΕΣ όλων των tags.
    regime_text = strip_accents(" ".join(str(v) for v in all_tags.values())).upper()
    if "ΑΠΑΛΛΑΣΣΟΜΕΝ" in regime_text or "ΜΙΚΡΩΝ ΕΠΙΧΕΙΡΗΣΕΩΝ" in regime_text:
        vat_subject = False

    raw = str(all_tags.get("kathgoriabibliwn") or "").strip()
    letter = books_category_letter(raw)
    upper = strip_accents(raw).upper()
    # Το κείμενο της ΑΑΔΕ δηλώνει συχνά την περίοδο ΦΠΑ (μια Β κατηγορία μπορεί να είναι ΜΗΝΙΑΙΑ), οπότε
    # διαβάζεται πρώτο. ΤΡΙΜΗΝ πριν από ΜΗΝΙΑ: το «ΤΡΙΜΗΝΙΑΙΑ» περιέχει το «ΜΗΝΙΑ».
    if "ΤΡΙΜΗΝ" in upper:
        period = "quarterly"
    elif "ΜΗΝΙΑ" in upper:
        period = "monthly"
    else:
        period = _BOOKS_TO_PERIOD.get(letter, "")
    return {"vat_subject": vat_subject, "books_category": letter, "books_category_raw": raw, "vat_period_type": period}
