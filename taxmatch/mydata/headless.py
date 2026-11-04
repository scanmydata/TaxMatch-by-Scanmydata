"""PDF παραστατικών «μόνο online» με αόρατο (headless) Edge/Chrome — port του `download/headless.py` του Timologio
Downloader (ρητό αίτημα 2026-11-04: «για τις λήψεις pdf παραστατικών ενημερώσου για τις νέες διαδικασίες»).

Κάποιοι πάροχοι (eskap, Alpha Ψηφιακή, SPA προβολές) δεν δίνουν αρχείο PDF· φτιάχνουν το παραστατικό στον browser.
Εδώ ο εγκατεστημένος browser ανοίγει αόρατα τη σελίδα, περιμένουμε να στοιχειοθετηθεί και την τυπώνουμε σε PDF με το
DevTools protocol (`Page.printToPDF`). Raw CDP με τον δικό μας WebSocket client (`aml/gemi_browser.Cdp`) — χωρίς driver
και χωρίς εξάρτηση.

ΟΡΙΟ (όπως στο timologio): πάροχοι πίσω από Cloudflare «είστε άνθρωπος» (Epsilon DocViewer) μένουν κενοί σε headless —
το ανιχνεύουμε (λίγο κείμενο) και τα υπόλοιπα περνούν στο «stealth» πέρασμα (`stealth.py`, αυτούσιο port του
timologio — ρητό αίτημα 2026-11-04).
"""
from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

#: Κάτω από τόσους χαρακτήρες ορατού κειμένου η σελίδα δεν στοιχειοθετήθηκε — δεν αποθηκεύουμε λευκό PDF.
MIN_TEXT = 300


class RenderError(Exception):
    """Η σελίδα δεν έγινε PDF (κενή προβολή, Cloudflare, χρονικό όριο)."""


#: Ονόματα του timologio (για το `stealth.py`, που μεταφέρθηκε αυτούσιο).
HeadlessError = RenderError


class HeadlessCancelled(RenderError):
    """Ο χρήστης διέκοψε."""


#: JS που πατά το κουμπί «Αποθήκευση ως PDF» του παρόχου (Epsilon DocViewer) — το ΙΔΙΟ κουμπί που θα πατούσε ο χρήστης.
_SAVE_PDF_CLICK_JS = """
(function () {
  var els = Array.prototype.slice.call(document.querySelectorAll('a, button, [role="button"]'));
  var t = els.find(function (e) {
    var tx = (e.textContent || '').trim();
    var ti = (e.getAttribute && e.getAttribute('title')) || '';
    return /save as pdf|αποθήκευση ως pdf/i.test(ti) || tx === 'PDF';
  });
  if (t) { t.click(); return true; }
  return false;
})()
"""


def find_browsers() -> list[Path]:
    """Edge πρώτα, μετά Chrome — όπως το timologio (το stealth θέλει ΜΟΝΟ Chrome)."""
    import os
    pf, pf86 = os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [rf"{pf86}\Microsoft\Edge\Application\msedge.exe", rf"{pf}\Microsoft\Edge\Application\msedge.exe",
                  rf"{pf}\Google\Chrome\Application\chrome.exe", rf"{pf86}\Google\Chrome\Application\chrome.exe",
                  rf"{local}\Google\Chrome\Application\chrome.exe" if local else ""]
    out: list[Path] = []
    for c in candidates:
        if c and Path(c).exists() and Path(c) not in out:
            out.append(Path(c))
    return out


def available() -> bool:
    from ..aml.gemi_browser import find_browser
    return bool(find_browser())


def _json(url: str, timeout: float = 3.0, method: str = "GET") -> Any:
    with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=timeout) as r:   # μόνο 127.0.0.1
        return json.loads(r.read().decode("utf-8"))


class HeadlessRenderer:
    """Ένας αόρατος browser για πολλά παραστατικά (`with HeadlessRenderer() as r: r.render(url)`)."""

    def __init__(self, timeout: float = 30.0, launcher: Callable[..., Any] = subprocess.Popen) -> None:
        self.timeout, self._launcher = timeout, launcher
        self._proc: Any = None
        self._profile: Optional[Path] = None
        self._port: Optional[int] = None

    def __enter__(self) -> "HeadlessRenderer":
        from ..aml.gemi_browser import find_browser
        exe = find_browser()
        if not exe:
            raise RenderError("δεν βρέθηκε Edge/Chrome")
        self._profile = Path(tempfile.mkdtemp(prefix="taxmatch-render-"))
        self._proc = self._launcher([exe, "--headless=new", "--disable-gpu", "--remote-debugging-port=0",
                                     f"--user-data-dir={self._profile}", "--no-first-run",
                                     "--no-default-browser-check", "about:blank"])
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            try:
                self._port = int((self._profile / "DevToolsActivePort").read_text().split()[0])
                _json(f"http://127.0.0.1:{self._port}/json/version", 1.0)
                return self
            except (OSError, ValueError, IndexError):
                time.sleep(0.3)
        self.__exit__(None, None, None)
        raise RenderError("ο browser δεν ξεκίνησε")

    def __exit__(self, *_exc) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:                           # noqa: BLE001
                pass
        if self._profile is not None:
            shutil.rmtree(self._profile, ignore_errors=True)

    def render(self, url: str) -> bytes:
        from ..aml.gemi_browser import Cdp
        tab = _json(f"http://127.0.0.1:{self._port}/json/new?about:blank", method="PUT")   # νέος Chrome: μόνο PUT
        cdp = Cdp(tab["webSocketDebuggerUrl"])
        try:
            cdp.call("Page.navigate", {"url": url})
            deadline = time.monotonic() + self.timeout
            text = 0
            while time.monotonic() < deadline:
                time.sleep(0.8)
                if cdp.evaluate("document.readyState") != "complete":
                    continue
                text = cdp.evaluate("document.body ? document.body.innerText.trim().length : 0") or 0
                if text >= MIN_TEXT:
                    time.sleep(1.0)                     # γραμματοσειρές/εικόνες
                    break
            if text < MIN_TEXT:
                raise RenderError("η σελίδα δεν στοιχειοθετήθηκε (κενή προβολή — πιθανός έλεγχος «είστε άνθρωπος»)")
            res = cdp.call("Page.printToPDF", {"printBackground": True, "preferCSSPageSize": True})
            data = base64.b64decode(res.get("data") or "")
            if not data.startswith(b"%PDF"):
                raise RenderError("ο browser δεν έδωσε PDF")
            return data
        finally:
            cdp.close()
            try:
                _json(f"http://127.0.0.1:{self._port}/json/close/{tab['id']}")
            except (OSError, ValueError, KeyError):
                pass
