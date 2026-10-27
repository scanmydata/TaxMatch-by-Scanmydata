"""Port της λογικής του ScanMyData Tax Center (ρητό αίτημα χρήστη 2026-10-22): ΚΕΑΟ ανά φορέα (keao-debts.js +
KeaoCard.kt), τρέχουσες εισφορές e-ΕΦΚΑ (efka-notices.js), «Τα Αιτήματά μου» ΑΑΔΕ (easynotify/aade-request.js +
AadeRequests.kt), μέλη εταιρειών (Μητρώο ΑΑΔΕ / Excel), στοιχεία επικοινωνίας με την πηγή τους. Χωρίς δίκτυο."""
import json

from taxmatch import debts, efka, keao, members, requests_aade as req
from taxmatch.business_profiles import credentials, import_excel, service

AFM = "094259216"
MEMBER = "123456783"


def _upd(uid, html):
    return f'<partial-response><changes><update id="{uid}"><![CDATA[{html}]]></update></changes></partial-response>'


_LANDING = ('<div id="viewsPanel">Καλώς ήρθατε</div><a href="/ssp.keao/land.xhtml"><span>i</span>'
            '<span>Ηλεκτρονική Πλατφόρμα Οφειλετών - KEAO</span></a>'
            '<a href="/ssp.efka.non.employees/views/insuranceObligations.xhtml"><span>person</span> Υποχρεώσεις Ασφάλισης</a>')
_AMO_PAGE = ('<form id="amoForm" action="/ssp.keao/land.xhtml"><input name="jakarta.faces.ViewState" value="VS"/>'
             '<div id="amoForm:dt-table_s">rowCount: 1</div></form>')
_CARRIER_ROW = ('<tr data-ri="0"><td>111</td><td>ΕΦΚΑ (ΠΡΩΗΝ ΙΚΑ) - ΕΡΓΟΔΟΤΗΣ</td><td>9000000001</td><td>ΑΛΦΑ</td>'
                '<td><button id="amoForm:dt-table:0:btn">Επιλογή</button></td></tr>')
_MENU = ('<a role="menuitem" onclick="PrimeFaces.ab({s:&quot;menu:trans&quot;,f:1})">Κινήσεις Οφειλέτη</a>'
         '<a role="menuitem" onclick="PrimeFaces.ab({s:&quot;menu:pay&quot;,f:1})">Πληρωμή/IRIS</a>')
_TRANS = ('<div><span>Ταυτότητα Οφειλέτη:</span></div><div class="x">RF55 0000 1111</div>'
          '<div><span>Αρμόδιο Υποκ/μα:</span></div><div class="x">ΑΘΗΝΩΝ</div>'
          '<div class="ui-panelgrid-cell">Υπόλοιπο:</div><div class="ui-panelgrid-cell">1.500,00</div>')
_REG = ('<tr data-ri="0"><td>ΑΘ</td><td>12/2025 απόφαση</td><td>1.000,00</td><td>50,00</td><td>20,00</td><td>1.070,00</td>'
        '<td>Ρύθμιση 120 δόσεων</td><td>Πάγια</td><td>Ενεργή</td></tr>'
        '<ul id="debtorTransForm:mainTabView:dt-table-3:0:actionSelect3_menu"><li><a id="reg0:dos">Δόσεων</a></li></ul>'
        '<tr data-ri="1"><td>ΑΘ</td><td>03/2020 παλιά</td><td>0</td><td>0</td><td>0</td><td>500,00</td>'
        '<td>Ρύθμιση 24 δόσεων</td><td>Πάγια</td><td>Απολεσθείσα</td></tr>')
_INST = ('<tr data-ri="0"><td>1</td><td>31/01/2026</td><td>100,00</td><td>100,00</td><td>0,00</td><td>0,00</td></tr>'
         '<tr data-ri="1"><td>2</td><td>28/02/2026</td><td>100,00</td><td>0,00</td><td>100,00</td><td>5,00</td></tr>')
_PAY = ('<div id="paymentsForm:currentDebtsTable_s">rowCount: 1</div>')
_PAY_ROWS = '<tr data-ri="0"><td>01/09/2026</td><td>Εισφορές 08/2026</td><td>300,00</td><td>30,00</td><td>330,00</td></tr>'
_CURRENT = ('<table><tr><th>Είδος Οφειλής</th><th>Ημ. Αναφοράς</th><th>Πληρωτέο Ποσό</th><th>Εμπρόθεσμη Πληρωμή Μέχρι</th>'
            '<th>Κωδικός Πληρωμής</th></tr><tr class="ui-datatable-even"><td>Εισφορά ΕΦΚΑ</td><td>9/2026</td><td>250,50</td>'
            '<td>31/10/2026</td><td>RF12 3456</td></tr></table>')
_OBLIG = ('<input name="jakarta.faces.ViewState" value="VS2"/><div id="form-id:accordion-insurance-obligations-citizen">'
          '<ul><li class="ui-tabs-header" data-index="0"><a href="#form-id:accordion-insurance-obligations-citizen:t0">'
          'Τρέχουσες Οφειλές</a></li></ul></div>')


class _FakeEfka:
    def __init__(self):
        self.posts = []

    def follow(self, method, url, form=None, headers=None):
        form = form or {}
        src = form.get("jakarta.faces.source", "")
        self.posts.append(src)
        if url == efka.HOME:
            return {"url": url, "text": '<form action="/broker/nonemp"><a id="social-external-non-employee">x</a></form>'}
        if "broker/nonemp" in url:
            return {"url": "https://oauth2.gsis.gr/oauth2server/login.jsp", "text": ""}
        if url == efka.GSIS_CHECK:
            return {"url": "https://kc/role", "text": '<form id="kc-form-select-role" action="/role"></form>'}
        if url.endswith("/role"):
            assert form["role"] == "external-non-employee" and form["amka"] == "01017012345"
            return {"url": efka.SVC, "text": _LANDING}
        if "land.xhtml" in url and method == "GET":
            return {"url": url, "text": _AMO_PAGE}
        if src == "amoForm:dt-table":
            return {"url": url, "text": _upd("amoForm:dt-table", _CARRIER_ROW)}
        if src == "amoForm:dt-table:0:btn":
            return {"url": url, "text": _upd("MenuForm", _MENU)}
        if src == "menu:trans":
            return {"url": url, "text": _upd("mainContentPanel", _TRANS)}
        if src == "debtorTransForm:mainTabView":
            return {"url": url, "text": _upd("debtorTransForm:mainTabView", "")}
        if src == "debtorTransForm:mainTabView:dt-table-3":
            return {"url": url, "text": _upd("x", _REG) + "totalRecords&quot;:2"}
        if src == "reg0:dos":
            return {"url": url, "text": _upd("debtorTransForm:settlementInstallmentAnalysisDialog",
                                             '<div id="debtorTransForm:dt-settlement-installment-analysis_s">rowCount: 2</div>')}
        if src == "debtorTransForm:dt-settlement-installment-analysis":
            return {"url": url, "text": _upd("x", _INST)}
        if src == "menu:pay":
            return {"url": url, "text": _upd("mainContentPanel", _PAY)}
        if src == "paymentsForm:currentDebtsTable":
            return {"url": url, "text": _upd("x", _PAY_ROWS)}
        if "insuranceObligations" in url and method == "GET":
            return {"url": url, "text": _OBLIG}
        if src == "form-id:accordion-insurance-obligations-citizen":
            return {"url": url, "text": _upd("form-id:accordion-insurance-obligations-citizen", _CURRENT)}
        raise AssertionError((method, url, src))


def test_keao_and_efka_current_for_natural_person(conn):
    service.add(conn, AFM, "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ")
    credentials.set_(conn, AFM, "u", "p")
    assert efka.set_amka(conn, AFM, "1017012345")                       # Excel χάνει το 0 μπροστά
    fake = _FakeEfka()
    res = debts.check_client(conn, AFM, sources=("keao", "efka"),
                             efka_login=lambda u, p, a, **kw: efka.services_login(u, p, a, http_factory=lambda: fake, **kw))
    assert res["ok"] and not res["errors"], res
    rows = {d["kind"]: d for d in debts.client_debts(conn, AFM, include_inactive=True)}
    carrier, reg, old, cur = rows["carrier"], rows["keao_arrangement"], rows["keao_inactive"], rows["efka"]
    # οφειλή φορέα = εκτός ρύθμισης (330) + ό,τι μένει από τις δόσεις της ΕΝΕΡΓΗΣ ρύθμισης (100 + 5 προσαυξήσεις)
    assert (carrier["title"], carrier["code"], carrier["total"]) == ("ΕΦΚΑ (ΠΡΩΗΝ ΙΚΑ)", "RF55 0000 1111", 435.0)
    assert (reg["next_due"], reg["next_amount"], reg["unpaid"], reg["instalments"]) == ("28/02/2026", 105.0, 1, 2)
    assert old["total"] == 500.0 and old["code"] == ""                   # απολεσθείσα: ιστορικό, χωρίς κωδικό
    assert (cur["code"], cur["total"], cur["next_due"]) == ("RF123456", 250.5, "31/10/2026")
    assert "keao_inactive" not in {d["kind"] for d in debts.client_debts(conn, AFM)}
    assert debts.source_sync(conn, AFM)["keao"]["total"] == 435.0


def test_keao_reports_card_and_regulation_pdfs(tmp_path):
    carrier = {"amo": "111", "description": "ΕΦΚΑ (ΠΡΩΗΝ ΙΚΑ) - ΕΡΓΟΔΟΤΗΣ", "carrier_am": "9000000001", "company": "ΑΛΦΑ",
               "debtor_id": "RF55", "branch": "ΑΘΗΝΩΝ", "totals": {"balance": "1.500,00"},
               "regulated": [{"info": "12/2025 απόφαση", "primary": "1.000,00", "additional": "50,00", "interest": "20,00",
                              "total": "1.070,00", "type": "120 δόσεις", "pay_type": "", "status": "Ενεργή",
                              "instalments": [{"no": "1", "due": "28/02/2026", "amount": "100,00", "paid": "0,00",
                                               "balance": "100,00", "increments": "5,00"}]}],
               "outstanding": []}
    paths = keao.keao_reports([carrier], "ΑΛΦΑ", AFM, "ΓΡΑΦΕΙΟ", "22/10/2026", tmp_path)
    assert set(paths) == {"keao|111", "keao|111|12/2025|0"}
    from pypdf import PdfReader
    assert all(len(PdfReader(p).pages) >= 1 for p in paths.values())


def test_carrier_owed_falls_back_to_balance_without_details():
    c = {"totals": {"balance": "1.234,56"}, "regulated": [{"status": "Απολεσθείσα", "instalments": [], "total": "9,00"}],
         "outstanding": []}
    assert keao.carrier_owed(c) == {"total": 1234.56, "outside": 0.0, "regulated": 0.0, "fallback": True}
    assert not keao.is_active("Ανενεργή") and keao.is_active("Ενεργή ρύθμιση")


# ---------------------------------------------------------------- αιτήματα ΑΑΔΕ
class _FakeReq:
    def __init__(self, items, detail=None):
        self.items, self.detail, self.calls = items, detail or {}, []

    def follow(self, method, url, form=None, headers=None):
        self.calls.append(url)
        if url.endswith("/getUser"):
            return {"url": url, "text": json.dumps({"userData": {"userVat": AFM}})}
        if "/getMessageComm/" in url:
            return {"url": url, "text": "[]"}
        if "/downloadFile/" in url:
            return {"url": url, "status": 200, "text": "", "content": b"%PDF-1.4 apantisi", "ct": "application/pdf"}
        raise AssertionError(url)

    def post_raw(self, url, body, headers=None):
        if url.endswith("/filterMessages"):
            assert json.loads(body)["transactorVat"] == AFM
            return {"status": 200, "text": json.dumps({"pageCount": 1, "entityModels": self.items})}
        if url.endswith("/fetch"):
            assert body == "555"
            return {"status": 200, "text": json.dumps(self.detail)}
        raise AssertionError(url)


def _item(status, answer=""):
    return {"messageId": 555, "caseNumber": "Α-1", "submittedDate": "2026-10-01 10:00:00",
            "updatedDate": "2026-10-20 12:00:00", "messageStatusText": status, "diadikasiaText": "Διόρθωση Μητρώου",
            "answerText": answer}


def test_requests_first_fetch_is_quiet_then_changes_are_flagged(conn, tmp_path):
    service.add(conn, AFM, "ΑΛΦΑ")
    credentials.set_(conn, AFM, "u", "p")
    login = lambda items: (lambda u, p: {"ok": True, "http": _FakeReq(items)})  # noqa: E731
    first = req.refresh(conn, AFM, login_fn=login([_item("Υποβληθέν")]))
    assert first["ok"] and first["changes"] == {} and req.unseen(conn) == []   # πρώτη λήψη: τίποτα «νέο»
    second = req.refresh(conn, AFM, login_fn=login([_item("Σε εκκρεμότητα από τον συναλλασσόμενο")]))
    assert second["changes"] == {"555": "waiting_client"}
    third = req.refresh(conn, AFM, login_fn=login([_item("Απαντημένο", "Έγινε η διόρθωση.")]))
    assert third["changes"] == {"555": "answered"} and req.unseen(conn)[0]["change"] == "answered"
    from taxmatch import notices
    assert any("Αιτήματα ΑΑΔΕ" in n["text"] for n in notices.collect(conn))
    detail = {"messageId": 555, "messageFiles": [{"fileId": 9, "fileName": "Απάντηση ΑΑΔΕ.pdf",
                                                  "fileOrigin": "internalFile", "fileSize": 10}]}
    out = req.open_request(conn, AFM, "555", login_fn=lambda u, p: {"ok": True, "http": _FakeReq([], detail)})
    assert out["ok"] and out["files"][0]["path"].endswith("9_Απάντηση_ΑΑΔΕ.pdf")
    assert req.unseen(conn) == []                                              # το άνοιγμα το σημειώνει ως ιδωμένο


# ---------------------------------------------------------------- μέλη εταιρειών
def test_members_from_registry_only_active_and_merged_roles(conn):
    melh = json.dumps([{"epwnymia": "ΠΑΠΑΣ ΓΙΩΡΓΟΣ", "afmmelous": MEMBER, "eidossymmetoxhs": "ΕΤΑΙΡΟΣ", "percentage": "50.5",
                        "hmenarxhs": "01/01/2020"},
                       {"epwnymia": "ΠΑΛΙΟΣ", "afmmelous": "094259216", "eidossymmetoxhs": "ΕΤΑΙΡΟΣ",
                        "hmdiakophs": "01/01/2024"}])
    sxeseis = json.dumps([{"epwnymia": "ΠΑΠΑΣ ΓΙΩΡΓΟΣ", "afmsxeshs": MEMBER, "eidossxeshs": "ΔΙΑΧΕΙΡΙΣΤΗΣ"}])
    found = members.parse_members(melh, sxeseis)
    assert found == [{"afm": MEMBER, "name": "ΠΑΠΑΣ ΓΙΩΡΓΟΣ", "role": "ΕΤΑΙΡΟΣ · ΔΙΑΧΕΙΡΙΣΤΗΣ", "share": "50,5%",
                      "since": "01/01/2020"}]
    service.add(conn, AFM, "ΑΛΦΑ ΙΚΕ")
    members.store(conn, AFM, found, "aade")
    (m,) = members.company_members(conn, AFM)
    assert not m["is_client"] and m["role"] == "ΕΤΑΙΡΟΣ · ΔΙΑΧΕΙΡΙΣΤΗΣ"
    assert members.add_as_client(conn, AFM, MEMBER)
    assert members.company_members(conn, AFM)[0]["is_client"]
    members.store(conn, AFM, [], "aade")                                       # αποχώρησε: σβήνεται (πηγή ΑΑΔΕ)
    assert members.company_members(conn, AFM) == []


def test_members_and_amka_from_excel(conn):
    head = ("Α.Φ.Μ.", "Επωνυμία", "ΑΜΚΑ", "ΑΦΜ εταιρείας")
    res = import_excel.parse_rows([head, (AFM, "ΑΛΦΑ ΙΚΕ", None, None), (MEMBER, "ΠΑΠΑΣ ΓΙΩΡΓΟΣ", "1017012345", AFM)])
    out = service.import_result(conn, res)
    assert out["members"] == 1 and efka.get_amka(conn, MEMBER) == "01017012345"
    (m,) = members.company_members(conn, AFM)
    assert (m["member_afm"], m["name"], m["source"], m["is_client"]) == (MEMBER, "ΠΑΠΑΣ ΓΙΩΡΓΟΣ", "excel", True)


def test_contact_origin_and_date_are_stored(conn):
    service.add(conn, AFM, "ΑΛΦΑ")
    credentials.set_(conn, AFM, "u", "p")

    def fake_fetch(user, pwd, afm, **kw):
        return {"ok": True, "afm": afm, "name": "ΑΛΦΑ", "kind": "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ", "business_state": "active",
                "kads": [{"code": "62.01", "descr": "x", "is_main": True}],
                "all_tags": {}, "contact": {"email": "rep@office.gr", "mobile": "", "phone": "", "origin": "mailemep"}}
    from tests.test_business_profiles import portal_session2
    service.lookup_and_store(conn, AFM, portal_session2(None), fake_fetch)
    b = conn.execute("SELECT email, email_origin, contact_at FROM businesses WHERE afm=?", (AFM,)).fetchone()
    assert (b["email"], b["email_origin"]) == ("rep@office.gr", "mailemep") and b["contact_at"]


def test_companies_never_use_amka_only_employer_ame(conn):
    """Οι εταιρείες δεν έχουν ΑΜΚΑ (ρητή παρατήρηση χρήστη): το Excel δεν το αποθηκεύει, η σύνδεση e-ΕΦΚΑ γίνεται ΜΟΝΟ
    ως εργοδότης με ΑΜΕ — πρώτα με κωδικούς ΙΚΑ εργοδότη, μετά TAXISnet (ScanMyData kartela_ergodoti.py)."""
    head = ("Α.Φ.Μ.", "Επωνυμία", "ΑΜΚΑ")
    service.import_result(conn, import_excel.parse_rows([head, (AFM, "ΑΛΦΑ ΙΚΕ", "1017012345")]))
    assert efka.get_amka(conn, AFM) == ""
    assert debts.efka_plan(conn, AFM) == {}
    efka.set_(conn, AFM, ame="0020012345", user="ika_u", password="ika_p")
    credentials.set_(conn, AFM, "taxis_u", "taxis_p")
    assert debts.efka_plan(conn, AFM) == {"amka": "", "ame": "0020012345"}
    tried = []

    def login(user, password, afm, ame):
        tried.append(user)
        return {"ok": user == "taxis_u", "reason": "LandPage"}
    assert efka.employer_login_any(conn, AFM, "0020012345", login)["ok"] and tried == ["ika_u", "taxis_u"]


# ---------------------------------------------------------------- μέλη ως πελάτες + ατομική επιχείρηση (2026-10-23)
_RG_XML = ("<env:Envelope><env:Body><srvc:rgWsPublic2AfmMethodResponse><srvc:result><rg_ws_public2_result_rtType>"
           "<error_rec><error_code/><error_descr/></error_rec><basic_rec><afm>123456783</afm><doy_descr>Α ΑΘΗΝΩΝ</doy_descr>"
           "<onomasia>ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ</onomasia><commer_title>ΓΙΩΡΓΟΣ ΚΑΦΕ</commer_title>"
           "<firm_flag_descr>ΕΠΙΤΗΔΕΥΜΑΤΙΑΣ</firm_flag_descr><deactivation_flag>1</deactivation_flag>"
           "<regist_date>2019-03-01</regist_date><stop_date/><normal_vat_system_flag>Y</normal_vat_system_flag>"
           "</basic_rec><firm_act_tab><item><firm_act_code>56301000</firm_act_code><firm_act_descr>ΚΑΦΕΤΕΡΙΑ</firm_act_descr>"
           "<firm_act_kind>1</firm_act_kind></item></firm_act_tab></rg_ws_public2_result_rtType></srvc:result>"
           "</srvc:rgWsPublic2AfmMethodResponse></env:Body></env:Envelope>")


def test_rgwspublic_parse_and_business_state():
    from taxmatch.business_profiles import rgwspublic
    res = rgwspublic.parse(_RG_XML)
    assert res["ok"] and res["firm"] and res["trade_name"] == "ΓΙΩΡΓΟΣ ΚΑΦΕ"
    assert res["kads"] == [{"code": "56301000", "descr": "ΚΑΦΕΤΕΡΙΑ", "is_main": True}]
    assert rgwspublic.business_state(res) == "active"
    assert rgwspublic.business_state(rgwspublic.parse(_RG_XML.replace("<firm_flag_descr>ΕΠΙΤΗΔΕΥΜΑΤΙΑΣ",
                                                                      "<firm_flag_descr>ΜΗ ΕΠΙΤΗΔΕΥΜΑΤΙΑΣ"))) == "none"
    assert not rgwspublic.parse("<error_rec><error_code>RG_WS_PUBLIC_WRONG_AFM</error_code></error_rec>")["ok"]


def test_members_become_clients_and_individual_business_is_checked(conn):
    from taxmatch import members
    from taxmatch.business_profiles import rgwspublic
    service.add(conn, AFM, "ΑΛΦΑ ΙΚΕ")
    members.store(conn, AFM, [{"afm": "123456783", "name": "ΠΑΠΑΔΟΠΟΥΛΟΣ ΓΙΩΡΓΟΣ", "role": "ΔΙΑΧΕΙΡΙΣΤΗΣ"}], "aade")
    added = members.sync_member_clients(conn, AFM, rg_lookup=lambda a: rgwspublic.parse(_RG_XML))
    assert added == ["123456783"]
    b = conn.execute("SELECT * FROM businesses WHERE afm='123456783'").fetchone()
    assert b["activity_state"] == "active" and b["trade_name"] == "ΓΙΩΡΓΟΣ ΚΑΦΕ" and b["source"] == "member"
    assert members.parent_company(conn) == {"123456783": AFM}
    assert members.sync_member_clients(conn, AFM, check_business=False) == []          # ήδη πελάτης


def test_company_members_are_preserved_by_tax_year(conn):
    from taxmatch import members
    service.add(conn, AFM, "ΑΛΦΑ ΙΚΕ")
    members.store(conn, AFM, [{"afm": "123456783", "name": "ΠΑΠΑΣ"}], "aade")
    members.snapshot(conn, AFM, 2025)
    members.store(conn, AFM, [{"afm": "987654321", "name": "ΝΕΟ ΜΕΛΟΣ"}], "aade")

    assert [m["member_afm"] for m in members.company_members(conn, AFM, 2025)] == ["123456783"]
    assert [m["member_afm"] for m in members.company_members(conn, AFM)] == ["987654321"]


def test_company_member_details_show_sole_business_and_other_company(conn):
    from taxmatch import members
    member_afm, other_company = "123456783", "987654321"
    service.add(conn, AFM, "ΑΛΦΑ ΙΚΕ")
    service.add(conn, member_afm, "ΠΑΠΑΣ")
    service.add(conn, other_company, "ΒΗΤΑ ΑΕ")
    conn.execute("UPDATE businesses SET activity_state='active', legal_form='ΑΤΟΜΙΚΗ ΕΠΙΧΕΙΡΗΣΗ' WHERE afm=?",
                 (member_afm,))
    members.store(conn, AFM, [{"afm": member_afm, "name": "ΠΑΠΑΣ"}], "aade")
    members.store(conn, other_company, [{"afm": member_afm, "name": "ΠΑΠΑΣ", "role": "ΜΕΤΟΧΟΣ"}], "aade")

    (row,) = members.company_members(conn, AFM)
    assert row["own_business"] == "Ναι — ενεργή"
    assert row["other_companies"] == f"ΒΗΤΑ ΑΕ ({other_company})"
