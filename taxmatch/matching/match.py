"""Matching engine: scope άρθρου × προφίλ επιχείρησης. Καθαρές συναρτήσεις, χωρίς I/O.

Κανόνες:
* scope "all"            -> ταιριάζει σε όλους (confidence 1.0).
* scope "targeted"       -> ΚΑΘΕ κριτήριο που δηλώνεται πρέπει να ικανοποιείται (AND)·
                            μέσα σε κριτήριο, ταιριάζει οποιαδήποτε τιμή (OR).
* κριτήριο χωρίς στοιχεία στην επιχείρηση (π.χ. λείπει η κατηγορία βιβλίων) είναι «άγνωστο»:
  αν τα υπόλοιπα ταιριάζουν, επιστρέφεται match με confidence 0.5 και σημείωση να επιβεβαιωθεί·
  αν δεν ταιριάζει κανένα κριτήριο με βεβαιότητα, ΔΕΝ γίνεται match.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from ..identifiers import format_kad, kad_matches
from ..textutil import strip_accents

# Πλήρης ονομασία -> συντομογραφία (ώστε «Ι.Κ.Ε.» και «Ιδιωτική Κεφαλαιουχική Εταιρεία» να ταυτίζονται)
_LEGAL_FORM_LONG = {
    "ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ": "ΙΚΕ",
    "ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ": "ΑΕ",
    "ΕΤΑΙΡΕΙΑ ΠΕΡΙΟΡΙΣΜΕΝΗΣ ΕΥΘΥΝΗΣ": "ΕΠΕ",
    "ΟΜΟΡΡΥΘΜΗ": "ΟΕ",
    "ΕΤΕΡΟΡΡΥΘΜΗ": "ΕΕ",
    "ΑΤΟΜΙΚΗ": "ΑΤΟΜΙΚΗ",
    "ΜΟΝΟΠΡΟΣΩΠΗ": "ΜΟΝΟΠΡΟΣΩΠΗ",
    "ΚΟΙΝΩΝΙΚΗ ΣΥΝΕΤΑΙΡΙΣΤΙΚΗ": "ΚΟΙΝΣΕΠ",
    "ΑΣΤΙΚΗ ΜΗ ΚΕΡΔΟΣΚΟΠΙΚΗ": "ΑΜΚΕ",
}


_strip_accents = strip_accents


def legal_form_tokens(text: str) -> set[str]:
    """Σύνολο κανονικοποιημένων tokens νομικής μορφής (κεφαλαία, χωρίς τόνους/τελείες)."""
    t = _strip_accents(str(text or "").upper())
    tokens: set[str] = set()
    for long_name, short in _LEGAL_FORM_LONG.items():
        if long_name in t:
            tokens.add(short)
    compact = re.sub(r"[.\s]+", "", t)  # 'Ι.Κ.Ε.' -> 'ΙΚΕ'
    for word in re.split(r"[^A-ZΑ-Ω]+", t):
        if word:
            tokens.add(word)
    tokens.add(compact)
    return tokens


@dataclass
class Match:
    reason: str
    confidence: float


def _criterion_kad(prefixes: list[str], business: dict[str, Any]) -> tuple[Optional[bool], str]:
    codes = [c for c in (business.get("kads") or []) if c]
    if not codes:
        return None, "ΚΑΔ"
    for code in codes:
        for p in prefixes:
            if kad_matches(code, p):
                return True, f"ΚΑΔ {format_kad(code)} (πρόθεμα {format_kad(p)})"
    return False, "ΚΑΔ"


def _criterion_books(cats: list[str], business: dict[str, Any]) -> tuple[Optional[bool], str]:
    cat = (business.get("books_category") or "").strip()
    if not cat:
        return None, "κατηγορία βιβλίων"
    return (cat in cats), f"Κατηγορία βιβλίων {cat}"


def _criterion_vat(want: bool, business: dict[str, Any]) -> tuple[Optional[bool], str]:
    v = business.get("vat_subject")
    if v is None:
        return None, "καθεστώς ΦΠΑ"
    return (bool(v) == want), ("Υπόχρεος ΦΠΑ" if v else "Μη υπόχρεος ΦΠΑ")


def _criterion_form(forms: list[str], business: dict[str, Any]) -> tuple[Optional[bool], str]:
    lf = (business.get("legal_form") or "").strip()
    if not lf:
        return None, "νομική μορφή"
    have = legal_form_tokens(lf)
    for f in forms:
        if f in have or re.sub(r"[.\s]+", "", _strip_accents(f)) in have:
            return True, f"Νομική μορφή {lf}"
    return False, "νομική μορφή"


def match_business(extracted: dict[str, Any], business: dict[str, Any]) -> Optional[Match]:
    """`extracted` = κανονικοποιημένο JSON (scope.normalize), `business` = {kads, books_category,
    vat_subject, legal_form, …}. Επιστρέφει Match ή None."""
    if not extracted.get("relevant", True):
        return None
    scope = extracted.get("scope") or {}
    if scope.get("type", "all") == "all":
        return Match("Αφορά όλες τις επιχειρήσεις", 1.0)

    results: list[tuple[Optional[bool], str]] = []
    if scope.get("kad_prefixes"):
        results.append(_criterion_kad(scope["kad_prefixes"], business))
    if scope.get("books_categories"):
        results.append(_criterion_books(scope["books_categories"], business))
    if scope.get("vat_subject") is not None:
        results.append(_criterion_vat(scope["vat_subject"], business))
    if scope.get("legal_forms"):
        results.append(_criterion_form(scope["legal_forms"], business))

    if not results or any(r is False for r, _ in results):
        return None
    confirmed = [label for r, label in results if r is True]
    unknown = [label for r, label in results if r is None]
    if not confirmed:
        return None
    reason = " · ".join(confirmed)
    if unknown:
        return Match(f"{reason} — δεν υπάρχουν στοιχεία: {', '.join(unknown)} (να επιβεβαιωθεί)", 0.5)
    return Match(reason, 1.0)
