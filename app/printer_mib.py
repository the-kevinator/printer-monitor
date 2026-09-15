"""
Printer MIB (RFC 3805/1759) and Host Resources MIB (RFC 2790) decoding.

Turns raw SNMP values into the toner / tray / counter / status figures the
dashboard shows. All the vendor-agnostic interpretation lives here so the rest
of the app never has to think about OIDs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from typing import Any

from .snmp import SnmpClient, SnmpError, oid_str, raw_bytes as snmp_raw_bytes

# ---------------------------------------------------------------------------
# OIDs
# ---------------------------------------------------------------------------

# SNMPv2-MIB / system group
SYS_DESCR = "1.3.6.1.2.1.1.1.0"
SYS_OBJECT_ID = "1.3.6.1.2.1.1.2.0"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
SYS_CONTACT = "1.3.6.1.2.1.1.4.0"
SYS_NAME = "1.3.6.1.2.1.1.5.0"
SYS_LOCATION = "1.3.6.1.2.1.1.6.0"

# IF-MIB: used only to pick up a MAC address for inventory
IF_PHYS_ADDRESS = "1.3.6.1.2.1.2.2.1.6"

# Host Resources MIB
HR_DEVICE_DESCR = "1.3.6.1.2.1.25.3.2.1.3"
HR_DEVICE_STATUS = "1.3.6.1.2.1.25.3.2.1.5"
HR_PRINTER_STATUS = "1.3.6.1.2.1.25.3.5.1.1"
HR_PRINTER_DETECTED_ERROR_STATE = "1.3.6.1.2.1.25.3.5.1.2"

# Printer MIB -- general
PRT_GENERAL = "1.3.6.1.2.1.43"
PRT_GENERAL_PRINTER_NAME = "1.3.6.1.2.1.43.5.1.1.16"
PRT_GENERAL_SERIAL_NUMBER = "1.3.6.1.2.1.43.5.1.1.17"

# Printer MIB -- marker (page counters)
PRT_MARKER_LIFE_COUNT = "1.3.6.1.2.1.43.10.2.1.4"
PRT_MARKER_POWER_ON_COUNT = "1.3.6.1.2.1.43.10.2.1.5"

# Printer MIB -- input trays
PRT_INPUT_MAX_CAPACITY = "1.3.6.1.2.1.43.8.2.1.9"
PRT_INPUT_CURRENT_LEVEL = "1.3.6.1.2.1.43.8.2.1.10"
PRT_INPUT_STATUS = "1.3.6.1.2.1.43.8.2.1.11"
PRT_INPUT_MEDIA_NAME = "1.3.6.1.2.1.43.8.2.1.12"
PRT_INPUT_NAME = "1.3.6.1.2.1.43.8.2.1.13"
PRT_INPUT_DESCRIPTION = "1.3.6.1.2.1.43.8.2.1.18"

# Printer MIB -- supplies (toner, drums, waste)
PRT_SUPPLIES_CLASS = "1.3.6.1.2.1.43.11.1.1.4"
PRT_SUPPLIES_TYPE = "1.3.6.1.2.1.43.11.1.1.5"
PRT_SUPPLIES_DESCRIPTION = "1.3.6.1.2.1.43.11.1.1.6"
PRT_SUPPLIES_UNIT = "1.3.6.1.2.1.43.11.1.1.7"
PRT_SUPPLIES_MAX_CAPACITY = "1.3.6.1.2.1.43.11.1.1.8"
PRT_SUPPLIES_LEVEL = "1.3.6.1.2.1.43.11.1.1.9"
PRT_SUPPLIES_COLORANT_INDEX = "1.3.6.1.2.1.43.11.1.1.3"

# Printer MIB -- colorants
PRT_COLORANT_VALUE = "1.3.6.1.2.1.43.12.1.1.4"

# Printer MIB -- front panel text
PRT_CONSOLE_DISPLAY_TEXT = "1.3.6.1.2.1.43.16.5.1.2"

# ---------------------------------------------------------------------------
# Enumeration tables
# ---------------------------------------------------------------------------

HR_PRINTER_STATUS_NAMES = {
    1: "other",
    2: "unknown",
    3: "idle",
    4: "printing",
    5: "warmup",
}

HR_DEVICE_STATUS_NAMES = {
    1: "unknown",
    2: "running",
    3: "warning",
    4: "testing",
    5: "down",
}

# hrPrinterDetectedErrorState is a BITS value: bit 0 is the most significant
# bit of the first octet.
DETECTED_ERROR_BITS = [
    "lowPaper",
    "noPaper",
    "lowToner",
    "noToner",
    "doorOpen",
    "jammed",
    "offline",
    "serviceRequested",
    "inputTrayMissing",
    "outputTrayMissing",
    "markerSupplyMissing",
    "outputNearFull",
    "outputFull",
    "inputTrayEmpty",
    "overduePreventMaint",
]

# Errors that mean the printer cannot print right now.
CRITICAL_ERRORS = {
    "noPaper",
    "noToner",
    "doorOpen",
    "jammed",
    "offline",
    "serviceRequested",
    "markerSupplyMissing",
    "outputFull",
    "inputTrayEmpty",
}

SUPPLY_TYPE_NAMES = {
    1: "other",
    2: "unknown",
    3: "toner",
    4: "wasteToner",
    5: "ink",
    6: "inkCartridge",
    7: "inkRibbon",
    8: "wasteInk",
    9: "opc",
    10: "developer",
    11: "fuserOil",
    12: "solidWax",
    13: "ribbonWax",
    14: "wasteWax",
    15: "fuser",
    16: "coronaWire",
    17: "fuserOilWick",
    18: "cleanerUnit",
    19: "fuserCleaningPad",
    20: "transferUnit",
    21: "tonerCartridge",
    22: "fuserOiler",
    23: "water",
    24: "wasteWater",
    25: "gluewaterAdditive",
    26: "wastePaper",
    27: "bindingSupply",
    28: "bandingSupply",
    29: "stitchingWire",
    30: "shrinkWrap",
    31: "paperWrap",
    32: "staples",
    33: "inserts",
    34: "covers",
}

# Types that represent consumed ink/toner rather than drums, fusers or waste.
TONER_TYPES = {"toner", "tonerCartridge", "ink", "inkCartridge", "inkRibbon"}

SUPPLY_CLASS_NAMES = {
    1: "other",
    3: "supplyThatIsConsumed",
    4: "receptacleThatIsFilled",
}

SUPPLY_UNIT_NAMES = {
    1: "other",
    2: "unknown",
    3: "tenThousandthsOfInches",
    4: "micrometers",
    7: "impressions",
    8: "sheets",
    11: "hours",
    12: "thousandthsOfOunces",
    13: "tenthsOfGrams",
    14: "hundredthsOfFluidOunces",
    15: "tenthsOfMilliliters",
    16: "feet",
    17: "meters",
    18: "items",
    19: "percent",
}

UNIT_PERCENT = 19

COLORANT_CANONICAL = {
    "black": "black",
    "blck": "black",
    "k": "black",
    "cyan": "cyan",
    "c": "cyan",
    "magenta": "magenta",
    "m": "magenta",
    "yellow": "yellow",
    "y": "yellow",
    "red": "red",
    "green": "green",
    "blue": "blue",
    "gray": "gray",
    "grey": "gray",
    "lightcyan": "lightCyan",
    "lightmagenta": "lightMagenta",
    "photoblack": "black",
    "matteblack": "black",
    "none": None,
    "unknown": None,
    "other": None,
}

# Level sentinels defined by the Printer MIB.
LEVEL_OTHER = -1
LEVEL_UNKNOWN = -2
LEVEL_SOME_REMAINING = -3

CAPACITY_OTHER = -1
CAPACITY_UNKNOWN = -2


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Supply:
    """One row of prtMarkerSuppliesTable, interpreted."""

    index: str
    description: str
    type: str
    supply_class: str
    colorant: str | None
    unit: str
    level_raw: int | None
    max_raw: int | None
    percent: int | None
    state: str  # ok | low | empty | some_remaining | unknown
    is_toner: bool
    is_receptacle: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InputTray:
    index: str
    name: str
    media: str
    level_raw: int | None
    max_raw: int | None
    percent: int | None
    state: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PrinterStats:
    """Everything one poll of a printer produced."""

    reachable: bool = False
    error: str | None = None
    sys_descr: str | None = None
    sys_name: str | None = None
    sys_location: str | None = None
    sys_contact: str | None = None
    sys_object_id: str | None = None
    uptime_seconds: int | None = None
    mac_address: str | None = None
    model: str | None = None
    vendor: str | None = None
    printer_name: str | None = None
    serial_number: str | None = None
    console_text: str | None = None
    printer_status: str | None = None
    device_status: str | None = None
    errors: list[str] = field(default_factory=list)
    page_count: int | None = None
    power_on_page_count: int | None = None
    supplies: list[Supply] = field(default_factory=list)
    trays: list[InputTray] = field(default_factory=list)

    # -- derived ---------------------------------------------------------

    @property
    def toner_supplies(self) -> list[Supply]:
        return [s for s in self.supplies if s.is_toner]

    @property
    def lowest_toner_percent(self) -> int | None:
        values = [
            s.percent for s in self.toner_supplies if s.percent is not None
        ]
        return min(values) if values else None

    def health(self, low_threshold: int = 20) -> str:
        """Roll everything up into ok | warning | critical | offline."""
        if not self.reachable:
            return "offline"
        if any(e in CRITICAL_ERRORS for e in self.errors):
            return "critical"
        if self.device_status == "down":
            return "critical"
        if any(s.state == "empty" for s in self.supplies):
            # Covers both a spent consumable and a waste receptacle that is
            # full; both stop the printer.
            return "critical"
        if self.errors or self.device_status == "warning":
            return "warning"
        # Recompute the "running low" test against the threshold passed in,
        # rather than trusting Supply.state, which was frozen at poll time
        # using whatever threshold was configured then.
        for supply in self.supplies:
            if supply.percent is None:
                continue
            remaining = (
                100 - supply.percent if supply.is_receptacle else supply.percent
            )
            if remaining <= low_threshold:
                return "warning"
        return "ok"

    def as_dict(self, low_threshold: int = 20) -> dict[str, Any]:
        data = asdict(self)
        data["supplies"] = [s.as_dict() for s in self.supplies]
        data["trays"] = [t.as_dict() for t in self.trays]
        data["lowest_toner_percent"] = self.lowest_toner_percent
        data["health"] = self.health(low_threshold)
        return data


# ---------------------------------------------------------------------------
# Interpretation helpers
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("latin-1")
        except Exception:
            return None
    if isinstance(value, str):
        cleaned = re.sub(r"\s+", " ", value.replace("\x00", "")).strip()
        return cleaned or None
    if isinstance(value, tuple):
        return oid_str(value)
    return str(value)


def compute_percent(level: int | None, capacity: int | None, unit_code: int | None) -> int | None:
    """
    Turn a Printer MIB level/capacity pair into 0-100, or None if unknown.

    Handles the negative sentinels and the percent-unit special case where the
    level is already a percentage.
    """
    if level is None:
        return None
    if level < 0:
        # -1/-2 are unknown; -3 means "some left, amount unknown".
        return None
    if capacity is not None and capacity > 0:
        return max(0, min(100, round(level * 100.0 / capacity)))
    if unit_code == UNIT_PERCENT and 0 <= level <= 100:
        return int(level)
    if capacity in (None, CAPACITY_OTHER, CAPACITY_UNKNOWN) and 0 <= level <= 100:
        # Many agents report a percentage with an unknown capacity.
        return int(level)
    return None


def classify_level(
    level: int | None,
    percent: int | None,
    low_threshold: int = 20,
    is_receptacle: bool = False,
) -> str:
    """Map a level to ok | low | empty | some_remaining | unknown."""
    if level == LEVEL_SOME_REMAINING:
        return "some_remaining"
    if percent is None:
        if level is None or level < 0:
            return "unknown"
        return "unknown"
    if is_receptacle:
        # A receptacle is a problem when it is FULL, not when it is empty.
        if percent >= 100:
            return "empty"  # i.e. needs emptying -- treated as critical
        if percent >= 100 - low_threshold:
            return "low"
        return "ok"
    if percent <= 0:
        return "empty"
    if percent <= low_threshold:
        return "low"
    return "ok"


def canonical_colorant(raw: str | None) -> str | None:
    if not raw:
        return None
    key = re.sub(r"[^a-z]", "", raw.lower())
    return COLORANT_CANONICAL.get(key, raw.strip() or None)


def colorant_from_description(description: str | None) -> str | None:
    """Infer a colour from a supply description like 'Black Toner Cartridge'."""
    if not description:
        return None
    text = description.lower()
    # Order matters: check the compound names before the bare ones.
    for needle, colour in (
        ("light cyan", "lightCyan"),
        ("light magenta", "lightMagenta"),
        ("photo black", "black"),
        ("matte black", "black"),
        ("black", "black"),
        ("cyan", "cyan"),
        ("magenta", "magenta"),
        ("yellow", "yellow"),
    ):
        if needle in text:
            return colour
    return None


def decode_detected_errors(raw: Any) -> list[str]:
    """Decode the hrPrinterDetectedErrorState BITS value into names."""
    if raw is None:
        return []
    if isinstance(raw, int):
        # A few agents return it as an integer instead of an octet string.
        length = max(1, (raw.bit_length() + 7) // 8)
        raw = raw.to_bytes(length, "big")
    recovered = snmp_raw_bytes(raw)
    if recovered is not None:
        raw = recovered
    elif isinstance(raw, str):
        raw = raw.encode("latin-1", errors="ignore")
    if not isinstance(raw, (bytes, bytearray)):
        return []
    names: list[str] = []
    for bit_index, name in enumerate(DETECTED_ERROR_BITS):
        byte_index, offset = divmod(bit_index, 8)
        if byte_index >= len(raw):
            break
        if raw[byte_index] & (0x80 >> offset):
            names.append(name)
    return names


VENDOR_PATTERNS = [
    (r"\bhp\b|hewlett[- ]?packard|laserjet|officejet|designjet|pagewide", "HP"),
    (r"\bcanon\b|imagerunner|imageclass|\bmf\d", "Canon"),
    (r"\bbrother\b|\bhl-|\bmfc-|\bdcp-", "Brother"),
    (r"\bxerox\b|versalink|altalink|workcentre|phaser", "Xerox"),
    (r"\blexmark\b", "Lexmark"),
    (r"\bepson\b|workforce|ecotank", "Epson"),
    (r"\bricoh\b|aficio|\bim c|\bmp c", "Ricoh"),
    (r"\bkyocera\b|ecosys|taskalfa", "Kyocera"),
    (r"\bsharp\b|\bmx-", "Sharp"),
    (r"\bkonica\b|minolta|bizhub", "Konica Minolta"),
    (r"\boki\b|okidata", "OKI"),
    (r"\bsamsung\b|\bclx-|\bml-", "Samsung"),
    (r"\bdell\b", "Dell"),
    (r"\btoshiba\b|e-studio", "Toshiba"),
    (r"\bzebra\b|\bzt\d|\bgk\d", "Zebra"),
    (r"\bdymo\b", "DYMO"),
    (r"\bfuji\b|fujifilm", "Fujifilm"),
]


def guess_vendor(*candidates: str | None) -> str | None:
    """
    Identify the manufacturer from the device's own strings.

    Candidates are tried in order of authority (sysDescr first, then the
    Host Resources / Printer MIB names), and each is matched on its own. They
    must not be concatenated: a single haystack lets pattern order beat field
    authority, so a Xerox whose prtGeneralPrinterName still carries a previous
    owner's "HP ..." string would be reported as HP.
    """
    for candidate in candidates:
        if not candidate:
            continue
        text = candidate.lower()
        for pattern, vendor in VENDOR_PATTERNS:
            if re.search(pattern, text):
                return vendor
    return None


def guess_model(sys_descr: str | None, hr_device_descr: str | None,
                printer_name: str | None) -> str | None:
    """
    Pick the most model-like string available.

    hrDeviceDescr and prtGeneralPrinterName are usually clean model names;
    sysDescr is often a long banner, so it is used as a last resort and
    trimmed to its first line/segment.
    """
    for candidate in (hr_device_descr, printer_name):
        text = _as_text(candidate)
        if text and len(text) <= 80:
            return text
    text = _as_text(sys_descr)
    if not text:
        return None
    # sysDescr banners are frequently key/value soup delimited by ";" or ","
    # ("HP ETHERNET MULTI-ENVIRONMENT,ROM none,JETDIRECT,JD153,..."), so keep
    # only the leading segment. Model names themselves do not contain these.
    first = re.split(r"[;,\n]", text)[0].strip()
    return first[:120] or None


# ---------------------------------------------------------------------------
# Reading a printer
# ---------------------------------------------------------------------------

_SCALAR_OIDS = [
    SYS_DESCR,
    SYS_NAME,
    SYS_LOCATION,
    SYS_CONTACT,
    SYS_OBJECT_ID,
    SYS_UPTIME,
]


def looks_like_printer(client: SnmpClient) -> tuple[bool, dict[str, Any]]:
    """
    Decide whether a host that answers SNMP is a printer.

    The test is whether anything exists under the Printer MIB (1.3.6.1.2.1.43),
    which is the definitive signal. Returns (is_printer, identity_values) so
    the caller can reuse the values it already fetched.
    """
    identity: dict[str, Any] = {}
    try:
        identity = client.get_many([SYS_DESCR, SYS_NAME, SYS_OBJECT_ID])
    except SnmpError:
        return False, identity

    if not identity:
        return False, identity

    try:
        bindings = client.get_next(PRT_GENERAL)
    except SnmpError:
        return False, identity

    for binding in bindings:
        if binding.is_exception:
            continue
        if oid_str(binding.oid).startswith(PRT_GENERAL + "."):
            return True, identity
    return False, identity


def _first_column_value(rows: dict[str, Any]) -> Any:
    """Return the value of the lowest-indexed row of a walked column."""
    if not rows:
        return None

    def sort_key(key: str):
        try:
            return tuple(int(part) for part in key.split("."))
        except ValueError:
            return (1 << 30,)

    return rows[sorted(rows, key=sort_key)[0]]


def _largest_column_value(rows: dict[str, Any]) -> int | None:
    """
    Page counters can appear on several marker rows; the real life count is the
    largest non-negative one.
    """
    values = [v for v in (_as_int(x) for x in rows.values()) if v is not None and v >= 0]
    return max(values) if values else None


def read_supplies(client: SnmpClient, low_threshold: int = 20) -> list[Supply]:
    levels = client.walk_column(PRT_SUPPLIES_LEVEL)
    if not levels:
        return []
    capacities = client.walk_column(PRT_SUPPLIES_MAX_CAPACITY)
    descriptions = client.walk_column(PRT_SUPPLIES_DESCRIPTION)
    types = client.walk_column(PRT_SUPPLIES_TYPE)
    classes = client.walk_column(PRT_SUPPLIES_CLASS)
    units = client.walk_column(PRT_SUPPLIES_UNIT)
    colorant_indexes = client.walk_column(PRT_SUPPLIES_COLORANT_INDEX)
    colorants = client.walk_column(PRT_COLORANT_VALUE)

    supplies: list[Supply] = []
    for index in sorted(levels, key=lambda k: [int(p) for p in k.split(".") if p.isdigit()] or [0]):
        level = _as_int(levels.get(index))
        capacity = _as_int(capacities.get(index))
        unit_code = _as_int(units.get(index))
        type_code = _as_int(types.get(index))
        class_code = _as_int(classes.get(index))
        description = _as_text(descriptions.get(index)) or ""

        type_name = SUPPLY_TYPE_NAMES.get(type_code or 0, "unknown")
        class_name = SUPPLY_CLASS_NAMES.get(class_code or 0, "other")
        is_receptacle = class_name == "receptacleThatIsFilled"

        # Colorant: prefer the colorant table, fall back to the description.
        colorant: str | None = None
        colorant_index = _as_int(colorant_indexes.get(index))
        if colorant_index and colorant_index > 0:
            # prtMarkerColorantValue rows are keyed marker.colorant; match on
            # the trailing colorant index within the same marker where possible.
            marker = index.split(".")[0] if "." in index else "1"
            for key in (f"{marker}.{colorant_index}", str(colorant_index)):
                if key in colorants:
                    colorant = canonical_colorant(_as_text(colorants[key]))
                    break
        if not colorant:
            colorant = colorant_from_description(description)

        percent = compute_percent(level, capacity, unit_code)
        is_toner = type_name in TONER_TYPES
        state = classify_level(
            level, percent, low_threshold=low_threshold,
            is_receptacle=is_receptacle,
        )

        supplies.append(
            Supply(
                index=index,
                description=description or type_name,
                type=type_name,
                supply_class=class_name,
                colorant=colorant,
                unit=SUPPLY_UNIT_NAMES.get(unit_code or 0, "unknown"),
                level_raw=level,
                max_raw=capacity,
                percent=percent,
                state=state,
                is_toner=is_toner,
                is_receptacle=is_receptacle,
            )
        )
    return supplies


def read_trays(client: SnmpClient, low_threshold: int = 20) -> list[InputTray]:
    levels = client.walk_column(PRT_INPUT_CURRENT_LEVEL)
    if not levels:
        return []
    capacities = client.walk_column(PRT_INPUT_MAX_CAPACITY)
    names = client.walk_column(PRT_INPUT_NAME)
    descriptions = client.walk_column(PRT_INPUT_DESCRIPTION)
    media = client.walk_column(PRT_INPUT_MEDIA_NAME)

    trays: list[InputTray] = []
    for index in sorted(levels):
        level = _as_int(levels.get(index))
        capacity = _as_int(capacities.get(index))
        percent = compute_percent(level, capacity, None)
        name = (
            _as_text(names.get(index))
            or _as_text(descriptions.get(index))
            or f"Tray {index}"
        )
        trays.append(
            InputTray(
                index=index,
                name=name,
                media=_as_text(media.get(index)) or "",
                level_raw=level,
                max_raw=capacity,
                percent=percent,
                state=classify_level(level, percent, low_threshold),
            )
        )
    return trays


def read_printer(
    client: SnmpClient,
    low_threshold: int = 20,
    identity: dict[str, Any] | None = None,
) -> PrinterStats:
    """
    Poll one printer and return everything we could read.

    Never raises for an unreachable or partly-answering device: failures land
    in `reachable`/`error` so one bad printer cannot break a dashboard refresh.
    """
    stats = PrinterStats()
    try:
        scalars = dict(identity or {})
        missing = [oid for oid in _SCALAR_OIDS if oid not in scalars]
        if missing:
            scalars.update(client.get_many(missing))
        if not scalars:
            stats.error = "no SNMP response"
            return stats
        stats.reachable = True

        stats.sys_descr = _as_text(scalars.get(SYS_DESCR))
        stats.sys_name = _as_text(scalars.get(SYS_NAME))
        stats.sys_location = _as_text(scalars.get(SYS_LOCATION))
        stats.sys_contact = _as_text(scalars.get(SYS_CONTACT))
        stats.sys_object_id = _as_text(scalars.get(SYS_OBJECT_ID))
        uptime_ticks = _as_int(scalars.get(SYS_UPTIME))
        if uptime_ticks is not None:
            stats.uptime_seconds = uptime_ticks // 100

        # Identity from the Printer MIB.
        stats.printer_name = _as_text(
            _first_column_value(client.walk_column(PRT_GENERAL_PRINTER_NAME))
        )
        stats.serial_number = _as_text(
            _first_column_value(client.walk_column(PRT_GENERAL_SERIAL_NUMBER))
        )
        hr_device_descr = _as_text(
            _first_column_value(client.walk_column(HR_DEVICE_DESCR))
        )
        stats.model = guess_model(
            stats.sys_descr, hr_device_descr, stats.printer_name
        )
        stats.vendor = guess_vendor(
            stats.sys_descr, hr_device_descr, stats.printer_name, stats.model
        )

        mac = _first_column_value(client.walk_column(IF_PHYS_ADDRESS))
        stats.mac_address = _format_mac(mac)

        # Status.
        printer_status = _as_int(
            _first_column_value(client.walk_column(HR_PRINTER_STATUS))
        )
        stats.printer_status = HR_PRINTER_STATUS_NAMES.get(printer_status or 0)
        device_status = _as_int(
            _first_column_value(client.walk_column(HR_DEVICE_STATUS))
        )
        stats.device_status = HR_DEVICE_STATUS_NAMES.get(device_status or 0)
        stats.errors = decode_detected_errors(
            _first_column_value(
                client.walk_column(HR_PRINTER_DETECTED_ERROR_STATE)
            )
        )

        # Console/front-panel message, joined across display lines.
        console_rows = client.walk_column(PRT_CONSOLE_DISPLAY_TEXT)
        console_lines = [
            _as_text(console_rows[key]) for key in sorted(console_rows)
        ]
        joined = " ".join(line for line in console_lines if line)
        stats.console_text = joined or None

        # Counters.
        stats.page_count = _largest_column_value(
            client.walk_column(PRT_MARKER_LIFE_COUNT)
        )
        stats.power_on_page_count = _largest_column_value(
            client.walk_column(PRT_MARKER_POWER_ON_COUNT)
        )

        # Consumables and paper.
        stats.supplies = read_supplies(client, low_threshold=low_threshold)
        stats.trays = read_trays(client, low_threshold=low_threshold)

    except SnmpError as exc:
        if not stats.reachable:
            stats.error = str(exc)
        else:
            # Partial read: keep whatever we got but record the problem.
            stats.error = f"partial read: {exc}"
    except Exception as exc:  # pragma: no cover - defensive
        stats.error = f"{type(exc).__name__}: {exc}"

    return stats


def _format_mac(value: Any) -> str | None:
    if value is None:
        return None
    raw = snmp_raw_bytes(value)
    if raw is None and isinstance(value, str):
        raw = value.encode("latin-1", errors="ignore")
    if raw is None:
        return None
    if len(raw) != 6 or not any(raw):
        return None
    return ":".join(f"{byte:02x}" for byte in raw)


def parse_oid_compat(text: str):
    """Convenience re-export so tests and callers can build OID keys."""
    from .snmp import parse_oid

    return parse_oid(text)
