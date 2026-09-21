from taxmatch.identifiers import (format_kad, is_valid_afm, kad_matches, normalize_afm,
                                  normalize_kad_prefix)


def test_afm_checksum():
    assert is_valid_afm("094259216")      # ΔΕΗ
    assert not is_valid_afm("094259215")
    assert not is_valid_afm("000000000")
    assert not is_valid_afm("12345")


def test_normalize_afm_variants():
    assert normalize_afm("EL094259216") == "094259216"
    assert normalize_afm(" 094 259 216 ") == "094259216"
    assert normalize_afm(94259216) == "094259216"          # Excel έχασε το αρχικό 0
    assert normalize_afm(94259216.0) == "094259216"
    assert normalize_afm("abc") == ""
    assert normalize_afm(None) == ""


def test_kad_matching_ignores_formatting():
    assert normalize_kad_prefix("47.11") == "4711"
    assert normalize_kad_prefix("7") is None
    assert kad_matches("47.11.10.01", "4711")
    assert kad_matches("47111001", "4711")
    assert not kad_matches("56.10.11.01", "4711")
    assert format_kad("47111001") == "47.11.10.01"
    assert format_kad("4711") == "47.11"
