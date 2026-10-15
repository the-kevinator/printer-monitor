"""
API and rendering tests.

The app builds its state at import time, so the environment is set up before
`app.main` is imported and the background poller is switched off -- these tests
must not touch the network.
"""

import json
import os
import time
import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    root = tmp_path_factory.mktemp("pm")
    data_dir = root / "data"
    data_dir.mkdir()

    # Two seeded printers: one healthy colour laser, one offline unit.
    (data_dir / "printers.json").write_text(json.dumps({
        "schema_version": 1,
        "printers": [
            {
                "ip": "192.0.2.10",
                "hostname": "prn-color",
                "sys_name": "PRN-FLOOR2-COLOR",
                "model": "HP Color LaserJet M553",
                "vendor": "HP",
                "serial_number": "CNBRK12345",
                "location": "Floor 2 East",
                "mac_address": "00:1e:0b:12:34:56",
                "web_url": "https://192.0.2.10",
                "source": "discovered",
                "enabled": True,
                "online": True,
                "health": "warning",
                "last_seen": time.time(),
                "last_polled": time.time(),
                "stats": {
                    "reachable": True,
                    "health": "warning",
                    "page_count": 184321,
                    "power_on_page_count": 9021,
                    "printer_status": "idle",
                    "device_status": "running",
                    "uptime_seconds": 1234567,
                    "sys_descr": "HP ETHERNET MULTI-ENVIRONMENT",
                    "console_text": "Ready",
                    "errors": [],
                    "lowest_toner_percent": 14,
                    "supplies": [
                        {"index": "1.1", "description": "Black Cartridge",
                         "type": "toner", "supply_class": "supplyThatIsConsumed",
                         "colorant": "black", "unit": "percent", "level_raw": 82,
                         "max_raw": 100, "percent": 82, "state": "ok",
                         "is_toner": True, "is_receptacle": False},
                        {"index": "1.2", "description": "Cyan Cartridge",
                         "type": "toner", "supply_class": "supplyThatIsConsumed",
                         "colorant": "cyan", "unit": "percent", "level_raw": 14,
                         "max_raw": 100, "percent": 14, "state": "low",
                         "is_toner": True, "is_receptacle": False},
                        {"index": "1.5", "description": "Toner Collection Unit",
                         "type": "wasteToner", "supply_class": "receptacleThatIsFilled",
                         "colorant": None, "unit": "percent", "level_raw": 35,
                         "max_raw": 100, "percent": 35, "state": "ok",
                         "is_toner": False, "is_receptacle": True},
                    ],
                    "trays": [
                        {"index": "1.1", "name": "Tray 2", "media": "Letter",
                         "level_raw": 400, "max_raw": 500, "percent": 80,
                         "state": "ok"},
                    ],
                    "polled_at": time.time(),
                },
            },
            {
                "ip": "192.0.2.99",
                "label": "Warehouse label printer",
                "model": "Zebra ZT411",
                "vendor": "Zebra",
                "source": "manual",
                "enabled": True,
                "online": False,
                "health": "offline",
                "consecutive_failures": 4,
                "last_polled": time.time(),
                "stats": {
                    "reachable": False, "health": "offline",
                    "error": "no SNMP response from 192.0.2.99:161",
                    "supplies": [], "trays": [], "errors": [],
                    "polled_at": time.time(),
                },
            },
        ],
    }))

    config = root / "config.json"
    config.write_text(json.dumps({
        "subnets": ["192.0.2.0/24"],
        "data_dir": str(data_dir),
        "poller_enabled": False,
        "auto_discover_on_start": False,
        "discovery_interval_seconds": 0,
        "low_toner_threshold": 20,
    }))

    os.environ["PRINTER_MONITOR_CONFIG"] = str(config)
    import app.main as main
    importlib.reload(main)
    with TestClient(main.app) as test_client:
        test_client.app_module = main
        yield test_client


# -- pages ------------------------------------------------------------------


def test_dashboard_renders(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.text
    assert "Printer Monitor" in body
    assert "192.0.2.0/24" in body
    assert "/static/app.js" in body
    # The page is a shell; printers arrive via the API.
    assert 'id="main"' in body


def test_detail_page_renders_printer_facts(client):
    response = client.get("/printer/192.0.2.10")
    assert response.status_code == 200
    body = response.text
    assert "PRN-FLOOR2-COLOR" in body
    assert "HP Color LaserJet M553" in body
    assert "CNBRK12345" in body
    assert "184,321" in body  # lifetime pages, formatted
    assert "00:1e:0b:12:34:56" in body


def test_detail_page_shows_offline_banner(client):
    response = client.get("/printer/192.0.2.99")
    assert response.status_code == 200
    assert "Not responding to SNMP" in response.text
    assert "Warehouse label printer" in response.text


def test_detail_page_404_for_unknown(client):
    assert client.get("/printer/192.0.2.200").status_code == 404


def test_detail_page_400_for_garbage_ip(client):
    assert client.get("/printer/not-an-ip").status_code == 400


def test_open_redirects_to_device_web_ui(client):
    response = client.get("/open/192.0.2.10", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "https://192.0.2.10"


def test_open_falls_back_to_http_when_url_unknown(client):
    response = client.get("/open/192.0.2.99", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "http://192.0.2.99"


# -- API --------------------------------------------------------------------


def test_list_printers(client):
    payload = client.get("/api/printers").json()
    assert len(payload["printers"]) == 2
    first = payload["printers"][0]
    assert first["ip"] == "192.0.2.10"
    assert first["display_name"] == "PRN-FLOOR2-COLOR"
    assert first["summary"]["lowest_toner_percent"] == 14
    assert first["summary"]["page_count"] == 184321
    assert len(first["summary"]["supplies"]) == 3
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["by_health"] == {
        "ok": 0, "warning": 1, "critical": 0, "offline": 1, "unknown": 0
    }
    assert payload["low_toner_threshold"] == 20


def test_display_name_prefers_the_user_label(client):
    payload = client.get("/api/printers/192.0.2.99").json()
    assert payload["display_name"] == "Warehouse label printer"


def test_filter_by_health(client):
    payload = client.get("/api/printers?health=offline").json()
    assert [p["ip"] for p in payload["printers"]] == ["192.0.2.99"]

    payload = client.get("/api/printers?health=warning,offline").json()
    assert len(payload["printers"]) == 2

    payload = client.get("/api/printers?health=critical").json()
    assert payload["printers"] == []


def test_search_matches_model_and_location(client):
    assert len(client.get("/api/printers?q=zebra").json()["printers"]) == 1
    assert len(client.get("/api/printers?q=floor+2").json()["printers"]) == 1
    assert len(client.get("/api/printers?q=nothing-here").json()["printers"]) == 0


def test_get_single_printer_and_404(client):
    assert client.get("/api/printers/192.0.2.10").json()["vendor"] == "HP"
    assert client.get("/api/printers/192.0.2.123").status_code == 404


def test_patch_updates_editable_fields(client):
    response = client.patch(
        "/api/printers/192.0.2.10",
        json={"label": "Second floor colour", "notes": "Key in reception"},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Second floor colour"

    # Round-trips through the store.
    assert client.get("/api/printers/192.0.2.10").json()["notes"] == "Key in reception"

    # Put it back so later tests see the original name.
    client.patch("/api/printers/192.0.2.10", json={"label": ""})


def test_patch_rejects_unknown_fields(client):
    response = client.patch("/api/printers/192.0.2.10", json={"ip": "10.0.0.1"})
    assert response.status_code == 400


def test_patch_404_for_unknown_printer(client):
    response = client.patch("/api/printers/192.0.2.250", json={"label": "x"})
    assert response.status_code == 404


def test_summary_endpoint(client):
    payload = client.get("/api/summary").json()
    assert payload["summary"]["total"] == 2
    assert "192.0.2.99" in payload["summary"]["needs_attention"]


def test_config_endpoint_masks_the_community_string(client):
    payload = client.get("/api/config").json()
    assert payload["community"] == "***"
    assert payload["subnets"] == ["192.0.2.0/24"]
    assert payload["sweep_target_count"] == 254
    assert payload["poller_enabled"] is False


def test_healthz(client):
    payload = client.get("/healthz").json()
    assert payload["status"] == "ok"
    assert payload["printers"] == 2


def test_discovery_status_is_idle(client):
    payload = client.get("/api/discover").json()
    assert payload["running"] is False


def test_add_and_delete_printer(client):
    response = client.post("/api/printers", json={"ip": "192.0.2.77", "label": "Test unit"})
    assert response.status_code == 201
    body = response.json()
    assert body["printer"]["ip"] == "192.0.2.77"
    assert body["printer"]["source"] == "manual"
    # 192.0.2.0/24 is TEST-NET-1 and unroutable, so it cannot answer.
    assert body["reachable"] is False

    assert client.get("/api/printers/192.0.2.77").status_code == 200
    assert client.delete("/api/printers/192.0.2.77").status_code == 200
    assert client.get("/api/printers/192.0.2.77").status_code == 404
    assert client.delete("/api/printers/192.0.2.77").status_code == 404


def test_add_printer_validates_the_address(client):
    assert client.post("/api/printers", json={"ip": "banana"}).status_code == 400
    assert client.post("/api/printers", json={}).status_code == 400


def test_inventory_is_persisted_to_disk(client):
    store_path = Path(client.app_module.state.settings.printers_file)
    client.patch("/api/printers/192.0.2.10", json={"notes": "persisted note"})
    saved = json.loads(store_path.read_text())
    notes = {p["ip"]: p.get("notes") for p in saved["printers"]}
    assert notes["192.0.2.10"] == "persisted note"


def test_urls_respect_an_iis_virtual_application_prefix(client):
    """
    Under IIS the app is commonly a virtual application below a site, started
    with --root-path. Every asset, link and API URL must carry that prefix or
    the page resolves them against the site root and breaks.
    """
    from fastapi.testclient import TestClient

    with TestClient(client.app_module.app, root_path="/printers") as prefixed:
        body = prefixed.get("/").text
        assert '<body data-base="/printers"' in body
        assert 'href="/printers/static/style.css"' in body
        assert 'src="/printers/static/app.js"' in body

        detail = prefixed.get("/printer/192.0.2.10").text
        assert 'href="/printers/static/style.css"' in detail
        assert 'href="/printers/"' in detail


def test_urls_have_no_prefix_when_mounted_at_the_root(client):
    body = client.get("/").text
    assert '<body data-base=""' in body
    assert 'href="/static/style.css"' in body


def test_web_url_must_be_http_or_https(client):
    """
    web_url is user-editable and lands in an href and a redirect target, so a
    script URL there would be clickable XSS.
    """
    for bad in [
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///c:/windows/win.ini",
        "//evil.example.com",
        "not a url",
    ]:
        response = client.patch("/api/printers/192.0.2.10", json={"web_url": bad})
        assert response.status_code == 400, f"{bad!r} was accepted"

    # The printer's real interface is still settable.
    ok = client.patch(
        "/api/printers/192.0.2.10", json={"web_url": "https://192.0.2.10:8443"}
    )
    assert ok.status_code == 200
    assert ok.json()["web_url"] == "https://192.0.2.10:8443"

    redirect = client.get("/open/192.0.2.10", follow_redirects=False)
    assert redirect.headers["location"] == "https://192.0.2.10:8443"

    # Put it back for any later test.
    client.patch("/api/printers/192.0.2.10", json={"web_url": "https://192.0.2.10"})


# -- colour tags ------------------------------------------------------------


def test_color_tag_can_be_set_and_cleared(client):
    response = client.patch("/api/printers/192.0.2.10", json={"color": "Blue"})
    assert response.status_code == 200
    assert response.json()["color"] == "blue"
    assert client.get("/api/printers/192.0.2.10").json()["color"] == "blue"

    # The detail page marks the saved swatch as the selected one.
    page = client.get("/printer/192.0.2.10").text
    assert 'value="blue" checked' in page

    cleared = client.patch("/api/printers/192.0.2.10", json={"color": ""})
    assert cleared.status_code == 200
    assert not cleared.json()["color"]


def test_color_tag_must_be_a_known_swatch(client):
    """It becomes a CSS class name, so free text is refused, not escaped."""
    for bad in ["chartreuse", "red; background:url(x)", "#ff0000", 3]:
        response = client.patch("/api/printers/192.0.2.10", json={"color": bad})
        assert response.status_code == 400, f"{bad!r} was accepted"


# -- exports ----------------------------------------------------------------


def test_dashboard_offers_exports_and_theme_toggle(client):
    body = client.get("/").text
    assert "/export/printers.csv" in body
    assert "/export/printers.pdf" in body
    assert "data-theme-toggle" in body
    assert "/static/theme.js" in body
    assert 'data-colors="red,' in body


def test_csv_export_includes_every_printer_and_its_notes(client):
    import csv
    import io

    client.patch(
        "/api/printers/192.0.2.99",
        json={"notes": "=HYPERLINK(\"http://x\") by the dock", "color": "teal"},
    )
    try:
        response = client.get("/export/printers.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        assert response.content.startswith(b"\xef\xbb\xbf")  # BOM, for Excel

        rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
        by_ip = {row["IP address"]: row for row in rows}
        assert set(by_ip) == {"192.0.2.10", "192.0.2.99"}
        zebra = by_ip["192.0.2.99"]
        assert zebra["Name"] == "Warehouse label printer"
        assert zebra["Color"] == "teal"
        assert zebra["Status"] == "Offline"
        # A note that starts like a formula is defused, not dropped.
        assert zebra["Notes"] == "'=HYPERLINK(\"http://x\") by the dock"
        assert by_ip["192.0.2.10"]["Page count"] == "184321"
    finally:
        client.patch("/api/printers/192.0.2.99", json={"notes": "", "color": ""})


def test_pdf_export_is_a_pdf(client):
    response = client.get("/export/printers.pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert "attachment" in response.headers["content-disposition"]
    assert response.content.startswith(b"%PDF-1.4")
    assert response.content.rstrip().endswith(b"%%EOF")
