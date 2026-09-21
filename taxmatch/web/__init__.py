"""Flask app: μόνο 127.0.0.1, με per-launch token ώστε να μην μπορεί άλλη διεργασία/ιστοσελίδα να μιλήσει στην εφαρμογή."""
from __future__ import annotations

import hmac
import secrets
from datetime import date, datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

from flask import Flask, abort, g, make_response, redirect, request

from .. import APP_TITLE, __version__, db, jobs
from ..identifiers import format_kad
from ..ingestion import sources

COOKIE = "tm_auth"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

MONTHS_EL = ["Ιανουάριος", "Φεβρουάριος", "Μάρτιος", "Απρίλιος", "Μάιος", "Ιούνιος", "Ιούλιος", "Αύγουστος",
             "Σεπτέμβριος", "Οκτώβριος", "Νοέμβριος", "Δεκέμβριος"]
WEEKDAYS_EL = ["Δευ", "Τρι", "Τετ", "Πέμ", "Παρ", "Σαβ", "Κυρ"]


def create_app(token: Optional[str] = None, testing: bool = False) -> Flask:
    """`token=None` απενεργοποιεί τον έλεγχο (μόνο για ενσωμάτωση/δοκιμές χωρίς HTTP)."""
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(SECRET_KEY=secrets.token_hex(32), TM_TOKEN=token, TESTING=testing,
                      MAX_CONTENT_LENGTH=16 * 1024 * 1024, SESSION_COOKIE_SAMESITE="Strict",
                      SESSION_COOKIE_HTTPONLY=True)
    app.extensions["jobs"] = jobs.JobManager()

    @app.before_request
    def guard():
        tok = app.config["TM_TOKEN"]
        if tok is None:
            return None
        host = (request.host or "").rsplit(":", 1)[0].strip("[]").lower()
        if host not in ALLOWED_HOSTS:                       # DNS rebinding
            abort(403)
        if request.method in UNSAFE:                        # CSRF: Origin/Sec-Fetch-Site πρέπει να είναι ίδια προέλευση
            origin = request.headers.get("Origin")
            if origin and urlsplit(origin).netloc != request.host:
                abort(403)
            if request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
                abort(403)
        cookie = request.cookies.get(COOKIE, "")
        if hmac.compare_digest(cookie, tok):
            return None
        supplied = request.args.get("t", "")
        if supplied and hmac.compare_digest(supplied, tok):
            resp = make_response(redirect(request.path))
            resp.set_cookie(COOKIE, tok, httponly=True, samesite="Strict")
            return resp
        abort(403)

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    @app.errorhandler(403)
    def forbidden(_e):
        return ("Δεν επιτρέπεται η πρόσβαση. Ανοίξτε την εφαρμογή από το εικονίδιο TaxMatch.", 403)

    # ------------------------------------------------------------ template helpers
    @app.template_filter("date_el")
    def date_el(value: Optional[str]) -> str:
        if not value:
            return "—"
        try:
            return date.fromisoformat(value[:10]).strftime("%d/%m/%Y")
        except ValueError:
            return value

    @app.template_filter("datetime_el")
    def datetime_el(value: Optional[str]) -> str:
        if not value:
            return "—"
        try:
            dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).astimezone()
            return dt.strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return value

    @app.template_filter("kad")
    def kad_filter(code: str) -> str:
        return format_kad(code)

    @app.template_filter("due")
    def due_filter(value: Optional[str]) -> dict:
        """Προθεσμία -> {label, cls} (overdue | today | soon | later)."""
        if not value:
            return {"label": "", "cls": "none", "days": None}
        try:
            n = (date.fromisoformat(value[:10]) - date.today()).days
        except ValueError:
            return {"label": "", "cls": "none", "days": None}
        if n < 0:
            return {"label": f"έληξε πριν {-n} ημ.", "cls": "overdue", "days": n}
        if n == 0:
            return {"label": "σήμερα", "cls": "today", "days": 0}
        if n == 1:
            return {"label": "αύριο", "cls": "soon", "days": 1}
        return {"label": f"σε {n} ημ.", "cls": "soon" if n <= 7 else "later", "days": n}

    @app.template_filter("source_name")
    def source_name(source_id: str) -> str:
        src = sources.BY_ID.get(source_id)
        return src.name if src else source_id

    @app.context_processor
    def inject():
        return {"app_title": APP_TITLE, "app_version": __version__, "months_el": MONTHS_EL, "weekdays_el": WEEKDAYS_EL}

    from .views import bp
    app.register_blueprint(bp)
    return app


def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db
