"""Μητρώο ΑΑΔΕ (ΚΑΔ, διακοπή, είδος), αλυσίδα lookup ΑΑΔΕ→ΓΕΜΗ→VIES, μηνύματα λάθους κωδικών, ουρά ανάκτησης,
διαχείριση μοντέλου LLM και ειδοποιήσεις. ΣΥΝΘΕΤΙΚΑ δεδομένα με την ίδια δομή XML/JSON που επιστρέφει η ΑΑΔΕ."""
import json
import threading
import time
from datetime import date

import pytest

from taxmatch import db, jobs, notices, settings_store
from taxmatch.business_profiles import credentials, legal_form, lookup_aade as la, service
from taxmatch.extraction import llm_extract
from taxmatch.extraction.llm_extract import LLMClient, LLMError
from tests.fakes import FakeResponse, FakeSession
from tests.test_extraction import add_article, llm_reply

AFM = "094259216"
NOW = date(2026, 9, 21)


# ---------------------------------------------------------------- συνθετικές απαντήσεις Μητρώου

def userdata(afm, name="ΕΠΩΝΥΜΟ ΟΝΟΜΑ"):
    return (f"<mhtrwoLogin><afm>{afm}</afm><longepwnymia>{name}</longepwnymia>"
            f"<onomatepwnymo>{name}</onomatepwnymo><username>U</username></mhtrwoLogin>")


def fysiko(afm, *, status="ΚΑΝΟΝΙΚΗ", doy="ΔΟΥ ΤΕΣΤ", home="ΟΔΟΣ 1 ΤΚ:11111 ΑΘΗΝΑ"):
    # περιλαμβάνει (συνθετικά) προσωπικά πεδία, για να ελεγχθεί ότι ΔΕΝ αποθηκεύονται
    return (f"<mhtrwofusikou><afm>{afm}</afm><armodiadoy>{doy}</armodiadoy><artaytothtas>ΧΧ000000</artaytothtas>"
            f"<dieykatoikias>{home}</dieykatoikias><epwnymoa>ΕΠΩΝΥΜΟ ΟΝΟΜΑ</epwnymoa><hmgennhshs>01/01/1970</hmgennhshs>"
            f"<katastashforologoumenoy>{status}</katastashforologoumenoy></mhtrwofusikou>")


def epix(*, cease=None, reason=None, state="ΕΝΕΡΓΗ", books="Β-ΑΠΛΟΓΡΑΦΙΚΑ", extra=""):
    return ("<mhtrwoepixeirhshs><aa>1</aa>"
            + (f"<aitiadiakophs>{reason}</aitiadiakophs>" if reason else "")
            + "<dieyaskhshsdrasthrio>ΕΔΡΑ 5 ΤΚ:22222 ΠΟΛΗ</dieyaskhshsdrasthrio><doydescription>ΔΟΥ ΕΠΙΧ</doydescription>"
            + (f"<hmdiakophs>{cease}</hmdiakophs>" if cease else "") + "<hmenarxhs>04/09/2014</hmenarxhs>"
            + f"<katastashepixeirhshs>{state}</katastashepixeirhshs><kathgoriabibliwn>{books}</kathgoriabibliwn>"
            + "<ypagwghfpa>NAI</ypagwghfpa>" + extra + "</mhtrwoepixeirhshs>")


ACT = json.dumps([
    {"kwdikos": "56101101", "drasthriothta": "ΕΣΤΙΑΤΟΡΙΑ", "eidos": "ΚΥΡΙΑ", "hmdiakophs": None},
    {"kwdikos": "47111001", "drasthriothta": "ΛΙΑΝΙΚΟ", "eidos": "ΔΕΥΤΕΡΕΥΟΥΣΑ", "hmdiakophs": None},
    {"kwdikos": "10000000", "drasthriothta": "ΠΑΛΙΑ", "eidos": "ΔΕΥΤΕΡΕΥΟΥΣΑ", "hmdiakophs": "01/01/2020"},
], ensure_ascii=False)


def profile(afm=AFM, login=AFM, **kw):
    return la.build_profile(afm, login, kw.get("ud", userdata(login)), kw.get("fy", fysiko(afm)), kw.get("ep", epix()),
                            kw.get("act", ACT), today=NOW)


def test_active_sole_proprietor_gets_kads_kind_and_books():
    p = profile()
    assert p["ok"] and p["kind"] == "ΑΤΟΜΙΚΗ ΕΠΙΧΕΙΡΗΣΗ" and p["business_state"] == "active"
    assert [k["code"] for k in p["kads"]] == ["56101101", "47111001"]          # χωρίς την ήδη διακομμένη δραστηριότητα
    assert [k["is_main"] for k in p["kads"]] == [True, False]
    assert p["legal_form"] == "ΑΤΟΜΙΚΗ ΕΠΙΧΕΙΡΗΣΗ" and p["doy"] == "ΔΟΥ ΕΠΙΧ" and p["active"] is True
    assert p["address"] == "ΕΔΡΑ 5 ΤΚ:22222 ΠΟΛΗ"


def test_ceased_business_is_detected_even_though_status_says_active():
    """Η ΑΑΔΕ γράφει ΕΝΕΡΓΗ και μετά τη διακοπή· μόνο το hmdiakophs λέει την αλήθεια."""
    p = profile(ep=epix(cease="30/01/2026", reason="ΠΑΥΣΗ ΕΡΓΑΣΙΩΝ"), act="[]")
    assert p["business_state"] == "ceased" and p["cease_date"] == "30/01/2026" and p["cease_reason"] == "ΠΑΥΣΗ ΕΡΓΑΣΙΩΝ"
    assert p["kind"] == "ΙΔΙΩΤΗΣ" and p["kads"] == []                          # φυσικό πρόσωπο: ιδιώτης
    assert p["doy"] == "ΔΟΥ ΤΕΣΤ" and p["address"] == "ΟΔΟΣ 1 ΤΚ:11111 ΑΘΗΝΑ"    # ΔΟΥ/διεύθυνση του προσώπου, όχι της κλειστής
    assert p["active"] is True                                                  # ο πελάτης εξακολουθεί να υποβάλλει Ε1


def test_future_cease_date_is_not_ceased_yet():
    p = profile(ep=epix(cease="31/12/2026"))
    assert p["business_state"] == "active" and p["cease_date"] == "31/12/2026"


def test_legal_entity_stays_legal_entity_when_ceased_and_person_without_business():
    legal = la.build_profile(AFM, "999999999", userdata("999999999"), "<mhtrwofusikou></mhtrwofusikou>",
                             epix(cease="01/02/2026"), "[]", today=NOW)
    assert legal["kind"] == "ΝΟΜΙΚΟ ΠΡΟΣΩΠΟ" and legal["business_state"] == "ceased" and legal["active"] is False
    assert legal["name"] == ""                                                  # ξένο νομικό πρόσωπο: όνομα από ΓΕΜΗ/VIES
    person = la.build_profile(AFM, AFM, userdata(AFM), fysiko(AFM), "<mhtrwoepixeirhshs></mhtrwoepixeirhshs>", "[]", today=NOW)
    assert person["kind"] == "ΙΔΙΩΤΗΣ" and person["business_state"] == "none" and person["kads"] == []


def test_not_in_registry_and_personal_data_not_kept_in_raw():
    assert la.build_profile(AFM, "1", "", "<x/>", "<y/>", "[]", today=NOW) == {"ok": False, "reason": "NoRegistry", "afm": AFM}
    dump = json.dumps(profile()["raw"], ensure_ascii=False)
    assert "ΧΧ000000" not in dump and "1970" not in dump                       # ταυτότητα / ημ. γέννησης δεν αποθηκεύονται
    assert "kathgoriabibliwn" in dump


def test_activities_parser_is_tolerant():
    assert la.parse_activities("δεν είναι json") == [] and la.parse_activities("{}") == []
    assert la.parse_activities(json.dumps([{"kwdikos": ""}, "x", {"kwdikos": "47.11"}]))[0]["code"] == "47.11"


# ---------------------------------------------------------------- αλυσίδα lookup + μηνύματα κωδικών

def aade_ok(**over):
    base = {"ok": True, "name": "ΕΠΩΝΥΜΟ ΟΝΟΜΑ", "kind": "ΑΤΟΜΙΚΗ ΕΠΙΧΕΙΡΗΣΗ", "legal_form": "ΑΤΟΜΙΚΗ ΕΠΙΧΕΙΡΗΣΗ",
            "business_state": "active", "cease_date": "", "cease_reason": "", "business_start": "04/09/2014",
            "doy": "ΔΟΥ ΕΠΙΧ", "address": "ΕΔΡΑ 5", "kads": [{"code": "56101101", "descr": "ΕΣΤΙΑΤΟΡΙΑ", "is_main": True}],
            "all_tags": {"ypagwghfpa": "NAI", "kathgoriabibliwn": "Β-ΑΠΛΟΓΡΑΦΙΚΑ"}, "raw": {"kind": "x"}}
    return {**base, **over}


def test_aade_gives_kads_state_books_and_gemi_is_not_called(conn):
    settings_store.set_value(conn, "business_portal_key", "k")           # υπάρχει key, αλλά η ΑΑΔΕ έδωσε ΚΑΔ → δεν χρειάζεται
    service.add(conn, AFM)
    credentials.set_(conn, AFM, "u", "p")
    s = FakeSession()
    out = service.lookup_and_store(conn, AFM, s, lambda *a: aade_ok())
    b = service.get(conn, AFM)
    assert out["sources"] == ["ΑΑΔΕ"] and out["status"] == "ok" and not s.calls
    assert (b["activity_state"], b["books_category"], b["legal_form"], b["kad_main_code"]) == \
           ("active", "Β", "ΑΤΟΜΙΚΗ ΕΠΙΧΕΙΡΗΣΗ", "56.10.11.01") and b["status"] == "ΕΝΕΡΓΗ"
    assert credentials.status_map(conn)[AFM]["check_status"] == "ok"


def test_ceased_client_is_complete_without_kads_and_old_kads_are_cleared(conn):
    service.add(conn, AFM, "ΧΧΧ", kads=["47.11.10.01"])
    credentials.set_(conn, AFM, "u", "p")
    out = service.lookup_and_store(conn, AFM, None, lambda *a: aade_ok(
        business_state="ceased", cease_date="30/01/2026", cease_reason="ΠΑΥΣΗ ΕΡΓΑΣΙΩΝ", kads=[], kind="ΙΔΙΩΤΗΣ", legal_form="ΙΔΙΩΤΗΣ"))
    b = service.get(conn, AFM)
    assert out["status"] == "ok" and b["kads"] == [] and b["activity_state"] == "ceased" and b["cease_date"] == "30/01/2026"
    assert "ΔΙΑΚΟΠΗ ΕΡΓΑΣΙΩΝ 30/01/2026" in b["status"]
    # ξαναδουλεύει: αν ξανανοίξει η επιχείρηση, η διακοπή καθαρίζει
    service.lookup_and_store(conn, AFM, None, lambda *a: aade_ok())
    assert service.get(conn, AFM)["cease_date"] == "" and service.get(conn, AFM)["activity_state"] == "active"


def test_wrong_client_credentials_are_reported_marked_and_office_fallback_tried(conn):
    service.add(conn, AFM)
    credentials.set_(conn, AFM, "u", "λάθος")
    settings_store.set_value(conn, "aade_user", "office")
    settings_store.set_value(conn, "aade_pass", "pw")
    seen = []

    def fake(user, pwd, afm):
        seen.append(user)
        return {"ok": False, "reason": "InvalidCredentials"} if user == "u" else {"ok": False, "reason": "NoRegistry"}
    out = service.lookup_and_store(conn, AFM, None, fake)
    assert seen == ["u", "office"]                                            # πρώτα του πελάτη, μετά του γραφείου
    assert [i["code"] for i in out["issues"]] == ["bad_creds_client"]
    st = credentials.status_map(conn)[AFM]
    assert st["check_status"] == "invalid" and "Λάθος" in st["check_message"]
    assert settings_store.get(conn, "aade_office_status") == "ok"              # το login του γραφείου πέτυχε (απλώς δεν βλέπει τον πελάτη)
    assert any("δικό του Μητρώο" in e for e in out["errors"])                  # καθαρό μήνυμα γιατί ο λογαριασμός γραφείου δεν αρκεί
    assert out["status"] == "failed"                                           # οριστική αποτυχία, όχι «σε αναμονή»
    # ...αλλά δεν υποβαθμίζονται στοιχεία που ήδη έχουμε
    service.lookup_and_store(conn, AFM, None, lambda *a: aade_ok())
    out = service.lookup_and_store(conn, AFM, None, lambda *a: {"ok": False, "reason": "InvalidCredentials"})
    assert out["status"] == "ok" and service.get(conn, AFM)["lookup_status"] == "ok"
    assert [i["code"] for i in out["issues"]] == ["bad_creds_client", "bad_creds_office"]   # και οι δύο λογαριασμοί αναφέρονται
    assert service.get(conn, AFM)["kad_main_code"] == "56.10.11.01"


def test_wrong_credentials_keep_vies_name(conn, monkeypatch):
    from taxmatch.business_profiles import vies
    monkeypatch.setattr(vies, "lookup", lambda afm, session=None: vies.ViesResult(valid=True, name="ΕΤΑΙΡΕΙΑ ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε", address="Α 1"))
    service.add(conn, AFM)
    credentials.set_(conn, AFM, "u", "x")
    out = service.lookup_and_store(conn, AFM, None, lambda *a: {"ok": False, "reason": "InvalidCredentials"})
    b = service.get(conn, AFM)
    assert out["status"] == "partial" and b["name"].startswith("ΕΤΑΙΡΕΙΑ")
    assert b["legal_form"] == "ΜΟΝΟΠΡΟΣΩΠΗ ΙΚΕ"                                # τελευταίο fallback: από την κατάληξη της επωνυμίας


def test_gemi_is_the_fallback_when_aade_has_no_kads_or_no_access(conn):
    from tests.test_business_profiles import portal_session2
    from taxmatch.business_profiles import lookup_business_portal as portal
    portal.clear_cache()
    settings_store.set_value(conn, "business_portal_key", "k")
    service.add(conn, AFM)
    s = portal_session2({"arGemi": "9", "coNameEl": "ΤΟ ΚΑΛΟ Ι.Κ.Ε.", "legalType": {"descr": "ΙΚΕ"},
                         "activities": [{"code": "47.11.10.01", "descr": "Λιανικό"}]})
    out = service.lookup_and_store(conn, AFM, s, lambda *a: {"ok": False, "reason": "x"})   # χωρίς κωδικούς: δεν καλείται
    b = service.get(conn, AFM)
    assert out["sources"] == ["ΓΕΜΗ"] and b["legal_form"] == "ΙΚΕ" and b["kads"][0]["code"] == "47.11.10.01"


def test_legal_form_from_name():
    f = legal_form.from_name
    assert f("CHARGELINE ΜΟΝΟΠΡΟΣΩΠΗ Ι Κ Ε") == "ΜΟΝΟΠΡΟΣΩΠΗ ΙΚΕ"
    assert f("ΧΧΧ Ι.Κ.Ε.") == "ΙΚΕ" and f("ΧΧΧ ΑΕ") == "ΑΕ" and f("ΧΧΧ Α.Ε.") == "ΑΕ" and f("ΧΧΧ Ο.Ε.") == "ΟΕ"
    assert f("ΧΧΧ ΙΔΙΩΤΙΚΗ ΚΕΦΑΛΑΙΟΥΧΙΚΗ ΕΤΑΙΡΕΙΑ") == "ΙΚΕ" and f("XXX EPE") == "ΕΠΕ"
    assert f("ΓΕΩΡΓΙΟΣ ΠΑΠΑΔΟΠΟΥΛΟΣ") == "" and f("") == ""                # δεν εφευρίσκει


# ---------------------------------------------------------------- ειδοποιήσεις

def test_notices_show_and_clear_themselves(conn):
    assert [n["level"] for n in notices.collect(conn)] == ["warn"]            # δεν υπάρχει LLM key
    settings_store.set_value(conn, "openrouter_api_key", "k")                 # openrouter είναι ο προεπιλεγμένος πάροχος
    assert notices.collect(conn) == []
    settings_store.set_value(conn, "llm_last_error", "Το μοντέλο «x» δεν είναι διαθέσιμο")
    n = notices.collect(conn)
    assert n[0]["level"] == "danger" and "«x»" in n[0]["text"] and "matches" in n[0]["text"]
    settings_store.set_value(conn, "llm_last_error", "")
    service.add(conn, AFM, "ΠΕΛΑΤΗΣ ΑΕ")
    credentials.set_(conn, AFM, "u", "p")
    credentials.record_check(conn, AFM, "invalid", "Λάθος ή κλειδωμένος λογαριασμό TAXISnet.")
    settings_store.set_value(conn, "aade_office_status", "invalid")
    texts = " | ".join(n["text"] for n in notices.collect(conn))
    assert "γραφείου" in texts and "ΠΕΛΑΤΗΣ ΑΕ (094259216)" in texts
    credentials.record_check(conn, AFM, "ok", "ok")                            # διορθώθηκε → η ειδοποίηση εξαφανίζεται
    assert all("πελάτ" not in n["text"] or "ΠΕΛΑΤΗΣ" not in n["text"] for n in notices.collect(conn))


# ---------------------------------------------------------------- ουρά ανάκτησης

def wait(cond, timeout=8.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_lookup_queue_runs_while_another_job_is_running(conn):
    """Το «τρέχει ήδη άλλη εργασία» δεν πρέπει να απορρίπτει την ανάκτηση νέου πελάτη."""
    service.add(conn, AFM, "Α")
    service.add(conn, "123456783", "Β")
    m = jobs.JobManager()
    gate = threading.Event()
    assert m.start("run", lambda progress: gate.wait(5))                       # «Ανάλυση άρθρου 15/60»
    assert m.start("other", lambda p: None) is False                           # δεύτερο βαρύ job: όχι
    assert m.lookups.enqueue([AFM, "123456783", AFM]) == 2                     # η ουρά δέχεται (χωρίς διπλότυπα)
    assert wait(lambda: not m.lookups.snapshot()["running"] and m.lookups.snapshot()["done"] == 2)
    snap = m.snapshot()
    assert snap["running"] is True and snap["lookup"]["done"] == 2 and snap["lookup"]["pending"] == 2   # καμία πηγή ρυθμισμένη
    assert conn.execute("SELECT COUNT(*) FROM businesses WHERE lookup_at IS NOT NULL").fetchone()[0] == 0
    gate.set()


def test_lookup_queue_counts_wrong_credentials_and_survives_deleted_client(conn, monkeypatch):
    service.add(conn, AFM, "Α")
    real = service.lookup_and_store

    def fake(c, afm, *a, **k):
        if afm == "123456783":
            raise KeyError(afm)                                                # διαγράφηκε στο μεταξύ
        return {"status": "failed", "errors": [], "sources": [], "issues": [{"code": "bad_creds_client", "message": "x"}]}
    monkeypatch.setattr(service, "lookup_and_store", fake)
    q = jobs.LookupQueue()
    q.enqueue([AFM, "123456783"])
    assert wait(lambda: not q.snapshot()["running"] and q.snapshot()["done"] == 2)
    s = q.snapshot()
    assert s["bad_creds"] == 1 and s["failed"] == 1
    monkeypatch.setattr(service, "lookup_and_store", real)


# ---------------------------------------------------------------- LLM: μοντέλο που δεν υπάρχει

MODEL_404 = ('{"error":{"message":"The model `llama-3.3-70b-versatile` does not exist or you do not have access to it.",'
             '"type":"invalid_request_error","code":"model_not_found"}}')


def test_model_error_detection():
    assert llm_extract._is_model_error(404, MODEL_404)
    assert llm_extract._is_model_error(403, "The model `x` is blocked at the organization level")
    assert not llm_extract._is_model_error(404, "page not found") and not llm_extract._is_model_error(500, MODEL_404)


def test_pick_model_prefers_list_and_skips_non_chat():
    avail = ["whisper-large-v3", "meta-llama/llama-guard-4-12b", "openai/gpt-oss-20b", "openai/gpt-oss-120b"]
    assert llm_extract.pick_model("groq", avail) == "openai/gpt-oss-120b"
    assert llm_extract.pick_model("groq", avail, avoid="openai/gpt-oss-120b") == "openai/gpt-oss-20b"
    assert llm_extract.pick_model("groq", ["whisper-large-v3", "canopylabs/orpheus"]) == ""
    assert llm_extract.pick_model("openrouter", ["some/obscure-model"]) == ""     # στο OpenRouter δεν μαντεύουμε


def test_bad_model_is_replaced_automatically_and_failed_articles_are_retried(conn):
    """Πραγματικό σενάριο: το κλειδί δεν έχει πρόσβαση στο default μοντέλο → 60 άρθρα «failed», 0 matches."""
    add_article(conn, url="https://x.gr/old")
    conn.execute("UPDATE articles SET extraction_status='failed', extraction_tries=1, extraction_error=?", ("HTTP 404: " + MODEL_404,))
    add_article(conn, url="https://x.gr/new")
    ok = llm_reply({"relevant": True, "summary": "Σ", "scope": {"type": "all"}})

    def chat(url, **kw):
        return FakeResponse(MODEL_404, 404) if kw["json"]["model"] == "llama-3.3-70b-versatile" else ok
    s = (FakeSession().route("/models", FakeResponse(json_data={"data": [{"id": "whisper-large-v3"}, {"id": "openai/gpt-oss-120b"}]}))
         .route("chat/completions", chat))
    client = LLMClient("groq", "k", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1/chat/completions", s)
    out = llm_extract.extract_pending(conn, client, 10, 30, sleep=lambda _s: None)
    assert out["stopped"] == "" and out["done"] == 2 and out["model"] == "openai/gpt-oss-120b"
    assert settings_store.get(conn, "llm_model_groq") == "openai/gpt-oss-120b"     # αποθηκεύτηκε για τις επόμενες φορές
    assert conn.execute("SELECT MAX(extraction_tries) FROM articles").fetchone()[0] == 0


def test_no_usable_model_stops_with_clear_message_without_burning_tries(conn):
    add_article(conn)
    s = FakeSession().route("/models", FakeResponse(json_data={"data": [{"id": "whisper-large-v3"}]})).route(
        "chat/completions", FakeResponse(MODEL_404, 404))
    client = LLMClient("groq", "k", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1/chat/completions", s)
    out = llm_extract.extract_pending(conn, client, 10, 30, sleep=lambda _s: None)
    assert "δεν βρέθηκε καμία εναλλακτική" in out["stopped"] and out["processed"] == 0
    assert conn.execute("SELECT extraction_status, extraction_tries FROM articles").fetchone()[:] == ("pending", 0)


# ---------------------------------------------------------------- LLM: εξαντλημένο υπόλοιπο/όριο χρήσης

def test_402_raises_credits_error_not_generic_bad_response():
    s = FakeSession().route("chat/completions", FakeResponse(b'{"error":"insufficient balance"}', 402))
    client = LLMClient("openrouter", "k", "m", "https://openrouter.ai/api/v1/chat/completions", s)
    with pytest.raises(LLMError) as exc_info:
        client.complete_json("s", "u")
    assert exc_info.value.kind == "credits" and "402" in str(exc_info.value)


def test_429_with_quota_wording_is_credits_not_a_transient_rate_limit():
    s = FakeSession().route("chat/completions", FakeResponse(b'{"error":"insufficient_quota: daily limit reached"}', 429))
    client = LLMClient("openrouter", "k", "m", "https://openrouter.ai/api/v1/chat/completions", s)
    with pytest.raises(LLMError) as exc_info:
        client.complete_json("s", "u")
    assert exc_info.value.kind == "credits"


def test_429_without_quota_wording_stays_a_transient_rate_limit():
    s = FakeSession().route("chat/completions", FakeResponse(b"", 429, {"retry-after": "3"}))
    client = LLMClient("groq", "k", "m", "https://api.groq.com/openai/v1/chat/completions", s)
    with pytest.raises(LLMError) as exc_info:
        client.complete_json("s", "u")
    assert exc_info.value.kind == "rate_limit"


def test_exhausted_credits_switches_model_on_groq_but_not_openrouter(conn):
    """Groq: ένα μοντέλο μπορεί να έχει δικό του ξεχωριστό όριο — δοκιμάζουμε ΑΛΛΟ δωρεάν μοντέλο πριν σταματήσουμε
    (ίδια λογική με το «μοντέλο δεν υπάρχει»). OpenRouter: το όριο των δωρεάν (':free') μοντέλων είναι ΑΝΑ
    ΛΟΓΑΡΙΑΣΜΟ, όχι ανά μοντέλο — αλλαγή μοντέλου θα χτυπήσει το ίδιο όριο αμέσως, άρα ΔΕΝ τη δοκιμάζουμε καν."""
    ok = llm_reply({"relevant": True, "summary": "Σ", "scope": {"type": "all"}})

    def chat_groq(url, **kw):
        if kw["json"]["model"] == "llama-3.3-70b-versatile":
            return FakeResponse(b'{"error":"quota exceeded"}', 429)
        return ok

    add_article(conn)
    s_groq = (FakeSession().route("/models", FakeResponse(json_data={"data": [
                {"id": "llama-3.3-70b-versatile"}, {"id": "openai/gpt-oss-120b"}]}))
             .route("chat/completions", chat_groq))
    client_groq = LLMClient("groq", "k", "llama-3.3-70b-versatile", "https://api.groq.com/openai/v1/chat/completions", s_groq)
    out = llm_extract.extract_pending(conn, client_groq, 10, 30, sleep=lambda _s: None)
    assert out["stopped"] == "" and out["done"] == 1 and out["model"] == "openai/gpt-oss-120b"
    assert settings_store.get(conn, "llm_model_groq") == "openai/gpt-oss-120b"

    conn.execute("DELETE FROM articles")
    add_article(conn, url="https://x.gr/2")

    def chat_or(url, **kw):
        return FakeResponse(b'{"error":"Rate limit exceeded: free-models-per-day. Add 10 credits to unlock."}', 429)

    s_or = (FakeSession().route("/models", FakeResponse(json_data={"data": [
              {"id": "meta-llama/llama-3.3-70b-instruct:free"}, {"id": "deepseek/deepseek-chat-v3.1:free"}]}))
           .route("chat/completions", chat_or))
    client_or = LLMClient("openrouter", "k", "meta-llama/llama-3.3-70b-instruct:free", "https://openrouter.ai/api/v1/chat/completions", s_or)
    out = llm_extract.extract_pending(conn, client_or, 10, 30, sleep=lambda _s: None)
    assert out["model"] == "meta-llama/llama-3.3-70b-instruct:free"  # ΔΕΝ άλλαξε
    assert "ΑΝΑ ΛΟΓΑΡΙΑΣΜΟ" in out["stopped"]
    assert settings_store.get(conn, "llm_model_openrouter") != "deepseek/deepseek-chat-v3.1:free"


def test_json_mode_rejected_falls_back_to_plain_completion(conn):
    calls = []

    def chat(url, **kw):
        calls.append("response_format" in kw["json"])
        return FakeResponse('{"error":{"message":"response_format is not supported with this model"}}', 400) \
            if "response_format" in kw["json"] else llm_reply({"ok": True})
    s = FakeSession().route("chat/completions", chat)
    client = LLMClient("groq", "k", "m", "https://api.groq.com/openai/v1/chat/completions", s)
    assert json.loads(client.complete_json("s", "u")) == {"ok": True} and calls == [True, False]


# ---------------------------------------------------------------- web

def make_web():
    from taxmatch.web import create_app
    app = create_app("tok", testing=True)
    c = app.test_client()
    c.get("/?t=tok")
    return app, c


def test_new_client_is_looked_up_automatically_even_while_a_job_runs(conn, monkeypatch):
    app, c = make_web()
    got = []
    monkeypatch.setattr(service, "lookup_and_store",
                        lambda cn, afm, *a, **k: got.append(afm) or {"status": "ok", "errors": [], "sources": ["ΑΑΔΕ"], "issues": []})
    gate = threading.Event()
    assert app.extensions["jobs"].start("run", lambda progress: gate.wait(5))          # έλεγχος σε εξέλιξη
    r = c.post("/clients/new", data={"afm": AFM, "name": "Α", "taxis_user": "u", "taxis_pass": "p"})
    assert r.status_code == 302
    assert wait(lambda: got == [AFM])                                                 # ανακτήθηκε ΧΩΡΙΣ να περιμένει τον έλεγχο
    assert wait(lambda: not app.extensions["jobs"].lookups.snapshot()["running"])
    gate.set()
    snap = c.get("/api/job").get_json()
    assert snap["lookup"]["ok"] == 1 and snap["lookup"]["done"] == 1 and "queued" in snap["lookup"]


def test_saving_credentials_triggers_lookup_and_test_button_reports_wrong_credentials(conn, monkeypatch):
    app, c = make_web()
    service.add(conn, AFM, "Α")
    got = []
    monkeypatch.setattr(service, "lookup_and_store",
                        lambda cn, afm, *a, **k: got.append(afm) or {"status": "ok", "errors": [], "sources": [], "issues": []})
    c.post(f"/clients/{AFM}/credentials", data={"action": "save", "taxis_user": "u", "taxis_pass": "p"})
    assert wait(lambda: got == [AFM])
    monkeypatch.setattr(la, "aade_login", lambda u, p: {"ok": False, "reason": "InvalidCredentials"})
    r = c.post(f"/clients/{AFM}/credentials", data={"action": "test"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Αποτυχία σύνδεσης TAXISnet" in html and "Λάθος κωδικοί TAXISnet" in html        # flash + banner προφίλ
    assert "Λάθος ή κλειδωμένοι κωδικοί TAXISnet σε 1 πελάτη" in html                        # ειδοποίηση πάνω-πάνω


def test_pages_have_in_app_dialogs_and_no_native_confirm(conn):
    _, c = make_web()
    html = c.get("/").get_data(as_text=True)
    assert 'id="confirmDialog"' in html and 'id="toasts"' in html and 'id="newsDialog"' in html
    js = c.get("/static/js/app.js").get_data(as_text=True)
    assert "window.confirm" not in js and "alert(" not in js and "confirmDialog(" in js
    assert "newsDialog.showModal()" in js and "/api/open-external" in js       # popup πρώτα, το link ανοίγει μόνο από εκεί


def test_news_click_shows_in_app_popup_before_the_weblink(conn, monkeypatch):
    from taxmatch import db as dbmod
    _, c = make_web()
    now = dbmod.utcnow()
    service.add(conn, AFM, "Α")
    conn.execute("INSERT INTO articles(source,title,url,url_hash,published_at,fetched_at,extraction_status,extracted_json) "
                 "VALUES ('taxheaven_new','Παράταση ΦΠΑ','https://x.gr/1','h1',?,?,'done',?)",
                 (now, now, json.dumps({"summary": "Η προθεσμία μετατίθεται", "action_required": "Υποβολή ΦΠΑ",
                                        "topic": "ΦΠΑ", "scope": {"type": "all"}, "relevant": True})))
    conn.execute("INSERT INTO matches(article_id,afm,matched_reason,confidence,created_at) VALUES (1,?,?,1.0,?)",
                 (AFM, "Αφορά όλες τις επιχειρήσεις", now))
    html = c.get("/news?only=all").get_data(as_text=True)
    assert 'data-summary="Η προθεσμία μετατίθεται"' in html and 'data-action="Υποβολή ΦΠΑ"' in html
    assert 'href="https://x.gr/1"' in html and 'target="_blank"' in html   # ο ίδιος σύνδεσμος υπάρχει (για δεξί κλικ/hover)


def test_client_pages_show_activity_state_and_filters(conn):
    _, c = make_web()
    service.add(conn, AFM, "ΚΛΕΙΣΤΗ ΕΠΙΧΕΙΡΗΣΗ")
    conn.execute("UPDATE businesses SET activity_state='ceased', cease_date='30/01/2026', cease_reason='ΠΑΥΣΗ ΕΡΓΑΣΙΩΝ' WHERE afm=?", (AFM,))
    lst = c.get("/clients").get_data(as_text=True)
    assert 'data-activity="ceased"' in lst and "Διακοπή 30/01/2026" in lst and 'value="badcreds"' in lst
    det = c.get(f"/clients/{AFM}").get_data(as_text=True)
    assert "Διακοπή εργασιών από 30/01/2026" in det and "ΠΑΥΣΗ ΕΡΓΑΣΙΩΝ" in det


def test_llm_test_button_switches_model_and_reports_it(conn, monkeypatch):
    _, c = make_web()
    settings_store.set_value(conn, "llm_provider", "groq")  # το default άλλαξε σε openrouter
    settings_store.set_value(conn, "groq_api_key", "k")
    settings_store.set_value(conn, "llm_last_error", "παλιό σφάλμα")
    ok = llm_reply({"ok": True})
    s = (FakeSession().route("/models", FakeResponse(json_data={"data": [{"id": "openai/gpt-oss-20b"}]}))
         .route("chat/completions", lambda url, **kw: FakeResponse(MODEL_404, 404) if kw["json"]["model"] == "llama-3.3-70b-versatile" else ok))
    monkeypatch.setattr(llm_extract, "make_session", lambda retries=1: s)
    html = c.post("/settings/test-llm", follow_redirects=True).get_data(as_text=True)
    assert "αυτόματη αλλαγή σε «openai/gpt-oss-20b»" in html and "λειτουργεί" in html
    assert settings_store.get(conn, "llm_last_error") == ""                     # η ειδοποίηση σβήνει


def test_token_redirect_keeps_other_query_params():
    from taxmatch.web import create_app
    r = create_app("tok").test_client().get("/clients?theme=light&t=tok")
    assert r.status_code == 302 and r.headers["Location"] == "/clients?theme=light"
    assert create_app("tok").test_client().get("/?t=tok").headers["Location"] == "/"


# ---------------------------------------------------------------- πλήρες ημερολόγιο (σελίδα taxheaven, όχι feed)

def test_calendar_view_syncs_the_viewed_month_on_demand(conn, monkeypatch):
    from taxmatch.ingestion import taxheaven_calendar as thc
    seen = []
    monkeypatch.setattr(thc, "ensure_month_synced", lambda c, s, y, m: seen.append((y, m)) or False)
    _, c = make_web()
    assert c.get("/calendar?month=2026-07").status_code == 200
    assert seen == [(2026, 7)]


def test_calendar_view_survives_sync_failure(conn, monkeypatch):
    from taxmatch.ingestion import taxheaven_calendar as thc

    def boom(*a):
        raise RuntimeError("δίκτυο κάτω")
    monkeypatch.setattr(thc, "ensure_month_synced", boom)
    _, c = make_web()
    assert c.get("/calendar?month=2026-07").status_code == 200          # δεν ρίχνει τη σελίδα


# ---------------------------------------------------------------- LLM: δωρεάν πρώτα, paid μόνο ως πρόταση

def test_pick_model_prefers_free_openrouter_models_and_never_silently_picks_paid():
    avail = ["meta-llama/llama-3.3-70b-instruct", "meta-llama/llama-3.3-70b-instruct:free", "openai/gpt-oss-120b"]
    assert llm_extract.pick_model("openrouter", avail) == "meta-llama/llama-3.3-70b-instruct:free"
    # χωρίς κανένα δωρεάν διαθέσιμο: δεν επιστρέφει σιωπηλά το πληρωμένο
    assert llm_extract.pick_model("openrouter", ["meta-llama/llama-3.3-70b-instruct", "openai/gpt-oss-120b"]) == ""
    # μόνο ρητά (free_only=False) επιστρέφει πληρωμένο, για το μήνυμα πρότασης — όχι για αυτόματη χρήση
    assert llm_extract.pick_model("openrouter", ["openai/gpt-oss-120b"], free_only=False) == "openai/gpt-oss-120b"


def test_is_free_model():
    assert llm_extract.is_free_model("groq", "llama-3.3-70b-versatile")            # όλο το Groq είναι δωρεάν βαθμίδα
    assert llm_extract.is_free_model("openrouter", "meta-llama/llama-3.3-70b-instruct:free")
    assert not llm_extract.is_free_model("openrouter", "meta-llama/llama-3.3-70b-instruct")


def test_openrouter_recover_switches_to_free_model_automatically(conn):
    add_article(conn)
    ok = llm_reply({"relevant": True, "summary": "Σ", "scope": {"type": "all"}})
    s = (FakeSession().route("/models", FakeResponse(json_data={"data": [
            {"id": "meta-llama/llama-3.3-70b-instruct"}, {"id": "meta-llama/llama-3.3-70b-instruct:free"}]}))
         .route("chat/completions", lambda url, **kw: FakeResponse(MODEL_404, 404)
                if kw["json"]["model"] == "meta-llama/llama-3.3-70b-instruct" else ok))
    client = LLMClient("openrouter", "k", "meta-llama/llama-3.3-70b-instruct",
                       "https://openrouter.ai/api/v1/chat/completions", s)
    out = llm_extract.extract_pending(conn, client, 10, 30, sleep=lambda _s: None)
    assert out["model"] == "meta-llama/llama-3.3-70b-instruct:free" and out["done"] == 1 and out["stopped"] == ""


def test_openrouter_recover_suggests_paid_when_no_free_model_works(conn):
    add_article(conn)
    s = (FakeSession().route("/models", FakeResponse(json_data={"data": [
            {"id": "meta-llama/llama-3.3-70b-instruct"}, {"id": "openai/gpt-oss-120b"}]}))     # κανένα δεν είναι ':free'
         .route("chat/completions", FakeResponse(MODEL_404, 404)))
    client = LLMClient("openrouter", "k", "meta-llama/llama-3.3-70b-instruct",
                       "https://openrouter.ai/api/v1/chat/completions", s)
    out = llm_extract.extract_pending(conn, client, 10, 30, sleep=lambda _s: None)
    assert "Κανένα δωρεάν μοντέλο" in out["stopped"] and "πληρωμένο μοντέλο" in out["stopped"]
    assert conn.execute("SELECT extraction_status, extraction_tries FROM articles").fetchone()[:] == ("pending", 0)


# ---------------------------------------------------------------- κύριος κωδικός (ίδιο σχήμα με το timologio downloader)

def test_settings_page_shows_master_password_status(conn):
    _, c = make_web()
    html = c.get("/settings").get_data(as_text=True)
    assert 'id="master-password"' in html and "ανενεργός" in html and "Ενεργοποίηση προστασίας" in html


def test_enable_change_and_remove_master_password_roundtrip(conn, monkeypatch):
    from taxmatch import crypto
    monkeypatch.delenv("TAXMATCH_ENC_KEY", raising=False)      # ο πραγματικός μηχανισμός αρχείου, όχι το test override
    _, c = make_web()
    path = crypto.keyfile_path()
    settings_store.set_value(conn, "groq_api_key", "gsk_secret")     # κάτι κρυπτογραφημένο να προστατεύεται
    r = c.post("/settings/master-password", data={"action": "set", "new_password": "hunter2-orange-bus", "confirm_password": "hunter2-orange-bus"},
              follow_redirects=True)
    assert "ενεργοποιήθηκε" in r.get_data(as_text=True) and crypto.is_protected(path)
    assert "ενεργός" in c.get("/settings").get_data(as_text=True)

    r = c.post("/settings/master-password", data={"action": "change", "current_password": "λάθος",
                                                   "new_password": "allo-allo-1234", "confirm_password": "allo-allo-1234"}, follow_redirects=True)
    assert "Λάθος τρέχων κωδικός" in r.get_data(as_text=True)

    r = c.post("/settings/master-password", data={"action": "change", "current_password": "hunter2-orange-bus",
                                                   "new_password": "allo-allo-1234", "confirm_password": "allo-allo-1234"}, follow_redirects=True)
    assert "άλλαξε" in r.get_data(as_text=True)

    r = c.post("/settings/master-password", data={"action": "remove", "current_password": "allo-allo-1234"}, follow_redirects=True)
    assert "αφαιρέθηκε" in r.get_data(as_text=True) and not crypto.is_protected(path)
    assert settings_store.get(conn, "groq_api_key") == "gsk_secret"          # τα δεδομένα δεν χάθηκαν σε όλο αυτό


def test_master_password_rejects_short_or_mismatched(conn):
    _, c = make_web()
    r = c.post("/settings/master-password", data={"action": "set", "new_password": "abc", "confirm_password": "abc"}, follow_redirects=True)
    assert "τουλάχιστον" in r.get_data(as_text=True)
    r = c.post("/settings/master-password", data={"action": "set", "new_password": "abcdefghijk", "confirm_password": "allodiaforetiko"}, follow_redirects=True)
    assert "δεν ταιριάζουν" in r.get_data(as_text=True)


def test_protected_folder_redirects_to_unlock_and_accepts_password(conn, monkeypatch):
    from taxmatch import crypto
    monkeypatch.delenv("TAXMATCH_ENC_KEY", raising=False)
    path = crypto.keyfile_path()
    settings_store.set_value(conn, "aade_user", "office-user")
    crypto.set_password(path, "hunter2-orange-bus")
    crypto.forget()
    _, c = make_web()
    r = c.post("/settings/test-aade")          # διαβάζει aade_user/pass -> KeyfileLocked -> /unlock
    assert r.status_code == 302 and "/unlock" in r.headers["Location"]
    unlock_page = c.get(r.headers["Location"])
    assert unlock_page.status_code == 200 and "Ο φάκελος δεδομένων είναι προστατευμένος" in unlock_page.get_data(as_text=True)
    bad = c.post("/unlock", data={"password": "λάθος", "next": "/settings"})
    assert "Λάθος κωδικός" in bad.get_data(as_text=True)
    ok = c.post("/unlock", data={"password": "hunter2-orange-bus", "next": "/settings"})
    assert ok.status_code == 302 and ok.headers["Location"] == "/settings"
    assert settings_store.get(conn, "aade_user") == "office-user"          # ξεκλειδώθηκε πραγματικά


def test_unlock_ignores_external_next_url(conn, monkeypatch):
    from taxmatch import crypto
    monkeypatch.delenv("TAXMATCH_ENC_KEY", raising=False)
    crypto.set_password(crypto.keyfile_path(), "hunter2-orange-bus")
    crypto.forget()
    _, c = make_web()
    r = c.post("/unlock", data={"password": "hunter2-orange-bus", "next": "https://evil.example.com/"})
    assert r.status_code == 302 and r.headers["Location"] == "/"


def test_unlock_page_redirects_away_when_not_protected(conn):
    _, c = make_web()
    assert c.get("/unlock").status_code == 302
