"""Το «scope» ενός άρθρου: σε ποιες επιχειρήσεις αφορά. Κοινό σχήμα για extraction και matching.

Κανονικοποιημένη μορφή (extracted_json):
{
  "relevant": true,                       # false = δεν αφορά φορολογικά/λογιστικά/εργατικά επιχειρήσεων
  "summary": "…",
  "scope": {
     "type": "all" | "targeted",
     "kad_prefixes": ["4711", "56"],      # ψηφία, χωρίς τελείες
     "books_categories": ["Β", "Γ"],
     "vat_subject": true | false | null,
     "legal_forms": ["ΙΚΕ", "ΟΕ"],        # κεφαλαία tokens
     "entity_types": ["ΙΔΙΩΤΕΣ", "ΑΤΟΜΙΚΕΣ", "ΝΟΜΙΚΑ_ΠΡΟΣΩΠΑ"],   # είδος πελάτη (2026-11-04)
     "employers_only": true | null        # μόνο εργοδότες (μισθοδοσία/ΕΡΓΑΝΗ/ΑΠΔ)
  },
  "deadline": "YYYY-MM-DD" | null,
  "action_required": "…" | null,
  "topic": "ΦΠΑ" | "ΦΟΡΟΛΟΓΙΑ ΕΙΣΟΔΗΜΑΤΟΣ" | …
}
Οι κριτήρια ενός "targeted" scope συνδυάζονται με AND· οι τιμές μέσα σε κάθε λίστα με OR.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Optional

from .identifiers import normalize_kad_prefix
from .textutil import strip_accents

TOPICS = ["ΦΠΑ", "ΦΟΡΟΛΟΓΙΑ ΕΙΣΟΔΗΜΑΤΟΣ", "myDATA", "ΕΡΓΑΤΙΚΑ", "ΑΣΦΑΛΙΣΤΙΚΑ", "ΤΕΛΩΝΕΙΑΚΑ",
          "ΑΚΙΝΗΤΑ", "ΛΟΓΙΣΤΙΚΑ", "ΕΠΙΧΟΡΗΓΗΣΕΙΣ", "ΑΛΛΟ"]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


#: Είδη πελάτη που μπορεί να ορίσει το LLM (`scope.entity_types`) → ό,τι ξέρει η εφαρμογή (matching/match.py).
ENTITY_TYPES = ("ΙΔΙΩΤΕΣ", "ΑΤΟΜΙΚΕΣ", "ΝΟΜΙΚΑ_ΠΡΟΣΩΠΑ")
_ENTITY_ALIASES = {"ΙΔΙΩΤ": "ΙΔΙΩΤΕΣ", "ΦΥΣΙΚ": "ΙΔΙΩΤΕΣ", "ΑΤΟΜΙΚ": "ΑΤΟΜΙΚΕΣ", "ΕΛΕΥΘΕΡ": "ΑΤΟΜΙΚΕΣ",
                   "ΝΟΜΙΚ": "ΝΟΜΙΚΑ_ΠΡΟΣΩΠΑ", "ΕΤΑΙΡ": "ΝΟΜΙΚΑ_ΠΡΟΣΩΠΑ"}


def empty_scope() -> dict[str, Any]:
    return {"type": "all", "kad_prefixes": [], "books_categories": [], "vat_subject": None, "legal_forms": [],
            "entity_types": [], "employers_only": None}


def _entity_type(v: Any) -> Optional[str]:
    s = re.sub(r"[^Α-ΩA-Z_]", "", _upper_plain(str(v or "")))
    if s in ENTITY_TYPES:
        return s
    return next((t for k, t in _ENTITY_ALIASES.items() if s.startswith(k)), None)


def _upper_plain(s: str) -> str:
    return strip_accents(s).upper().replace(" ", "_")


def _books_letter(v: Any) -> Optional[str]:
    s = str(v or "").strip().upper()
    if not s:
        return None
    if s[0] in ("Β", "B"):
        return "Β"
    if s[0] in ("Γ", "G"):
        return "Γ"
    if s[0] in ("Α", "A"):
        return "Α"
    return None


def _as_list(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple, set)) else [v]


def normalize_deadline(v: Any) -> Optional[str]:
    if not isinstance(v, str) or not _DATE_RE.match(v.strip()):
        return None
    try:
        date.fromisoformat(v.strip())
    except ValueError:
        return None
    return v.strip()


def normalize(raw: Any) -> dict[str, Any]:
    """Καθαρίζει την έξοδο του LLM σε έγκυρο σχήμα. Δεν ρίχνει ποτέ exception σε άκυρο input."""
    raw = raw if isinstance(raw, dict) else {}
    rs = raw.get("scope") if isinstance(raw.get("scope"), dict) else {}

    kads: list[str] = []
    for item in _as_list(rs.get("kad_prefixes")):
        p = normalize_kad_prefix(item)
        if p and p not in kads:
            kads.append(p)
    books: list[str] = []
    for item in _as_list(rs.get("books_categories")):
        b = _books_letter(item)
        if b and b not in books:
            books.append(b)
    forms = [str(x).strip().upper() for x in _as_list(rs.get("legal_forms")) if str(x).strip()]
    vat = rs.get("vat_subject")
    vat = vat if isinstance(vat, bool) else None
    entities: list[str] = []
    for item in _as_list(rs.get("entity_types")):
        t = _entity_type(item)
        if t and t not in entities:
            entities.append(t)
    if len(entities) == len(ENTITY_TYPES):
        entities = []                                   # όλα τα είδη = κανένας περιορισμός
    employers = True if rs.get("employers_only") is True else None

    has_criteria = bool(kads or books or forms or vat is not None or entities or employers)
    declared_all = str(rs.get("type") or "").lower() == "all"
    scope_type = "targeted" if (has_criteria and not declared_all) else "all"
    if scope_type == "all":
        kads, books, forms, vat, entities, employers = [], [], [], None, [], None

    topic = str(raw.get("topic") or "").strip().upper()
    action = raw.get("action_required")
    return {
        "relevant": bool(raw.get("relevant", True)),
        "summary": str(raw.get("summary") or "").strip(),
        "scope": {"type": scope_type, "kad_prefixes": kads, "books_categories": books,
                  "vat_subject": vat, "legal_forms": forms, "entity_types": entities,
                  "employers_only": employers},
        "deadline": normalize_deadline(raw.get("deadline")),
        "action_required": str(action).strip() if isinstance(action, str) and action.strip() else None,
        "topic": topic if topic in TOPICS else "ΑΛΛΟ",
    }
