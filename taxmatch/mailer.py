"""Email προς τους πελάτες: οφειλές ΑΑΔΕ (με τα PDF των ταυτοτήτων οφειλής) και νέα που τους αφορούν — χειροκίνητα από
την καρτέλα πελάτη ή αυτόματα στον καθημερινό έλεγχο (ρητό αίτημα χρήστη 2026-10-22).

* Αποστολή με SMTP του γραφείου (Ρυθμίσεις → Email): `smtp_host/port/security/user/pass/from`· ο κωδικός SMTP
  κρυπτογραφημένος (`settings_store` secret), ποτέ σε logs.
* Παραλήπτης: το email του πελάτη από το Μητρώο ΑΑΔΕ (`getLdapInfo`) ή όπως το διόρθωσε ο χρήστης.
* Κείμενο σε δύο μορφές (σκέτο + HTML) — όπως το ScanMyData Tax Center (`MailTemplates.kt`): πολλοί clients δεν δείχνουν
  HTML. ΠΟΤΕ κωδικοί πρόσβασης μέσα στο μήνυμα.
* Κάθε αποστολή → `email_log` (και αποτυχίες). Οι αυτόματες ΔΕΝ ξαναστέλνουν το ίδιο: οφειλές μόνο όταν αλλάξουν
  (`fingerprint` = ταυτότητες + ποσά), νέα μόνο όσα δεν έχουν σταλεί ήδη στον πελάτη.
"""
from __future__ import annotations

import html
import logging
import mimetypes
import re
import smtplib
import sqlite3
import ssl
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Callable, Optional

from . import db, debts, office, settings_store

log = logging.getLogger(__name__)
EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]{2,}$")
KIND_LABEL = {"debts": "Οφειλές", "news": "Νέα", "test": "Δοκιμή"}
MAX_ATTACH_BYTES = 15 * 1024 * 1024            # τα περισσότερα SMTP κόβουν στα 20-25MB μετά το base64


@dataclass
class Message:
    to: str
    subject: str
    text: str
    html: str
    attachments: list[Path] = field(default_factory=list)
    fingerprint: str = ""
    items: int = 0


class MailError(Exception):
    pass


# ------------------------------------------------------------------ ρυθμίσεις / αποστολή
def configured(conn: sqlite3.Connection) -> bool:
    return bool(settings_store.get(conn, "smtp_host") and sender(conn))


def sender(conn: sqlite3.Connection) -> str:
    return (settings_store.get(conn, "smtp_from") or settings_store.get(conn, "office_email")
            or settings_store.get(conn, "smtp_user")).strip()


def _signature(conn: sqlite3.Connection) -> str:
    custom = settings_store.get(conn, "email_signature").strip()
    if custom:
        return custom
    o = {k: settings_store.get(conn, k) for k in ("office_name", "office_phone", "office_email")}
    lines = ["Με εκτίμηση,", o["office_name"]] + [x for x in (o["office_phone"], o["office_email"]) if x]
    return "\n".join(x for x in lines if x)


def build_mime(conn: sqlite3.Connection, msg: Message) -> EmailMessage:
    m = EmailMessage()
    name = settings_store.get(conn, "office_name")
    m["From"] = formataddr((name, sender(conn))) if name else sender(conn)
    m["To"] = msg.to
    bcc = settings_store.get(conn, "email_bcc").strip()
    if bcc:
        m["Bcc"] = bcc
    m["Subject"] = msg.subject
    m["Message-ID"] = make_msgid(domain=(sender(conn).split("@")[-1] or "taxmatch.local"))
    m.set_content(msg.text)
    m.add_alternative(msg.html, subtype="html")
    total = 0
    for path in msg.attachments:
        data = path.read_bytes()
        total += len(data)
        if total > MAX_ATTACH_BYTES:
            raise MailError("Τα συνημμένα ξεπερνούν τα 15MB — στείλτε λιγότερα PDF.")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        maintype, subtype = ctype.split("/", 1)
        m.add_attachment(data, maintype=maintype, subtype=subtype, filename=path.name)
    return m


def _smtp_send(conn: sqlite3.Connection, mime: EmailMessage) -> None:
    host = settings_store.get(conn, "smtp_host").strip()
    port = int(settings_store.get(conn, "smtp_port") or "587")
    security = settings_store.get(conn, "smtp_security") or "starttls"
    user, password = settings_store.get(conn, "smtp_user").strip(), settings_store.get(conn, "smtp_pass")
    ctx = ssl.create_default_context()
    smtp: smtplib.SMTP = (smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) if security == "ssl"
                          else smtplib.SMTP(host, port, timeout=60))
    try:
        if security == "starttls":
            smtp.starttls(context=ctx)
        if user and password:
            smtp.login(user, password)
        smtp.send_message(mime)
    finally:
        try:
            smtp.quit()
        except smtplib.SMTPException:
            pass


def send(conn: sqlite3.Connection, afm: str, kind: str, msg: Message, auto: bool = False,
         transport: Optional[Callable[[sqlite3.Connection, EmailMessage], None]] = None) -> tuple[bool, str]:
    """Στέλνει και καταγράφει. (ok, μήνυμα για τον χρήστη)."""
    transport = transport or _smtp_send
    if not configured(conn):
        return False, "Δεν έχουν οριστεί στοιχεία SMTP (Ρυθμίσεις → Email)."
    if not EMAIL_RE.match(msg.to or ""):
        return False, "Ο πελάτης δεν έχει έγκυρο email."
    status, error = "sent", ""
    try:
        transport(conn, build_mime(conn, msg))
    except (MailError, OSError, smtplib.SMTPException) as exc:
        status, error = "failed", _friendly(exc)
    conn.execute("INSERT INTO email_log(afm, kind, recipient, subject, fingerprint, auto, status, error, sent_at) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (afm, kind, msg.to, msg.subject, msg.fingerprint, int(auto), status, error,
                                                db.utcnow()))
    if status == "sent":
        return True, f"Το email στάλθηκε στο {msg.to}."
    log.warning("email %s/%s: %s", afm, kind, error)
    return False, f"Το email δεν στάλθηκε: {error}"


def _friendly(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "ο διακομιστής SMTP απέρριψε το όνομα χρήστη/κωδικό (για Gmail/Outlook χρειάζεται «κωδικός εφαρμογής»)."
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "ο διακομιστής απέρριψε τη διεύθυνση του παραλήπτη."
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not isinstance(exc, smtplib.SMTPException):
        return f"δεν έγινε σύνδεση στον διακομιστή SMTP ({str(exc)[:80]})."
    return str(exc)[:160]


def history(conn: sqlite3.Connection, afm: str, limit: int = 30) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM email_log WHERE afm=? ORDER BY id DESC LIMIT ?", (afm, limit))]


# ------------------------------------------------------------------ σύνθεση μηνυμάτων
def _client(conn: sqlite3.Connection, afm: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM businesses WHERE afm=?", (afm,)).fetchone()
    return dict(row) if row else {"afm": afm, "name": afm, "email": ""}


def _wrap(intro: str, blocks_text: list[str], blocks_html: list[str], closing: str, signature: str) -> tuple[str, str]:
    text = intro + "\n\n" + "\n\n".join(blocks_text) + ("\n\n" + closing if closing else "") + "\n\n" + signature
    body = "".join(blocks_html)
    sig = html.escape(signature).replace("\n", "<br>")
    page = ("<div style=\"font-family:system-ui,Arial,sans-serif;font-size:14px;color:#0B1B2B\">"
            f"<p>{html.escape(intro)}</p>{body}"
            + (f"<p>{html.escape(closing)}</p>" if closing else "") + f"<p style=\"color:#41546b\">{sig}</p></div>")
    return text, page


def _debt_parts(d: dict[str, Any]) -> list[str]:
    """Οι γραμμές μιας οφειλής στο μήνυμα: ποσά, η δόση που χρωστιέται ΤΩΡΑ (η παλαιότερη απλήρωτη — όχι «η επόμενη
    χρονικά», βλ. Tax Center `Line.next`), λήξη, ταυτότητα πληρωμής."""
    parts = []
    if d["kind"] == "declaration":
        a = d["details"]
        if a.get("total_due"):
            parts.append(f"Ποσό: {office.money(a['total_due'])}")
        if a.get("installment") and a.get("installment") != a.get("total_due"):
            parts.append(f"Δόση: {office.money(a['installment'])}")
        if a.get("pay_by"):
            parts.append(f"Πληρωμή έως: {a['pay_by']}")
    else:
        if d["overdue"]:
            parts.append(f"Ληξιπρόθεσμο: {office.money(d['overdue'])}")
        if d["non_overdue"] and d["kind"] not in ("efka",):
            parts.append(f"{'Σε ρύθμιση' if d['kind'] == 'carrier' else 'Εμπρόθεσμο'}: {office.money(d['non_overdue'])}")
        if d["total"] and not (d["overdue"] or d["non_overdue"]) or d["kind"] in ("carrier", "keao_arrangement", "efka"):
            parts.append(f"{'Πληρωτέο' if d['kind'] == 'efka' else 'Υπόλοιπο'}: {office.money(d['total'])}")
        if d.get("next_due"):
            late = _is_late(d["next_due"])
            amount = f" · {office.money(d['next_amount'])}" if d.get("next_amount") else ""
            label = "Εμπρόθεσμη πληρωμή έως" if d["kind"] == "efka" else ("Δόση που έληξε" if late else "Επόμενη δόση")
            parts.append(f"{label}: {d['next_due']}{amount}"
                         + (f" ({d['unpaid']} από {d['instalments']} δόσεις απλήρωτες)" if d.get("instalments") else ""))
    if d["code"]:
        label = "Ταυτότητα Οφειλέτη" if d["kind"] in ("carrier", "keao_arrangement") else "Ταυτότητα πληρωμής (RF)"
        parts.append(f"{label}: {d['code']}")
    return parts


def _is_late(due: str) -> bool:
    try:
        d, m, y = (int(x) for x in due.split("/"))
        return date(y, m, d) < date.today()
    except ValueError:
        return False


def compose_debts(conn: sqlite3.Connection, afm: str) -> Optional[Message]:
    """Οι οφειλές του πελάτη ανά πύλη (ΑΑΔΕ, ΚΕΑΟ — ανά φορέα με τη δική του Ταυτότητα Οφειλέτη, τρέχουσες εισφορές ΕΦΚΑ,
    χρεωστικές δηλώσεις) με ταυτότητα πληρωμής, δόσεις και τα PDF (ταυτότητες, δοσολόγια) ως συνημμένα. None = καμία."""
    c = _client(conn, afm)
    rows = [d for d in debts.client_debts(conn, afm) if (d["total"] or d["overdue"] or d["non_overdue"])]
    if not rows:
        return None
    groups = (("aade", "ΑΑΔΕ"), ("keao", "ΚΕΑΟ (ανά φορέα — κάθε φορέας με τη δική του Ταυτότητα Οφειλέτη)"),
              ("efka", "e-ΕΦΚΑ — τρέχουσες εισφορές"))
    blocks_t, blocks_h, files = [], [], []
    for source, heading in groups:
        mine = [d for d in rows if d.get("source", "aade") == source]
        if not mine:
            continue
        lines_t, lines_h = [], []
        for d in mine:
            parts = _debt_parts(d)
            title = f"{debts.KIND_LABEL.get(d['kind'], '')}: {d['title']}".strip(": ")
            lines_t.append("• " + title + ("\n    " + "\n    ".join(parts) if parts else ""))
            lines_h.append(f"<li style=\"margin-bottom:8px\"><b>{html.escape(title)}</b><br>"
                           + "<br>".join(html.escape(p) for p in parts) + "</li>")
            if d["pdf_path"] and Path(d["pdf_path"]).exists() and Path(d["pdf_path"]) not in files:
                files.append(Path(d["pdf_path"]))
        blocks_t.append(heading.upper() + "\n" + "\n\n".join(lines_t))
        blocks_h.append(f"<h3 style=\"margin:14px 0 6px 0\">{html.escape(heading)}</h3><ul>{''.join(lines_h)}</ul>")
    sync = debts.sync_info(conn, afm) or {}
    totals = ""
    if sync.get("status") == "ok" and (sync.get("overdue") or sync.get("non_overdue")):
        totals = (f"Σύνολο στην ΑΑΔΕ: ληξιπρόθεσμο {office.money(sync['overdue'])} · εμπρόθεσμο "
                  f"{office.money(sync['non_overdue'])}.")
    intro = (f"Αγαπητέ/ή {c.get('name') or ''},\n\nσας ενημερώνουμε για τις οφειλές σας (ΑΦΜ {afm}) όπως εμφανίζονται "
             f"σήμερα ({date.today().strftime('%d/%m/%Y')})." + (f"\n{totals}" if totals else ""))
    closing = ("Στα συνημμένα θα βρείτε τις ταυτότητες οφειλής και τα δοσολόγια για την πληρωμή (e-banking/ΔΙΑΣ, με τον "
               "κωδικό πληρωμής). " if files else "") + "Για οποιαδήποτε διευκρίνιση είμαστε στη διάθεσή σας."
    text, page = _wrap(intro, blocks_t, blocks_h, closing, _signature(conn))
    return Message(c.get("email") or "", f"Οφειλές — {c.get('name') or afm}", text, page, files,
                   debts.fingerprint(rows), len(rows))


def _sent_articles(conn: sqlite3.Connection, afm: str) -> set[str]:
    out: set[str] = set()
    for r in conn.execute("SELECT fingerprint FROM email_log WHERE afm=? AND kind='news' AND status='sent'", (afm,)):
        out.update(x for x in (r["fingerprint"] or "").split(",") if x)
    return out


def compose_news(conn: sqlite3.Connection, afm: str, days: int = 30, only_new: bool = False) -> Optional[Message]:
    """Νέα που αφορούν ΣΥΓΚΕΚΡΙΜΕΝΑ τον πελάτη (όχι τα «για όλες τις επιχειρήσεις», όχι 👎, όχι «να επιβεβαιωθεί»),
    με περίληψη, προθεσμία και σύνδεσμο. `only_new`: μόνο όσα δεν του έχουν ήδη σταλεί."""
    from .matching import engine
    from .matching.match import ALL_SCOPE_REASON
    c = _client(conn, afm)
    sent = _sent_articles(conn, afm) if only_new else set()
    rows = [r for r in engine.digest(conn, days=days, afm=afm)
            if r["matched_reason"] != ALL_SCOPE_REASON and r["user_feedback"] != -1
            and (r["confidence"] >= 1 or r["user_feedback"] == 1) and str(r["article_id"]) not in sent]
    if not rows:
        return None
    lines_t, lines_h = [], []
    for r in rows:
        deadline = f"Προθεσμία: {date.fromisoformat(r['deadline']).strftime('%d/%m/%Y')}" if r.get("deadline") else ""
        summary = (r.get("summary") or "").strip()
        lines_t.append("• " + r["title"] + ("\n  " + summary if summary else "") + ("\n  " + deadline if deadline else "")
                       + (f"\n  {r['url']}" if r.get("url") else ""))
        lines_h.append(f"<li style=\"margin-bottom:10px\"><b>{html.escape(r['title'])}</b>"
                       + (f"<br>{html.escape(summary)}" if summary else "")
                       + (f"<br><b style=\"color:#b45309\">{html.escape(deadline)}</b>" if deadline else "")
                       + (f"<br><a href=\"{html.escape(r['url'])}\">Περισσότερα</a>" if r.get("url") else "") + "</li>")
    intro = (f"Αγαπητέ/ή {c.get('name') or ''},\n\nσας ενημερώνουμε για πρόσφατες φορολογικές/εργατικές εξελίξεις "
             "που αφορούν τη δραστηριότητά σας:")
    closing = "Επικοινωνήστε μαζί μας αν χρειάζεται κάποια ενέργεια από μέρους σας."
    text, page = _wrap(intro, ["\n\n".join(lines_t)], [f"<ul>{''.join(lines_h)}</ul>"], closing, _signature(conn))
    return Message(c.get("email") or "", f"Ενημέρωση — {len(rows)} {'νέο' if len(rows) == 1 else 'νέα'} που σας αφορούν",
                   text, page, [], ",".join(sorted(str(r["article_id"]) for r in rows)), len(rows))


def compose_test(conn: sqlite3.Connection, to: str) -> Message:
    text, page = _wrap("Δοκιμαστικό μήνυμα από το TaxMatch.", ["Οι ρυθμίσεις SMTP λειτουργούν."],
                       ["<p>Οι ρυθμίσεις SMTP λειτουργούν.</p>"], "", _signature(conn))
    return Message(to, "TaxMatch — δοκιμαστικό email", text, page)


# ------------------------------------------------------------------ αυτόματη αποστολή (καθημερινός έλεγχος)
def _last_sent(conn: sqlite3.Connection, afm: str, kind: str) -> Optional[dict[str, Any]]:
    row = conn.execute("SELECT * FROM email_log WHERE afm=? AND kind=? AND status='sent' ORDER BY id DESC LIMIT 1",
                       (afm, kind)).fetchone()
    return dict(row) if row else None


def news_due(conn: sqlite3.Connection, today: Optional[date] = None) -> bool:
    """Συχνότητα αυτόματης αποστολής νέων: καθημερινά ή εβδομαδιαία (ημέρα 1 = Δευτέρα … 7 = Κυριακή)."""
    today = today or date.today()
    if settings_store.get(conn, "auto_email_news_freq") != "weekly":
        return True
    return today.isoweekday() == int(settings_store.get(conn, "auto_email_weekday") or "1")


def run_scheduled(conn: sqlite3.Connection, today: Optional[date] = None, refresh_debts: bool = True,
                  transport: Optional[Callable] = None, fetch_debts: Optional[Callable] = None) -> dict[str, int]:
    """Αυτόματα email: οφειλές (μόνο όταν άλλαξαν από το τελευταίο email) και νέα (μόνο όσα δεν έχουν σταλεί).
    `refresh_debts`: φρεσκάρει πρώτα τις οφειλές από την ΑΑΔΕ για όσους πελάτες θα λάβουν email."""
    stats = {"debts": 0, "news": 0, "failed": 0, "skipped": 0}
    if not configured(conn):
        return stats
    want_debts = settings_store.get(conn, "auto_email_debts") == "1"
    want_news = settings_store.get(conn, "auto_email_news") == "1" and news_due(conn, today)
    if not (want_debts or want_news):
        return stats
    fetch_debts = fetch_debts or debts.check_client
    # email_auto = 0: ο χρήστης εξαίρεσε τον πελάτη από τα αυτόματα email (καρτέλα πελάτη → Προφίλ)
    # Όχι αυτόματα σε email ΕΚΠΡΟΣΩΠΟΥ/ΛΟΓΙΣΤΗ του Μητρώου (mailemep — ο πελάτης δεν έχει δηλώσει δικό του): θα έφευγαν
    # στο γραφείο, όχι στον πελάτη. Αν ο χρήστης το επιβεβαιώσει/διορθώσει (contact_source='manual'), στέλνεται κανονικά.
    for b in conn.execute("SELECT afm FROM businesses WHERE email<>'' AND email_auto=1 AND NOT (contact_source='aade' "
                          "AND email_origin='mailemep') ORDER BY afm").fetchall():
        afm = b["afm"]
        if want_debts:
            has_creds = conn.execute("SELECT 1 FROM client_credentials WHERE afm=?", (afm,)).fetchone() is not None
            if refresh_debts and has_creds:
                try:
                    fetch_debts(conn, afm, want_pdf=False)          # «μόνο δεδομένα» — γρήγορα, για να κριθεί αν άλλαξε
                except Exception as exc:                      # noqa: BLE001 — ένας πελάτης δεν σταματά τους άλλους
                    log.warning("οφειλές %s: %s", afm, exc)
            msg = compose_debts(conn, afm)
            last = _last_sent(conn, afm, "debts")
            if msg and not (last and last["fingerprint"] == msg.fingerprint):
                if has_creds:
                    # Άλλαξε: τώρα και τα έντυπα (ταυτότητες, δοσολόγια), ώστε το email να φύγει με τα σωστά συνημμένα.
                    try:
                        fetch_debts(conn, afm, want_pdf=True)
                    except Exception as exc:                  # noqa: BLE001
                        log.warning("έντυπα οφειλών %s: %s", afm, exc)
                    msg = compose_debts(conn, afm) or msg
                ok, _ = send(conn, afm, "debts", msg, auto=True, transport=transport)
                stats["debts" if ok else "failed"] += 1
            else:
                stats["skipped"] += 1
        if want_news:
            msg = compose_news(conn, afm, days=int(settings_store.get(conn, "auto_email_news_days") or "14"),
                               only_new=True)
            if msg:
                ok, _ = send(conn, afm, "news", msg, auto=True, transport=transport)
                stats["news" if ok else "failed"] += 1
    return stats

