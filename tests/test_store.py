"""Inventory persistence, user-edit preservation, and corruption handling."""

import json
import time

from app.store import PrinterStore


def _store(tmp_path, **kwargs):
    return PrinterStore(tmp_path / "printers.json", **kwargs)


def test_upsert_creates_then_updates(tmp_path):
    store = _store(tmp_path)
    record = store.upsert("10.0.0.5", model="HP M404", vendor="HP")
    assert record.ip == "10.0.0.5"
    assert record.source == "discovered"
    assert store.count() == 1

    store.upsert("10.0.0.5", serial_number="ABC123")
    again = store.get("10.0.0.5")
    assert again.model == "HP M404"  # preserved
    assert again.serial_number == "ABC123"
    assert store.count() == 1


def test_upsert_never_clears_a_value_with_none(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5", model="HP M404", location="Floor 1")
    store.upsert("10.0.0.5", model=None, location=None)
    record = store.get("10.0.0.5")
    assert record.model == "HP M404"
    assert record.location == "Floor 1"


def test_rediscovery_preserves_user_edits(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5", model="HP M404")
    store.set_fields("10.0.0.5", label="Reception printer", notes="Key at desk",
                     enabled=False)

    # A later sweep finds it again with fresh device facts.
    store.upsert("10.0.0.5", model="HP LaserJet Pro M404dn", vendor="HP")

    record = store.get("10.0.0.5")
    assert record.label == "Reception printer"
    assert record.notes == "Key at desk"
    assert record.enabled is False
    assert record.model == "HP LaserJet Pro M404dn"


def test_manual_source_survives_rediscovery(tmp_path):
    store = _store(tmp_path)
    store.add_manual("10.0.0.9", label="Label printer")
    store.upsert("10.0.0.9", model="Zebra ZT411")
    record = store.get("10.0.0.9")
    assert record.source == "manual"
    assert record.label == "Label printer"


def test_set_fields_ignores_non_editable_keys(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5", model="HP M404")
    store.set_fields("10.0.0.5", model="TAMPERED", health="ok", ip="1.2.3.4")
    record = store.get("10.0.0.5")
    assert record.model == "HP M404"
    assert record.ip == "10.0.0.5"


def test_display_name_precedence(tmp_path):
    store = _store(tmp_path)
    record = store.upsert("10.0.0.5")
    assert record.display_name == "10.0.0.5"          # falls back to the IP
    store.upsert("10.0.0.5", model="HP M404")
    assert store.get("10.0.0.5").display_name == "HP M404"
    store.upsert("10.0.0.5", hostname="prn-1")
    assert store.get("10.0.0.5").display_name == "prn-1"
    store.upsert("10.0.0.5", sys_name="PRN-RECEPTION")
    assert store.get("10.0.0.5").display_name == "PRN-RECEPTION"
    store.set_fields("10.0.0.5", label="Reception")
    assert store.get("10.0.0.5").display_name == "Reception"


def test_records_sort_numerically_by_address(tmp_path):
    store = _store(tmp_path)
    for ip in ["10.0.0.100", "10.0.0.9", "10.0.0.20", "10.0.0.1"]:
        store.upsert(ip)
    assert [r.ip for r in store.all()] == [
        "10.0.0.1", "10.0.0.9", "10.0.0.20", "10.0.0.100",
    ]


def test_record_poll_updates_identity_and_health(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5")
    store.record_poll("10.0.0.5", {
        "reachable": True, "health": "warning", "sys_name": "PRN-A",
        "model": "HP M404", "vendor": "HP", "serial_number": "S1",
        "sys_location": "Floor 3", "mac_address": "aa:bb:cc:dd:ee:ff",
    })
    record = store.get("10.0.0.5")
    assert record.online is True
    assert record.health == "warning"
    assert record.sys_name == "PRN-A"
    assert record.location == "Floor 3"
    assert record.mac_address == "aa:bb:cc:dd:ee:ff"
    assert record.consecutive_failures == 0
    assert record.last_seen is not None


def test_record_poll_counts_failures_and_keeps_last_known_identity(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5", model="HP M404")
    store.record_poll("10.0.0.5", {"reachable": True, "health": "ok"})
    seen = store.get("10.0.0.5").last_seen

    for _ in range(3):
        store.record_poll("10.0.0.5", {"reachable": False, "health": "offline"})

    record = store.get("10.0.0.5")
    assert record.online is False
    assert record.health == "offline"
    assert record.consecutive_failures == 3
    assert record.model == "HP M404"     # identity retained while offline
    assert record.last_seen == seen      # not bumped by a failed poll


def test_record_poll_defers_the_write_until_asked(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5")            # this one autosaves
    store.record_poll("10.0.0.5", {"reachable": True, "health": "ok"})
    on_disk = json.loads(store.path.read_text())["printers"][0]
    assert on_disk["health"] == "unknown"   # poll not flushed yet

    store.save(force=True)
    on_disk = json.loads(store.path.read_text())["printers"][0]
    assert on_disk["health"] == "ok"


def test_record_poll_can_save_immediately(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5")
    store.record_poll("10.0.0.5", {"reachable": True, "health": "ok"}, save=True)
    assert json.loads(store.path.read_text())["printers"][0]["health"] == "ok"


def test_round_trip_through_disk(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5", model="HP M404", vendor="HP")
    store.set_fields("10.0.0.5", label="Reception", notes="n")
    store.record_poll("10.0.0.5", {"reachable": True, "health": "ok",
                                   "page_count": 42}, save=True)

    reloaded = _store(tmp_path)
    reloaded.load()
    record = reloaded.get("10.0.0.5")
    assert record.label == "Reception"
    assert record.model == "HP M404"
    assert record.stats["page_count"] == 42
    assert record.health == "ok"


def test_load_of_a_missing_file_is_empty_not_an_error(tmp_path):
    store = _store(tmp_path)
    store.load()
    assert store.all() == []


def test_corrupt_file_is_quarantined_and_the_service_still_starts(tmp_path):
    path = tmp_path / "printers.json"
    path.write_text("{ this is not json")
    store = PrinterStore(path)
    store.load()
    assert store.all() == []
    quarantined = list(tmp_path.glob("printers.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{ this is not json"


def test_load_skips_unusable_entries_but_keeps_the_rest(tmp_path):
    path = tmp_path / "printers.json"
    path.write_text(json.dumps({"printers": [
        {"ip": "10.0.0.1", "model": "Good"},
        {"model": "no ip at all"},
        "not even an object",
        {"ip": "10.0.0.2", "unknown_future_field": 1},
    ]}))
    store = PrinterStore(path)
    store.load()
    assert sorted(r.ip for r in store.all()) == ["10.0.0.1", "10.0.0.2"]


def test_load_accepts_a_bare_list(tmp_path):
    path = tmp_path / "printers.json"
    path.write_text(json.dumps([{"ip": "10.0.0.1", "model": "Legacy format"}]))
    store = PrinterStore(path)
    store.load()
    assert store.get("10.0.0.1").model == "Legacy format"


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5")
    store.save(force=True)
    assert [p.name for p in tmp_path.iterdir()] == ["printers.json"]


def test_delete(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.5")
    assert store.delete("10.0.0.5") is True
    assert store.delete("10.0.0.5") is False
    assert store.count() == 0


def test_enabled_filters_disabled_printers(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.1")
    store.upsert("10.0.0.2")
    store.set_fields("10.0.0.2", enabled=False)
    assert [r.ip for r in store.enabled()] == ["10.0.0.1"]


def test_summary_excludes_disabled_from_health_counts(tmp_path):
    store = _store(tmp_path)
    store.upsert("10.0.0.1")
    store.upsert("10.0.0.2")
    store.upsert("10.0.0.3")
    store.record_poll("10.0.0.1", {"reachable": True, "health": "ok"})
    store.record_poll("10.0.0.2", {"reachable": True, "health": "critical"})
    store.record_poll("10.0.0.3", {"reachable": False, "health": "offline"})
    store.set_fields("10.0.0.3", enabled=False)

    summary = store.summary()
    assert summary["total"] == 3
    assert summary["enabled"] == 2
    assert summary["online"] == 2
    assert summary["by_health"]["ok"] == 1
    assert summary["by_health"]["critical"] == 1
    assert summary["by_health"]["offline"] == 0   # the disabled one
    assert summary["needs_attention"] == ["10.0.0.2"]


def test_concurrent_writes_do_not_lose_records(tmp_path):
    import threading

    store = _store(tmp_path, autosave=False)
    errors = []

    def worker(start):
        try:
            for index in range(start, start + 40):
                store.upsert(f"10.0.{index // 256}.{index % 256}")
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n * 40,)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert store.count() == 160
    store.save(force=True)
    assert len(json.loads(store.path.read_text())["printers"]) == 160
