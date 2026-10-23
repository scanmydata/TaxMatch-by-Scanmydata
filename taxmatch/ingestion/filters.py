"""Φθηνά φίλτρα ΠΡΙΝ το LLM: λέξεις-κλειδιά για γενικά portals και εντοπισμός ίδιου θέματος από άλλη πηγή."""
from __future__ import annotations

import re
from typing import Iterable, Optional

from ..textutil import strip_accents

# Ρίζες (χωρίς τόνους, μικρά), ταιριάζουν ΜΟΝΟ στην αρχή λέξης. Ένα ψευδώς θετικό κοστίζει μία κλήση LLM, ένα ψευδώς
# αρνητικό χάνεται — αλλά (αναφορά χρήστη 2026-10-23, «καλύτερο φιλτράρισμα») οι παλιές ρίζες ήταν τόσο ευρείες που
# περνούσαν πολιτικά/αστυνομικά: το «ικα» ταίριαζε ΜΕΣΑ στο «πολιτικα»/«δικαστικα», το «δηλωσ» σε κάθε δήλωση πολιτικού,
# το «επιχειρησ» σε κάθε επιχείρηση της ΕΛ.ΑΣ. Τώρα: ΙΣΧΥΡΟΙ όροι (λογιστική/φορολογική/μισθοδοτική διαχείριση
# επιχείρησης) — ένας αρκεί· ΑΔΥΝΑΜΟΙ — χρειάζονται δύο διαφορετικοί· ΑΡΝΗΤΙΚΟΙ στον τίτλο (κύκλωμα, κατάσχεση,
# ποδόσφαιρο…) — απορρίπτουν εκτός αν υπάρχουν δύο ισχυροί.
STRONG_STEMS = (
    "φορολογ", "φορος", "φορου", "φορων", "φοροι", "φπα", "φ π α", "ααδε", "α α δ ε", "mydata", "my data", "εφκα", "efka",
    "εργανη", "ενφια", "λογιστ", "τιμολογ", "γεμη", "εγκυκλι", "παρακρατ", "ταμειακ", "vies", "intrastat", "κφδ", "κφε",
    "κφπα", "ισολογισμ", "χαρτοσημ", "τελος επιτηδευμ", "προκαταβολ", "ρυθμιση οφειλ", "120 δοσ", "72 δοσ", "εισφορ",
    "ασφαλιστικ", "μισθοδοσ", "κατωτατος μισθ", "κατωτατου μισθ", "ηλεκτρονικα βιβλια", "ψηφιακ πελατολογ",
    "ψηφιακ δελτι", "ψηφιακο τελος", "τελωνει", "κωδικοι αριθμοι δραστηριοτ", "εργοδοτ", "υπερωρ",
    "συλλογικ συμβασ", "τηλεργασ", "αναγγελι", "προσληψ", "απολυσ", "δωρο χριστουγ", "δωρο πασχα", "επιδομα αδειας",
    "οικονομικες καταστασ", "μερισμ", "αποσβεσ", "υποχρεοι", "e invoicing",
    "ηλεκτρονικη τιμολογ", "ηλεκτρονικης τιμολογ",
)
WEAK_STEMS = (
    "δηλωσ", "προστιμ", "παραταση", "προθεσμ", "επιχειρησ", "επιχειρηματ", "οφειλ", "πληρωμ", "επιδοτ", "επιχορηγ",
    "μισθωσ", "ενοικι", "αδει", "ενσημ", "αφμ", "καδ", "επαγγελματ", "αποδειξ", "πλατφορμ", "εργασιακ", "εργατικ",
    "ωραρι", "ικα", "εργαζομεν", "κωδικ", "ελεγχ", "κεφαλαιακ", "εκκαθαρισ", "νομοσχεδι", "τροπολογ", "υπουργικ αποφασ", "φεκ",
)
NEGATIVE_TITLE_STEMS = (
    "κυκλωμα", "κατασχεσ", "λαθρεμπορ", "ναρκωτ", "συλληψ", "συνεληφθ", "εξαρθρω", "ποδοσφ", "μπασκετ", "τροχαι",
    "δολοφον", "σεισμ", "πυρκαγι",
)
# συμβατότητα: όλες οι θετικές ρίζες
KEYWORD_STEMS = STRONG_STEMS + WEAK_STEMS


def _stem_re(stems: tuple[str, ...]) -> re.Pattern:
    # «ψηφιακ πελατολογ» = ρίζα + ρίζα: η πρώτη λέξη μπορεί να συνεχίζει («ψηφιακο πελατολογιο»)
    parts = ["[0-9a-zα-ω]* ".join(re.escape(w) for w in x.split()) for x in stems]
    return re.compile(r"(?<![0-9a-zα-ω])(" + "|".join(parts) + ")")


_STRONG_RE, _WEAK_RE, _NEG_RE = _stem_re(STRONG_STEMS), _stem_re(WEAK_STEMS), _stem_re(NEGATIVE_TITLE_STEMS)
_STEM_RE = _stem_re(KEYWORD_STEMS)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zα-ω]+", " ", strip_accents(text or "").lower())).strip()


def relevance(title: str, summary: str = "") -> dict[str, list[str]]:
    """Ποιοι όροι βρέθηκαν: {'strong', 'weak', 'negative'} (για αιτιολόγηση/tests)."""
    text = normalize_text(f"{title} {summary}")
    return {"strong": sorted(set(_STRONG_RE.findall(text))), "weak": sorted(set(_WEAK_RE.findall(text))),
            "negative": sorted(set(_NEG_RE.findall(normalize_text(title))))}


def is_tax_relevant(title: str, summary: str = "") -> bool:
    r = relevance(title, summary)
    if r["negative"] and len(r["strong"]) < 2:
        return False
    return bool(r["strong"]) or len(r["weak"]) >= 2


def title_key(title: str) -> str:
    return normalize_text(title)


def _tokens(key: str) -> frozenset[str]:
    return frozenset(t for t in key.split() if len(t) > 2)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def find_duplicate(key: str, recent: Iterable[tuple[int, str]], threshold: float = 0.8, min_tokens: int = 4) -> Optional[int]:
    """`recent`: (id, title_key) πρόσφατων άρθρων. Επιστρέφει το id του πρώτου ίδιου θέματος: ίδιο key ή Jaccard ≥ threshold
    (μόνο για τίτλους με αρκετές λέξεις, ώστε να μη ταυτίζονται οι σύντομοι/γενικοί τίτλοι)."""
    if not key:
        return None
    toks = _tokens(key)
    for art_id, other in recent:
        if other == key:
            return art_id
        if len(toks) >= min_tokens:
            o = _tokens(other)
            if len(o) >= min_tokens and jaccard(toks, o) >= threshold:
                return art_id
    return None
