"""«Συνέχεια στον browser»: όταν ένας έλεγχος κολλήσει ΜΕΤΑ από επιτυχημένη σύνδεση (π.χ. ο e-ΕΦΚΑ/ΤΕΚΑ ζητά
επιβεβαίωση στοιχείων επικοινωνίας, η ΑΑΔΕ ένα ενδιάμεσο μήνυμα), ο χρήστης ανοίγει τον τοπικό Chrome/Edge ΗΔΗ
συνδεδεμένο, στο σημείο όπου σταμάτησε η εφαρμογή — ρητό αίτημα χρήστη 2026-10-27.

Πώς: τα cookies της συνεδρίας του δικού μας HTTP client (`_HyperHttp.jar`) περνούν στον browser μέσω CDP
(`Network.setCookie`), μετά `Page.navigate` στη σελίδα που ζητήσαμε. Ξεχωριστό, ΠΡΟΣΩΡΙΝΟ προφίλ ανά άνοιγμα
(`<data>/browser/handoff-*`, σβήνεται το επόμενο άνοιγμα) — ποτέ το προφίλ του χρήστη.

Ασφάλεια: τα cookies μένουν ΜΟΝΟ στη μνήμη της εφαρμογής (λίστα `_PENDING`, χάνεται με το κλείσιμο) — ποτέ σε βάση,
αρχείο ή log. Κωδικοί δεν χρειάζονται (η σύνδεση έχει ήδη γίνει). Μια συνεδρία ΑΑΔΕ/ΕΦΚΑ λήγει σε λίγα λεπτά
αδράνειας· αν έχει λήξει, ο browser δείχνει απλώς τη σελίδα σύνδεσης.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import config

log = logging.getLogger(__name__)

_PENDING: list[dict[str, Any]] = []
_LOCK = threading.Lock()
MAX_PENDING = 30


def remember(label: str, afm: str, http: Any, url: str = "", reason: str = "") -> Optional[dict[str, Any]]:
    """Κρατά (στη μνήμη) τη συνεδρία ενός ελέγχου που κόλλησε μετά τη σύνδεση. `url`: η σελίδα που ζητήσαμε —
    αλλιώς η τελευταία που φόρτωσε ο client."""
    jar = getattr(http, "jar", None)
    target = url or getattr(http, "last_url", "")
    if not jar or not target:
        return None
    entry = {"id": f"{afm}:{label}:{time.monotonic()}", "label": label, "afm": afm, "url": target, "reason": reason,
             "cookies": {h: dict(kv) for h, kv in jar.items() if kv}, "at": datetime.now().strftime("%H:%M")}
    with _LOCK:
        _PENDING[:] = [p for p in _PENDING if not (p["afm"] == afm and p["label"] == label)][-MAX_PENDING + 1:]
        _PENDING.append(entry)
    log.info("[handoff] %s %s: %s", afm, label, target.split("?")[0])                 # ποτέ cookies
    return entry


def pending(afm: str = "", since: float = 0.0) -> list[dict[str, Any]]:
    with _LOCK:
        return [p for p in _PENDING if (not afm or p["afm"] == afm)
                and (not since or float(p["id"].rsplit(":", 1)[1]) >= since)]


def forget(entry_id: str) -> None:
    with _LOCK:
        _PENDING[:] = [p for p in _PENDING if p["id"] != entry_id]


def _fresh_profile() -> Path:
    root = config.data_dir() / "browser"
    for old in root.glob("handoff-*"):
        shutil.rmtree(old, ignore_errors=True)               # προηγούμενα (ο browser τους ίσως είναι ακόμη ανοιχτός)
    profile = root / f"handoff-{int(time.time())}"
    profile.mkdir(parents=True, exist_ok=True)
    return profile


def open_in_browser(entry: dict[str, Any], launcher: Callable[..., Any] = subprocess.Popen,
                    timeout: float = 30.0) -> dict[str, Any]:
    """Ανοίγει νέο παράθυρο Chrome/Edge με τα cookies της συνεδρίας και πηγαίνει στη σελίδα όπου κόλλησε ο έλεγχος."""
    from .aml import gemi_browser as gb
    exe = gb.find_browser()
    if not exe:
        return {"ok": False, "reason": "Δεν βρέθηκε Chrome ή Edge στον υπολογιστή."}
    profile = _fresh_profile()
    launcher([exe, "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
              "--no-default-browser-check", "about:blank"])
    deadline, port = time.monotonic() + timeout, None
    while time.monotonic() < deadline and not gb._alive(port):                    # noqa: SLF001
        time.sleep(0.3)
        port = gb._active_port(profile)                                           # noqa: SLF001
    if not gb._alive(port):                                                       # noqa: SLF001
        return {"ok": False, "reason": "Ο browser δεν απάντησε (χρονικό όριο)."}
    target = None
    while time.monotonic() < deadline and target is None:
        try:
            target = next((t for t in gb._json(f"http://127.0.0.1:{port}/json/list")     # noqa: SLF001
                           if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
        except (OSError, ValueError):
            pass
        if target is None:
            time.sleep(0.3)
    if target is None:
        return {"ok": False, "reason": "Δεν άνοιξε σελίδα στον browser."}
    cdp = gb.Cdp(target["webSocketDebuggerUrl"])
    try:
        cdp.call("Network.enable")
        for host, kv in entry["cookies"].items():
            for name, value in kv.items():
                cdp.call("Network.setCookie", {"name": name, "value": value, "url": f"https://{host}/",
                                               "secure": True})
        cdp.call("Page.navigate", {"url": entry["url"]})
    finally:
        cdp.close()
    return {"ok": True, "reason": ""}
