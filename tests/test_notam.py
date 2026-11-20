# NOTAM ayrıştırıcı testleri: geçerli meydan kapanışı, AFTN başlığıyla, desteklenmeyen kod ve bozuk alanlar.
from datetime import datetime

import pytest

from app.notam import NotamParseError, is_notam, parse

CLOSURE = """(A1234/26 NOTAMN
Q) LTBB/QFALC/IV/NBO/A/000/999/4116N02844E005
A) LTFM B) 2611230800 C) 2611231000
E) AD CLSD DUE TO HEAVY SNOW)"""


def test_parses_aerodrome_closure():
    notam = parse(CLOSURE)
    assert notam.notam_id == "A1234/26"
    assert notam.aerodrome == "LTFM"
    assert notam.start == datetime(2026, 11, 23, 8, 0)
    assert notam.end == datetime(2026, 11, 23, 10, 0)
    assert notam.text == "AD CLSD DUE TO HEAVY SNOW"
    assert notam.summary() == "NOTAM A1234/26 LTFM AD CLSD 0800-1000"


def test_aftn_header_does_not_change_hash():
    assert parse("GG LTFMYNYX\n230700 LTAAYNYX\n" + CLOSURE).body_hash() == parse(CLOSURE).body_hash()


def test_is_notam_distinguishes_from_ats_messages():
    assert is_notam(CLOSURE)
    assert not is_notam("(DLA-THY1591-LTFM0830-EDDF-DOF/261123)")


@pytest.mark.parametrize(
    "raw, reason",
    [
        (CLOSURE.replace("QFALC", "QMRLC"), "unsupported Q-code QMRLC"),  # pist kapalı: bu demoda ele alınmıyor
        (CLOSURE.replace("NOTAMN", "NOTAMC"), "unsupported NOTAM type"),
        (CLOSURE.replace("B) 2611230800 ", ""), r"item B\) missing"),
        (CLOSURE.replace("2611231000", "2611230700"), "must be later"),
        (CLOSURE.replace("2611230800", "2613990800"), "invalid date-time"),
        ("NOTAM OLMAYAN BIR METIN", "id line not found"),
    ],
)
def test_rejects_with_meaningful_error(raw, reason):
    with pytest.raises(NotamParseError, match=reason):
        parse(raw)
