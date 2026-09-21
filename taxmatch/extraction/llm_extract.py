"""Άρθρο -> δομημένο scope JSON μέσω Groq ή OpenRouter (και τα δύο OpenAI-compatible)."""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlsplit

import requests

from .. import config, db, scope as scope_mod, settings_store
from ..http import make_session
from ..textutil import html_to_text

log = logging.getLogger(__name__)

PROVIDERS = {
    "groq": {"url": "https://api.groq.com/openai/v1/chat/completions", "key": "groq_api_key",
             "model": "llm_model_groq"},
    "openrouter": {"url": "https://openrouter.ai/api/v1/chat/completions", "key": "openrouter_api_key",
                   "model": "llm_model_openrouter"},
}
MAX_TRIES = 3
FULL_TEXT_MAX = 5000          # χαρακτήρες προς το LLM (τα ελληνικά «κοστίζουν» πολλά tokens)
STORED_TEXT_MAX = 8000
MIN_SECONDS_BETWEEN_CALLS = 2.0


class LLMError(Exception):
    """kind: auth | rate_limit | network | bad_response"""

    def __init__(self, kind: str, message: str, retry_after: float = 0.0):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after


class NotConfigured(Exception):
    pass


@dataclass
class LLMClient:
    provider: str
    api_key: str
    model: str
    url: str
    session: requests.Session

    @classmethod
    def from_settings(cls, conn: sqlite3.Connection, session: Optional[requests.Session] = None) -> "LLMClient":
        provider = settings_store.get(conn, "llm_provider") or "groq"
        cfg = PROVIDERS.get(provider)
        if not cfg:
            raise NotConfigured(f"Άγνωστος πάροχος LLM: {provider}")
        key = settings_store.get(conn, cfg["key"])
        if not key:
            raise NotConfigured(f"Δεν έχει οριστεί API key για {provider} (Ρυθμίσεις).")
        return cls(provider, key, settings_store.get(conn, cfg["model"]), cfg["url"], session or make_session(retries=1))

    def complete_json(self, system: str, user: str, timeout: int = 90) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.provider == "openrouter":
            headers["X-Title"] = "TaxMatch by ScanMyData"
        try:
            resp = self.session.post(self.url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as exc:
            raise LLMError("network", str(exc)) from exc
        if resp.status_code in (401, 403):
            raise LLMError("auth", f"HTTP {resp.status_code}: άκυρο ή χωρίς δικαιώματα API key")
        if resp.status_code == 429:
            raise LLMError("rate_limit", "HTTP 429: όριο αιτημάτων", _retry_after(resp))
        if resp.status_code >= 400:
            raise LLMError("bad_response", f"HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()["choices"][0]["message"]["content"] or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("bad_response", "Μη αναμενόμενη μορφή απάντησης") from exc


def _retry_after(resp: requests.Response) -> float:
    try:
        return float(resp.headers.get("retry-after", "0"))
    except ValueError:
        return 0.0


def system_prompt(today: Optional[date] = None) -> str:
    text = (config.resource_dir() / "taxmatch" / "extraction" / "prompts" / "scope_extraction.md").read_text(encoding="utf-8")
    return text.replace("{today}", (today or date.today()).isoformat())


def parse_json_loose(text: str) -> dict:
    """Το JSON mode συνήθως επιστρέφει καθαρό JSON· αν όχι (OpenRouter/μικρά μοντέλα) βγάζουμε το πρώτο {...}."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("δεν βρέθηκε JSON στην απάντηση")
        obj = json.loads(text[start:end + 1])
    if not isinstance(obj, dict):
        raise ValueError("το JSON δεν είναι αντικείμενο")
    return obj


def _is_public_http(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if host == "localhost" or host.endswith(".local"):
        return False
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return True


def fetch_full_text(url: str, session: requests.Session, max_chars: int = STORED_TEXT_MAX) -> str:
    """Κείμενο της σελίδας του άρθρου· '' σε οποιαδήποτε αποτυχία (το summary του RSS μένει fallback)."""
    if not _is_public_http(url):
        return ""
    try:
        resp = session.get(url, timeout=20)
        if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", "html").lower():
            return ""
        return html_to_text(resp.text, max_chars=max_chars)
    except requests.RequestException:
        return ""


def build_user_message(article: sqlite3.Row, text: str) -> str:
    body = text or article["raw_summary"] or ""
    return (
        f"Πηγή: {article['source']}\n"
        f"Ημερομηνία δημοσίευσης: {(article['published_at'] or '')[:10]}\n"
        f"Κατηγορία: {article['category'] or '-'}\n"
        f"Τίτλος: {article['title']}\n\n"
        f"Κείμενο:\n{body[:FULL_TEXT_MAX]}"
    )


def extract_article(conn: sqlite3.Connection, article: sqlite3.Row, client: LLMClient, system: str,
                    session: Optional[requests.Session] = None, use_full_text: bool = True) -> str:
    """Επεξεργάζεται ένα άρθρο και ενημερώνει τη βάση. Επιστρέφει την τελική κατάσταση
    ('done' | 'irrelevant'). Σε LLMError το εκτοξεύει προς τον καλούντα (αυτός αποφασίζει: stop/retry)."""
    text = article["full_text"]
    if use_full_text and not text and session is not None:
        text = fetch_full_text(article["url"], session)
        if text:
            conn.execute("UPDATE articles SET full_text=? WHERE id=?", (text, article["id"]))
    raw = client.complete_json(system, build_user_message(article, text))
    try:
        extracted = scope_mod.normalize(parse_json_loose(raw))
    except ValueError as exc:
        raise LLMError("bad_response", f"Άκυρο JSON: {exc}") from exc
    status = "done" if extracted["relevant"] else "irrelevant"
    conn.execute(
        "UPDATE articles SET extracted_json=?, extraction_status=?, extraction_error='', extraction_model=?, deadline=? "
        "WHERE id=?",
        (json.dumps(extracted, ensure_ascii=False), status, f"{client.provider}:{client.model}",
         extracted["deadline"], article["id"]),
    )
    return status


def extract_pending(conn: sqlite3.Connection, client: LLMClient, limit: int, lookback_days: int,
                    session: Optional[requests.Session] = None, use_full_text: bool = True,
                    on_progress: Optional[Callable[[str], None]] = None,
                    sleep: Callable[[float], None] = time.sleep) -> dict:
    """Επεξεργάζεται εκκρεμή άρθρα (νεότερα πρώτα). Σταματά νωρίς σε auth/rate-limit/δίκτυο."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT * FROM articles WHERE extraction_status IN ('pending','failed') AND extraction_tries < ? "
        "AND COALESCE(published_at, fetched_at) >= ? ORDER BY COALESCE(published_at, fetched_at) DESC LIMIT ?",
        (MAX_TRIES, cutoff, limit),
    ).fetchall()
    system = system_prompt()
    stats = {"processed": 0, "done": 0, "irrelevant": 0, "failed": 0, "remaining": 0, "stopped": ""}
    consecutive_network = 0
    for i, art in enumerate(rows, 1):
        if on_progress:
            on_progress(f"Ανάλυση άρθρου {i}/{len(rows)}")
        attempts = 0
        while True:
            attempts += 1
            try:
                status = extract_article(conn, art, client, system, session, use_full_text)
                stats[status] += 1
                stats["processed"] += 1
                consecutive_network = 0
                break
            except LLMError as exc:
                if exc.kind == "auth":
                    stats["stopped"] = f"Σφάλμα πιστοποίησης LLM: {exc}"
                    return _finish(conn, stats, cutoff)
                if exc.kind == "rate_limit":
                    wait = min(max(exc.retry_after, 5.0), 65.0)
                    if attempts <= 2:
                        if on_progress:
                            on_progress(f"Όριο αιτημάτων LLM — αναμονή {int(wait)}\"")
                        sleep(wait)
                        continue
                    stats["stopped"] = "Όριο αιτημάτων LLM — τα υπόλοιπα άρθρα θα αναλυθούν στον επόμενο έλεγχο."
                    return _finish(conn, stats, cutoff)
                # network / bad_response: μετράει προσπάθεια στο άρθρο
                consecutive_network = consecutive_network + 1 if exc.kind == "network" else 0
                conn.execute("UPDATE articles SET extraction_status='failed', extraction_error=?, "
                             "extraction_tries=extraction_tries+1 WHERE id=?", (str(exc)[:300], art["id"]))
                stats["failed"] += 1
                stats["processed"] += 1
                break
        if consecutive_network >= 3:
            stats["stopped"] = "Πρόβλημα δικτύου προς το LLM."
            break
        sleep(MIN_SECONDS_BETWEEN_CALLS)
    return _finish(conn, stats, cutoff)


def _finish(conn: sqlite3.Connection, stats: dict, cutoff: str) -> dict:
    stats["remaining"] = conn.execute(
        "SELECT COUNT(*) FROM articles WHERE extraction_status IN ('pending','failed') AND extraction_tries < ? "
        "AND COALESCE(published_at, fetched_at) >= ?", (MAX_TRIES, cutoff)).fetchone()[0]
    return stats
