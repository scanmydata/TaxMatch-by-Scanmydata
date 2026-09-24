"""Σύνδεση στο Γ.Ε.ΜΗ. (services.businessportal.gr) ΜΕΣΑ στον τοπικό browser του υπολογιστή — από εκεί και πέρα ο
χρήστης επιλέγει μόνος του ενέργειες (π.χ. πιστοποιητικό εκπροσώπησης/ισχύος). Ρητό αίτημα χρήστη (2026-09-24).

Ροή == `gemiLogin` του runner του χρήστη (`recerse-engineer/runner/lib/hyper-http.js`, port του Hyper.Server
`WebRequestHelper.LoginGemi`), αλλά εκτελείται ΜΕΣΑ στη σελίδα του businessportal (same-origin, όπως το ίδιο το SPA)
ώστε το cookie συνεδρίας να μείνει στον browser:

1. εκκίνηση Chrome (ή Edge) με ΞΕΧΩΡΙΣΤΟ προφίλ της εφαρμογής (`<data>/browser/gemi` — ο Chrome δεν επιτρέπει αυτοματισμό
   στο κύριο προφίλ του χρήστη) και `--remote-debugging-port=0` (τυχαία θύρα, μόνο 127.0.0.1· τη διαβάζουμε από το
   `DevToolsActivePort` του προφίλ)· αν τρέχει ήδη από προηγούμενη φορά, ανοίγει νέα καρτέλα στο ίδιο παράθυρο.
2. μέσω Chrome DevTools Protocol, στη σελίδα `https://services.businessportal.gr/`:
   `POST api/welcome/login?lang=el {username,password}` → `GET api/authentication/checkSession` (== decompiled).
3. επαλήθευση `session.username == user`, επαναφόρτωση σελίδας (το SPA βλέπει τη συνεδρία) — και αποσύνδεση του
   αυτοματισμού. Ο browser μένει ανοιχτός για τον χρήστη.

ΜΗΝ καταγράφεις ποτέ την έκφραση JS (περιέχει τον κωδικό). ΔΕΝ έχει δοκιμαστεί ζωντανά (δεν υπήρχαν κωδικοί Γ.Ε.ΜΗ.).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

from .. import config, crypto, db

log = logging.getLogger(__name__)

GEMI_HOME = "https://services.businessportal.gr/"
REASON_EL = {"NoBrowser": "Δεν βρέθηκε Google Chrome ή Microsoft Edge στον υπολογιστή.",
             "BrowserTimeout": "Ο browser δεν απάντησε (ίσως τον μπλόκαρε το antivirus).",
             "PageError": "Η σελίδα του Γ.Ε.ΜΗ. δεν φόρτωσε σωστά (ίσως είναι εκτός λειτουργίας).",
             "InvalidCredentials": "Λάθος κωδικοί Γ.Ε.ΜΗ.",
             "NoCredentials": "Δεν υπάρχουν κωδικοί Γ.Ε.ΜΗ. για τον πελάτη."}


# ------------------------------------------------------------------ κωδικοί Γ.Ε.ΜΗ. ανά πελάτη
def get_credentials(conn: sqlite3.Connection, afm: str) -> Optional[tuple[str, str]]:
    row = conn.execute("SELECT gemi_user, gemi_pass FROM aml_gemi_credentials WHERE afm=?", (afm,)).fetchone()
    if not row or not row["gemi_user"] or not row["gemi_pass"]:
        return None
    return crypto.dec(row["gemi_user"]), crypto.dec(row["gemi_pass"])


def set_credentials(conn: sqlite3.Connection, afm: str, user: str, password: str) -> bool:
    """Κενός κωδικός = μένει ο αποθηκευμένος. Ο κωδικός δεν επιστρέφεται ποτέ στο UI."""
    user, password = (user or "").strip(), password or ""
    if not user and not password:
        return False
    old = conn.execute("SELECT gemi_user, gemi_pass FROM aml_gemi_credentials WHERE afm=?", (afm,)).fetchone()
    conn.execute("INSERT INTO aml_gemi_credentials(afm, gemi_user, gemi_pass, updated_at) VALUES (?,?,?,?) "
                 "ON CONFLICT(afm) DO UPDATE SET gemi_user=excluded.gemi_user, gemi_pass=excluded.gemi_pass, "
                 "updated_at=excluded.updated_at",
                 (afm, crypto.enc(user) if user else (old["gemi_user"] if old else ""),
                  crypto.enc(password) if password else (old["gemi_pass"] if old else ""), db.utcnow()))
    return True


def user_of(conn: sqlite3.Connection, afm: str) -> str:
    row = conn.execute("SELECT gemi_user FROM aml_gemi_credentials WHERE afm=?", (afm,)).fetchone()
    return crypto.dec(row["gemi_user"]) if row and row["gemi_user"] else ""


# ------------------------------------------------------------------ browser
def find_browser() -> Optional[str]:
    env = os.environ
    candidates = []
    for base in (env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("LOCALAPPDATA")):
        if base:
            candidates += [Path(base) / "Google/Chrome/Application/chrome.exe",
                           Path(base) / "Microsoft/Edge/Application/msedge.exe"]
    for c in candidates:
        if c.is_file():
            return str(c)
    return shutil.which("chrome") or shutil.which("google-chrome") or shutil.which("msedge")


def profile_dir() -> Path:
    return config.data_dir() / "browser" / "gemi"


def _active_port(profile: Path) -> Optional[int]:
    try:
        return int((profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def _json(url: str, method: str = "GET", timeout: float = 2.0) -> Any:
    req = urllib.request.Request(url, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:                  # μόνο 127.0.0.1
        return json.loads(r.read().decode("utf-8"))


def _alive(port: Optional[int]) -> bool:
    if not port:
        return False
    try:
        _json(f"http://127.0.0.1:{port}/json/version", timeout=1.0)
        return True
    except (OSError, ValueError):
        return False


# ------------------------------------------------------------------ ελάχιστος WebSocket client (CDP)
class _WebSocket:
    """RFC 6455, μόνο ό,τι χρειάζεται το CDP: text frames, masking, κατάτμηση, ping/close. Χωρίς εξωτερική εξάρτηση."""

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        u = urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
                          .encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise OSError("websocket handshake")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise OSError("websocket handshake")
        self._buf = head.split(b"\r\n\r\n", 1)[1]

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def send(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        parts = []
        while True:
            b1, b2 = self._read(2)
            opcode, n = b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else b""
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0x8:
                raise OSError("websocket closed")
            parts.append(data)
            if b1 & 0x80:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, b"")
        except OSError:
            pass
        self.sock.close()


class Cdp:
    def __init__(self, ws_url: str) -> None:
        self.ws = _WebSocket(ws_url)
        self._id = 0

    def call(self, method: str, params: Optional[dict] = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mid:
                if "error" in msg:
                    raise OSError(msg["error"].get("message", "cdp error"))
                return msg.get("result", {})

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        res = self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": await_promise,
                                             "returnByValue": True})
        return (res.get("result") or {}).get("value")

    def close(self) -> None:
        self.ws.close()


# ------------------------------------------------------------------ login
def login_expression(user: str, password: str) -> str:
    """Η ίδια ακολουθία με το `gemiLogin` (runner), same-origin μέσα στη σελίδα. Τα στοιχεία μπαίνουν ως JSON literals."""
    return ("(async () => {"
            "const gm = await fetch('/api/public/getGlobalMessage?lang=el', {credentials: 'include'});"
            "if (gm.status !== 200) return JSON.stringify({reason: 'PageError'});"
            "const body = new URLSearchParams();"
            f"body.set('username', {json.dumps(user)}); body.set('password', {json.dumps(password)});"
            "const r = await fetch('/api/welcome/login?lang=el', {method: 'POST', body, credentials: 'include'});"
            "let msg = ''; try { msg = (await r.clone().json()).message || ''; } catch (e) {}"
            "const cs = await fetch('/api/authentication/checkSession?lang=el', {credentials: 'include'});"
            "let s = null; try { s = await cs.json(); } catch (e) {}"
            "return JSON.stringify({status: r.status, message: msg,"
            " username: (s && s.session && s.session.username) || ''});"
            "})()")


def login_in_page(cdp: Any, user: str, password: str) -> dict[str, Any]:
    raw = cdp.evaluate(login_expression(user, password), await_promise=True)
    try:
        res = json.loads(raw or "{}")
    except ValueError:
        return {"ok": False, "reason": "PageError"}
    if res.get("reason"):
        return {"ok": False, "reason": res["reason"]}
    if str(res.get("username", "")) == str(user) and res.get("status", 500) < 400:
        cdp.call("Page.reload", {"ignoreCache": False})
        return {"ok": True, "reason": ""}
    return {"ok": False, "reason": "InvalidCredentials", "message": res.get("message", "")}


def _page_target(port: int, timeout: float) -> Optional[dict[str, Any]]:
    host = urlparse(GEMI_HOME).hostname
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            for t in _json(f"http://127.0.0.1:{port}/json/list"):
                if t.get("type") == "page" and urlparse(t.get("url", "")).hostname == host:
                    return t
        except (OSError, ValueError):
            pass
        time.sleep(0.4)
    return None


def open_logged_in(user: str, password: str, progress: Callable[[str], None] = lambda _m: None,
                   launcher: Callable[..., Any] = subprocess.Popen, timeout: float = 40.0) -> dict[str, Any]:
    """Ανοίγει τον τοπικό browser συνδεδεμένο στο Γ.Ε.ΜΗ. -> {ok, reason, message}."""
    exe = find_browser()
    if not exe:
        return {"ok": False, "reason": "NoBrowser"}
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    port = _active_port(profile)
    progress("Άνοιγμα browser…")
    if _alive(port):
        try:
            _json(f"http://127.0.0.1:{port}/json/new?{GEMI_HOME}", method="PUT")
        except (OSError, ValueError):
            launcher([exe, f"--user-data-dir={profile}", GEMI_HOME])
    else:
        try:
            (profile / "DevToolsActivePort").unlink()
        except OSError:
            pass
        launcher([exe, "--remote-debugging-port=0", f"--user-data-dir={profile}", "--no-first-run",
                  "--no-default-browser-check", GEMI_HOME])
        deadline = time.monotonic() + timeout
        port = None
        while time.monotonic() < deadline and not _alive(port):
            time.sleep(0.4)
            port = _active_port(profile)
        if not _alive(port):
            return {"ok": False, "reason": "BrowserTimeout"}
    progress("Φόρτωση Γ.Ε.ΜΗ.…")
    target = _page_target(port, timeout)
    if not target or not target.get("webSocketDebuggerUrl"):
        return {"ok": False, "reason": "PageError"}
    cdp = Cdp(target["webSocketDebuggerUrl"])
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and cdp.evaluate("document.readyState") != "complete":
            time.sleep(0.4)
        progress("Σύνδεση στο Γ.Ε.ΜΗ.…")
        res = login_in_page(cdp, user, password)
    finally:
        cdp.close()
    log.info("[gemi-browser] σύνδεση %s", "OK" if res["ok"] else res["reason"])     # ποτέ κωδικοί
    return res
