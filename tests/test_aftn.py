# AFTN ayrıştırıcı birim testleri: geçerli, başlıklı, bozuk, bilinmeyen tip, bilinmeyen uçuş.
# Tüm mesajlar uydurmadır; sadece çağrı kodu biçimi ve ICAO meydan kodları gerçektir.
from datetime import date

import pytest

from app.aftn import AftnParseError, UnknownFlightError, parse, resolve_flight

DLA = "(DLA-THY1591-LTFM0830-EDDF-DOF/261123)"


def test_parses_plain_dla():
    msg = parse(DLA)
    assert msg.msg_type == "DLA"
    assert msg.callsign == "THY1591"
    assert (msg.adep, msg.ades) == ("LTFM", "EDDF")
    assert msg.time_hhmm == "0830"
    assert msg.minutes == 8 * 60 + 30
    assert msg.dof == date(2026, 11, 23)
    assert msg.triggers_replan


def test_skips_priority_and_address_header():
    raw = "FF LTFMZPZX\n231200 EDDFZQZX\n" + DLA + "\nNNNN"
    assert parse(raw) == parse(DLA)


def test_same_body_with_different_header_has_same_hash():
    # Aynı mesaj farklı zaman damgasıyla yeniden iletilirse idempotency bozulmamalı.
    a = parse("FF LTFMZPZX\n231200 EDDFZQZX\n" + DLA)
    b = parse("FF LTFMZPZX\n231205 EDDFZQZX\n" + DLA)
    assert a.body_hash() == b.body_hash()


def test_body_wrapped_over_lines_and_lowercase():
    raw = "(dla-THY1591-LTFM0830-\n  EDDF-DOF/261123)"
    assert parse(raw).body == DLA


def test_cnl_triggers_replan():
    msg = parse("(CNL-THY1593-LTFM1100-EGLL-DOF/261123)")
    assert msg.msg_type == "CNL" and msg.triggers_replan


def test_dep_does_not_trigger_replan():
    msg = parse("(DEP-THY1591-LTFM0842-EDDF-DOF/261123)")
    assert msg.msg_type == "DEP" and not msg.triggers_replan
    assert msg.time_hhmm == "0842"


def test_arr_time_is_attached_to_destination():
    msg = parse("(ARR-THY1591-LTFM-EDDF1105-DOF/261123)")
    assert (msg.adep, msg.ades, msg.time_hhmm) == ("LTFM", "EDDF", "1105")
    assert not msg.triggers_replan


def test_field18_with_other_indicators():
    msg = parse("(DLA-THY1591-LTFM0830-EDDF-REG/TCJJA DOF/261123)")
    assert msg.dof == date(2026, 11, 23)


@pytest.mark.parametrize(
    "raw, reason",
    [
        ("DLA-THY1591-LTFM0830-EDDF-DOF/261123", "body missing"),
        ("(DLA-THY1591-LTFM0830-EDDF-DOF/261123", "closing"),
        ("(DLA-THY1591-LTFM0830-EDDF)", "expected 5 fields"),
        ("(DLA-THY1591-LTFM0830-EDDF-RMK/LATE)", "DOF/"),
        ("(DLA-THY1591-LTFM2460-EDDF-DOF/261123)", "invalid time"),
        ("(DLA-THY1591-LTFM-EDDF-DOF/261123)", "invalid departure aerodrome"),
        ("(DLA-1591-LTFM0830-EDDF-DOF/261123)", "callsign"),
        ("(DLA-THY1591-LTFM0830-EDD-DOF/261123)", "destination aerodrome"),
        ("(DLA-THY1591-LTFM0830-EDDF-DOF/261341)", "invalid DOF"),
        ("", "body missing"),
    ],
)
def test_malformed_messages_raise_meaningful_error(raw, reason):
    with pytest.raises(AftnParseError, match=reason):
        parse(raw)


@pytest.mark.parametrize("msg_type", ["FPL", "CHG", "XYZ"])
def test_unknown_type_is_rejected(msg_type):
    with pytest.raises(AftnParseError, match="unsupported message type"):
        parse(f"({msg_type}-THY1591-LTFM0830-EDDF-DOF/261123)")


def test_resolve_known_flight():
    flights = {("THY1591", date(2026, 11, 23), "LTFM"): 7}
    assert resolve_flight(parse(DLA), flights) == 7


def test_resolve_unknown_flight():
    flights = {("THY1591", date(2026, 11, 24), "LTFM"): 7}  # farklı gün
    with pytest.raises(UnknownFlightError, match="THY1591"):
        resolve_flight(parse(DLA), flights)
