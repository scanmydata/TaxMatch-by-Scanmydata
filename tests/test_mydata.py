"""myDATA ανά πελάτη: parser, λήψη μήνα, παρακρατούμενοι φόροι, VIES/Intrastat, Ημερολόγιο, PDF/ZIP (2026-10-22)."""
import io
import json
import zipfile
from datetime import date

import pytest

from taxmatch import obligations
from taxmatch.business_profiles import service
from taxmatch.mydata import analysis, client as mclient, credentials as mcreds, parse, pdfs, store

AFM = "094259216"
NSX = 'xmlns="http://www.aade.gr/myDATA/invoice/v1.0"'


def inv(mark, itype, issue, issuer=("111111111", "ΠΡΟΜΗΘΕΥΤΗΣ", "GR"), counter=(AFM, "ΠΕΛΑΤΗΣ", "GR"), net=1000.0,
        withheld=None, url="https://p.example.gr/doc/abc"):
    det = ""
    if withheld:
        det = "".join(f"<invoiceDetails><lineNumber>{i}</lineNumber><netValue>{net}</netValue>"
                      f"<withheldAmount>{amt}</withheldAmount><withheldPercentCategory>{cat}</withheldPercentCategory>"
                      "</invoiceDetails>" for i, (cat, amt) in enumerate(withheld.items(), 1))
    total_w = sum((withheld or {}).values())
    return (f"<invoice><mark>{mark}</mark><issuer><vatNumber>{issuer[0]}</vatNumber><country>{issuer[2]}</country>"
            f"<name>{issuer[1]}</name></issuer><counterpart><vatNumber>{counter[0]}</vatNumber><country>{counter[2]}</country>"
            f"<name>{counter[1]}</name></counterpart><invoiceHeader><series>Α</series><aa>{mark}</aa>"
            f"<issueDate>{issue}</issueDate><invoiceType>{itype}</invoiceType></invoiceHeader>{det}"
            f"<invoiceSummary><totalNetValue>{net}</totalNetValue><totalVatAmount>0</totalVatAmount>"
            f"<totalWithheldAmount>{total_w}</totalWithheldAmount><totalGrossValue>{net}</totalGrossValue></invoiceSummary>"
            f"<downloadingInvoiceUrl>{url}</downloadingInvoiceUrl></invoice>")


def page(*invoices):
    return f"<RequestedDoc {NSX}><invoicesDoc>{''.join(invoices)}</invoicesDoc></RequestedDoc>".encode()


class FakeClient:
    def __init__(self, incoming=(), outgoing=()):
        self.incoming, self.outgoing = incoming, outgoing

    def __call__(self, user, key):
        assert (user, key) == ("md_user", "md_key")
        return self

    def fetch_pages(self, incoming, date_from, date_to):
        ym = f"{date_from[6:10]}-{date_from[3:5]}"
        src = self.incoming if incoming else self.outgoing
        return [page(*[x for x in src if f"<issueDate>{ym}" in x])]

    def close(self):
        pass


@pytest.fixture
def client_with_creds(conn):
    service.add(conn, AFM, "ΠΕΛΑΤΗΣ")
    mcreds.set_(conn, AFM, "md_user", "md_key")
    return conn


# ---------------------------------------------------------------- parser / client
def test_parser_reads_withholding_lines_and_countries():
    d = parse.parse_page(page(inv("1", "2.1", "2026-03-10", withheld={"3": 200.0, "4": 30.0})), "incoming")[0]
    assert d["withheld_lines"] == {"3": 200.0, "4": 30.0} and d["withheld"] == 230.0
    assert d["issuer_country"] == "GR" and d["issue_date"] == "2026-03-10" and d["provider"] == "p.example.gr"
    xml = (f"<RequestedDoc {NSX}><invoice><mark>2</mark><invoiceHeader><invoiceType>2.1</invoiceType>"
           "<issueDate>2026-03-10</issueDate></invoiceHeader><taxesTotals><taxes><taxType>1</taxType><taxCategory>3"
           "</taxCategory><taxAmount>50</taxAmount></taxes></taxesTotals></invoice></RequestedDoc>").encode()
    assert parse.parse_page(xml, "incoming")[0]["withheld_lines"] == {"3": 50.0}


def test_mydata_key_is_never_sent_to_another_host():
    c = mclient.MydataClient("u", "k", retry=False)
    with pytest.raises(ValueError):
        c._get_once("https://evil.example.com/RequestDocs", {})
    with pytest.raises(mclient.AuthError):
        mclient.MydataClient("", "k")


def test_credentials_are_encrypted_and_key_kept_when_blank(conn):
    mcreds.set_(conn, AFM, "u", "secretkey")
    raw = conn.execute("SELECT mydata_key FROM mydata_credentials").fetchone()[0]
    assert raw.startswith("enc:1:") and "secretkey" not in raw
    mcreds.set_(conn, AFM, "u2", "")
    assert mcreds.get(conn, AFM) == ("u2", "secretkey")


# ---------------------------------------------------------------- λήψη / ανάλυση
def test_sync_month_and_withholding_due_end_of_second_month(client_with_creds):
    conn = client_with_creds
    fake = FakeClient(incoming=[inv("10", "2.1", "2026-03-05", withheld={"3": 200.0}),
                                inv("11", "2.1", "2026-03-20", withheld={"4": 30.0}, net=1000),
                                inv("12", "5.2", "2026-03-25", withheld={"3": 50.0}, net=250),     # πιστωτικό
                                inv("13", "2.1", "2026-03-26", withheld={"8": 40.0})])            # προκαταβλητέος: όχι
    out = store.sync_month(conn, AFM, "2026-03", client_factory=fake)
    assert out["ok"] and out["n"] == 4
    w = {x["category"]: x for x in analysis.withholding(conn, AFM, "2026-03")}
    assert set(w) == {"3", "4"} and w["3"]["amount"] == 150.0 and w["4"]["amount"] == 30.0
    assert w["3"]["due"] == "2026-05-29" and "επιχειρηματικής" in w["3"]["declaration"]   # 31/5/2026 Κυριακή
    assert w["4"]["easycheck"] == "ΕΡΓΟΛΑΒΩΝ"


def test_sync_month_reports_rejected_credentials(client_with_creds):
    class Rejecting(FakeClient):
        def fetch_pages(self, *a):
            raise mclient.AuthError("403")
    out = store.sync_month(client_with_creds, AFM, "2026-03", client_factory=Rejecting())
    assert not out["ok"] and "απέρριψε" in out["reason"]
    assert client_with_creds.execute("SELECT check_status FROM mydata_credentials").fetchone()[0] == "invalid"


def test_vies_and_intrastat_from_intra_community_documents(client_with_creds):
    conn = client_with_creds
    eu = ("DE123", "GMBH", "DE")
    outgoing = [inv("20", "14.3", "2026-02-10", issuer=eu, net=500),                      # μόνο λήψη υπηρεσιών
                inv("21", "14.1", "2026-03-10", issuer=eu, net=260_000),                  # αφίξεις > 250.000
                inv("22", "1.2", "2026-03-11", issuer=(AFM, "ΠΕΛΑΤΗΣ", "GR"), counter=eu, net=10_000)]
    fake = FakeClient(outgoing=outgoing)
    for ym in ["2026-01", "2026-02", "2026-03"]:
        store.sync_month(conn, AFM, ym, client_factory=fake)
    assert analysis.vies(conn, AFM, "2026-01") is False
    assert analysis.vies(conn, AFM, "2026-02") is True
    feb = analysis.intrastat(conn, AFM, "2026-02")
    assert feb["any"] is None                                     # λείπει το 2025 → δεν λέμε «όχι»
    mar = analysis.intrastat(conn, AFM, "2026-03")
    assert mar["arrivals"] is True and mar["any"] is True and "αφίξεις" in mar["reason"]
    assert analysis.vies(conn, AFM, "2026-04") is None             # δεν έχει κατέβει


def test_intrastat_no_when_full_history_below_thresholds(client_with_creds):
    conn = client_with_creds
    fake = FakeClient(outgoing=[inv("30", "14.1", "2026-01-10", issuer=("DE1", "X", "DE"), net=1000)])
    for m in range(1, 13):
        store.sync_month(conn, AFM, f"2025-{m:02d}", client_factory=fake)
    store.sync_month(conn, AFM, "2026-01", client_factory=fake)
    assert analysis.intrastat(conn, AFM, "2026-01")["any"] is False


def test_calendar_liability_uses_mydata_per_reference_month(client_with_creds):
    conn = client_with_creds
    fake = FakeClient(incoming=[inv("40", "2.1", "2026-03-05", withheld={"3": 200.0})],
                      outgoing=[inv("41", "14.3", "2026-03-10", issuer=("DE1", "X", "DE"), net=100)])
    store.sync_month(conn, AFM, "2026-03", client_factory=fake)
    store.sync_month(conn, AFM, "2026-04", client_factory=fake)
    b = {"afm": AFM, "activity_state": "active", "vat_subject": 1,
         "mydata": analysis.month_flags(conn)[AFM]}
    assert obligations.liability("vies", b, "2026-03")[0] == "yes"
    assert obligations.liability("vies", b, "2026-04") == ("no", "καμία ενδοκοινοτική συναλλαγή στο myDATA")
    assert obligations.liability("vies", b, "2026-05")[0] == "maybe"            # χωρίς στοιχεία
    assert obligations.liability("withholding_fees", b, "2026-03")[0] == "yes"
    occ = [o for o in obligations.occurrences(date(2026, 5, 1), date(2026, 5, 31), b) if o.rule_id == "withholding_fees"]
    assert occ and occ[0].ref_month == "2026-03" and not occ[0].conditional and "Μάρτιος 2026" in occ[0].title
    assert not [o for o in obligations.occurrences(date(2026, 6, 1), date(2026, 6, 30), b) if o.rule_id == "withholding_fees"]
    from taxmatch import deadlines
    ev = {"kind": "rule", "id": "vies", "date": "2026-04-27", "title": "VIES", "ref_month": "2026-03"}
    people = deadlines.clients_for_event(conn, ev)
    assert people and people[0]["afm"] == AFM and people[0]["status"] == "yes"


# ---------------------------------------------------------------- PDF / ZIP
def test_provider_urls_like_timologio():
    assert pdfs.pdf_url("https://x.gr/inv/abc/") == "https://x.gr/inv/abc/pdf"
    assert pdfs.pdf_url("https://x.gr/inv/abc/pdf") == "https://x.gr/inv/abc/pdf"
    hexid = "0123456789abcdef0123456789abcdef"
    assert pdfs.epsilon_pdf_url(f"https://a.epsilonnet.gr/fd/{hexid}:1").endswith(
        "documentId=01234567-89ab-cdef-0123-456789abcdef")
    with pytest.raises(pdfs.NotAPdf):
        pdfs.fetch_pdf("https://www.eskap.gr/invoice/abc")


def test_download_month_pdfs_and_zip(client_with_creds):
    conn = client_with_creds
    fake = FakeClient(incoming=[inv("50", "1.1", "2026-03-05"), inv("51", "1.1", "2026-03-06", url="https://eskap.gr/x"),
                                inv("52", "1.1", "2026-03-07", url="")])
    store.sync_month(conn, AFM, "2026-03", client_factory=fake)

    def fetch(url):
        if "eskap" in url:
            raise pdfs.NotAPdf("viewer")
        return b"%PDF-1.4 fake"

    counts = pdfs.download_month(conn, AFM, "2026-03", fetch=fetch)
    assert counts == {"ok": 1, "viewer": 1, "no_url": 1, "failed": 0}
    z = zipfile.ZipFile(io.BytesIO(pdfs.zip_month(conn, AFM, "2026-03")))
    assert len(z.namelist()) == 1 and z.namelist()[0].startswith("Λήψεις/ΠΡΟΜΗΘΕΥΤΗΣ_111111111_2026-03-05")


def test_easycheck_checks_withholding_declarations_found_in_mydata(client_with_creds):
    from taxmatch import easycheck
    conn = client_with_creds
    fake = FakeClient(incoming=[inv("60", "2.1", "2026-03-05", withheld={"3": 200.0}),
                                inv("61", "2.1", "2026-03-06", withheld={"4": 30.0}),
                                inv("62", "2.1", "2026-03-07", withheld={"2": 10.0})])
    store.sync_month(conn, AFM, "2026-03", client_factory=fake)
    assert easycheck.withheld_keys(conn, AFM, 2026) == {"ΦΜΥ_ΕΠΙΧ", "ΕΡΓΟΛΑΒΩΝ", "ΦΜΥ_ΔΙΚΑΙΩΜΑΤΑ"}
    keys = easycheck.default_keys({"legal_form": "ΙΚΕ", "activity_state": "active", "employer": 0},
                                  easycheck.withheld_keys(conn, AFM, 2026))
    assert {"ΦΜΥ_ΕΠΙΧ", "ΕΡΓΟΛΑΒΩΝ", "ΦΜΥ_ΔΙΚΑΙΩΜΑΤΑ"} <= set(keys) and "ΦΜΥ" not in keys
