"""Logging σε αρχείο (το windowed exe δεν έχει stdout)."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from . import config


def setup(filename: str, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(getattr(h, "_taxmatch", False) for h in root.handlers):
        return
    root.setLevel(level)
    fh = RotatingFileHandler(config.log_dir() / filename, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    fh._taxmatch = True  # type: ignore[attr-defined]
    root.addHandler(fh)
    if sys.stderr is not None:                     # σε windowed exe το stderr είναι None
        sh = logging.StreamHandler()
        sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        sh._taxmatch = True  # type: ignore[attr-defined]
        root.addHandler(sh)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
