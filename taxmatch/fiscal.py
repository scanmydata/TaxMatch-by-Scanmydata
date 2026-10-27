"""Η χρήση (φορολογικό έτος) στην οποία δουλεύει ο λογιστής — ίδιοι κανόνες με το Timologio Downloader
(`mydata-etimologio-bridge/desktop/src/timologio/fiscal.py`), χωρίς Qt ώστε να τους ελέγχουν τα tests:

* Η χρήση αλλάζει μόνη της με το ημερολογιακό έτος, αλλά ο χρήστης μπορεί να διαλέξει άλλη (π.χ. τον Ιανουάριο κλείνει
  ακόμη την περσινή). Η επιλογή του ισχύει μέσα στο ημερολογιακό έτος που την έκανε· με την αλλαγή του χρόνου η εφαρμογή
  περνά στη νέα χρήση.
* Προσφέρονται φέτος, πέρσι και όσα έτη έχουν δεδομένα (φθίνουσα σειρά).
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional


def resolve_year(stored: Optional[int], chosen_in: Optional[int], today: Optional[date] = None) -> int:
    today = today or date.today()
    if not stored or chosen_in != today.year:
        return today.year
    return int(stored)


def year_choices(years_with_data: Iterable[int] = (), today: Optional[date] = None) -> list[int]:
    today = today or date.today()
    years = {today.year, today.year - 1}
    years.update(y for y in years_with_data if 2000 <= y <= today.year)
    return sorted(years, reverse=True)
