"""Realistic MIB fixtures for a colour laser, a mono laser, and an oddball."""

from app.snmp import parse_oid

P = parse_oid


def _col(column: str, rows: dict):
    return {P(f"{column}.{index}"): value for index, value in rows.items()}


# A 4-colour HP-style device: percentages derived from level/capacity, a waste
# receptacle, a drum, and two paper trays.
COLOUR_LASER = {
    P("1.3.6.1.2.1.1.1.0"): "HP ETHERNET MULTI-ENVIRONMENT,ROM none,JETDIRECT,JD153,EEPROM V.36.25",
    P("1.3.6.1.2.1.1.2.0"): P("1.3.6.1.4.1.11.2.3.9.1"),
    P("1.3.6.1.2.1.1.3.0"): 123456700,  # ticks -> 1234567 s
    P("1.3.6.1.2.1.1.4.0"): "IT Helpdesk",
    P("1.3.6.1.2.1.1.5.0"): "PRN-FLOOR2-COLOR",
    P("1.3.6.1.2.1.1.6.0"): "Floor 2 East Copy Room",
    P("1.3.6.1.2.1.2.2.1.6.1"): b"\x00\x1e\x0b\x12\x34\x56",
    P("1.3.6.1.2.1.25.3.2.1.3.1"): "HP Color LaserJet Enterprise M553",
    P("1.3.6.1.2.1.25.3.2.1.5.1"): 2,  # running
    P("1.3.6.1.2.1.25.3.5.1.1.1"): 3,  # idle
    P("1.3.6.1.2.1.25.3.5.1.2.1"): b"\x00\x00",  # no errors
    P("1.3.6.1.2.1.43.5.1.1.16.1"): "HP Color LaserJet M553",
    P("1.3.6.1.2.1.43.5.1.1.17.1"): "CNBRK12345",
    P("1.3.6.1.2.1.43.10.2.1.4.1.1"): 184321,
    P("1.3.6.1.2.1.43.10.2.1.5.1.1"): 9021,
    P("1.3.6.1.2.1.43.16.5.1.2.1.1"): "Ready",
}
# supplies: 1=black 2=cyan 3=magenta 4=yellow 5=waste 6=drum
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.4", {
    "1.1": 3, "1.2": 3, "1.3": 3, "1.4": 3, "1.5": 4, "1.6": 3,
}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.5", {
    "1.1": 3, "1.2": 3, "1.3": 3, "1.4": 3, "1.5": 4, "1.6": 9,
}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.6", {
    "1.1": "Black Cartridge HP 508A",
    "1.2": "Cyan Cartridge HP 508A",
    "1.3": "Magenta Cartridge HP 508A",
    "1.4": "Yellow Cartridge HP 508A",
    "1.5": "Toner Collection Unit",
    "1.6": "Imaging Drum",
}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.7", {
    "1.1": 19, "1.2": 19, "1.3": 19, "1.4": 19, "1.5": 19, "1.6": 19,
}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.8", {
    "1.1": 100, "1.2": 100, "1.3": 100, "1.4": 100, "1.5": 100, "1.6": 100,
}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.9", {
    "1.1": 82, "1.2": 14, "1.3": 61, "1.4": 0, "1.5": 35, "1.6": 77,
}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.3", {
    "1.1": 1, "1.2": 2, "1.3": 3, "1.4": 4, "1.5": 0, "1.6": 0,
}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.12.1.1.4", {
    "1.1": "black", "1.2": "cyan", "1.3": "magenta", "1.4": "yellow",
}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.8.2.1.9", {"1.1": 500, "1.2": 250}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.8.2.1.10", {"1.1": 400, "1.2": 25}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.8.2.1.13", {"1.1": "Tray 2", "1.2": "Tray 1"}))
COLOUR_LASER.update(_col("1.3.6.1.2.1.43.8.2.1.12", {"1.1": "Letter", "1.2": "Legal"}))


# A mono Brother that is jammed, reports raw grams rather than percent, and
# uses the -3 "some remaining" sentinel for the drum.
MONO_LASER = {
    P("1.3.6.1.2.1.1.1.0"): "Brother HL-L6200DW series",
    P("1.3.6.1.2.1.1.3.0"): 500,
    P("1.3.6.1.2.1.1.5.0"): "BRN30055C",
    P("1.3.6.1.2.1.1.6.0"): "Warehouse",
    P("1.3.6.1.2.1.25.3.2.1.3.1"): "Brother HL-L6200DW",
    P("1.3.6.1.2.1.25.3.2.1.5.1"): 3,  # warning
    P("1.3.6.1.2.1.25.3.5.1.1.1"): 1,  # other
    # bits: lowPaper(0) + jammed(5) -> byte0 = 0b10000100 = 0x84
    P("1.3.6.1.2.1.25.3.5.1.2.1"): b"\x84\x00",
    P("1.3.6.1.2.1.43.5.1.1.17.1"): "U63899L2N123456",
    P("1.3.6.1.2.1.43.10.2.1.4.1.1"): 52310,
    P("1.3.6.1.2.1.43.16.5.1.2.1.1"): "Paper Jam",
}
MONO_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.4", {"1.1": 3, "1.2": 3}))
MONO_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.5", {"1.1": 3, "1.2": 9}))
MONO_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.6", {
    "1.1": "TN-3430 Black Toner", "1.2": "DR-3400 Drum Unit",
}))
MONO_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.7", {"1.1": 13, "1.2": 13}))
MONO_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.8", {"1.1": 3000, "1.2": -2}))
MONO_LASER.update(_col("1.3.6.1.2.1.43.11.1.1.9", {"1.1": 1500, "1.2": -3}))


# A device that answers SNMP but is NOT a printer (no 1.3.6.1.2.1.43 subtree).
NETWORK_SWITCH = {
    P("1.3.6.1.2.1.1.1.0"): "Cisco IOS Software, C2960X Software",
    P("1.3.6.1.2.1.1.5.0"): "sw-core-01",
    P("1.3.6.1.2.1.1.3.0"): 999,
    P("1.3.6.1.2.1.25.3.2.1.3.1"): "Cisco Catalyst",
}
