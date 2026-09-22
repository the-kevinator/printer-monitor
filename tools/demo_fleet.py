"""
Seed a throwaway Printer Monitor instance with a believable fake fleet, so the
README screenshots can be retaken without pointing at anyone's real network.

Everything in here is invented -- RFC1918 addresses, made-up serials, a `jdoe`
account -- and the demo tree lives outside the repo, so nothing about the
machine that takes the screenshots ends up in them. Stats are built through the
app's own dataclasses, so the shapes stay right if the poller's payload changes.

    python tools/demo_fleet.py            # seed /tmp/printer-monitor
    PRINTER_MONITOR_CONFIG=/tmp/printer-monitor/config.json \
        python manage.py user add jdoe
    PRINTER_MONITOR_CONFIG=/tmp/printer-monitor/config.json \
        python manage.py serve --port 8899

Then `python tools/screenshots.py` retakes everything in docs/.
"""

import json
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from app.printer_mib import InputTray, PrinterStats, Supply  # noqa: E402

DEMO = pathlib.Path(os.environ.get("PM_DEMO_DIR", "/tmp/printer-monitor"))
DATA = DEMO / "data"
NOW = time.time()


def supply(index, description, colorant, percent, *,
           kind="toner", receptacle=False, unit="percent"):
    if percent is None:
        state = "unknown"
    elif percent <= 0:
        state = "empty"
    elif percent <= 20:
        state = "low"
    else:
        state = "ok"
    if receptacle:
        # A receptacle's "percent" is how full it is; it is spent at 100.
        state = "empty" if percent >= 100 else "ok"
    return Supply(
        index=index,
        description=description,
        type=kind,
        supply_class="supplyThatIsConsumed",
        colorant=colorant,
        unit=unit,
        level_raw=percent,
        max_raw=100,
        percent=percent,
        state=state,
        is_toner=kind == "toner",
        is_receptacle=receptacle,
    )


def tray(index, name, media, percent):
    return InputTray(
        index=index, name=name, media=media,
        level_raw=percent, max_raw=100, percent=percent,
        state="low" if percent is not None and percent <= 20 else "ok",
    )


def cmyk(black, cyan, magenta, yellow, prefix="Toner Cartridge"):
    return [
        supply("1.1", f"Black {prefix}", "black", black),
        supply("1.2", f"Cyan {prefix}", "cyan", cyan),
        supply("1.3", f"Magenta {prefix}", "magenta", magenta),
        supply("1.4", f"Yellow {prefix}", "yellow", yellow),
    ]


FLEET = [
    dict(
        ip="10.10.20.41", hostname="prn-floor2-color", sys_name="PRN-FLOOR2-COLOR",
        model="HP Color LaserJet M553", vendor="HP", serial_number="CNBRK418841",
        location="Floor 2 East", mac_address="00:1e:0b:1a:41:5c",
        web_url="https://10.10.20.41",
        notes="Key is at reception; tray 3 takes legal only.",
        stats=PrinterStats(
            reachable=True,
            sys_descr="HP ETHERNET MULTI-ENVIRONMENT,ROM none,JETDIRECT,JD153",
            sys_name="PRN-FLOOR2-COLOR", sys_location="Floor 2 East",
            sys_object_id="1.3.6.1.4.1.11.2.3.9.1",
            uptime_seconds=1178100, mac_address="00:1e:0b:1a:41:5c",
            model="HP Color LaserJet M553", vendor="HP",
            printer_name="PRN-FLOOR2-COLOR", serial_number="CNBRK418841",
            console_text="Cyan cartridge low", printer_status="idle",
            device_status="warning", errors=["lowToner"],
            page_count=184321, power_on_page_count=15360,
            supplies=cmyk(82, 14, 61, 47) + [
                supply("1.5", "Toner Collection Unit", None, 35,
                       kind="wasteToner", receptacle=True),
            ],
            trays=[tray("1.2", "Tray 2", "Letter", 80),
                   tray("1.3", "Tray 3", "Legal", 18)],
        ),
    ),
    dict(
        ip="10.10.20.52", hostname="prn-acct-mfp", sys_name="PRN-ACCT-MFP",
        model="Canon imageRUNNER C5535", vendor="Canon",
        serial_number="GTR04913", location="Accounting",
        mac_address="00:1e:8f:3c:09:11", web_url="https://10.10.20.52",
        stats=PrinterStats(
            reachable=True, sys_name="PRN-ACCT-MFP", sys_location="Accounting",
            uptime_seconds=1178220, mac_address="00:1e:8f:3c:09:11",
            model="Canon imageRUNNER C5535", vendor="Canon",
            serial_number="GTR04913", console_text="Ready",
            printer_status="idle", device_status="running", errors=[],
            page_count=92044, power_on_page_count=4102,
            supplies=cmyk(74, 55, 68, 71),
            trays=[tray("1.1", "Cassette 1", "Letter", 90),
                   tray("1.2", "Cassette 2", "A4", 45)],
        ),
    ),
    dict(
        ip="10.10.20.58", hostname="prn-reception", sys_name="PRN-RECEPTION",
        model="HP LaserJet Pro M404dn", vendor="HP",
        serial_number="PHBQK22107", location="Reception",
        mac_address="a4:5d:36:0b:77:e2", web_url="http://10.10.20.58",
        stats=PrinterStats(
            reachable=True, sys_name="PRN-RECEPTION", sys_location="Reception",
            uptime_seconds=1178340, mac_address="a4:5d:36:0b:77:e2",
            model="HP LaserJet Pro M404dn", vendor="HP",
            serial_number="PHBQK22107", console_text="Ready",
            printer_status="idle", device_status="running", errors=[],
            page_count=28755, power_on_page_count=980,
            supplies=[
                supply("1.1", "Black Toner Cartridge", "black", 63),
                supply("1.2", "Imaging Drum", None, 71, kind="drum"),
            ],
            trays=[tray("1.1", "Tray 1", "Letter", 60)],
        ),
    ),
    dict(
        ip="10.10.20.63", hostname="prn-warehouse", sys_name="PRN-WAREHOUSE",
        model="Brother HL-L6400DW", vendor="Brother",
        serial_number="U64812J1N203117", location="Warehouse",
        mac_address="30:05:5c:21:6a:40", web_url="http://10.10.20.63",
        online=False,
        stats=PrinterStats(
            reachable=False, error="no response to SNMP",
            model="Brother HL-L6400DW", vendor="Brother",
        ),
    ),
    dict(
        ip="10.10.20.71", hostname="prn-design-a3", sys_name="PRN-DESIGN-A3",
        model="Xerox VersaLink C7000", vendor="Xerox",
        serial_number="3417805542", location="Design Studio",
        mac_address="9c:93:4e:18:2b:05", web_url="https://10.10.20.71",
        stats=PrinterStats(
            reachable=True, sys_name="PRN-DESIGN-A3", sys_location="Design Studio",
            uptime_seconds=1178400, mac_address="9c:93:4e:18:2b:05",
            model="Xerox VersaLink C7000", vendor="Xerox",
            serial_number="3417805542", console_text="Replace magenta toner",
            printer_status="idle", device_status="down",
            errors=["noToner", "lowPaper"],
            page_count=310909, power_on_page_count=22415,
            supplies=cmyk(41, 22, 0, 18),
            trays=[tray("1.1", "Tray 1", "A3", 12),
                   tray("1.2", "Tray 2", "A4", 55)],
        ),
    ),
    dict(
        ip="10.10.20.84", hostname="prn-floor3-mfp", sys_name="PRN-FLOOR3-MFP",
        model="Ricoh IM C3000", vendor="Ricoh",
        serial_number="G194P900217", location="Floor 3 North",
        mac_address="00:26:73:9d:14:ab", web_url="https://10.10.20.84",
        stats=PrinterStats(
            reachable=True, sys_name="PRN-FLOOR3-MFP", sys_location="Floor 3 North",
            uptime_seconds=1178460, mac_address="00:26:73:9d:14:ab",
            model="Ricoh IM C3000", vendor="Ricoh",
            serial_number="G194P900217", console_text="Ready",
            printer_status="idle", device_status="running", errors=[],
            page_count=55310, power_on_page_count=7731,
            supplies=cmyk(91, 88, 79, 84),
            trays=[tray("1.1", "Tray 1", "Letter", 75),
                   tray("1.2", "Tray 2", "Letter", 88)],
        ),
    ),
]


def build():
    DATA.mkdir(parents=True, exist_ok=True)
    records = []
    for entry in FLEET:
        stats = entry.pop("stats")
        payload = stats.as_dict(low_threshold=20)
        payload["community"] = "public"
        payload["polled_at"] = NOW - 70  # renders as "1 min ago"
        record = {
            "community": "public",
            "source": "discovered",
            "enabled": True,
            "first_seen": NOW - 600000,
            "last_seen": NOW - 70 if stats.reachable else NOW - 90000,
            "last_polled": NOW - 70,
            "online": entry.pop("online", stats.reachable),
            "health": payload["health"],
            "consecutive_failures": 0 if stats.reachable else 9,
            "stats": payload,
            "label": None,
            "notes": None,
            **entry,
        }
        records.append(record)

    (DATA / "printers.json").write_text(json.dumps(
        {"schema_version": 1, "saved_at": NOW, "printers": records}, indent=2))

    (DEMO / "config.json").write_text(json.dumps({
        "subnets": ["10.10.20.0/24"],
        "exclude_ips": ["10.10.20.1"],
        "data_dir": str(DATA),
        "poller_enabled": False,
        "auto_discover_on_start": False,
        "discovery_interval_seconds": 0,
        "low_toner_threshold": 20,
        "poll_interval_seconds": 300,
    }, indent=2))
    print(f"seeded {len(records)} printers in {DEMO}")


if __name__ == "__main__":
    build()
