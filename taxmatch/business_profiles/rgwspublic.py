"""«Αναζήτηση Βασικών Στοιχείων Μητρώου Επιχειρήσεων» της ΑΑΔΕ (web service RgWsPublic2) — βασικά στοιχεία ΟΠΟΙΟΥΔΗΠΟΤΕ
ΑΦΜ με τους **ειδικούς κωδικούς** του γραφείου (εγγραφή στο TAXISnet → «Ειδικοί κωδικοί για χρήση σε web services»),
χωρίς τους κωδικούς του ίδιου του ΑΦΜ.

Χρήση: μέλη εταιρειών (ρητό αίτημα χρήστη 2026-10-23 — «να εξετάζεται αν τα μέλη έχουν ατομική επιχείρηση») — το
Μητρώο του TAXISnet (`lookup_aade`) δείχνει ΜΟΝΟ τον ΑΦΜ του λογαριασμού που συνδέθηκε, άρα για ένα μέλος χωρίς κωδικούς
είναι η μόνη επίσημη πηγή: επιτηδευματίας ή όχι (`firm_flag_descr`), ενεργός ή απενεργοποιημένος ΑΦΜ, διακοπή
(`stop_date`), ΚΑΔ (`firm_act_tab`), κανονικό καθεστώς ΦΠΑ (`normal_vat_system_flag`).

Πρωτόκολλο: SOAP 1.2 με WS-Security UsernameToken (δημόσια τεκμηρίωση ΑΑΔΕ «RgWsPublic2 — Οδηγίες χρήσης»).
**ΔΕΝ έχει δοκιμαστεί ζωντανά** (δεν υπάρχουν ειδικοί κωδικοί στο μηχάνημα ανάπτυξης)· οι κωδικοί δεν γράφονται ποτέ σε
logs. Μόνο ανάγνωση.
"""
from __future__ import annotations

import html
import logging
import re
from typing import Any, Optional

import requests

log = logging.getLogger(__name__)

ENDPOINT = "https://www1.gsis.gr/wsaade/RgWsPublic2/RgWsPublic2"

_ENVELOPE = """<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
 xmlns:ns1="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
 xmlns:ns2="http://rgwspublic2/RgWsPublic2Service" xmlns:ns3="http://rgwspublic2/RgWsPublic2">
 <env:Header><ns1:Security><ns1:UsernameToken><ns1:Username>{user}</ns1:Username><ns1:Password>{password}</ns1:Password>
 </ns1:UsernameToken></ns1:Security></env:Header>
 <env:Body><ns2:rgWsPublic2AfmMethod><ns2:INPUT_REC><ns3:afm_called_by>{by}</ns3:afm_called_by>
 <ns3:afm_called_for>{afm}</ns3:afm_called_for></ns2:INPUT_REC></ns2:rgWsPublic2AfmMethod></env:Body>
</env:Envelope>"""


def _tag(xml: str, name: str) -> str:
    m = re.search(rf"<(?:\w+:)?{name}\b[^>]*>(.*?)</(?:\w+:)?{name}>", xml, re.S)
    return html.unescape(m.group(1).strip()) if m else ""


def parse(xml: str) -> dict[str, Any]:
    """Απάντηση → {ok, reason, afm, name, trade_name, firm (True/False/None), active_afm, stop_date, start_date,
    legal_status, doy, kads, normal_vat}."""
    code, descr = _tag(xml, "error_code"), _tag(xml, "error_descr")
    if code:
        return {"ok": False, "reason": f"{code}: {descr}"[:200]}
    afm = _tag(xml, "afm")
    if not afm:
        return {"ok": False, "reason": "Κενή απάντηση της ΑΑΔΕ."}
    firm_descr = _tag(xml, "firm_flag_descr").upper()
    firm = None
    if firm_descr:
        # «ΕΠΙΤΗΔΕΥΜΑΤΙΑΣ» / «ΜΗ ΕΠΙΤΗΔΕΥΜΑΤΙΑΣ» / «ΠΡΩΗΝ ΕΠΙΤΗΔΕΥΜΑΤΙΑΣ»
        firm = firm_descr.startswith("ΕΠΙΤΗΔΕΥΜΑΤ")
    kads = []
    for item in re.findall(r"<(?:\w+:)?item\b[^>]*>(.*?)</(?:\w+:)?item>", xml, re.S):
        code_ = _tag(item, "firm_act_code")
        if code_:
            kads.append({"code": code_, "descr": _tag(item, "firm_act_descr"),
                         "is_main": _tag(item, "firm_act_kind") == "1"})
    return {
        "ok": True, "reason": "", "afm": afm, "name": _tag(xml, "onomasia"), "trade_name": _tag(xml, "commer_title"),
        "firm": firm, "firm_label": _tag(xml, "firm_flag_descr"),
        "active_afm": _tag(xml, "deactivation_flag") == "1", "stop_date": _tag(xml, "stop_date"),
        "start_date": _tag(xml, "regist_date"), "legal_status": _tag(xml, "legal_status_descr"),
        "doy": _tag(xml, "doy_descr"), "kads": kads, "normal_vat": _tag(xml, "normal_vat_system_flag") == "Y",
        "person": _tag(xml, "i_ni_flag_descr"),
    }


def lookup(afm: str, user: str, password: str, called_by: str = "",
           session: Optional[requests.Session] = None, timeout: int = 20) -> dict[str, Any]:
    body = _ENVELOPE.format(user=html.escape(user), password=html.escape(password), by=html.escape(called_by),
                            afm=html.escape(afm))
    try:
        r = (session or requests).post(ENDPOINT, data=body.encode("utf-8"), timeout=timeout,
                                       headers={"Content-Type": "application/soap+xml;charset=UTF-8"})
    except requests.RequestException as exc:
        log.warning("RgWsPublic2 %s: %s", afm, type(exc).__name__)
        return {"ok": False, "reason": "Δεν απάντησε η υπηρεσία Μητρώου της ΑΑΔΕ."}
    return parse(r.text)


def business_state(res: dict[str, Any]) -> str:
    """'active' (ενεργός επιτηδευματίας) | 'ceased' (πρώην / διακοπή) | 'none' (ποτέ επιτηδευματίας) | '' (άγνωστο)."""
    if not res.get("ok") or res.get("firm") is None:
        return ""
    if res["firm"] and not res.get("stop_date"):
        return "active"
    if "ΠΡΩΗΝ" in (res.get("firm_label") or "").upper() or res.get("stop_date"):
        return "ceased"
    return "none"
