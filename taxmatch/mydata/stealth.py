"""Λήψη «μόνο online» παραστατικών πίσω από Cloudflare interactive Turnstile — ΑΥΤΟΥΣΙΟ port του
`download/stealth.py` του Timologio Downloader (ρητό αίτημα 2026-11-04). Τρίτο πέρασμα μετά το άμεσο
endpoint του παρόχου και τον αόρατο headless browser (`headless.py`).


Mechanism (measured live, 2026-10-31): the Epsilon tenants that **do not** expose a
server-side PDF (``/filedocument/getfile?fileType=2`` → 404) serve their DocViewer
page with an **embedded interactive Turnstile widget** (``render=explicit``, mode
``normal`` — a checkbox the user must click). The Blazor/.NET app does not render
the document until the challenge is passed. A raw headless Chrome (πέρασμα 1 of
``download_viewer_only``) fails its fingerprint checks and the page stays empty.

This module drives a **real, visible, patched browser** through the open-source
`undetected-chromedriver <https://github.com/ultrafunkamsterdam/undetected-chromedriver>`_
(MIT, pure Python, no paid service): its CDP patcher restores the Chrome runtime
bits Cloudflare checks, which is what makes the challenge's risk score pass — and
when it does not auto-pass, we click the checkbox through the DevTools protocol
exactly where the user would click, then wait again. No CAPTCHA-solving service,
no token farm, no third-party API: same family of open-source solutions as
FlareSolverr (which is what it is built on, undetected-chromedriver).

If the package is not installed, ``available()`` returns False and the caller
skips this pass entirely — the document falls through to the visible headed
pass (where the user clicks) exactly as before.
"""

from __future__ import annotations

import base64
import logging
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import types
from collections.abc import Callable
from pathlib import Path

from .headless import MIN_TEXT, HeadlessCancelled, HeadlessError, _SAVE_PDF_CLICK_JS

log = logging.getLogger(__name__)

#: Richardson-Lucas style: how long to wait for the app to render (seconds) before
#: the Turnstile click attempt, and after it.
_WAIT_FIRST = 9.0  # first render attempt; Turnstile checkbox appears ~3-5s in
# (longer waits are paid per page anyway via the poll loop)
_WAIT_AFTER_CLICK = 25.0


#: Το UC «μπαλώνει» ένα κοινό αρχείο chromedriver: δύο εκκινήσεις μαζί (λήψη
#: + «μόνο online») χαλούσαν η μία την άλλη. Μία εκκίνηση τη φορά.
_LAUNCH_LOCK = threading.Lock()

#: Τα flags που κρατούν ζωντανή τη σελίδα όταν το παράθυρο είναι εκτός οθόνης:
#: αλλιώς τα Windows το θεωρούν «κρυμμένο», ο Chrome παγώνει timers/rendering
#: και το Turnstile δεν τελειώνει ποτέ.
_KEEP_ALIVE_ARGS = (
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
    "--no-first-run",
    "--no-default-browser-check",
)
#: Σελίδες ανά συνεδρία Chrome. Μετρημένο στην Epsilon: η 5η φόρτωση της
#: ίδιας συνεδρίας μένει μισή (206 χαρακτήρες) — με νέα συνεδρία κάθε 4, 0
#: κολλήματα.
_PAGES_PER_SESSION = 4

#: Εκτός οθόνης: δουλεύει σαν κανονικό (ορατό, όχι headless) παράθυρο — αυτό
#: θέλει το Cloudflare — αλλά ο χρήστης δεν βλέπει τίποτα να ανοιγοκλείνει.
_OFFSCREEN_ARGS = ("--window-position=-32000,-32000", "--window-size=1280,1000")


def _ensure_distutils() -> None:
    """Το UC 3.5.5 κάνει ``from distutils.version import LooseVersion``. Η Python
    3.12+ δεν έχει distutils· στο venv το «δανείζει» το setuptools, στο .exe
    όχι — και τότε το stealth έλειπε σιωπηλά από την εγκατεστημένη εφαρμογή.
    Ένα ελάχιστο LooseVersion αρκεί (το UC κρατά μόνο τα ``version``/``vstring``)."""
    try:
        from distutils.version import LooseVersion  # noqa: F401
        return
    except ImportError:
        pass

    class LooseVersion:
        def __init__(self, vstring: str = "") -> None:
            self.vstring = str(vstring).strip()
            self.version = [int(x) if x.isdigit() else x
                            for x in re.split(r"[.\-]", self.vstring) if x]

        def __str__(self) -> str:
            return self.vstring

        def __repr__(self) -> str:
            return f"LooseVersion({self.vstring!r})"

        def _key(self, other):
            return other.version if isinstance(other, LooseVersion) else LooseVersion(other).version

        def __eq__(self, other) -> bool:
            return self.version == self._key(other)

        def __lt__(self, other) -> bool:
            return self.version < self._key(other)

        def __le__(self, other) -> bool:
            return self.version <= self._key(other)

        def __gt__(self, other) -> bool:
            return self.version > self._key(other)

        def __ge__(self, other) -> bool:
            return self.version >= self._key(other)

    pkg = sys.modules.get("distutils") or types.ModuleType("distutils")
    mod = types.ModuleType("distutils.version")
    mod.LooseVersion = LooseVersion  # type: ignore[attr-defined]
    pkg.version = mod  # type: ignore[attr-defined]
    sys.modules["distutils"] = pkg
    sys.modules["distutils.version"] = mod


def available() -> bool:
    """True if undetected-chromedriver is importable in this environment."""
    _ensure_distutils()
    try:
        import undetected_chromedriver  # noqa: F401
        import selenium  # noqa: F401
    except ImportError:
        return False
    return chrome_path() is not None


def chrome_path() -> Path | None:
    """Ο Google Chrome (το UC δουλεύει ΜΟΝΟ με Chrome, όχι Edge)."""
    from .headless import find_browsers

    for path in find_browsers():
        if path.name.lower() == "chrome.exe":
            return path
    return None


def chrome_major(path: Path | None) -> int | None:
    """Η κύρια έκδοση του εγκατεστημένου Chrome (π.χ. 156).

    Χωρίς αυτήν το UC κατεβάζει τον driver της ΤΕΛΕΥΤΑΙΑΣ σταθερής έκδοσης·
    όσο ο Chrome του χρήστη δεν έχει ενημερωθεί (ή έχει, αλλά δεν ξανάνοιξε)
    οι δύο δεν ταιριάζουν και η εκκίνηση αποτυγχάνει — το «μια δουλεύει, μια
    όχι». Ο φάκελος δίπλα στο chrome.exe έχει το όνομα της έκδοσης."""
    if path is None:
        return None
    versions = [
        tuple(int(x) for x in d.name.split("."))
        for d in path.parent.iterdir()
        if d.is_dir() and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", d.name)
    ] if path.parent.exists() else []
    return max(versions)[0] if versions else None


def _mismatch_major(error: str) -> int | None:
    """«Current browser version is 156.0…» → 156 (για δεύτερη προσπάθεια)."""
    m = re.search(r"Current browser version is (\d+)", error)
    return int(m.group(1)) if m else None


class StealthRenderer:
    """A patched real Chrome (via undetected-chromedriver) that waits on pages and
    prints them to PDF through CDP, mirroring ``HeadlessRenderer``'s interface.

    Used as a context manager like ``HeadlessRenderer``::

        with StealthRenderer() as r:
            pdf = r.render_pdf(url)

    It is a real, non-headless window (that is what Cloudflare's score needs),
    but with ``hidden=True`` (default) it opens OFF-SCREEN: the user sees no
    window flashing during a fetch or the «automatic» online-only pass. If the
    checkbox cannot be passed, the page stays empty and the row stays «μόνο
    online».
    """

    def __init__(
        self,
        *,
        browser: Path | None = None,
        profile_dir: str | Path | None = None,
        should_cancel: Callable[[], bool] | None = None,
        launch_timeout: float = 30.0,
        hidden: bool = True,
    ) -> None:
        # ``browser`` is accepted for interface parity but only Chrome is supported
        # (undetected-chromedriver patches Chrome specifically — never Edge).
        self._should_cancel = should_cancel
        self._chrome_tmp: str | None = None
        # Download destination for /fd/ file links (Chrome saves instead of opening
        # a PDF viewer, which would close the tab and raise NoSuchWindow).
        self._dl_dir: Path = Path(tempfile.mkdtemp(prefix="tl_stealth_dl_"))
        self._dl_seen: set[str] = set()
        self._dl_behavior = False
        self.downloaded: bytes | None = None
        self._renders = 0
        _ensure_distutils()
        try:
            import undetected_chromedriver as uc
        except ImportError as exc:  # pragma: no cover
            raise HeadlessError(
                f"undetected-chromedriver not installed: {exc}"
            ) from exc
        chrome = browser if browser is not None and browser.name.lower() == "chrome.exe" \
            else chrome_path()
        if chrome is None:
            raise HeadlessError("Δεν βρέθηκε Google Chrome (το stealth θέλει Chrome)")
        kwargs: dict = {
            "headless": False,
            "browser_executable_path": str(chrome),
            "version_main": chrome_major(chrome),
            "use_subprocess": True,
        }
        if profile_dir is not None:
            kwargs["user_data_dir"] = str(profile_dir)
            self._persistent_profile = True
        else:
            # Our own temp profile so cleanup is deterministic.
            self._chrome_tmp = tempfile.mkdtemp(prefix="tl_stealth_")
            kwargs["user_data_dir"] = self._chrome_tmp
            self._persistent_profile = False
        def options():
            # Νέο αντικείμενο σε κάθε προσπάθεια: το UC δεν δέχεται ξανά το ίδιο.
            opts = uc.ChromeOptions()
            for arg in _KEEP_ALIVE_ARGS + (_OFFSCREEN_ARGS if hidden else ()):
                opts.add_argument(arg)
            return opts

        def launch():
            with _LAUNCH_LOCK:
                try:
                    return uc.Chrome(options=options(), **kwargs)
                except Exception as exc:  # noqa: BLE001
                    # Driver και Chrome διαφορετικής έκδοσης: ξανά με αυτήν που
                    # λέει το ίδιο το σφάλμα.
                    major = _mismatch_major(str(exc))
                    if not major or major == kwargs["version_main"]:
                        raise
                    log.warning("UC: driver/Chrome mismatch, retry with %s", major)
                    kwargs["version_main"] = major
                    return uc.Chrome(options=options(), **kwargs)

        self._launch = launch
        self._kwargs = kwargs
        started = time.time()
        try:
            self._driver = launch()
            self._limit_page_load()
        except Exception as exc:  # noqa: BLE001 — surface as flow error
            if self._chrome_tmp:
                shutil.rmtree(self._chrome_tmp, ignore_errors=True)
                self._chrome_tmp = None
            raise HeadlessError(f"UC Chrome launch failed: {exc}") from exc
        if time.time() - started > launch_timeout:  # pragma: no cover
            log.warning("UC launch αργό (%.0fs)", time.time() - started)

    def _restart(self) -> bool:
        """Νέος Chrome με ΚΑΘΑΡΟ προφίλ (~2″). Η Epsilon «κολλάει» κάθε 5η
        φόρτωση της ίδιας συνεδρίας (μετρημένο: 7η, 12η, 17η, 22η, 27η)· ούτε
        ξαναφόρτωμα ούτε νέα καρτέλα βοηθούν — νέα συνεδρία ναι."""
        try:
            self._driver.quit()
        except Exception:  # noqa: BLE001
            pass
        if not self._persistent_profile:
            if self._chrome_tmp:
                shutil.rmtree(self._chrome_tmp, ignore_errors=True)
            self._chrome_tmp = tempfile.mkdtemp(prefix="tl_stealth_")
            self._kwargs["user_data_dir"] = self._chrome_tmp
        self._dl_behavior = False
        try:
            self._driver = self._launch()
            self._limit_page_load()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Stealth restart failed: %s", exc)
            return False

    def _limit_page_load(self) -> None:
        # Χωρίς όριο, μια σελίδα που «πέθανε» κρατούσε το get() για λεπτά και
        # μαζί όλη την παρτίδα.
        try:
            self._driver.set_page_load_timeout(45)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------- navigate
    def _cdp(self, method: str, params: dict):
        """One CDP command through UC's passthrough (returns result dict)."""
        try:
            return self._driver.execute_cdp_cmd(method, params)
        except Exception as exc:  # noqa: BLE001
            raise HeadlessError(f"CDP {method}: {exc}") from exc

    def _ensure_download_dir(self) -> None:
        """Allow Chrome downloads into our temp dir (once per renderer)."""
        if self._dl_behavior:
            return
        for method in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
            try:
                self._cdp(method, {"behavior": "allow",
                                   "downloadPath": str(self._dl_dir),
                                   "eventsEnabled": True})
                self._dl_behavior = True
                return
            except HeadlessError:
                continue

    def _take_download(self) -> bytes | None:
        """Ένα PDF που μόλις ολοκληρώθηκε στον φάκελο λήψεων, ή ``None``."""
        for p in self._dl_dir.glob("*.pdf"):
            if p.name in self._dl_seen or Path(str(p) + ".crdownload").exists():
                continue
            try:
                data = p.read_bytes()
            except OSError:
                continue
            if data.startswith(b"%PDF") and len(data) > 1000:
                self._dl_seen.add(p.name)
                return data
        return None

    def _capture_download(self, timeout: float, *, page_aware: bool = False) -> bytes | None:
        """Poll our download dir for a freshly saved PDF.

        ``page_aware``: σταματά νωρίς αν δεν κατεβαίνει τίποτα και η καρτέλα
        δείχνει ΣΕΛΙΔΑ (κείμενο ή κουτάκι Turnstile). Πολλά /fd/ της Epsilon
        σερβίρονται ως σελίδα προβολής, όχι ως αρχείο — εκεί περιμέναμε άδικα
        15″ σε κάθε παραστατικό πριν την τυπώσουμε."""
        start = time.time()
        deadline = start + timeout
        while time.time() < deadline:
            downloading = any(self._dl_dir.glob("*.crdownload"))
            if (page_aware and not downloading and time.time() - start > 1.5
                    and (self._text_length() >= MIN_TEXT
                         or self._turnstile_rect() is not None)):
                return None
            for p in self._dl_dir.glob("*.pdf"):
                if p.name in self._dl_seen or Path(str(p) + ".crdownload").exists():
                    continue
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                if data.startswith(b"%PDF") and len(data) > 1000:
                    self._dl_seen.add(p.name)
                    return data
            if self._should_cancel and self._should_cancel():
                raise HeadlessCancelled()
            time.sleep(0.5)
        return None

    def _text_length(self) -> int:
        try:
            value = self._driver.execute_script(
                "return document.body ? document.body.innerText.length : 0"
            )
            return int(value or 0)
        except Exception:  # noqa: BLE001 — page/navigation hiccups
            return 0

    def _await_render(
        self,
        min_text: int,
        timeout: float,
        *,
        patient: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> int:
        """Wait until the page's visible text stabilizes above ``min_text``."""
        cancel = should_cancel or self._should_cancel
        deadline = time.time() + timeout
        start = time.time()
        last, stable, textlen = -1, 0, 0
        while time.time() < deadline:
            if cancel and cancel():
                raise HeadlessCancelled()
            textlen = self._text_length()
            if textlen == last:
                stable += 1
                if textlen >= min_text and stable >= 3:
                    return textlen
                if (
                    not patient
                    and textlen < min_text
                    and time.time() - start > 12
                    and stable >= 4
                ):
                    return textlen
            else:
                stable, last = 0, textlen
            for _ in range(5):
                if cancel and cancel():
                    raise HeadlessCancelled()
                time.sleep(0.1)
        return textlen

    # ------------------------------------------------------------ turnstile
    def _turnstile_rect(self) -> tuple[int, int, int, int] | None:
        """Pixel box of the embedded widget (``#cf-turnstile``) or None."""
        js = (
            "(() => { var e = document.getElementById('cf-turnstile');"
            "if (!e) return null;"
            "var b = e.getBoundingClientRect();"
            "if (b.width < 50 || b.height < 30) return null;"
            "return [b.x, b.y, b.width, b.height]; })()"
        )
        try:
            box = self._driver.execute_script("return " + js)
        except Exception:  # noqa: BLE001
            return None
        if not box:
            return None
        return tuple(int(round(v)) for v in box)  # type: ignore

    def _turnstile_token(self) -> str:
        js = (
            "(() => { var i = document.querySelector("
            "\"input[name='cf-turnstile-response']\"); return i ? (i.value || '') : ''; })()"
        )
        try:
            return str(self._driver.execute_script("return " + js) or "")
        except Exception:  # noqa: BLE001
            return ""

    def _pass_turnstile(self, timeout: float) -> bool:
        """Click the checkbox through the DevTools mouse protocol, human-style.

        Mouse path first (CF weighs the trajectory into the click), then
        Pressed/Released inside the checkbox box. Up to three attempts; after each
        successful click Cloudflare mints a token into the hidden input and the
        app's own JS callback proceeds.
        """
        deadline = time.time() + timeout
        for attempt in range(3):
            if time.time() > deadline:
                break
            box = self._turnstile_rect()
            if box is None:
                return bool(self._turnstile_token())
            x0, y0, w, h = box
            # The checkbox sits at the left edge of the widget.
            cx = x0 + 24 + random.randint(-3, 3)
            cy = y0 + h // 2 + random.randint(-4, 4)
            px, py = max(0, cx - 320), max(0, cy - 90)
            try:
                for i in range(1, 15):
                    t = i / 14.0
                    nx = int(px + (cx - px) * t + random.uniform(-5, 5))
                    ny = int(py + (cy - py) * t + random.uniform(-4, 4))
                    self._driver.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseMoved", "x": nx, "y": ny, "buttonCount": 0},
                    )
                    time.sleep(random.uniform(0.03, 0.12))
                jx, jy = cx + random.randint(-4, 4), cy + random.randint(-3, 3)
                # «buttons» είναι bitmask (1 = αριστερό), ΟΧΙ λίστα: με λίστα
                # το CDP απαντούσε «Invalid parameters» και το κουτάκι δεν
                # πατήθηκε ποτέ — περνούσαν μόνο σελίδες που δεν το ζητούσαν.
                for ev in ("mousePressed", "mouseReleased"):
                    self._driver.execute_cdp_cmd(
                        "Input.dispatchMouseEvent",
                        {"type": ev, "x": jx, "y": jy, "button": "left",
                         "buttons": 1 if ev == "mousePressed" else 0, "clickCount": 1},
                    )
                    time.sleep(random.uniform(0.05, 0.12))
                log.info("Turnstile checkbox clicked at (%d,%d) attempt %d", jx, jy, attempt + 1)
            except Exception as exc:  # noqa: BLE001
                log.warning("Turnstile click failed: %s", exc)
                return False
            for _ in range(int(_WAIT_AFTER_CLICK)):
                if self._should_cancel and self._should_cancel():
                    raise HeadlessCancelled()
                if self._turnstile_token() or self._text_length() >= MIN_TEXT:
                    return True
                time.sleep(1)
        return bool(self._turnstile_token())

    def _scroll_to_widget(self) -> None:
        try:
            self._driver.execute_script(
                "var e=document.getElementById('cf-turnstile');"
                "if(e) e.scrollIntoView({block:'center'});"
            )
        except Exception:  # noqa: BLE001
            pass

    def _render_or_pass(
        self, min_text: int, timeout: float,
        should_cancel: Callable[[], bool] | None = None,
        *, watch_downloads: bool = False,
    ) -> int:
        """Περιμένει να στοιχειοθετηθεί η σελίδα και, ΟΠΟΤΕ εμφανιστεί το
        κουτάκι του Turnstile, το πατά (έως 3 φορές, με κενό ανάμεσα).

        Παλιά: ένας έλεγχος στα 9″ — αν το κουτάκι ζωγραφιζόταν στα 11″ δεν
        πατιόταν ποτέ και η σελίδα έμενε κενή· άλλοτε πρόφταινε, άλλοτε όχι.
        """
        cancel = should_cancel or self._should_cancel
        deadline = time.time() + timeout
        clicks, last_click, passed_at = 0, 0.0, 0.0
        last, stable, textlen = -1, 0, 0
        self.downloaded = None
        self.passed = False
        while time.time() < deadline:
            if cancel and cancel():
                raise HeadlessCancelled()
            if watch_downloads:
                # /fd/: μετά τον έλεγχο του Cloudflare η καρτέλα γίνεται λήψη
                # αρχείου — η σελίδα δεν «στοιχειοθετείται» ποτέ, το PDF όμως
                # φτάνει στον φάκελο. Το bazaar περίμενε 60″ και αποτύγχανε.
                data = self._take_download()
                if data:
                    self.downloaded = data
                    return -1
            textlen = self._text_length()
            if textlen == last:
                stable += 1
                if textlen >= min_text and stable >= 3:
                    return textlen
            else:
                stable, last = 0, textlen
            if (textlen < min_text and clicks < 3 and time.time() - last_click > 8
                    and not self._turnstile_token()
                    and self._turnstile_rect() is not None):
                self._scroll_to_widget()
                if self._pass_turnstile(timeout=_WAIT_AFTER_CLICK):
                    log.info("Turnstile passed")
                    self.passed = True
                    passed_at = time.time()
                clicks += 1
                last_click = time.time()
                continue
            if textlen < min_text and stable >= 16 and self._turnstile_rect() is None:
                # Μισοφορτωμένη σελίδα που δεν αλλάζει για ~8″ και ΔΕΝ ζητά
                # κλικ: κόλλησε — ο καλών την ξαναφορτώνει μία φορά.
                return textlen
            if passed_at and textlen < min_text and time.time() - passed_at > 12:
                # Token πήραμε, η εφαρμογή όμως δεν προχώρησε: αντί να
                # περιμένουμε 60″ ακόμη, ο καλών ξαναφορτώνει τη σελίδα (το
                # cookie του Cloudflare υπάρχει πια).
                return textlen
            time.sleep(0.5)
        return textlen

    def _goto(self, url: str) -> bool:
        """Navigate; if UC's startup window died, open a fresh one and retry."""
        self._ensure_download_dir()
        try:
            self._driver.get(url)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Stealth navigate retry via new window: %s", exc)
            try:
                # Η καρτέλα έκλεισε (π.χ. έγινε λήψη αρχείου): πάμε σε όποια
                # ζει ακόμη και ανοίγουμε καινούργια. (Το παλιό new_window()
                # δεν υπάρχει στο selenium 4 — μετά από μία αποτυχία έπεφταν
                # ΟΛΑ τα επόμενα παραστατικά αμέσως.)
                if not self._restart():
                    return False
                self._ensure_download_dir()
                self._driver.get(url)
                return True
            except Exception as exc2:  # noqa: BLE001 — caller treats as «μόνο online»
                log.warning("Stealth navigate failed: %s", exc2)
                return False

    # ------------------------------------------------------------- render
    def render_pdf(
        self,
        url: str,
        *,
        min_text: int = MIN_TEXT,
        timeout: float = 45.0,
        patient: bool = False,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bytes | None:
        """Load the page, pass the challenge if one is pending, print to PDF.

        Returns the PDF bytes or ``None`` when the page never rendered — callers
        must treat ``None`` as «μόνο online» and fall to the next pass.
        """
        # Νέα συνεδρία ΠΡΙΝ από την 5η σελίδα: εκεί «κολλάει» η Epsilon, και
        # το κόλλημα κοστίζει ~10″ + ξανά, ενώ η επανεκκίνηση ~2″.
        self._renders += 1
        if self._renders > 1 and (self._renders - 1) % _PAGES_PER_SESSION == 0:
            self._restart()
        if not self._goto(url):
            return None
        if re.search(r"/fd/[0-9a-fA-F]+(?::\d+)?", url):
            # /fd/ links ARE the file: Chrome downloads it (the viewer tab
            # closes, which is why plain render_pdf used to die here).
            got = self._capture_download(min(15.0, timeout), page_aware=True)
            if got:
                return got
        is_file = bool(re.search(r"/fd/[0-9a-fA-F]+(?::\d+)?", url))
        textlen = self._render_or_pass(min_text, timeout, should_cancel,
                                       watch_downloads=is_file)
        if textlen == -1 and self.downloaded:
            return self.downloaded
        if textlen < min_text and self._restart() and self._goto(url):
            log.info("Stealth: νέα συνεδρία και ξανά για %s", url)
            textlen = self._render_or_pass(min_text, min(40.0, timeout), should_cancel,
                                           watch_downloads=is_file)
            if textlen == -1 and self.downloaded:
                return self.downloaded
        if is_file and textlen < min_text:
            # Τελευταία ευκαιρία: λήψη που τελείωσε την τελευταία στιγμή.
            got = self._capture_download(5.0)
            if got:
                return got
        if textlen < min_text:
            log.info("Stealth render: κενή after wait (%d χαρ.) for %s", textlen, url)
            return None
        result = self._cdp(
            "Page.printToPDF",
            {
                "printBackground": True,
                "displayHeaderFooter": False,
                "marginTop": 0.3, "marginBottom": 0.3,
                "marginLeft": 0.3, "marginRight": 0.3,
            },
        )
        pdf = base64.b64decode(result.get("data", ""))
        return pdf if pdf.startswith(b"%PDF") else None

    def save_via_button(
        self,
        url: str,
        download_dir: str | Path,
        *,
        min_text: int = MIN_TEXT,
        render_timeout: float = 40.0,
        download_timeout: float = 45.0,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bytes | None:
        """Open the page and click the provider's own «Αποθήκευση ως PDF» button,
        capturing the file that downloads — same semantics as
        ``HeadlessRenderer.save_via_button``.
        """
        download_dir = Path(download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        for method in ("Browser.setDownloadBehavior", "Page.setDownloadBehavior"):
            try:
                self._cdp(method, {"behavior": "allow", "downloadPath": str(download_dir),
                                   "eventsEnabled": True})
                break
            except HeadlessError:
                continue

        before = {p.name for p in download_dir.glob("*")}
        if not self._goto(url):
            return None
        cancel = should_cancel or self._should_cancel
        textlen = self._render_or_pass(min_text, render_timeout, cancel)
        if textlen < min_text:
            log.info("save_via_button (stealth): κενή σελίδα για %s", url)
            return None
        try:
            clicked = self._driver.execute_script("return " + _SAVE_PDF_CLICK_JS)
        except Exception:  # noqa: BLE001
            clicked = False
        if not clicked:
            log.info("save_via_button (stealth): no PDF button on %s", url)
            return None
        deadline = time.time() + download_timeout
        while time.time() < deadline:
            if cancel and cancel():
                raise HeadlessCancelled()
            for p in download_dir.glob("*.pdf"):
                if p.name in before:
                    continue
                if Path(str(p) + ".crdownload").exists():
                    continue
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                if data.startswith(b"%PDF") and len(data) > 1000:
                    return data
            time.sleep(0.5)
        log.info("save_via_button (stealth): no download within %ss", download_timeout)
        return None

    # -------------------------------------------------------------- cleanup
    def close(self) -> None:
        try:
            self._driver.quit()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self._dl_dir, ignore_errors=True)
        if self._chrome_tmp:
            shutil.rmtree(self._chrome_tmp, ignore_errors=True)
            self._chrome_tmp = None

    def __enter__(self) -> "StealthRenderer":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
