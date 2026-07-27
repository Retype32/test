"""
Tests for the BPS C1 serial report parser.

The sample reports below are representative layouts, not captures from a real
machine. Once a real printout is captured (python -m hardware.capture), add it
here verbatim as a regression test and adjust the profile if it differs.
"""

from decimal import Decimal

import pytest

from hardware.report_parser import ReportParseError, detect_profile, parse_report
from hardware.report_profile import load_profile
from hardware.simulate import DEFAULT_BATCH, LAYOUTS, build_report

PROFILE = load_profile("bps_c1_eur")


BASIC = """
Currency        EUR
Fitness Mode    ATM
------------------------------
Denom       Pcs        Amount
------------------------------
EUR 50       12        600.00
EUR 20       30        600.00
EUR 10        7         70.00
------------------------------
SubTotal     49       1270.00
Total        49       1270.00
"""


def test_parses_denominations_and_totals():
    r = parse_report(BASIC, PROFILE)
    assert r.denominations == {"€50": 12, "€20": 30, "€10": 7}
    assert r.currency == "EUR"
    assert r.fitness_mode == "ATM"
    assert r.total_quantity == 49
    assert r.total_value == Decimal("1270.00")
    assert r.counted_value == Decimal("1270.00")
    assert r.warnings == []
    assert r.is_reliable


def test_column_header_is_not_treated_as_data():
    r = parse_report(BASIC, PROFILE)
    assert r.unmatched_lines == []


def test_longest_prefix_match_does_not_confuse_5_and_500():
    text = """
Currency  EUR
Denom       Pcs        Amount
EUR 500       2       1000.00
EUR 5         3         15.00
Total         5       1015.00
"""
    r = parse_report(text, PROFILE)
    assert r.denominations == {"€500": 2, "€5": 3}
    assert r.warnings == []


def test_european_number_format():
    text = """
Currency  EUR
EUR 100      15       1.500,00
Total        15       1.500,00
"""
    r = parse_report(text, PROFILE)
    assert r.denominations == {"€100": 15}
    assert r.total_value == Decimal("1500.00")
    assert r.warnings == []


def test_quantity_mismatch_is_reported_not_silently_accepted():
    text = """
Currency  EUR
EUR 50       10        500.00
Total        99        500.00
"""
    r = parse_report(text, PROFILE)
    assert r.denominations == {"€50": 10}
    assert any("Total quantity mismatch" in w for w in r.warnings)
    assert not r.is_reliable


def test_value_mismatch_on_a_denomination_line_is_reported():
    text = """
Currency  EUR
EUR 20        5         200.00
Total         5         200.00
"""
    r = parse_report(text, PROFILE)
    assert any("does not match" in w for w in r.warnings)


def test_rejects_are_captured_and_excluded_from_counts():
    text = """
Currency  EUR
EUR 50       12        600.00
REJECT        3
Total        12        600.00
"""
    r = parse_report(text, PROFILE)
    assert r.reject_quantity == 3
    assert r.denominations == {"€50": 12}
    assert r.warnings == []


def test_printer_control_characters_and_blank_lines_are_stripped():
    text = "\x0c\x1bCurrency  EUR\r\n\r\nEUR 50   4   200.00\r\nTotal    4   200.00\r\n\x0c"
    r = parse_report(text, PROFILE)
    assert r.denominations == {"€50": 4}
    assert r.warnings == []


def test_subtotal_and_total_are_kept_apart():
    r = parse_report(BASIC, PROFILE)
    assert r.subtotal_quantity == 49
    assert r.total_quantity == 49
    assert r.subtotal_value == Decimal("1270.00")


def test_empty_input_raises():
    with pytest.raises(ReportParseError):
        parse_report("   \n\n  ", PROFILE)


def test_unrecognised_report_raises_rather_than_returning_zeros():
    text = """
SELF TEST PASSED
Firmware 3.11
Sensor calibration OK
"""
    with pytest.raises(ReportParseError):
        parse_report(text, PROFILE)


def test_mixed_currency_denomination_not_in_profile_is_surfaced():
    text = """
Currency  EUR
EUR 50        4        200.00
USD 20        3         60.00
Total         4        200.00
"""
    r = parse_report(text, PROFILE)
    assert r.denominations == {"€50": 4}
    assert any("USD 20" in line for line in r.unmatched_lines)


def test_zero_count_row_is_not_confused_with_a_larger_denomination():
    """"EUR 50 0" must resolve to EUR 50 with quantity 0, never to EUR 500."""
    text = """
Currency  EUR
EUR 50        0          0.00
EUR 20        4         80.00
Total         4         80.00
"""
    r = parse_report(text, PROFILE)
    assert r.denominations == {"€50": 0, "€20": 4}
    assert r.warnings == []


def test_face_value_aliases_do_not_collide_between_5_and_500():
    """Regression: a bad alias once mapped EUR 500 onto the key "5"."""
    keys_5 = {d for d in PROFILE.denoms if d.nexus == "€5"}.pop().match_keys()
    keys_500 = {d for d in PROFILE.denoms if d.nexus == "€500"}.pop().match_keys()
    assert "5" in keys_5 and "500" in keys_500
    assert not keys_5 & keys_500


def test_ambiguous_bare_value_alias_is_dropped():
    """A bare face value shared by two currencies must not match either."""
    from hardware.report_profile import profile_from_dict

    mixed = profile_from_dict({
        "name": "mixed",
        "labels": {"totals": "Total"},
        "denoms": [
            {"device": "EUR 50", "nexus": "€50", "value": 50},
            {"device": "USD 50", "nexus": "$50", "value": 50},
        ],
    })
    assert mixed.match_denomination(["50", "3", "150.00"]) is None
    assert mixed.match_denomination(["EUR", "50", "3"])[0].nexus == "€50"
    assert mixed.match_denomination(["USD", "50", "3"])[0].nexus == "$50"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_every_simulated_print_layout_parses(layout):
    """The real C1's template is unknown, so cover the plausible variants."""
    expected = sum(DEFAULT_BATCH.values())
    r = parse_report(build_report(DEFAULT_BATCH, layout), PROFILE)
    assert r.counted_quantity == expected
    assert r.warnings == []
    assert r.denominations["€500"] == DEFAULT_BATCH["EUR 500"]
    assert r.denominations["€5"] == DEFAULT_BATCH["EUR 5"]


def test_detect_profile_picks_a_working_profile():
    profile, report = detect_profile(build_report(DEFAULT_BATCH, "standard"))
    assert report.counted_quantity == sum(DEFAULT_BATCH.values())
    assert report.warnings == []
    assert profile.denoms


def test_detect_profile_rejects_text_that_is_not_a_report():
    with pytest.raises(ReportParseError):
        detect_profile("SELF TEST PASSED\nFirmware 3.11\n")


def test_profile_denominations_match_isa_plugin_config():
    """The ISA BPS C1 plugin config is the known-good machine configuration."""
    isa_codes = {
        "€500": "10701", "€200": "10612", "€100": "10501", "€50": "10401",
        "€20": "10301", "€10": "10161", "€5": "10081",
    }
    assert {d.nexus: d.isacode for d in PROFILE.denoms} == isa_codes
