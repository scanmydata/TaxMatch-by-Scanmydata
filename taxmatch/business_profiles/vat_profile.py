"""Ερμηνεία των tags του Μητρώου ΑΑΔΕ σε καθεστώς ΦΠΑ / κατηγορία βιβλίων / περίοδο ΦΠΑ.

Port της λογικής `_ar_detect_vat_profile_for_afm` + `_ar_books_category_letter` του app.py του ScanMyData
(εκεί επαληθεύτηκε σε πραγματικές εταιρείες — βλ. σχόλια στον αρχικό κώδικα). Αντί για JSON store
αποθηκεύει σε στήλες του πίνακα `businesses`.
"""
from __future__ import annotations

from typing import Optional

from ..textutil import strip_accents

_BOOKS_TO_PERIOD = {"Β": "quarterly", "Γ": "monthly"}


#: Το Μητρώο γράφει συχνά με ΛΑΤΙΝΙΚΑ γράμματα που μοιάζουν ελληνικά (ζωντανά, Οκτ. 2026: `<ypagwghfpa>NAI</ypagwghfpa>`
#: με λατινικά N, A, I — βλ. ScanMyData Tax Center `aade-profile.js`). Χωρίς αυτόν τον πίνακα το «ΝΑΙ» δεν ταιριάζει.
_LATIN_LOOKALIKE = str.maketrans({"A": "Α", "B": "Β", "E": "Ε", "H": "Η", "I": "Ι", "K": "Κ", "M": "Μ", "N": "Ν", "O": "Ο",
                                  "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ", "Z": "Ζ"})


def _up(value) -> str:
    """Κεφαλαία χωρίς τόνους, με τα λατινικά «ομοιώματα» σε ελληνικά — για συγκρίσεις κειμένου του Μητρώου."""
    return strip_accents(str(value or "")).strip().upper().translate(_LATIN_LOOKALIKE)


def books_category_letter(raw: Optional[str]) -> str:
    """'Γ-ΔΙΠΛΟΓΡΑΦΙΚΑ' / 'Β-ΑΠΛΟΓΡΑΦΙΚΑ ΜΕ ΜΗΝΙΑΙΑ ΠΕΡΙΟΔΟ ΦΠΑ' / 'G' / 'b' -> 'Β' | 'Γ' | ''."""
    s = str(raw or "").strip().upper()
    if s.startswith(("Γ", "G")) or "ΔΙΠΛΟΓΡΑΦ" in _up(s):
        return "Γ"
    if s.startswith(("Β", "B")) or "ΑΠΛΟΓΡΑΦ" in _up(s):
        return "Β"
    return ""


def interpret_vat_profile(all_tags: dict) -> dict:
    """{vat_subject: True|False|None, books_category: 'Β'|'Γ'|'', books_category_raw, vat_period_type}.

    Ίδια ερμηνεία με το ScanMyData Tax Center (`aade-profile.js: vatProfile`, 2026-10): απαλλαγή μικρών επιχειρήσεων,
    απαλλασσόμενες δραστηριότητες ή ειδικό καθεστώς αγροτών ⇒ ΜΗ υπόχρεος ακόμη κι αν η «υπαγωγή» γράφει ΝΑΙ· περίοδος
    από το κείμενο της κατηγορίας βιβλίων (ΤΡΙΜΗΝ πριν από ΜΗΝΙΑ: το «ΤΡΙΜΗΝΙΑΙΑ» περιέχει το «ΜΗΝΙΑ»), αλλιώς
    Γ = μηνιαία, Β = τριμηνιαία. Οι πραγματικές περίοδοι Φ2 από τον έλεγχο υποβολών (easycheck) υπερισχύουν όλων."""
    flag = _up(all_tags.get("ypagwghfpa"))
    vat_subject: Optional[bool] = True if flag == "ΝΑΙ" else (False if flag == "ΟΧΙ" else None)
    # Το «Καθεστώς ΦΠΑ» (`kathestwsfpa`, επαληθευμένο στο Tax Center)· αν λείπει, όλες οι τιμές (παλιά συμπεριφορά).
    regime = _up(all_tags.get("kathestwsfpa") if all_tags.get("kathestwsfpa") else " ".join(str(v) for v in all_tags.values()))
    if "ΑΠΑΛΛΑΣΣΟΜΕΝ" in regime or "ΜΙΚΡΩΝ ΕΠΙΧΕΙΡΗΣΕΩΝ" in regime or "ΑΓΡΟΤ" in regime:
        vat_subject = False

    raw = str(all_tags.get("kathgoriabibliwn") or "").strip()
    letter = books_category_letter(raw)
    upper = _up(raw)
    period = ""
    if vat_subject is not False:
        if "ΤΡΙΜΗΝ" in upper:
            period = "quarterly"
        elif "ΜΗΝΙΑ" in upper:
            period = "monthly"
        else:
            period = _BOOKS_TO_PERIOD.get(letter, "")
    return {"vat_subject": vat_subject, "books_category": letter, "books_category_raw": raw, "vat_period_type": period}
