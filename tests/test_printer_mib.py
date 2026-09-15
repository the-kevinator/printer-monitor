"""Tests for Printer MIB interpretation, using realistic device fixtures."""

import pytest

from app import printer_mib as mib
from app.snmp import SnmpClient
from tests.fake_printers import COLOUR_LASER, MONO_LASER, NETWORK_SWITCH
from tests.mock_agent import MockAgent


def _client(agent):
    return SnmpClient("127.0.0.1", port=agent.port, timeout=0.8, retries=1)


# -- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    "level,capacity,unit,expected",
    [
        (82, 100, 19, 82),
        (1500, 3000, 13, 50),
        (0, 100, 19, 0),
        (100, 100, 19, 100),
        # Negative sentinels are never a percentage.
        (-1, 100, 19, None),
        (-2, 100, 19, None),
        (-3, 100, 19, None),
        # Unknown capacity but a percent unit -> level is already a percent.
        (37, -2, 19, 37),
        (37, -1, 19, 37),
        # Unknown capacity, non-percent unit, value in 0..100 -> best effort.
        (37, -2, 13, 37),
        # Unknown capacity and an out-of-range level -> give up.
        (5000, -2, 13, None),
        # Rounding and clamping.
        (1, 3, 8, 33),
        (2, 3, 8, 67),
        (150, 100, 19, 100),
        (None, 100, 19, None),
    ],
)
def test_compute_percent(level, capacity, unit, expected):
    assert mib.compute_percent(level, capacity, unit) == expected


@pytest.mark.parametrize(
    "level,percent,expected",
    [
        (82, 82, "ok"),
        (20, 20, "low"),
        (5, 5, "low"),
        (0, 0, "empty"),
        (-3, None, "some_remaining"),
        (-2, None, "unknown"),
        (-1, None, "unknown"),
        (None, None, "unknown"),
    ],
)
def test_classify_level_consumable(level, percent, expected):
    assert mib.classify_level(level, percent, low_threshold=20) == expected


def test_classify_level_receptacle_inverts_the_scale():
    # A waste receptacle is bad when full.
    assert mib.classify_level(10, 10, 20, is_receptacle=True) == "ok"
    assert mib.classify_level(85, 85, 20, is_receptacle=True) == "low"
    assert mib.classify_level(100, 100, 20, is_receptacle=True) == "empty"


def test_decode_detected_errors_bit_order():
    assert mib.decode_detected_errors(b"\x00\x00") == []
    # MSB of byte 0 is bit 0 == lowPaper
    assert mib.decode_detected_errors(b"\x80\x00") == ["lowPaper"]
    assert mib.decode_detected_errors(b"\x40\x00") == ["noPaper"]
    assert mib.decode_detected_errors(b"\x20\x00") == ["lowToner"]
    assert mib.decode_detected_errors(b"\x02\x00") == ["offline"]
    assert mib.decode_detected_errors(b"\x01\x00") == ["serviceRequested"]
    # byte 1 continues at bit 8 == inputTrayMissing
    assert mib.decode_detected_errors(b"\x00\x80") == ["inputTrayMissing"]
    assert mib.decode_detected_errors(b"\x84\x00") == ["lowPaper", "jammed"]
    # A single short octet must not over-read.
    assert mib.decode_detected_errors(b"\x20") == ["lowToner"]
    assert mib.decode_detected_errors(None) == []


def test_decode_detected_errors_accepts_integer_form():
    assert mib.decode_detected_errors(0x2000) == ["lowToner"]


def test_colorant_inference_prefers_compound_names():
    assert mib.colorant_from_description("Light Cyan Ink") == "lightCyan"
    assert mib.colorant_from_description("Photo Black Cartridge") == "black"
    assert mib.colorant_from_description("Black Cartridge HP 508A") == "black"
    assert mib.colorant_from_description("Imaging Drum") is None


def test_canonical_colorant():
    assert mib.canonical_colorant("Black") == "black"
    assert mib.canonical_colorant("CYAN") == "cyan"
    assert mib.canonical_colorant("none") is None
    assert mib.canonical_colorant(None) is None


def test_guess_vendor():
    assert mib.guess_vendor("HP ETHERNET MULTI-ENVIRONMENT") == "HP"
    assert mib.guess_vendor("Brother HL-L6200DW series") == "Brother"
    assert mib.guess_vendor("KYOCERA ECOSYS M3550idn") == "Kyocera"
    assert mib.guess_vendor("Cisco IOS Software") is None
    assert mib.guess_vendor(None) is None


# -- end-to-end reads -------------------------------------------------------


def test_colour_laser_full_read():
    with MockAgent(COLOUR_LASER) as agent, _client(agent) as client:
        stats = mib.read_printer(client, low_threshold=20)

    assert stats.reachable
    assert stats.error is None
    assert stats.sys_name == "PRN-FLOOR2-COLOR"
    assert stats.sys_location == "Floor 2 East Copy Room"
    assert stats.model == "HP Color LaserJet Enterprise M553"
    assert stats.vendor == "HP"
    assert stats.serial_number == "CNBRK12345"
    assert stats.mac_address == "00:1e:0b:12:34:56"
    assert stats.uptime_seconds == 1234567
    assert stats.page_count == 184321
    assert stats.power_on_page_count == 9021
    assert stats.printer_status == "idle"
    assert stats.device_status == "running"
    assert stats.errors == []
    assert stats.console_text == "Ready"

    toners = {s.colorant: s.percent for s in stats.toner_supplies}
    assert toners == {"black": 82, "cyan": 14, "magenta": 61, "yellow": 0}
    assert stats.lowest_toner_percent == 0

    by_desc = {s.description: s for s in stats.supplies}
    waste = by_desc["Toner Collection Unit"]
    assert waste.is_receptacle is True
    assert waste.is_toner is False
    assert waste.percent == 35
    assert waste.state == "ok"

    drum = by_desc["Imaging Drum"]
    assert drum.type == "opc"
    assert drum.is_toner is False
    assert drum.percent == 77

    # An empty yellow cartridge is critical.
    assert stats.health(low_threshold=20) == "critical"

    trays = {t.name: t for t in stats.trays}
    assert trays["Tray 2"].percent == 80
    assert trays["Tray 2"].state == "ok"
    assert trays["Tray 1"].percent == 10
    assert trays["Tray 1"].state == "low"
    assert trays["Tray 1"].media == "Legal"


def test_mono_laser_read_with_sentinels_and_errors():
    with MockAgent(MONO_LASER) as agent, _client(agent) as client:
        stats = mib.read_printer(client, low_threshold=20)

    assert stats.reachable
    assert stats.vendor == "Brother"
    assert stats.model == "Brother HL-L6200DW"
    assert stats.page_count == 52310
    assert stats.errors == ["lowPaper", "jammed"]
    assert stats.console_text == "Paper Jam"

    supplies = {s.description: s for s in stats.supplies}
    toner = supplies["TN-3430 Black Toner"]
    assert toner.is_toner is True
    assert toner.percent == 50  # 1500 / 3000 tenths-of-grams
    assert toner.unit == "tenthsOfGrams"
    assert toner.state == "ok"
    assert toner.colorant == "black"

    drum = supplies["DR-3400 Drum Unit"]
    assert drum.level_raw == -3
    assert drum.percent is None
    assert drum.state == "some_remaining"

    # Jammed is a critical detected error.
    assert stats.health() == "critical"


def test_health_rolls_up_low_toner_as_warning():
    mib_copy = dict(COLOUR_LASER)
    # Raise yellow off zero so nothing is empty, leaving cyan at 14%.
    mib_copy[mib.parse_oid_compat("1.3.6.1.2.1.43.11.1.1.9.1.4")] = 55
    with MockAgent(mib_copy) as agent, _client(agent) as client:
        stats = mib.read_printer(client, low_threshold=20)
    assert stats.lowest_toner_percent == 14
    assert stats.health(low_threshold=20) == "warning"
    assert stats.health(low_threshold=10) == "ok"


def test_looks_like_printer_accepts_a_printer():
    with MockAgent(COLOUR_LASER) as agent, _client(agent) as client:
        is_printer, identity = mib.looks_like_printer(client)
    assert is_printer is True
    assert identity[mib.SYS_NAME] == "PRN-FLOOR2-COLOR"


def test_looks_like_printer_rejects_a_switch():
    with MockAgent(NETWORK_SWITCH) as agent, _client(agent) as client:
        is_printer, identity = mib.looks_like_printer(client)
    assert is_printer is False
    # Identity was still gathered, which is useful for diagnostics.
    assert identity[mib.SYS_DESCR].startswith("Cisco IOS")


def test_unreachable_host_returns_unreachable_not_an_exception():
    client = SnmpClient("127.0.0.1", port=1, timeout=0.2, retries=0)
    with client:
        stats = mib.read_printer(client)
    assert stats.reachable is False
    assert stats.error
    assert stats.health() == "offline"


def test_as_dict_is_json_ready():
    import json

    with MockAgent(COLOUR_LASER) as agent, _client(agent) as client:
        stats = mib.read_printer(client)
    payload = stats.as_dict(low_threshold=20)
    assert payload["health"] == "critical"
    assert payload["lowest_toner_percent"] == 0
    assert isinstance(payload["supplies"], list)
    json.dumps(payload)  # must not raise


def test_binary_octet_strings_survive_text_normalisation():
    """
    Regression: OCTET STRING fields holding packed binary must keep their NUL
    bytes. Decoding them as display text used to strip leading NULs, which
    silently shifted every bit in hrPrinterDetectedErrorState and truncated
    MAC addresses.
    """
    from app.snmp import SnmpString, raw_bytes

    value = SnmpString(b"\x00\x80")
    assert raw_bytes(value) == b"\x00\x80"
    assert mib.decode_detected_errors(value) == ["inputTrayMissing"]

    mac = SnmpString(b"\x00\x1e\x0b\x12\x34\x56")
    assert mib._format_mac(mac) == "00:1e:0b:12:34:56"

    # A MAC of all zeroes is "not reported", not a real address.
    assert mib._format_mac(SnmpString(b"\x00" * 6)) is None

    # Display text still reads cleanly.
    assert str(SnmpString(b"HP LaserJet\x00\x00")) == "HP LaserJet"


def test_guess_vendor_respects_field_authority_not_pattern_order():
    """
    Regression: candidates are matched one field at a time, in the order given.
    Concatenating them made the VENDOR_PATTERNS order decide, so any mention of
    an earlier-listed vendor anywhere won -- a Xerox reporting a stale "HP ..."
    printer name came back as HP.
    """
    assert mib.guess_vendor(
        "Xerox VersaLink C405DN; SS 60.35.10",   # sysDescr: authoritative
        "Xerox VersaLink C405",                  # hrDeviceDescr
        "HP Color LaserJet M553",                # stale prtGeneralPrinterName
    ) == "Xerox"

    assert mib.guess_vendor(
        "KYOCERA ECOSYS M3550idn", None, "HP Color LaserJet M553"
    ) == "Kyocera"

    # A later field still answers when the earlier ones say nothing useful.
    assert mib.guess_vendor(None, "", "Brother HL-L6200DW") == "Brother"
    assert mib.guess_vendor("generic printer", "unknown") is None


def test_guess_model_trims_sysdescr_banners():
    """sysDescr is often a comma/semicolon-delimited banner, not a model name."""
    assert mib.guess_model(
        "HP ETHERNET MULTI-ENVIRONMENT,ROM none,JETDIRECT,JD153,EEPROM V.36.25",
        None, None,
    ) == "HP ETHERNET MULTI-ENVIRONMENT"
    assert mib.guess_model("Xerox VersaLink C405DN; SS 60.35.10", None, None) \
        == "Xerox VersaLink C405DN"
    # A clean name from hrDeviceDescr always wins over the banner.
    assert mib.guess_model(
        "HP ETHERNET MULTI-ENVIRONMENT,ROM none", "HP Color LaserJet M553", None,
    ) == "HP Color LaserJet M553"
    assert mib.guess_model("Brother HL-L6200DW series", None, None) \
        == "Brother HL-L6200DW series"
    assert mib.guess_model(None, None, None) is None
