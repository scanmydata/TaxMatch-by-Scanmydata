"""TaxSystem 26.11 (repo Taxsystem-reverse-engineer-fix, 2026-11-18): «Ανάλυση Οφειλών Ε.Φ.Κ.Α.» — απλήρωτες μηνιαίες
εισφορές μη μισθωτού που δεν πέρασαν ακόμη στο ΚΕΑΟ — και «Απαιτητές Δόσεις Ρύθμισης» του ΚΕΑΟ. Ψεύτικες σελίδες."""
from taxmatch import keao
from taxmatch.gui.debts_tab import DebtsTab

AFM = "094259216"
RF = "RF12345678901234567"
FORM = "form-id:accordion-insurance-obligations-citizen"
TABS = ["Τρέχουσες Οφειλές", "Ασφαλιστικές Εισφορές", "Μηνιαία Ειδοποιητήρια", "Βεβαιωμένες Οφειλές Μη Μισθωτού στο ΚΕΑΟ"]


def _upd(uid, html):
    return f'<partial-response><changes><update id="{uid}"><![CDATA[{html}]]></update></changes></partial-response>'


def _tr(*cells):
    return "<tr class='ui-datatable-even'>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"


def _table(headers, rows, body_id="x_data"):
    return ("<table><thead><tr>" + "".join(f"<th>{h}</th>" for h in headers) + f"</tr></thead><tbody id='{body_id}'>"
            + "".join(rows) + "</tbody></table>")


CONTENT = {
    # δύο πίνακες: κωδικός/ποσό/προθεσμία και είδος/μήνας/ποσό/προθεσμία/RF — μόνο ο τρέχων μήνας (11/2026)
    0: _table(["Κωδικός Πληρωμής", "Ποσό", "Προθεσμία"], [_tr("RF12 3456 7890 1234 567", "250,50", "30/11/2026")])
       + _table(["Είδος Οφειλής", "Ημ. Αναφοράς", "Πληρωτέο Ποσό", "Εμπρόθεσμη Πληρωμή Μέχρι", "Κωδικός Πληρωμής"],
                [_tr("Εισφορά ΕΦΚΑ", "11/2026", "250,50", "30/11/2026", "RF12 3456 7890 1234 567")]),
    # διάταξη όπως ΖΩΝΤΑΝΑ (18/11/2026): παλιός ΕΦΚΑ/ΕΤΕΑΕΠ + e-ΕΦΚΑ με «Εξόφληση» Ναι/Όχι
    1: _table(["Ημ. Αναφοράς", "Φορέας", "Εισφορά", "Ιδιότητα", "Παρατηρήσεις", "Εξόφληση", "Εκκαθάριση"],
              [_tr("01/09/2026", "ΕΤΕΑΕΠ", "68,00 €", "ΑΥΤΟΑΠΑΣΧΟΛΟΥΜΕΝΟΣ", "", "Όχι", "Όχι", "ΑΝΑΛΥΣΗ ΜΗΝΑ"),
               _tr("01/12/2019", "ΕΦΚΑ", "185,18 €", "ΑΥΤΟΑΠΑΣΧΟΛΟΥΜΕΝΟΣ", "", "Ναι", "Ναι", "ΑΝΑΛΥΣΗ ΜΗΝΑ")])
       + _table(["Ημ/νία Αναφοράς", "Φορέας", "Ιδιότητα", "Μηνιαία Εισφορά", "Ποσό Απαίτησης", "Εξοφλημένο Ποσό",
                 "Ημ/νία Απαίτησης", "Ημ/νία Εξόφλησης", "Παρατηρήσεις", "Εξόφληση", "Εκκαθάριση"],
                [_tr("01/10/2026", "e-ΕΦΚΑ", "ΕΛ.ΕΠ.", "607,07 €", "607,07 €", "0,00 €", "30/11/2026", "", "", "Όχι", "Όχι", "Α"),
                 # μερική πληρωμή: έχει ημερομηνία εξόφλησης αλλά «Όχι» — μένουν 599,05
                 _tr("01/09/2026", "e-ΕΦΚΑ", "ΕΛ.ΕΠ.", "607,07 €", "607,07 €", "8,02 €", "31/10/2026", "23/10/2026", "", "Όχι",
                     "Όχι", "Α"),
                 _tr("01/08/2026", "e-ΕΦΚΑ", "ΕΛ.ΕΠ.", "607,07 €", "607,07 €", "0,00 €", "30/09/2026", "", "", "Όχι", "Όχι", "Α"),
                 _tr("01/07/2026", "e-ΕΦΚΑ", "ΕΛ.ΕΠ.", "607,07 €", "607,07 €", "0,00 €", "31/08/2026", "", "", "Όχι", "Όχι",
                     "Α"),                                                                      # στο ΚΕΑΟ
                 _tr("01/06/2026", "e-ΕΦΚΑ", "ΕΛ.ΕΠ.", "607,07 €", "607,07 €", "607,07 €", "31/07/2026", "15/07/2026", "",
                     "Ναι", "Όχι", "Α"),
                 _tr("01/05/2026", "e-ΕΦΚΑ", "ΕΛ.ΕΠ.", "607,07 €", "607,07 €", "0,00 €", "30/06/2026", "", "", "Όχι", "Όχι",
                     "Α")]),                                    # παλιότερος από τον τελευταίο βεβαιωμένο (άλλη σελίδα)
    2: _table(["Μήνας", "Φορέας", "Ποσό", "Εμπρόθεσμη πληρωμή", "Εκτύπωση"],
              [_tr("9/2026", "e-ΕΦΚΑ", "607,07", "31/10/2026", '<button id="mon:0:print">PDF</button>'),
               _tr("8/2026", "e-ΕΦΚΑ", "607,07", "30/09/2026", '<button id="mon:1:print">PDF</button>'),
               _tr("7/2026", "e-ΕΦΚΑ", "607,07", "31/08/2026", '<button id="mon:2:print">PDF</button>'),
               _tr("5/2026", "e-ΕΦΚΑ", "607,07", "30/06/2026", '<button id="mon:3:print">PDF</button>')]),
    3: _table(["Αριθμός Πράξης", "Ημ/νία Έκδοσης", "Ημ/νία Αναφοράς Οφειλής", "Ποσό"],
              [_tr("2026000123", "10/10/2026", "01/07/2026", "607,07"),
               _tr("", "10/10/2026", "01/08/2026", "12,00")]),       # προσαυξήσεις χωρίς πράξη: ΔΕΝ βεβαιωμένος
}


class _FakeObligations:
    def __init__(self):
        self.printed = []

    def follow(self, method, url, form=None, headers=None):
        form = form or {}
        if method == "GET":
            lis = "".join(f'<li class="ui-tabs-header" data-index="{i}"><a href="#{FORM}:t{i}">{t}</a></li>'
                          for i, t in enumerate(TABS))
            return {"url": url, "text": f'<input name="jakarta.faces.ViewState" value="VS"/><div id="{FORM}"><ul>{lis}</ul></div>'}
        if form.get("jakarta.faces.source") == FORM:
            return {"url": url, "text": _upd(FORM, CONTENT[int(form[FORM + "_tabindex"])])}
        btn = next((k for k in form if k.startswith("mon:")), "")
        if btn:
            self.printed.append(btn)
            return {"url": url, "is_pdf": True, "content": b"%PDF-1.4 fake", "text": ""}
        raise AssertionError((method, url, form))


def test_efka_unpaid_months_not_yet_in_keao(tmp_path):
    fake = _FakeObligations()
    res = keao.efka_current(fake, {}, AFM, tmp_path)
    assert res["current_rf"] == RF
    # 9 και 8: απλήρωτοι + ειδοποιητήριο + όχι στο ΚΕΑΟ (ο 8 έχει εκεί μόνο γραμμή ΧΩΡΙΣ αριθμό πράξης)· 10 χωρίς
    # ειδοποιητήριο· 7 βεβαιωμένος· 6 εξοφλημένος· 5 παλιότερος από τον τελευταίο βεβαιωμένο (7) ⇒ σε άλλη σελίδα του ΚΕΑΟ
    assert res["owed_months"] == ["2026-09", "2026-08"]
    by_title = {ln["title"]: ln for ln in res["lines"]}
    assert set(by_title) == {"Εισφορά ΕΦΚΑ", "Μηνιαία Εισφορά e-ΕΦΚΑ 9/2026", "Μηνιαία Εισφορά e-ΕΦΚΑ 8/2026",
}
    cur, sep = by_title["Εισφορά ΕΦΚΑ"], by_title["Μηνιαία Εισφορά e-ΕΦΚΑ 9/2026"]
    assert (cur["code"], cur["total"], cur["next_due"]) == (RF, 250.5, "30/11/2026")   # σωστές στήλες (2ος πίνακας)
    assert (sep["code"], sep["total"], sep["overdue"], sep["next_due"]) == (RF, 599.05, 599.05, "31/10/2026")
    assert sep["details"]["unpaid_month"] and sep["pdf_path"].endswith(f"EFKA_EFKA_{AFM}_2026-09.pdf")
    assert sorted(fake.printed) == ["mon:0:print", "mon:1:print"]                    # τελευταίος + απλήρωτοι
    assert "δεν έχει βεβαιωθεί ακόμη στο ΚΕΑΟ" in DebtsTab.details_html({**sep, "kind": "efka"})


def test_efka_without_the_extra_tabs_keeps_only_the_current_month():
    class Old(_FakeObligations):
        def follow(self, method, url, form=None, headers=None):
            if method == "GET":
                return {"url": url, "text": f'<input name="jakarta.faces.ViewState" value="VS"/><div id="{FORM}"><ul>'
                                            f'<li class="ui-tabs-header" data-index="0"><a href="#{FORM}:t0">'
                                            f'Τρέχουσες Οφειλές</a></li></ul></div>'}
            return super().follow(method, url, form, headers)
    res = keao.efka_current(Old(), {}, AFM)
    assert [ln["title"] for ln in res["lines"]] == ["Εισφορά ΕΦΚΑ"] and res["owed_months"] == []


# ------------------------------------------------------------------ ΚΕΑΟ: απαιτητές δόσεις ρύθμισης
_PAY = ('<div id="paymentsForm:installmentsTable_s">rowCount: 1</div>'
        '<table><tbody id="paymentsForm:installmentsTable_data">'
        '<tr data-ri="0"><td><span class="ui-column-title">Υποκατάστημα</span>ΑΘΗΝΩΝ</td>'
        '<td><span class="ui-column-title">Απόφαση</span><div>12345/2025</div><div>01/12/2025</div></td>'
        '<td><span class="ui-column-title">Είδος</span>Ρύθμιση 120 δόσεων</td>'
        '<td><span class="ui-column-title">Ποσό</span>105,00</td>'
        '<td><a id="paymentsForm:installmentsTable:0:ana">Ανάλυση Δόσεων</a></td></tr></tbody></table>')
_DLG = ('<table><tbody id="paymentsForm:dt-latest-unpaid-installment_data">'
        '<tr data-ri="0"><td>2</td><td>28/02/2026</td><td>100,00</td><td>0,00</td><td>100,00</td><td>5,00</td></tr>'
        '</tbody></table>')


def test_keao_required_regulated_instalments():
    class Http:
        def follow(self, method, url, form=None, headers=None):
            assert form["jakarta.faces.source"] == "paymentsForm:installmentsTable:0:ana"
            return {"url": url, "text": _upd("paymentsForm:latestUnpaidInstallmentDialog", _DLG)}
    req = keao._required_instalments(Http(), "VS", "/post", _PAY)
    assert req == [{"branch": "ΑΘΗΝΩΝ", "resolution": "12345/2025 - 01/12/2025", "type": "Ρύθμιση 120 δόσεων",
                    "amount": "105,00", "instalments": [{"no": "2", "due": "28/02/2026", "amount": "100,00",
                                                         "paid": "0,00", "balance": "100,00", "increments": "5,00"}]}]
    assert keao._required_instalments(Http(), "VS", "/post", "<div>χωρίς πίνακα</div>") == []
    carrier = {"amo": "111", "description": "ΕΦΚΑ (ΠΡΩΗΝ ΙΚΑ) - ΕΡΓΟΔΟΤΗΣ", "carrier_am": "", "company": "", "debtor_id": "RF55",
               "branch": "", "totals": {"balance": "105,00"}, "regulated": [], "outstanding": [], "required": req}
    line = keao.keao_lines([carrier])[0]
    assert line["details"]["required_total"] == 105.0 and line["total"] == 105.0       # ένδειξη, όχι διπλομέτρηση
    assert "Απαιτητή δόση ρύθμισης" in DebtsTab.details_html(line)


def test_keao_carrier_with_disabled_menus_is_not_an_error():
    """Ζωντανά: φορέας (ΟΑΕΕ) με «Κινήσεις Οφειλέτη»/«Πληρωμή/IRIS» απενεργοποιημένα — πριν χανόταν όλη η λήψη ΚΕΑΟ."""
    menu = ('<a role="menuitem" class="ui-menuitem-link ui-state-disabled" href="#" onclick="return false;">'
            '<span>Κινήσεις Οφειλέτη</span></a>'
            '<a role="menuitem" class="ui-menuitem-link ui-state-disabled" onclick="return false;"><span>Πληρωμή/IRIS</span></a>')

    class Http:
        posts = 0

        def follow(self, method, url, form=None, headers=None):
            self.posts += 1
            return {"url": url, "text": _upd("MenuForm", menu)}
    http = Http()
    row = '<td>1000001</td><td>Ληξιπρόθεσμο - ΟΑΕΕ - ΟΑΕΕ</td><td>1</td><td>Χ</td><td><button id="b0">Επιλογή</button></td>'
    c = keao._carrier(http, "VS", "/p", row)
    assert c["no_access"] and http.posts == 1 and c["regulated"] == [] and c["required"] == []
    line = keao.keao_lines([c])[0]
    assert line["total"] == 0.0 and "δεν δίνει κινήσεις" in DebtsTab.details_html(line)
    assert keao._menu_enabled('<a role="menuitem" onclick="PrimeFaces.ab({s:&quot;m:1&quot;})"><span>Πληρωμή/IRIS</span></a>',
                              "Πληρωμή/IRIS")
