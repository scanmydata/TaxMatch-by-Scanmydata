"""Φθηνά φίλτρα ΠΡΙΝ το LLM: λέξεις-κλειδιά για γενικά portals και εντοπισμός ίδιου θέματος από άλλη πηγή."""
from __future__ import annotations

import re
from typing import Iterable, Optional

from ..textutil import strip_accents

# Ρίζες (χωρίς τόνους, μικρά). Σκόπιμα ευρείες: ένα ψευδώς θετικό κοστίζει μία κλήση LLM, ένα ψευδώς αρνητικό χάνεται.
KEYWORD_STEMS = (
    "φορολογ", "φορος", "φορου", "φορων", "φπα", "φ π α", "ααδε", "α α δ ε", "εφορι", "mydata", "my data", "εφκα", "efka",
    "εργανη", "τελωνει", "εγκυκλι", "προστιμ", "ασφαλιστικ", "εισφορ", "λογιστ", "τιμολογ", "γεμη", "παραταση",
    "δηλωσ", "ενφια", "επιδοτ", "επιχορηγ", "μισθωσ", "ενοικι", "εργοδοτ", "μισθοδοσ", "επιχειρησ", "αφμ", "καδ ",
    "κωδικοι αριθμοι δραστηριοτ", "vies", "intrastat", "πλατφορμα", "ρυθμιση οφειλ", "οφειλ", "επαγγελματ", "αποδειξ",
    "ταμειακ", "πληρωμ",
)
_STEM_RE = re.compile("|".join(re.escape(s) for s in KEYWORD_STEMS))


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zα-ω]+", " ", strip_accents(text or "").lower())).strip()


def is_tax_relevant(title: str, summary: str = "") -> bool:
    return bool(_STEM_RE.search(normalize_text(f"{title} {summary}")))


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
