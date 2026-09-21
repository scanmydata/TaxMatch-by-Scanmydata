"""Διαδρομές και περιβάλλον. Όλα τα δεδομένα χρήστη ζουν σε ΕΝΑ φάκελο (data dir)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import APP_NAME


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir() -> Path:
    """`TAXMATCH_DATA_DIR` > %LOCALAPPDATA%\\TaxMatch (Windows) > ~/.taxmatch."""
    override = os.getenv("TAXMATCH_DATA_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt" and os.getenv("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"]) / APP_NAME
    else:
        base = Path.home() / f".{APP_NAME.lower()}"
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "tax_monitor.db"


def log_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def resource_dir() -> Path:
    """Ρίζα των bundled αρχείων (πρότυπα prompt κ.λπ.) — και σε PyInstaller."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[1]


def executable_command() -> list[str]:
    """Εντολή που ξαναξεκινά αυτή την εφαρμογή (για το Task Scheduler)."""
    if is_frozen():
        return [sys.executable]
    return [sys.executable, "-m", "taxmatch"]


def load_env() -> None:
    """.env από τον φάκελο δεδομένων και από τον τρέχοντα φάκελο (dev). Δεν αντικαθιστά υπάρχοντα env vars."""
    load_dotenv(data_dir() / ".env", override=False)
    load_dotenv(Path.cwd() / ".env", override=False)
