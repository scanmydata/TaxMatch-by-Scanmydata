"""Υποβολές στο Ημερολόγιο / ανά χρήση, ποσά δηλώσεων, νέες υποχρεώσεις από το myDATA (τέλος ανθεκτικότητας, ψηφιακό
τέλος), επωνυμίες VIES, γρήγορη λήψη myDATA, περίοδος ΦΠΑ από τις πραγματικές Φ2 (ρητά αιτήματα χρήστη 2026-10-22)."""
from datetime import date

from taxmatch import deadlines, easycheck, obligations
from taxmatch.business_profiles import credentials, service
from taxmatch.business_profiles.vat_profile import interpret_vat_profile
from taxmatch.mydata import analysis, credentials as mcreds, names, scheduled, store

from tests.test_mydata import AFM, FakeClient, inv


# ---------------------------------------------------------------- ΦΠΑ: μηνιαία/τριμηνιαία όπως το ScanMyData
def test_vat_profile_reads_latin_lookalikes_and_farmers_like_scanmydata():
    assert interpret_vat_profile({"ypagwghfpa": "ΝΑΙ", "kathgoriabibliwn": "Γ"})["vat_subject"] is True      # ελληνικά
    assert interpret_vat_profile({"ypagwghfpa": "NAI", "kathgoriabibliwn": "B"})["vat_period_type"] == "quarterly"
    farmer = interpret_vat_profile({"ypagwghfpa": "NAI", "kathestwsfpa": "ΕΙΔΙΚΟ ΚΑΘΕΣΤΩΣ ΑΓΡΟΤΩΝ", "kathgoriabibliwn": "Β"})
    assert farmer["vat_subject"] is False and farmer["vat_period_type"] == ""
    assert interpret_vat_profile({"kathgoriabibliwn": "ΔΙΠΛΟΓΡΑΦΙΚΑ"})["books_category"] == "Γ"


_F2_LIAB = ('<table><tr class="tblRow1"><td>3ος Μήνας</td><td>x</td><td>Έχει Υποβληθεί Δήλωση</td><td>'
            '<div onclick="doDisplayDeclarationsList(document.displayDeclarationsListForm,&quot;vatF2&quot;,&quot;2026&quot;,'
            '&quot;oneMonth&quot;,&quot;01/03/2026&quot;,&quot;31/03/2026&quot;,&quot;01/03/2026&quot;,&quot;31/03/2026&quot;);">'
            'Προβολή</div></td></tr></table>')
_F2_LIST = ('<tr class="tblRow1"><td>Internet</td><td>01/03/2026 - 31/03/2026</td><td>555</td><td>28/04/2026</td>'
            '<td>Αρχική</td><td>Οριστική</td><td><a onclick="doDisplayDeclarationState(document.displayDeclarationStateForm,'
            '&quot;vatF2&quot;,&quot;987654&quot;,&quot;x&quot;)">x</a></td></tr>')
_F2_STATE = ("<table><tr><td>Αποτέλεσμα:</td><td>Χρεωστική</td></tr>"
             "<tr><td>Δυνατότητες Πληρωμής</td><td>Εφάπαξ ποσό: 1.234,50 Ποσό 1ης Δόσης: 617,25</td></tr></table>")
_F2_DEBT = ("<table><tr><td>Ταυτότητα Οφειλής</td><td>RF 1234567890123</td></tr>"
            "<tr><td>Συνολικό ποσό οφειλής</td><td>1.234,50 €</td></tr></table>")


class _Http:
    def __init__(self):
        self.calls = []

    def follow(self, method, url, form=None, headers=None):
        self.calls.append(url)
        for key, page in (("displayDebtCode", _F2_DEBT), ("displayDeclarationState", _F2_STATE),
                          ("displayDeclarationsList", _F2_LIST), ("vatF2", _F2_LIAB)):
            if key in url:
                return {"text": page}
        return {"text": ""}


def test_easycheck_reads_f2_amounts_period_and_vat_period(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    service.update_fields(conn, AFM, {"vat_period_type": "quarterly"}, vat_subject=True)
    credentials.set_(conn, AFM, "u", "p")
    http = _Http()
    out = easycheck.check_client(conn, AFM, 2026, keys=["Φ2"], login_fn=lambda u, p: {"ok": True, "http": http})
    period = out["entries"][0]["periods"][0]
    assert period["period_end"] == "2026-03-31" and period["period_type"] == "month"
    a = period["amounts"]
    assert a["result"] == "Χρεωστική" and a["total_due"] == 1234.5 and a["debt_id"].startswith("1234567890")
    assert a["registration_no"] == "987654" and a["submitted_at"] == "28/04/2026"
    row = conn.execute("SELECT vat_period_type, vat_period_source FROM businesses WHERE afm=?", (AFM,)).fetchone()
    assert (row["vat_period_type"], row["vat_period_source"]) == ("monthly", "easycheck")   # οι πραγματικές Φ2 υπερισχύουν
    # δεύτερος έλεγχος: ίδια δήλωση ⇒ τα ποσά ΔΕΝ ξαναζητούνται (ταχύτητα)
    http2 = _Http()
    easycheck.check_client(conn, AFM, 2026, keys=["Φ2"], login_fn=lambda u, p: {"ok": True, "http": http2})
    assert not any("displayDebtCode" in u for u in http2.calls)
    # η ανάκτηση από το Μητρώο δεν πατά την περίοδο που βρήκε ο έλεγχος
    conn.execute("UPDATE businesses SET vat_period_type=CASE WHEN vat_period_source='easycheck' THEN vat_period_type "
                 "ELSE 'quarterly' END WHERE afm=?", (AFM,))
    assert conn.execute("SELECT vat_period_type FROM businesses WHERE afm=?", (AFM,)).fetchone()[0] == "monthly"


def test_period_end_from_text_when_no_button():
    assert easycheck.period_end(None, 2026, "2ο Τρίμηνο") == ("2026-06-30", "quarter")
    assert easycheck.period_end(None, 2026, "11ος Μήνας") == ("2026-11-30", "month")


# ---------------------------------------------------------------- Ημερολόγιο: αντιστοίχιση τίτλων ΑΑΔΕ/taxheaven
def test_general_titles_map_to_rules_and_nearest_reference_month():
    c = deadlines.classify_general
    assert c("Υποβολή δήλωσης Φ.Π.Α. (χρεωστικής - μηδενικής - πιστωτικής) για επιχειρήσεις με διπλογραφικά βιβλία, "
             "μηνός Σεπτεμβρίου") == ["vat_monthly"]
    assert "vat_quarterly" in c("Υποβολή δηλώσεων Φ.Π.Α. (χρεωστικών - μηδενικών - πιστωτικών) από επιχειρήσεις με "
                                "απλογραφικά βιβλία α) τριμήνου Ιουλίου, Αυγούστου, Σεπτεμβρίου … ή την επιλογή τους")
    assert c("Υποβολή δήλωσης επιλογής (ή ανάκλησης) δυνατότητας υποβολής δήλωσης Φ.Π.Α. ανά τρίμηνη περίοδο") == ["vat_quarter_option"]
    assert c("Υποβολή Α.Π.Δ. e-Ε.Φ.Κ.Α. μηνός Σεπτεμβρίου από τους εργοδότες κοινών επιχειρήσεων") == ["apd"]
    assert c("Υποβολή ανακεφαλαιωτικού πίνακα ενδοκοινοτικών παραδόσεων αγαθών") == ["vies"]
    assert c("Υποβολή δήλωσης απόδοσης του φόρου που παρακρατείται στο εισόδημα από μισθωτή εργασία και συντάξεις") == ["fmy_wages"]
    assert c("Υποβολή δήλωσης απόδοσης παρακρατούμενου φόρου, σύμφωνα με τις διατάξεις, της περ. δ’ της παρ. 1 του άρθ. 64") \
        == ["withholding_fees"]
    assert c("Υποβολή δήλωσης απόδοσης τέλους ανθεκτικότητας στην κλιματική κρίση, μηνός Σεπτεμβρίου") == ["climate_fee"]
    assert c("Υποβολή δήλωσης και απόδοση οφειλόμενου Ψηφιακού Τέλους Συναλλαγών") == ["digital_fee"]
    # ΑΠΔ στις 2/11/2026 (31/10 Σάββατο) = Σεπτέμβριος, όχι Οκτώβριος
    assert deadlines.nearest_ref("apd", date(2026, 11, 2)) == "2026-09"
    assert obligations.month_end_or_next(2026, 10) == date(2026, 11, 2)
    assert obligations.RULES_BY_ID["oss"].due(2026, 10) == date(2026, 10, 31)          # OSS: δεν μετατίθεται


def test_rule_hidden_when_feed_has_same_deadline_a_few_days_later(conn):
    from taxmatch import db
    conn.execute("INSERT INTO obligations_general(guid,title,due_date,source_url,fetched_at) VALUES "
                 "('a','Υποβολή Α.Π.Δ. e-Ε.Φ.Κ.Α. μηνός Σεπτεμβρίου από τους εργοδότες','2026-11-02','u',?)", (db.utcnow(),))
    evs = deadlines.events_between(conn, date(2026, 10, 25), date(2026, 11, 5))
    assert not any(e["kind"] == "rule" and e["id"] == "apd" for e in evs)
    gen = next(e for e in evs if e["kind"] == "general")
    assert gen["rules"] == ["apd"] and gen["ref_months"]["apd"] == "2026-09"


# ---------------------------------------------------------------- υποβολές ανά πελάτη, χειροκίνητο «ολοκληρώθηκε»
def _vat_client(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    service.update_fields(conn, AFM, {"vat_period_type": "monthly"}, vat_subject=True)
    conn.execute("UPDATE businesses SET activity_state='active' WHERE afm=?", (AFM,))


def test_submission_status_per_client_on_calendar_events(conn):
    _vat_client(conn)
    easycheck.store_results(conn, AFM, 2026, [{"obligation": "Φ2", "status": "submitted", "periods": [
        {"period": "9ος Μήνας", "status": "submitted", "detail": "", "period_end": "2026-09-30", "period_type": "month",
         "amounts": {"result": "Πιστωτική", "deduction": 50.0}}]}])
    ev = {"kind": "rule", "id": "vat_monthly", "date": "2026-10-30", "title": "ΦΠΑ", "ref_month": "2026-09"}
    people = deadlines.clients_for_event(conn, ev)
    me = next(p for p in people if p["afm"] == AFM)
    assert me["submission"]["status"] == "submitted" and "για έκπτωση 50,00 €" in obligations.amounts_text(
        me["submission"]["amounts"])
    evs = deadlines.events_between(conn, date(2026, 10, 30), date(2026, 10, 30), include_news=False)
    vat = next(e for e in evs if e["kind"] == "rule" and e["id"] == "vat_monthly")
    assert vat["n_submitted"] == 1 and vat["n_missing"] == 0
    # η χρήση στην καρτέλα του πελάτη
    rows = deadlines.client_year(conn, AFM, 2026)
    sep = next(r for r in rows if r["rule_id"] == "vat_monthly" and r["ref_month"] == "2026-09")
    assert sep["submission"]["status"] == "submitted"
    oct_ = next(r for r in rows if r["rule_id"] == "vat_monthly" and r["ref_month"] == "2026-10")
    assert oct_["submission"]["status"] == ""
    deadlines.mark_done(conn, AFM, "vat_monthly", "2026-10")
    oct_ = next(r for r in deadlines.client_year(conn, AFM, 2026) if r["rule_id"] == "vat_monthly" and r["ref_month"] == "2026-10")
    assert oct_["submission"]["status"] == "done" and oct_["submission"]["done_at"]
    deadlines.mark_done(conn, AFM, "vat_monthly", "2026-10", done=False)
    assert not deadlines._checks_for(conn, AFM)["done"]


def test_not_obligated_check_removes_liability(conn):
    _vat_client(conn)
    conn.execute("UPDATE businesses SET employer=1 WHERE afm=?", (AFM,))
    b = {**dict(conn.execute("SELECT * FROM businesses WHERE afm=?", (AFM,)).fetchone())}
    assert obligations.liability("fmy_wages", b, "2026-08")[0] == "yes"
    easycheck.store_results(conn, AFM, 2026, [{"obligation": "ΦΜΥ", "status": "not_obligated", "periods": []}])
    b.update(deadlines._checks_for(conn, AFM))
    status, reason = obligations.liability("fmy_wages", b, "2026-08")
    assert status == "no" and "ΑΑΔΕ" in reason


# ---------------------------------------------------------------- τέλη από τα έσοδα + προειδοποίηση ΦΠΑ
def _sync(conn, outgoing=(), incoming=()):
    service.add(conn, AFM, "ΠΕΛΑΤΗΣ")
    mcreds.set_(conn, AFM, "md_user", "md_key")
    fake = FakeClient(incoming=list(incoming), outgoing=list(outgoing))
    return store.sync_range(conn, AFM, ["2026-03", "2026-04"], client_factory=fake)


def _with(xml, extra_summary="", details=""):
    return xml.replace("</invoiceSummary>", extra_summary + "</invoiceSummary>").replace(
        "<invoiceSummary>", details + "<invoiceSummary>")


def test_climate_fee_and_digital_fee_create_obligations_from_income(conn):
    me = (AFM, "ΠΕΛΑΤΗΣ", "GR")
    buyer = ("222222222", "ΑΓΟΡΑΣΤΗΣ", "GR")
    out = [_with(inv("70", "8.2", "2026-03-10", issuer=me, counter=buyer, net=30)),
           _with(inv("71", "2.1", "2026-03-12", issuer=me, counter=buyer), "<totalStampDutyAmount>12.00</totalStampDutyAmount>")]
    FakeClient.calls = 0
    res = _sync(conn, outgoing=out)
    assert res["ok"] and res["months"] == 2 and FakeClient.calls == 2      # ΕΝΑ αίτημα ανά κατεύθυνση για 2 μήνες
    fees = analysis.special_fees(conn, AFM, "2026-03")
    assert fees["climate_docs"] == 1 and fees["digital_fee"] == 12.0
    b = {"afm": AFM, "activity_state": "active", "mydata": analysis.month_flags(conn)[AFM]}
    b["mydata_ever"] = deadlines._ever(b["mydata"])
    assert obligations.liability("digital_fee", b, "2026-03")[0] == "yes"
    assert obligations.liability("climate_fee", b, "2026-03")[0] == "yes"
    assert obligations.liability("digital_fee", b, "2026-04")[0] == "no"
    assert obligations.liability("digital_fee", b, "2026-07") == ("maybe", obligations._MAYBE_REASON["digital_fee"])
    assert obligations.submission("digital_fee", b, "2026-03")["manual"]


def test_digital_fee_never_seen_is_not_maybe_for_everyone():
    b = {"afm": AFM, "activity_state": "active", "mydata": {"2026-03": {"digital_fee": False}},
         "mydata_ever": {"digital_fee": False}}
    assert obligations.liability("digital_fee", b, "2026-07")[0] == "no"


def test_vat_warning_for_non_vat_client_with_vat_documents(conn):
    me = (AFM, "ΠΕΛΑΤΗΣ", "GR")
    line = "<invoiceDetails><lineNumber>1</lineNumber><netValue>100</netValue><vatCategory>1</vatCategory>" \
           "<vatAmount>24</vatAmount></invoiceDetails>"
    _sync(conn, outgoing=[_with(inv("80", "2.1", "2026-03-10", issuer=me, counter=("2", "Β", "GR")), details=line)],
          incoming=[_with(inv("81", "1.1", "2026-03-11"), details=line)])          # λήψη με ΦΠΑ από προμηθευτή: φυσιολογικό
    assert analysis.vat_warnings(conn, AFM) == []                                  # υπαγωγή άγνωστη -> καμία προειδοποίηση
    conn.execute("UPDATE businesses SET vat_subject=0 WHERE afm=?", (AFM,))
    warn = analysis.vat_warnings(conn, AFM, 2026)
    assert [d["mark"] for d in warn] == ["80"]


# ---------------------------------------------------------------- επωνυμίες: πελάτες > VIES > παραστατικό
def test_party_names_trust_order_and_vies(conn):
    _sync(conn, incoming=[inv("90", "1.1", "2026-03-05", issuer=("333333333", "ΔΙΑΚΡΙΤΙΚΟΣ ΤΙΤΛΟΣ", "GR")),
                          inv("91", "1.1", "2026-03-06", issuer=("444444444", "", "GR"))])
    assert names.name_map(conn)["333333333"] == "ΔΙΑΚΡΙΤΙΚΟΣ ΤΙΤΛΟΣ"                # από το παραστατικό
    assert set(names.pending(conn, AFM)) == {"333333333", "444444444"}            # κανένα δεν έχει επίσημη επωνυμία
    found = names.resolve_via_vies(conn, AFM, lookup=lambda vat: {"333333333": "ΕΠΙΣΗΜΗ ΕΠΩΝΥΜΙΑ Α.Ε."}.get(vat, ""))
    m = names.name_map(conn)
    assert found == 1 and m["333333333"] == "ΕΠΙΣΗΜΗ ΕΠΩΝΥΜΙΑ Α.Ε." and "444444444" not in m
    assert names.pending(conn, AFM) == []                                          # η αστοχία δεν ξαναρωτιέται
    names.learn(conn, AFM)                                                         # το παραστατικό δεν πατά το VIES
    assert names.name_map(conn)["333333333"] == "ΕΠΙΣΗΜΗ ΕΠΩΝΥΜΙΑ Α.Ε."
    doc = dict(conn.execute("SELECT * FROM mydata_docs WHERE mark='90'").fetchone())
    assert names.display_name(doc, names.name_map(conn), AFM) == ("ΕΠΙΣΗΜΗ ΕΠΩΝΥΜΙΑ Α.Ε.", "333333333")


# ---------------------------------------------------------------- χρονοπρογραμματισμός myDATA
def test_scheduled_mydata_fetches_only_needed_months(conn, monkeypatch):
    service.add(conn, AFM, "ΠΕΛΑΤΗΣ")
    mcreds.set_(conn, AFM, "md_user", "md_key")
    today = date(2026, 10, 22)
    store._store(conn, AFM, [], ["2026-07", "2026-08"])                            # ήδη κατεβασμένοι
    assert store.months_to_refresh(conn, AFM, today) == ["2026-09", "2026-10"]    # +ο τρέχων και ο προηγούμενος
    seen = []
    monkeypatch.setattr(store, "sync_range", lambda c, afm, months, **k: seen.append((afm, months)) or {"ok": True, "n": 3})
    monkeypatch.setattr(store, "months_to_refresh", lambda c, afm: ["2026-09", "2026-10"])
    stats = scheduled.run(conn, vies_limit=0)
    assert stats["ok"] == 1 and stats["docs"] == 3 and seen == [(AFM, ["2026-09", "2026-10"])]
