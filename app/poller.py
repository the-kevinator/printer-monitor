"""
Background polling.

A daemon thread wakes on a fixed interval, fans every enabled printer out
across a small thread pool, and writes the results into the store. SNMP is
blocking, so threads (rather than asyncio) keep this simple and let one slow
printer's timeout run concurrently with everything else.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
import time
from typing import Any

from .config import Settings
from .discovery import DiscoveryResult, DiscoveryRunner, probe_web_interface, reverse_dns
from .printer_mib import read_printer
from .snmp import SnmpClient
from .store import PrinterRecord, PrinterStore

log = logging.getLogger("printer_monitor.poller")


def poll_one(record: PrinterRecord, settings: Settings) -> dict[str, Any]:
    """
    Poll a single printer and return its stats as a plain dict.

    The record's own community is tried first; the configured list is the
    fallback, which lets a printer that was re-keyed recover on its own.
    """
    communities = [record.community] + [
        c for c in settings.community_list if c != record.community
    ]
    last: dict[str, Any] | None = None
    for community in communities:
        client = SnmpClient(
            record.ip,
            community=community,
            port=settings.snmp_port,
            timeout=settings.snmp_timeout,
            retries=settings.snmp_retries,
        )
        try:
            stats = read_printer(
                client, low_threshold=settings.low_toner_threshold
            )
        finally:
            client.close()
        payload = stats.as_dict(low_threshold=settings.low_toner_threshold)
        payload["community"] = community
        payload["polled_at"] = time.time()
        if stats.reachable:
            return payload
        last = payload
    return last or {
        "reachable": False,
        "error": "no community string succeeded",
        "health": "offline",
        "polled_at": time.time(),
        "supplies": [],
        "trays": [],
        "errors": [],
    }


class PollerService:
    """Owns the poll loop and the periodic rediscovery."""

    def __init__(
        self,
        settings: Settings,
        store: PrinterStore,
        discovery: DiscoveryRunner | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.discovery = discovery or DiscoveryRunner(settings)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_poll_started: float | None = None
        self.last_poll_finished: float | None = None
        self.last_poll_count: int = 0
        self.last_discovery_at: float | None = None
        self.poll_in_progress: bool = False

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="printer-poller", daemon=True
        )
        self._thread.start()
        log.info("poller started (interval=%ss)", self.settings.poll_interval_seconds)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        try:
            self.store.save(force=True)
        except OSError:
            pass
        log.info("poller stopped")

    def request_poll_now(self) -> None:
        """Ask the loop to run a cycle immediately."""
        self._wake.set()

    # -- the loop ----------------------------------------------------------

    def _loop(self) -> None:
        if self.settings.auto_discover_on_start and self.store.count() == 0:
            self.run_discovery()
        # Always take one reading straight away so the dashboard is not blank.
        self.poll_all()

        while not self._stop.is_set():
            interval = max(30, self.settings.poll_interval_seconds)
            woken = self._wake.wait(timeout=interval)
            self._wake.clear()
            if self._stop.is_set():
                break

            self.poll_all()

            if not woken and self._should_rediscover():
                self.run_discovery()

    def _should_rediscover(self) -> bool:
        every = self.settings.discovery_interval_seconds
        if every <= 0:
            return False
        if self.discovery.is_running:
            return False
        if self.last_discovery_at is None:
            return True
        return (time.time() - self.last_discovery_at) >= every

    # -- work --------------------------------------------------------------

    def poll_all(self) -> int:
        records = self.store.enabled()
        self.last_poll_started = time.time()
        self.poll_in_progress = True
        polled = 0
        try:
            if not records:
                return 0
            workers = max(1, min(self.settings.poll_workers, 64, len(records)))
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="poll"
            ) as pool:
                futures = {
                    pool.submit(self._poll_guarded, record): record
                    for record in records
                }
                for future in concurrent.futures.as_completed(futures):
                    record = futures[future]
                    stats = future.result()
                    if stats is None:
                        continue
                    self.store.record_poll(
                        record.ip,
                        stats,
                        low_threshold=self.settings.low_toner_threshold,
                    )
                    polled += 1
        finally:
            self.poll_in_progress = False
            self.last_poll_finished = time.time()
            self.last_poll_count = polled
            try:
                self.store.save(force=True)
            except OSError:
                pass
        return polled

    def _poll_guarded(self, record: PrinterRecord) -> dict[str, Any] | None:
        try:
            return poll_one(record, self.settings)
        except Exception as exc:
            log.warning("poll of %s failed: %s", record.ip, exc)
            return {
                "reachable": False,
                "error": f"{type(exc).__name__}: {exc}",
                "health": "offline",
                "polled_at": time.time(),
                "supplies": [],
                "trays": [],
                "errors": [],
            }

    def poll_single(self, ip: str) -> dict[str, Any] | None:
        """Poll one printer on demand (used by the refresh button)."""
        record = self.store.get(ip)
        if record is None:
            return None
        stats = self._poll_guarded(record)
        if stats is None:
            return None
        self.store.record_poll(
            ip,
            stats,
            low_threshold=self.settings.low_toner_threshold,
            save=True,
        )
        return stats

    # -- discovery ---------------------------------------------------------

    def _absorb(self, result: DiscoveryResult) -> None:
        """Fold a discovery hit into the store without losing user edits."""
        self.store.upsert(
            result.ip,
            source="discovered",
            hostname=result.hostname,
            sys_name=result.sys_name,
            model=result.model,
            vendor=result.vendor,
            community=result.community,
            web_url=result.web_url,
        )

    def run_discovery(self) -> list[DiscoveryResult]:
        """Blocking sweep; folds results into the store as they arrive."""
        self._ensure_manual_printers()
        found = self.discovery.run_blocking(on_found=self._absorb)
        self.last_discovery_at = time.time()
        try:
            self.store.save(force=True)
        except OSError:
            pass
        if found:
            # New printers have no stats yet; fill them in promptly.
            self.request_poll_now()
        return found

    def start_discovery(self) -> bool:
        """Non-blocking sweep, for the API."""
        self._ensure_manual_printers()

        def done(found: list[DiscoveryResult]) -> None:
            self.last_discovery_at = time.time()
            try:
                self.store.save(force=True)
            except OSError:
                pass
            self.request_poll_now()

        return self.discovery.start(on_found=self._absorb, on_complete=done)

    def _ensure_manual_printers(self) -> None:
        """Make sure anything listed in config is present in the store."""
        for entry in self.settings.manual_printers:
            ip = entry.strip()
            if ip and self.store.get(ip) is None:
                self.store.add_manual(ip)

    def enrich_record(self, ip: str) -> PrinterRecord | None:
        """Fill in hostname / web URL for a printer added by hand."""
        record = self.store.get(ip)
        if record is None:
            return None
        updates: dict[str, Any] = {}
        if not record.hostname:
            hostname = reverse_dns(ip)
            if hostname:
                updates["hostname"] = hostname
        if not record.web_url:
            url = probe_web_interface(
                ip,
                ports=self.settings.web_probe_ports,
                timeout=self.settings.web_probe_timeout,
            )
            if url:
                updates["web_url"] = url
        if updates:
            return self.store.upsert(ip, source=record.source, **updates)
        return record

    # -- introspection -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "poll_in_progress": self.poll_in_progress,
            "last_poll_started": self.last_poll_started,
            "last_poll_finished": self.last_poll_finished,
            "last_poll_count": self.last_poll_count,
            "poll_interval_seconds": self.settings.poll_interval_seconds,
            "last_discovery_at": self.last_discovery_at,
            "discovery_interval_seconds": self.settings.discovery_interval_seconds,
            "discovery": self.discovery.progress.snapshot(),
        }
