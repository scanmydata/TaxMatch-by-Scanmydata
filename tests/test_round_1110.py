"""Γύρος 2026-11-10: έλεγχος πληρωμών ΑΑΔΕ, όρια δωρεάν LLM («ψυγείο» μοντέλων / εναλλαγή παρόχου), αιτήματα ΑΑΔΕ ως
συνομιλία, τέλη κυκλοφορίας με email, έντυπο παρεπιδημούντων. Ψεύτικα δεδομένα — καμία πραγματική σελίδα."""
from __future__ import annotations

import json
from datetime import date

import pytest

from taxmatch import db as dbmod
from taxmatch import fee_portals, mailer, notices, payments, requests_aade, settings_store
from taxmatch.business_profiles import service
from taxmatch.extraction import llm_extract as L

AFM = "094259216"
TO = "094259216 910102230 629012000342"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setenv("TAXMATCH_DATA_DIR", str(tmp_path))
    c = dbmod.connect()
    yield c
    c.close()


PAYMENTS_PAGE = f"""<table>
<tr><th>Δ.Ο.Υ.</th><th>Ημ/νία</th><th>Ποσό</th><th>Είδος</th><th>Ταυτότητα</th><th></th></tr>
<tr><td>ΚΕΦΟΔΕ</td><td>31/08/2026</td><td>52,38</td><td>Τ.Ο.</td><td>{TO.replace(' ', '')}</td>
<td><a href="javascript:doViewCollectionNoticePdf(document.f,'0','','','','','','','')">Εκτύπωση</a></td></tr>
<tr><td>ΚΕΦΟΔΕ</td><td>30/09/2026</td><td>52,38</td><td>Τ.Ο.</td><td>{TO.replace(' ', '')}</td>
<td><a href="javascript:doViewCollectionNoticePdf(document.f,'0','','','','','','','')">Εκτύπωση</a></td></tr>
<tr><td>ΚΕΦΟΔΕ</td><td>30/10/2026</td><td>416,00</td><td>Τ.Ο.Τ.</td><td>094259216910102000000000001</td>
<td><a href="javascript:doViewCCPaymentPdf(document.f,'9','x')">Εκτύπωση</a></td></tr>
</table>"""


def test_payments_parse_and_reconcile():
    pays = payments.parse_payments(PAYMENTS_PAGE)
    assert [(p["pay_date"], p["amount"]) for p in pays] == [("2026-08-31", 52.38), ("2026-09-30", 52.38),
                                                            ("2026-10-30", 416.0)]
    debts = [{"kind": "debt", "ref": "a", "title": "ΦΟΡΟΣ ΕΙΣΟΔΗΜΑΤΟΣ", "total": 209.55, "code": TO},
             {"kind": "debt", "ref": "b", "title": "ΦΠΑ", "total": 1758.96, "code": "094259216 910102230 629000000001"}]
    res = payments.reconcile(debts, pays, {"total": 0.0, "rows": [], "overcollections": []})
    assert res["debts"]["debt|a"]["status"] == "with_payment" and res["debts"]["debt|a"]["paid"] == 104.76
    assert res["debts"]["debt|b"]["status"] == "unpaid"
    assert res["summary"]["unpaid"] == 1 and res["summary"]["unpaid_total"] == 1758.96
    assert res["anomalies"]["repeated"][0]["same_amount"] is True
    assert [p["matched"] for p in res["payments"]] == [True, True, False]


def test_returns_overcollection_is_the_only_overpayment_signal():
    page = ("<p>ΕΝΗΜΕΡΩΣΗ: σε περίπτωση υπερείσπραξης…</p><table><tr><td>01/10/2026</td><td>Υπερεισπράξεις τραπεζ. "
            "πληρωμών</td><td>12,00</td></tr></table><p>Συνολικό Ποσό Επιστροφής: 12,00</p>")
    r = payments.parse_returns(page)
    assert r["total"] == 12.0 and len(r["overcollections"]) == 1
    static_only = payments.parse_returns("<p>ΕΝΗΜΕΡΩΣΗ: υπερείσπραξη</p>")
    assert static_only["overcollections"] == []


def test_payments_store_info_and_debt_status(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    debts = [{"kind": "debt", "ref": "a", "title": "ΦΟΡΟΣ", "total": 209.55, "code": TO}]
    res = payments.reconcile(debts, payments.parse_payments(PAYMENTS_PAGE))
    res.update(date_from="2025-11-10", date_to="2026-11-10")
    payments.store(conn, AFM, res)
    info = payments.info(conn, AFM)
    assert info["summary"]["payments"] == 3
    st = payments.debt_status(info, {"kind": "debt", "ref": "a", "source": "aade"})
    assert "Πληρωμή 104,76 €" in payments.status_text(st)
    assert len(payments.client_payments(conn, AFM)) == 3


def test_llm_cooldown_and_provider_switch(conn):
    settings_store.set_value(conn, "groq_api_key", "test-groq")
    L.cool_down(conn, "a/model:free")
    assert "a/model:free" in L.cooled(conn)
    L.cool_down(conn, "", daily=True, provider="openrouter")
    assert L.provider_cooled(conn, "openrouter")
    cur = L.LLMClient("openrouter", "k", "a/model:free", L.PROVIDERS["openrouter"]["url"], None)
    other = L.other_provider_client(conn, cur)
    assert other is not None and other.provider == "groq"


def test_openrouter_fallbacks_at_most_three(monkeypatch):
    sent = {}

    class S:
        def post(self, url, headers=None, json=None, timeout=None):
            sent.update(json)

            class R:
                status_code = 200
                text = ""

                def json(self):
                    return {"choices": [{"message": {"content": "{}"}}]}
            return R()
    c = L.LLMClient("openrouter", "k", "liquid/lfm-2.5-2.6b:free", L.PROVIDERS["openrouter"]["url"], S())
    c.complete_json("s", "u")
    assert len(sent["models"]) <= 3


def test_limits_are_not_a_red_error(conn):
    settings_store.set_value(conn, "openrouter_api_key", "k")
    settings_store.set_value(conn, "llm_last_error", "Το μοντέλο «x:free» είναι στιγμιαία υπερφορτωμένο στον πάροχό του")
    llm = [n for n in notices.collect(conn) if "LLM" in n["text"]]
    assert llm and all(n["level"] == "info" for n in llm)


def test_request_thread_as_chat():
    r = {"submitted": "2026-10-01 10:00:00", "updated": "2026-10-20 09:00:00", "text": "Αίτηση βεβαίωσης",
         "answer": "Σας επισυνάπτουμε τη βεβαίωση", "name": "ΑΛΦΑ", "process": "Βεβαίωση",
         "comm_json": json.dumps([{"messageText": "Στείλτε το μισθωτήριο", "type": "CLAR_REQ",
                                   "createdAt": "2026-10-05T11:00:00"},
                                  {"messageText": "Επισυνάπτεται", "type": "CLAR_ANS", "createdAt": "2026-10-06T12:00:00"}])}
    t = requests_aade.thread(r)
    assert [n["who"] for n in t] == ["client", "aade", "client", "aade"]
    assert t[1]["label"] == "Η ΑΑΔΕ ζητά διευκρινίσεις"
    assert "ΑΑΔΕ (Απάντηση ΑΑΔΕ" in requests_aade.chat_line(r)
    assert requests_aade.PORTAL.format(id="123").endswith("/eticketaade/preview/123")


def test_traffic_email_has_the_notice_attached(conn, tmp_path):
    service.add(conn, AFM, "ΠΑΠΑΣ")
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    msg = mailer.compose_traffic(conn, AFM, 2027, str(pdf))
    assert msg.attachments == [pdf] and "2027" in msg.subject and "31/12/2026" in msg.text


def test_parepid_pdf_is_downloaded_once(tmp_path):
    calls = []

    class H:
        def follow(self, method, url, *a, **k):
            calls.append(url)
            return {"is_pdf": True, "content": b"%PDF-1.4 " + b"x" * 600}
    target = tmp_path / "p.pdf"
    assert fee_portals._parepid_pdf(H(), 11, 22, target) == str(target)
    assert fee_portals._parepid_pdf(H(), 11, 22, target) == str(target)
    assert calls == [fee_portals.DAE + "/ViewReports/ReportStatementYpovoli/11/22"]
