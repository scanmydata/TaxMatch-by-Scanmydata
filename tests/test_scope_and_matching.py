from taxmatch import scope
from taxmatch.matching.match import legal_form_tokens, match_business


def biz(**kw):
    base = {"kads": [], "books_category": "", "vat_subject": None, "legal_form": ""}
    base.update(kw)
    return base


def ex(**scope_kw):
    return scope.normalize({"relevant": True, "scope": scope_kw})


def test_normalize_survives_garbage():
    for junk in (None, "x", [], {"scope": "x"}, {"scope": {"kad_prefixes": "47.11", "books_categories": ["γ", "z"]}}):
        out = scope.normalize(junk)
        assert out["scope"]["type"] in ("all", "targeted")
    n = scope.normalize({"scope": {"kad_prefixes": ["47.11", "1", "abc"], "books_categories": ["γ", "Z", "Β-απλογραφικά"],
                                   "vat_subject": "yes"}, "deadline": "2026-13-45", "topic": "φπα"})
    assert n["scope"]["kad_prefixes"] == ["4711"]
    assert n["scope"]["books_categories"] == ["Γ", "Β"]
    assert n["scope"]["vat_subject"] is None
    assert n["deadline"] is None and n["topic"] == "ΦΠΑ"
    assert scope.normalize({"topic": "κάτι άγνωστο"})["topic"] == "ΑΛΛΟ"


def test_declared_all_drops_criteria():
    n = scope.normalize({"scope": {"type": "all", "kad_prefixes": ["47"]}})
    assert n["scope"]["type"] == "all" and n["scope"]["kad_prefixes"] == []


def test_no_criteria_means_all():
    assert scope.normalize({"scope": {"type": "targeted"}})["scope"]["type"] == "all"


def test_all_matches_everyone_even_without_data():
    m = match_business(ex(type="all"), biz())
    assert m and m.confidence == 1.0


def test_irrelevant_never_matches():
    assert match_business(scope.normalize({"relevant": False, "scope": {"type": "all"}}), biz()) is None


def test_kad_prefix_match_and_miss():
    e = ex(type="targeted", kad_prefixes=["47.11", "56"])
    assert match_business(e, biz(kads=["47.11.10.01"])).confidence == 1.0
    assert match_business(e, biz(kads=["56.10.11.01"])) is not None      # 2ψήφιο πρόθεμα 56
    assert match_business(e, biz(kads=["62.01.11.00"])) is None


def test_secondary_kad_counts():
    e = ex(type="targeted", kad_prefixes=["56"])
    assert match_business(e, biz(kads=["47.11.10.01", "56.10.11.01"]))


def test_and_semantics_between_criteria():
    e = ex(type="targeted", kad_prefixes=["47"], books_categories=["Γ"])
    assert match_business(e, biz(kads=["47.11"], books_category="Γ")).confidence == 1.0
    assert match_business(e, biz(kads=["47.11"], books_category="Β")) is None


def test_unknown_criterion_gives_low_confidence_not_silence():
    e = ex(type="targeted", kad_prefixes=["47"], books_categories=["Γ"])
    m = match_business(e, biz(kads=["47.11"], books_category=""))
    assert m and m.confidence == 0.5 and "κατηγορία βιβλίων" in m.reason


def test_only_unknown_criteria_do_not_match():
    e = ex(type="targeted", kad_prefixes=["47"])
    assert match_business(e, biz(kads=[])) is None


def test_vat_subject_criterion():
    e = ex(type="targeted", vat_subject=True)
    assert match_business(e, biz(vat_subject=1)).confidence == 1.0
    assert match_business(e, biz(vat_subject=0)) is None
    assert match_business(e, biz(vat_subject=None)) is None


def test_legal_form_long_and_short_names():
    assert "ΙΚΕ" in legal_form_tokens("Ιδιωτική Κεφαλαιουχική Εταιρεία")
    assert "ΙΚΕ" in legal_form_tokens("Ι.Κ.Ε.")
    assert "ΑΕ" in legal_form_tokens("ΑΝΩΝΥΜΗ ΕΤΑΙΡΕΙΑ")
    e = ex(type="targeted", legal_forms=["ΙΚΕ"])
    assert match_business(e, biz(legal_form="Ιδιωτική Κεφαλαιουχική Εταιρεία"))
    assert match_business(e, biz(legal_form="ΟΜΟΡΡΥΘΜΗ ΕΤΑΙΡΕΙΑ")) is None
