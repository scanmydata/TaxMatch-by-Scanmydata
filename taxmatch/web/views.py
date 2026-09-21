"""Οθόνες: Dashboard, Πελάτες, Ημερολόγιο, Νέα & Matches, Ρυθμίσεις + μικρό JSON API."""
from __future__ import annotations

import calendar as pycal
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import csv
import io

from flask import (Blueprint, Response, abort, current_app, flash, jsonify, redirect, render_template, request, url_for)

from .. import __version__, config, crypto, deadlines, pipeline, scheduler_win, settings_store
from ..business_profiles import credentials as client_creds, import_excel, lookup_aade, service as clients, vies
from ..extraction import llm_extract
from ..identifiers import is_valid_afm, normalize_afm
from ..ingestion import sources
from ..matching import engine
from . import MONTHS_EL, get_db

bp = Blueprint("main", __name__)

DAY_CHOICES = (1, 3, 7, 14, 30)
SECRET_KEYS = ["groq_api_key", "openrouter_api_key", "business_portal_key", "aade_user", "aade_pass"]


def _jobs():
    return current_app.extensions["jobs"]


def _int_arg(name: str, default: int, allowed: Optional[tuple] = None) -> int:
    try:
        v = int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default
    return v if (allowed is None or v in allowed) else default


@bp.errorhandler(crypto.KeyUnavailable)
def key_unavailable(exc):
    return render_template("error.html", title="Μη διαθέσιμο κλειδί κρυπτογράφησης", message=crypto.KeyUnavailable.message_el), 500


# ================================================================== Dashboard

@bp.route("/")
def dashboard():
    conn = get_db()
    days = _int_arg("days", 3, DAY_CHOICES)
    view = request.args.get("view", "articles")
    groups = engine.digest_articles(conn, days)
    by_client: list[dict[str, Any]] = []
    if view == "clients":
        bucket: dict[str, dict[str, Any]] = {}
        for r in engine.digest(conn, days=days, limit=20000):
            c = bucket.setdefault(r["afm"], {"afm": r["afm"], "name": r["business_name"], "items": []})
            c["items"].append(r)
        by_client = sorted(bucket.values(), key=lambda c: (-len(c["items"]), c["name"]))

    today = date.today()
    upcoming = deadlines.events_between(conn, today, today + timedelta(days=21))
    urgent = sum(1 for ev in upcoming if (date.fromisoformat(ev["date"]) - today).days <= 7)
    total_clients = clients.count(conn)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    kpi = {
        "clients": total_clients,
        "articles": conn.execute("SELECT COUNT(*) FROM articles WHERE COALESCE(published_at,fetched_at)>=?", (cutoff,)).fetchone()[0],
        "matched_articles": len(groups),
        "urgent": urgent,
    }
    pending = conn.execute("SELECT COUNT(*) FROM articles WHERE extraction_status='pending'").fetchone()[0]
    alerts = []
    if total_clients == 0:
        alerts.append(("info", "Δεν υπάρχουν πελάτες ακόμη.", url_for("main.clients_import"), "Εισαγωγή πελατών"))
    if not settings_store.is_set(conn, settings_store_llm_key(conn)):
        alerts.append(("warn", "Δεν έχει οριστεί API key για το LLM — τα άρθρα δεν αναλύονται.", url_for("main.settings"), "Ρυθμίσεις"))
    pending_clients = conn.execute("SELECT COUNT(*) FROM businesses WHERE lookup_status='pending'").fetchone()[0]
    if pending_clients and not (settings_store.is_set(conn, "business_portal_key") or settings_store.is_set(conn, "aade_user")):
        alerts.append(("warn", f"{pending_clients} πελάτες περιμένουν εμπλουτισμό στοιχείων (ΓΕΜΗ/ΑΑΔΕ) — χρειάζονται credentials.",
                       url_for("main.settings"), "Ρυθμίσεις"))
    run = pipeline.last_run(conn)
    if run and run["status"] == "error":
        alerts.append(("danger", f"Ο τελευταίος έλεγχος απέτυχε: {run['error'] or 'άγνωστο σφάλμα'}", None, None))
    return render_template("dashboard.html", nav="dashboard", days=days, day_choices=DAY_CHOICES, view=view, groups=groups,
                           by_client=by_client, upcoming=upcoming[:12], kpi=kpi, alerts=alerts, run=run, pending=pending)


def settings_store_llm_key(conn) -> str:
    return "openrouter_api_key" if settings_store.get(conn, "llm_provider") == "openrouter" else "groq_api_key"


# ================================================================== Πελάτες

@bp.route("/clients")
def clients_list():
    conn = get_db()
    q, kad = request.args.get("q", "").strip(), request.args.get("kad", "").strip()
    books, status = request.args.get("books", ""), request.args.get("status", "")
    rows = clients.list_all(conn, q=q, kad=kad, books=books, status=status)
    counts = {r["afm"]: r["n"] for r in conn.execute("SELECT afm, COUNT(*) AS n FROM matches GROUP BY afm")}
    return render_template("clients_list.html", nav="clients", rows=rows, q=q, kad=kad, books=books, status=status,
                           match_counts=counts, total=clients.count(conn), creds=client_creds.status_map(conn))


@bp.route("/clients/new", methods=["GET", "POST"])
def clients_new():
    conn = get_db()
    if request.method == "POST":
        afm = normalize_afm(request.form.get("afm"))
        name = request.form.get("name", "").strip()
        if not afm:
            flash("Ο ΑΦΜ πρέπει να είναι 9ψήφιος αριθμός.", "danger")
            return render_template("client_new.html", nav="clients", form=request.form), 400
        if not is_valid_afm(afm):
            flash("Ο ΑΦΜ δεν περνά τον έλεγχο ψηφίου ελέγχου — ελέγξτε τον. Καταχωρήθηκε όμως.", "warn")
        if not clients.add(conn, afm, name, source="manual"):
            flash("Ο πελάτης υπάρχει ήδη.", "warn")
            return redirect(url_for("main.client_detail", afm=afm))
        user, pwd = request.form.get("taxis_user", "").strip(), request.form.get("taxis_pass", "")
        if bool(user) != bool(pwd):
            flash("Οι κωδικοί TAXISnet θέλουν και χρήστη και κωδικό — δεν αποθηκεύτηκαν.", "warn")
        elif user:
            client_creds.set_(conn, afm, user, pwd)
        _start_lookup(afm)
        flash("Ο πελάτης προστέθηκε. Ανάκτηση στοιχείων σε εξέλιξη…", "ok")
        return redirect(url_for("main.client_detail", afm=afm))
    return render_template("client_new.html", nav="clients", form={})


@bp.get("/api/lookup-afm")
def api_lookup_afm():
    """Επωνυμία/διεύθυνση από ΑΦΜ για τον διάλογο «Νέος πελάτης»: πρώτα ό,τι ξέρουμε ήδη, μετά VIES (χωρίς key)."""
    afm = normalize_afm(request.args.get("afm"))
    if len(afm) != 9:
        return jsonify(ok=False, error="Το ΑΦΜ πρέπει να έχει 9 ψηφία."), 400
    conn = get_db()
    row = conn.execute("SELECT name, address FROM businesses WHERE afm=?", (afm,)).fetchone()
    if row and row["name"]:
        return jsonify(ok=True, source="local", name=row["name"], address=row["address"], exists=True,
                       checksum_ok=is_valid_afm(afm))
    res = vies.lookup(afm)
    if res.error:
        return jsonify(ok=False, error=f"{res.error} — γράψτε την επωνυμία χειροκίνητα.")
    if not res.valid or not res.name:
        return jsonify(ok=False, error="Το ΑΦΜ δεν βρέθηκε στο VIES (π.χ. μη υπόχρεος ΦΠΑ)· γράψτε την επωνυμία χειροκίνητα.")
    return jsonify(ok=True, source="vies", name=res.name, address=res.address, exists=bool(row),
                   checksum_ok=is_valid_afm(afm))


def _start_lookup(afm: str) -> bool:
    from .. import db as dbmod

    def work(progress):
        progress(f"Ανάκτηση στοιχείων για ΑΦΜ {afm}…")
        c = dbmod.connect()
        try:
            out = clients.lookup_and_store(c, afm)
            engine.rematch(c)
            return out
        finally:
            c.close()
    return _jobs().start("lookup", work)


@bp.route("/clients/import", methods=["GET", "POST"])
def clients_import():
    imports: dict = current_app.extensions.setdefault("imports", {})
    if request.method == "POST":
        f = request.files.get("file")
        if not f or not f.filename:
            flash("Επιλέξτε αρχείο .xlsx ή .csv.", "danger")
            return redirect(url_for("main.clients_import"))
        try:
            res = import_excel.parse_file(f.filename, f.read())
        except ValueError as exc:
            flash(str(exc), "danger")
            return redirect(url_for("main.clients_import"))
        except Exception as exc:                        # κατεστραμμένο/κλειδωμένο αρχείο
            flash(f"Δεν ήταν δυνατή η ανάγνωση του αρχείου: {exc}", "danger")
            return redirect(url_for("main.clients_import"))
        token = secrets.token_urlsafe(12)
        if len(imports) > 5:
            imports.pop(next(iter(imports)))
        imports[token] = res
        conn = get_db()
        existing = {r["afm"] for r in conn.execute("SELECT afm FROM businesses")}
        return render_template("clients_import_preview.html", nav="clients", res=res, token=token,
                               new_count=sum(1 for r in res.rows if r.afm not in existing),
                               existing_count=sum(1 for r in res.rows if r.afm in existing))
    return render_template("clients_import.html", nav="clients")


@bp.post("/clients/import/confirm")
def clients_import_confirm():
    imports: dict = current_app.extensions.setdefault("imports", {})
    res = imports.pop(request.form.get("token", ""), None)
    if res is None:
        flash("Η προεπισκόπηση έληξε — ανεβάστε ξανά το αρχείο.", "warn")
        return redirect(url_for("main.clients_import"))
    conn = get_db()
    out = clients.import_result(conn, res)
    engine.rematch(conn)
    flash(f"Προστέθηκαν {out['added']} πελάτες" + (f", ενημερώθηκαν {out['updated']}" if out["updated"] else "")
          + (f", αποθηκεύτηκαν κωδικοί TAXISnet για {out['credentials']}" if out["credentials"] else "")
          + ". Ο εμπλουτισμός στοιχείων γίνεται στο παρασκήνιο.", "ok")
    if out["added"] or out["credentials"]:
        _start_enrich()
    return redirect(url_for("main.clients_list"))


def _start_enrich() -> bool:
    from .. import db as dbmod

    def work(progress):
        c = dbmod.connect()
        try:
            out = clients.enrich_pending(c, limit=10_000, on_progress=progress)
            engine.rematch(c)
            return out
        finally:
            c.close()
    return _jobs().start("enrich", work)


@bp.post("/clients/enrich")
def clients_enrich():
    if _start_enrich():
        flash("Ξεκίνησε ο εμπλουτισμός στοιχείων πελατών.", "ok")
    else:
        flash("Τρέχει ήδη άλλη εργασία.", "warn")
    return redirect(request.referrer or url_for("main.clients_list"))


@bp.route("/clients/<afm>")
def client_detail(afm: str):
    conn = get_db()
    b = clients.get(conn, afm)
    if not b:
        abort(404)
    matches = engine.digest(conn, days=90, afm=afm)
    today = date.today()
    events = deadlines.events_between(conn, today, today + timedelta(days=45), afm=afm, include_conditional=False)
    try:
        raw = json.loads(b["lookup_raw"]) if b["lookup_raw"] else {}
    except ValueError:
        raw = {}
    cred = client_creds.status_map(conn).get(afm)
    return render_template("client_detail.html", nav="clients", b=b, matches=matches, events=events, has_raw=bool(raw),
                           cred=cred, cred_user=client_creds.masked_user(conn, afm) if cred else "",
                           office_creds=bool(settings_store.is_set(conn, "aade_user") and settings_store.is_set(conn, "aade_pass")))


@bp.post("/clients/<afm>/edit")
def client_edit(afm: str):
    conn = get_db()
    if not clients.get(conn, afm):
        abort(404)
    vat = request.form.get("vat_subject", "")
    clients.update_fields(conn, afm, {k: request.form.get(k, "") for k in clients.EDITABLE if k in request.form},
                          vat_subject={"1": True, "0": False}.get(vat))
    kads = []
    for line in request.form.get("kads", "").splitlines():
        line = line.strip()
        if not line:
            continue
        code, _, descr = line.partition(" ")
        kads.append({"code": code.strip(" -–:"), "descr": descr.strip(" -–:"), "is_main": not kads})
    clients.set_kads(conn, afm, kads)
    engine.rematch(conn)
    flash("Τα στοιχεία αποθηκεύτηκαν.", "ok")
    return redirect(url_for("main.client_detail", afm=afm))


@bp.post("/clients/<afm>/lookup")
def client_lookup(afm: str):
    if not clients.get(get_db(), afm):
        abort(404)
    if _start_lookup(afm):
        flash("Ανάκτηση στοιχείων σε εξέλιξη…", "ok")
    else:
        flash("Τρέχει ήδη άλλη εργασία.", "warn")
    return redirect(url_for("main.client_detail", afm=afm))


@bp.post("/clients/<afm>/credentials")
def client_credentials(afm: str):
    conn = get_db()
    if not clients.get(conn, afm):
        abort(404)
    action = request.form.get("action", "save")
    if action == "clear":
        client_creds.clear(conn, afm)
        flash("Οι κωδικοί TAXISnet του πελάτη διαγράφηκαν.", "ok")
    elif action == "test":
        if not client_creds.get(conn, afm):
            flash("Δεν έχουν οριστεί κωδικοί TAXISnet για τον πελάτη.", "warn")
        else:
            def work(progress):
                from .. import db as dbmod
                progress("Δοκιμή σύνδεσης στο TAXISnet…")
                c = dbmod.connect()
                try:
                    return client_creds.test(c, afm)[1]
                finally:
                    c.close()
            flash("Δοκιμή σύνδεσης σε εξέλιξη…" if _jobs().start("creds-test", work) else "Τρέχει ήδη άλλη εργασία.", "ok")
    else:
        user, pwd = request.form.get("taxis_user", "").strip(), request.form.get("taxis_pass", "")
        if not client_creds.set_(conn, afm, user, pwd):
            flash("Δεν δόθηκαν κωδικοί.", "warn")
        else:
            flash("Οι κωδικοί TAXISnet αποθηκεύτηκαν (κρυπτογραφημένοι).", "ok")
    return redirect(url_for("main.client_detail", afm=afm))


@bp.post("/clients/bulk")
def clients_bulk():
    """Μαζικές ενέργειες σε επιλεγμένους πελάτες: lookup | delete | clear_creds | set_creds."""
    conn = get_db()
    afms = [a for a in (normalize_afm(x) for x in request.form.getlist("afms")) if a]
    action = request.form.get("action", "")
    if not afms:
        flash("Δεν επιλέχθηκε κανένας πελάτης.", "warn")
        return redirect(url_for("main.clients_list"))
    if action == "delete":
        for a in afms:
            clients.delete(conn, a)
        flash(f"Διαγράφηκαν {len(afms)} πελάτες.", "ok")
    elif action == "clear_creds":
        for a in afms:
            client_creds.clear(conn, a)
        flash(f"Διαγράφηκαν οι κωδικοί TAXISnet σε {len(afms)} πελάτες.", "ok")
    elif action == "set_creds":
        user, pwd = request.form.get("taxis_user", "").strip(), request.form.get("taxis_pass", "")
        if not (user and pwd):
            flash("Δώστε και χρήστη και κωδικό TAXISnet.", "warn")
        else:
            out = client_creds.bulk_set(conn, [(a, user, pwd) for a in afms])
            flash(f"Οι κωδικοί TAXISnet ορίστηκαν σε {out['saved']} πελάτες.", "ok")
    elif action == "lookup":
        def work(progress):
            from .. import db as dbmod
            c = dbmod.connect()
            try:
                out = clients.enrich_pending(c, afms=afms, on_progress=progress)
                engine.rematch(c)
                return out
            finally:
                c.close()
        flash("Ανανέωση στοιχείων σε εξέλιξη…" if _jobs().start("enrich", work) else "Τρέχει ήδη άλλη εργασία.", "ok")
    else:
        abort(400)
    return redirect(url_for("main.clients_list"))


@bp.get("/clients/export.csv")
def clients_export():
    """Λίστα πελατών σε CSV (UTF-8 με BOM για Excel). ΔΕΝ περιλαμβάνει ποτέ κωδικούς."""
    conn = get_db()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ΑΦΜ", "Επωνυμία", "Νομική μορφή", "ΔΟΥ", "Κύριος ΚΑΔ", "Όλα τα ΚΑΔ", "Κατηγορία βιβλίων", "ΦΠΑ", "Περίοδος ΦΠΑ",
                "Διεύθυνση", "Κατάσταση στοιχείων"])
    for b in clients.list_all(conn):
        kads = "; ".join(r["code"] for r in conn.execute(
            "SELECT code FROM business_kad WHERE afm=? ORDER BY is_main DESC, code", (b["afm"],)))
        w.writerow([b["afm"], b["name"], b["legal_form"], b["doy"], b["kad_main_code"], kads, b["books_category"],
                    {1: "Υπόχρεος", 0: "Όχι"}.get(b["vat_subject"], ""),
                    {"monthly": "Μηνιαία", "quarterly": "Τριμηνιαία"}.get(b["vat_period_type"], ""), b["address"],
                    b["lookup_status"]])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=pelates.csv"})


@bp.get("/clients/import/template.xlsx")
def clients_import_template():
    return Response(import_excel.template_xlsx(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": "attachment; filename=pelates-protypo.xlsx"})


@bp.get("/logs")
def logs_view():
    """Τελευταίες γραμμές των αρχείων καταγραφής (εφαρμογή + καθημερινός έλεγχος)."""
    out = []
    for name in ("daily.log", "app.log", "crash.log"):
        path = config.log_dir() / name
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            out.append({"name": name, "text": "\n".join(lines)})
    return render_template("logs.html", nav="logs", logs=out, log_dir=str(config.log_dir()))


@bp.post("/clients/<afm>/delete")
def client_delete(afm: str):
    conn = get_db()
    if clients.get(conn, afm):
        clients.delete(conn, afm)
        flash("Ο πελάτης διαγράφηκε.", "ok")
    return redirect(url_for("main.clients_list"))


# ================================================================== Ημερολόγιο

@bp.route("/calendar")
def calendar_view():
    conn = get_db()
    today = date.today()
    try:
        y, m = (int(x) for x in request.args.get("month", f"{today.year}-{today.month}").split("-"))
        first = date(y, m, 1)
    except (ValueError, TypeError):
        first = today.replace(day=1)
    afm = request.args.get("afm", "").strip() or None
    show_news = ("1" in request.args.getlist("news")) if "news" in request.args else True
    show_rules = ("1" in request.args.getlist("rules")) if "rules" in request.args else True
    show_cond = ("1" in request.args.getlist("cond")) if "cond" in request.args else True
    weeks = pycal.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    events = deadlines.events_between(conn, weeks[0][0], weeks[-1][-1], afm=afm, include_news=show_news,
                                     include_rules=show_rules, include_conditional=show_cond)
    by_day: dict[str, list] = {}
    for ev in events:
        by_day.setdefault(ev["date"], []).append(ev)
    prev_m = (first - timedelta(days=1)).replace(day=1)
    next_m = (first + timedelta(days=32)).replace(day=1)
    agenda = [ev for ev in events if ev["date"][:7] == first.strftime("%Y-%m")]
    return render_template("calendar.html", nav="calendar", first=first, weeks=weeks, by_day=by_day, today=today,
                           prev_m=prev_m.strftime("%Y-%m"), next_m=next_m.strftime("%Y-%m"), afm=afm, show_news=show_news,
                           show_rules=show_rules, show_cond=show_cond,
                           agenda=agenda, month_name=MONTHS_EL[first.month - 1],
                           client_options=clients.list_all(conn))


# ================================================================== Νέα & Matches

@bp.route("/news")
def news():
    conn = get_db()
    days = _int_arg("days", 7, DAY_CHOICES)
    only = request.args.get("only", "matched")
    src = request.args.get("source", "")
    q = request.args.get("q", "").strip()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sql = ("SELECT a.*, (SELECT COUNT(*) FROM matches m WHERE m.article_id=a.id) AS n_matches FROM articles a "
           "WHERE COALESCE(a.published_at,a.fetched_at) >= ?")
    args: list[Any] = [cutoff]
    if only == "matched":
        sql += " AND EXISTS (SELECT 1 FROM matches m WHERE m.article_id=a.id)"
    elif only == "irrelevant":
        sql += " AND a.extraction_status='irrelevant'"
    elif only == "pending":
        sql += " AND a.extraction_status IN ('pending','failed')"
    elif only == "skipped":
        sql += " AND a.extraction_status='skipped'"
    elif only == "duplicate":
        sql += " AND a.extraction_status='duplicate'"
    if src:
        sql += " AND a.source=?"
        args.append(src)
    if q:
        sql += " AND a.title LIKE ?"
        args.append(f"%{q}%")
    sql += " ORDER BY COALESCE(a.published_at,a.fetched_at) DESC LIMIT 300"
    arts = [dict(r) for r in conn.execute(sql, args)]
    ids = [a["id"] for a in arts]
    matches: dict[int, list] = {}
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(f"SELECT m.id AS match_id, m.article_id, m.afm, m.matched_reason, m.confidence, m.user_feedback, b.name "
                              f"FROM matches m JOIN businesses b ON b.afm=m.afm WHERE m.article_id IN ({marks}) "
                              f"ORDER BY m.confidence DESC, b.name", ids):
            matches.setdefault(r["article_id"], []).append(dict(r))
    for a in arts:
        try:
            ex = json.loads(a["extracted_json"] or "{}")
        except ValueError:
            ex = {}
        a.update(summary=ex.get("summary", ""), action_required=ex.get("action_required"), topic=ex.get("topic", ""),
                 scope=(ex.get("scope") or {}), matches=matches.get(a["id"], []))
    return render_template("news.html", nav="news", arts=arts, days=days, day_choices=DAY_CHOICES, only=only, src=src, q=q,
                           sources=sources.SOURCES)


# ================================================================== Ρυθμίσεις

@bp.route("/settings", methods=["GET", "POST"])
def settings():
    conn = get_db()
    if request.method == "POST":
        section = request.form.get("section")
        if section == "keys":
            for key in SECRET_KEYS:
                if request.form.get(f"clear_{key}"):
                    settings_store.set_value(conn, key, "")
                elif request.form.get(key, "").strip():
                    settings_store.set_value(conn, key, request.form[key].strip())
            flash("Τα credentials αποθηκεύτηκαν (κρυπτογραφημένα).", "ok")
        elif section == "llm":
            provider = request.form.get("llm_provider", "groq")
            settings_store.set_value(conn, "llm_provider", provider if provider in llm_extract.PROVIDERS else "groq")
            for key in ("llm_model_groq", "llm_model_openrouter"):
                if request.form.get(key, "").strip():
                    settings_store.set_value(conn, key, request.form[key].strip())
            for key, lo, hi in (("lookback_days", 1, 60), ("max_extractions_per_run", 1, 500)):
                try:
                    settings_store.set_value(conn, key, str(min(max(int(request.form.get(key, "")), lo), hi)))
                except ValueError:
                    pass
            settings_store.set_value(conn, "fetch_full_text", "1" if request.form.get("fetch_full_text") else "0")
            flash("Οι ρυθμίσεις ανάλυσης αποθηκεύτηκαν.", "ok")
        elif section == "sources":
            for s in sources.SOURCES:
                settings_store.set_source_enabled(conn, s.id, bool(request.form.get(f"src_{s.id}")))
            flash("Οι πηγές ενημερώθηκαν.", "ok")
        elif section == "schedule":
            hhmm = request.form.get("daily_time", "08:00")
            if request.form.get("action") == "remove":
                ok, msg = scheduler_win.remove()
            else:
                try:
                    ok, msg = scheduler_win.install(hhmm)
                    if ok:
                        settings_store.set_value(conn, "daily_time", hhmm)
                except ValueError as exc:
                    ok, msg = False, str(exc)
            flash(msg, "ok" if ok else "danger")
        return redirect(url_for("main.settings"))

    secrets_state = {k: settings_store.is_set(conn, k) for k in SECRET_KEYS}
    from_env = {k: (settings_store.is_set(conn, k) and not conn.execute(
        "SELECT 1 FROM settings WHERE key=? AND value!=''", (k,)).fetchone()) for k in SECRET_KEYS}
    return render_template(
        "settings.html", nav="settings", secrets_state=secrets_state, from_env=from_env,
        values={k: settings_store.get(conn, k) for k in ("llm_provider", "llm_model_groq", "llm_model_openrouter",
                                                         "lookback_days", "max_extractions_per_run", "fetch_full_text", "daily_time")},
        sources=sources.SOURCES, enabled={s.id: settings_store.source_enabled(conn, s.id, s.default_enabled) for s in sources.SOURCES},
        task_supported=scheduler_win.supported(), task_installed=scheduler_win.is_installed(), run=pipeline.last_run(conn))


@bp.post("/settings/test-llm")
def test_llm():
    conn = get_db()
    try:
        client = llm_extract.LLMClient.from_settings(conn)
        client.complete_json("Απάντησε μόνο με JSON.", 'Επίστρεψε {"ok": true}', timeout=30)
        flash(f"Η σύνδεση με {client.provider} ({client.model}) λειτουργεί.", "ok")
    except llm_extract.NotConfigured as exc:
        flash(str(exc), "warn")
    except llm_extract.LLMError as exc:
        flash(f"Αποτυχία σύνδεσης LLM: {exc}", "danger")
    return redirect(url_for("main.settings"))


@bp.post("/settings/test-aade")
def test_aade():
    conn = get_db()
    user, pwd = settings_store.get(conn, "aade_user"), settings_store.get(conn, "aade_pass")
    if not (user and pwd):
        flash("Δεν έχουν οριστεί credentials TAXISnet.", "warn")
    else:
        try:
            res = lookup_aade.aade_login(user, pwd)
            if res.get("ok"):
                flash("Η σύνδεση στη ΑΑΔΕ (TAXISnet) πέτυχε.", "ok")
            else:
                flash("Η σύνδεση στη ΑΑΔΕ απέτυχε: " + lookup_aade.REASONS_EL.get(res.get("reason") or "", str(res.get("reason"))), "danger")
        except Exception as exc:
            flash(f"Σφάλμα σύνδεσης με ΑΑΔΕ: {exc}", "danger")
    return redirect(url_for("main.settings"))


# ================================================================== JSON API

@bp.post("/api/feedback")
def api_feedback():
    data = request.get_json(silent=True) or {}
    try:
        match_id = int(data.get("match_id"))
        value = data.get("value")
        value = None if value in (None, 0, "") else int(value)
        ok = engine.set_feedback(get_db(), match_id, value)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="άκυρα δεδομένα"), 400
    return jsonify(ok=ok, value=value), (200 if ok else 404)


@bp.post("/api/run")
def api_run():
    def work(progress):
        return pipeline.run_pipeline("manual", on_progress=progress)
    started = _jobs().start("run", work)
    return jsonify({**_jobs().snapshot(), "accepted": started}), (202 if started else 409)


@bp.post("/api/open-external")
def api_open_external():
    """Ανοίγει σύνδεσμο στον προεπιλεγμένο browser του συστήματος (το παράθυρο της εφαρμογής δεν πρέπει να πλοηγείται εκτός)."""
    import webbrowser
    from urllib.parse import urlsplit
    url = (request.get_json(silent=True) or {}).get("url", "")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return jsonify(ok=False), 400
    webbrowser.open(url)
    return jsonify(ok=True)


@bp.get("/api/job")
def api_job():
    snap = _jobs().snapshot()
    result = snap.get("result")
    snap["result"] = result if isinstance(result, (dict, list, str, int, float, type(None))) else str(result)
    return jsonify(snap)
