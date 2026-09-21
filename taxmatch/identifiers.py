"""ΑΦΜ και ΚΑΔ: κανονικοποίηση, επικύρωση, σύγκριση."""
from __future__ import annotations

import re
from typing import Optional


# ----------------------------------------------------------------- ΑΦΜ

def normalize_afm(raw: object) -> str:
    """'EL 123456789', 123456789.0 (Excel), '12345678' (χαμένο αρχικό 0) -> '123456789'/'012345678'.
    Επιστρέφει '' αν δεν βγαίνει 9ψήφιος αριθμός."""
    if raw is None:
        return ""
    if isinstance(raw, float) and raw.is_integer():
        raw = int(raw)
    text = str(raw).strip().upper()
    text = re.sub(r"^(EL|GR)\s*", "", text)
    text = re.sub(r"[\s.\-]", "", text)
    if not text.isdigit() or not (7 <= len(text) <= 9):
        return ""
    return text.zfill(9)


def is_valid_afm(afm: str) -> bool:
    """Έλεγχος ψηφίου ελέγχου ΑΦΜ (mod 11)."""
    if not re.fullmatch(r"\d{9}", afm or "") or afm == "000000000":
        return False
    total = sum(int(afm[i]) * (2 ** (8 - i)) for i in range(8))
    return (total % 11) % 10 == int(afm[8])


# ----------------------------------------------------------------- ΚΑΔ

def kad_digits(code: object) -> str:
    return re.sub(r"\D", "", str(code or ""))


def normalize_kad_prefix(raw: object) -> Optional[str]:
    """Επιστρέφει το πρόθεμα ΚΑΔ ως ψηφία ('47.11' -> '4711') ή None αν δεν είναι έγκυρο."""
    d = kad_digits(raw)
    if not (2 <= len(d) <= 8):
        return None
    return d


def kad_matches(code: object, prefix_digits: str) -> bool:
    """Το ΚΑΔ (οποιαδήποτε μορφή: '47.11.10.01', '47111001') ξεκινά με το πρόθεμα."""
    return kad_digits(code).startswith(prefix_digits)


def format_kad(code: object) -> str:
    """'47111001' -> '47.11.10.01'· μερικά προθέματα ('4711' -> '47.11')."""
    d = kad_digits(code)
    parts = [d[i:i + 2] for i in range(0, len(d), 2)]
    return ".".join(parts)
