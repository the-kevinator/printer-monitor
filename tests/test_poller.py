"""
Poller and discovery tests against mock SNMP agents on loopback.

Linux routes all of 127.0.0.0/8 to the loopback interface, so each fake printer
can have its own address on a shared high port (161 would need root). That lets
the real sweep and poll code paths run unchanged.
"""

import socket

import pytest

from app.config import Settings
from app.discovery import DiscoveryRunner, probe_host, probe_web_interface
from app.poller import PollerService, poll_one
from app.store import PrinterStore
from tests.fake_printers import COLOUR_LASER, MONO_LASER, NETWORK_SWITCH
from tests.mock_agent import MockAgent

PORT = 17161


def _agent_on(address: str, mib: dict, community: str = "public") -> MockAgent:
    agent = MockAgent(mib, community=community)
    agent._sock.close()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((address, PORT))
    agent._sock = sock
    agent.port = PORT
    return agent.start()


@pytest.fixture
def fleet():
    """Three printers and one non-printer, on 127.0.0.2-.5."""
    agents = [
        _agent_on("127.0.0.2", COLOUR_LASER),
        _agent_on("127.0.0.3", MONO_LASER),
        _agent_on("127.0.0.4", COLOUR_LASER, community="secret-string"),
        _agent_on("127.0.0.5", NETWORK_SWITCH),
    ]
    try:
        yield agents
    finally:
        for agent in agents:
            agent.stop()


def _settings(tmp_path, **kwargs):
    base = dict(
        subnets=["127.0.0.0/29"],
        snmp_port=PORT,
        snmp_timeout=0.6,
        snmp_retries=1,
        discovery_workers=8,
        poll_workers=4,
        web_probe_ports=[],          # nothing is listening on HTTP here
        web_probe_timeout=0.05,
        data_dir=str(tmp_path),
        poller_enabled=False,
        auto_discover_on_start=False,
        discovery_interval_seconds=0,
    )
    base.update(kwargs)
    return Settings(**base)


# -- discovery --------------------------------------------------------------


def test_sweep_finds_printers_and_skips_non_printers(fleet, tmp_path):
    settings = _settings(tmp_path)
    found = DiscoveryRunner(settings).run_blocking()
    addresses = sorted(result.ip for result in found)

    # .4 uses a community we have not configured, and .5 is a switch.
    assert addresses == ["127.0.0.2", "127.0.0.3"]

    by_ip = {result.ip: result for result in found}
    assert by_ip["127.0.0.2"].sys_name == "PRN-FLOOR2-COLOR"
    assert by_ip["127.0.0.2"].model == "HP Color LaserJet Enterprise M553"
    assert by_ip["127.0.0.2"].vendor == "HP"
    assert by_ip["127.0.0.3"].vendor == "Brother"


def test_sweep_tries_every_configured_community(fleet, tmp_path):
    settings = _settings(tmp_path, communities=["secret-string"])
    found = DiscoveryRunner(settings).run_blocking()
    by_ip = {result.ip: result for result in found}
    assert "127.0.0.4" in by_ip
    assert by_ip["127.0.0.4"].community == "secret-string"
    # The default community still wins where it works.
    assert by_ip["127.0.0.2"].community == "public"


def test_progress_is_reported(fleet, tmp_path):
    settings = _settings(tmp_path)
    runner = DiscoveryRunner(settings)
    runner.run_blocking()
    progress = runner.progress.snapshot()
    assert progress["running"] is False
    assert progress["total"] == 6          # 127.0.0.1 - .6
    assert progress["scanned"] == 6
    assert progress["found"] == 2
    assert progress["percent"] == 100
    assert progress["error"] is None
    assert progress["elapsed_seconds"] is not None


def test_exclusions_are_honoured(fleet, tmp_path):
    settings = _settings(tmp_path, exclude_ips=["127.0.0.2"])
    found = DiscoveryRunner(settings).run_blocking()
    assert [r.ip for r in found] == ["127.0.0.3"]


def test_probe_host_returns_none_for_a_non_printer(fleet, tmp_path):
    assert probe_host("127.0.0.5", _settings(tmp_path)) is None


def test_probe_host_returns_none_for_a_dead_address(tmp_path):
    assert probe_host("127.0.0.9", _settings(tmp_path)) is None


def test_web_probe_finds_a_listening_port():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        url = probe_web_interface("127.0.0.1", ports=[port], timeout=0.5)
        assert url == f"http://127.0.0.1:{port}"
    finally:
        listener.close()

    assert probe_web_interface("127.0.0.1", ports=[port], timeout=0.2) is None


def test_web_probe_labels_tls_ports_https():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 8443))
    listener.listen(1)
    try:
        assert probe_web_interface("127.0.0.1", ports=[8443], timeout=0.5) \
            == "https://127.0.0.1:8443"
    finally:
        listener.close()


# -- polling ----------------------------------------------------------------


def test_poll_one_reads_supplies(fleet, tmp_path):
    settings = _settings(tmp_path)
    store = PrinterStore(tmp_path / "printers.json")
    record = store.upsert("127.0.0.2")
    stats = poll_one(record, settings)

    assert stats["reachable"] is True
    assert stats["health"] == "critical"      # yellow is empty
    assert stats["page_count"] == 184321
    assert stats["community"] == "public"
    assert stats["lowest_toner_percent"] == 0
    assert len(stats["supplies"]) == 6


def test_poll_one_falls_back_to_another_community(fleet, tmp_path):
    settings = _settings(tmp_path, communities=["secret-string"])
    store = PrinterStore(tmp_path / "printers.json")
    # The stored community is stale -- the printer was re-keyed.
    record = store.upsert("127.0.0.4", community="public")
    stats = poll_one(record, settings)
    assert stats["reachable"] is True
    assert stats["community"] == "secret-string"


def test_poll_one_reports_unreachable_without_raising(tmp_path):
    settings = _settings(tmp_path)
    store = PrinterStore(tmp_path / "printers.json")
    record = store.upsert("127.0.0.9")
    stats = poll_one(record, settings)
    assert stats["reachable"] is False
    assert stats["health"] == "offline"
    assert stats["error"]


def test_poll_all_updates_every_enabled_printer(fleet, tmp_path):
    settings = _settings(tmp_path)
    store = PrinterStore(tmp_path / "printers.json")
    store.upsert("127.0.0.2")
    store.upsert("127.0.0.3")
    store.upsert("127.0.0.9")               # nothing there
    store.upsert("127.0.0.5")               # the switch
    store.set_fields("127.0.0.5", enabled=False)

    poller = PollerService(settings, store, DiscoveryRunner(settings))
    assert poller.poll_all() == 3            # the disabled one is skipped

    assert store.get("127.0.0.2").health == "critical"
    assert store.get("127.0.0.3").online is True
    assert store.get("127.0.0.9").online is False
    assert store.get("127.0.0.9").consecutive_failures == 1
    # Disabled printers keep their previous state.
    assert store.get("127.0.0.5").last_polled is None

    # One flush at the end of the cycle, not one per printer.
    import json
    saved = json.loads((tmp_path / "printers.json").read_text())
    healths = {p["ip"]: p["health"] for p in saved["printers"]}
    assert healths["127.0.0.2"] == "critical"


def test_run_discovery_folds_results_into_the_store(fleet, tmp_path):
    settings = _settings(tmp_path)
    store = PrinterStore(tmp_path / "printers.json")
    poller = PollerService(settings, store, DiscoveryRunner(settings))

    found = poller.run_discovery()
    assert len(found) == 2
    assert sorted(r.ip for r in store.all()) == ["127.0.0.2", "127.0.0.3"]
    assert store.get("127.0.0.2").source == "discovered"
    assert poller.last_discovery_at is not None


def test_run_discovery_keeps_manual_printers_from_config(fleet, tmp_path):
    settings = _settings(tmp_path, manual_printers=["10.9.9.9"])
    store = PrinterStore(tmp_path / "printers.json")
    poller = PollerService(settings, store, DiscoveryRunner(settings))
    poller.run_discovery()

    record = store.get("10.9.9.9")
    assert record is not None
    assert record.source == "manual"


def test_poll_single(fleet, tmp_path):
    settings = _settings(tmp_path)
    store = PrinterStore(tmp_path / "printers.json")
    store.upsert("127.0.0.2")
    poller = PollerService(settings, store, DiscoveryRunner(settings))

    stats = poller.poll_single("127.0.0.2")
    assert stats["reachable"] is True
    assert store.get("127.0.0.2").health == "critical"
    assert poller.poll_single("10.1.2.3") is None    # unknown printer


def test_status_snapshot(fleet, tmp_path):
    settings = _settings(tmp_path)
    store = PrinterStore(tmp_path / "printers.json")
    poller = PollerService(settings, store, DiscoveryRunner(settings))
    store.upsert("127.0.0.2")
    poller.poll_all()

    status = poller.status()
    assert status["running"] is False        # never started as a thread
    assert status["last_poll_count"] == 1
    assert status["poll_interval_seconds"] == settings.poll_interval_seconds
    assert "discovery" in status
