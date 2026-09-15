"""
Network discovery.

Sweeps the configured subnets with SNMP and keeps anything that exposes the
Printer MIB. SNMP is the probe because it is also the thing that yields toner
levels -- a host that answers it is a host we can actually monitor.
"""

from __future__ import annotations

import concurrent.futures
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .config import Settings
from .printer_mib import (
    HR_DEVICE_DESCR,
    SYS_DESCR,
    SYS_NAME,
    _as_text,
    guess_model,
    guess_vendor,
    looks_like_printer,
)
from .snmp import SnmpClient, SnmpError


@dataclass
class DiscoveryResult:
    ip: str
    hostname: str | None
    sys_name: str | None
    sys_descr: str | None
    model: str | None
    vendor: str | None
    community: str
    web_url: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "hostname": self.hostname,
            "sys_name": self.sys_name,
            "sys_descr": self.sys_descr,
            "model": self.model,
            "vendor": self.vendor,
            "community": self.community,
            "web_url": self.web_url,
        }


@dataclass
class DiscoveryProgress:
    """Live state of a sweep, safe to read from a request handler."""

    running: bool = False
    started_at: float | None = None
    finished_at: float | None = None
    total: int = 0
    scanned: int = 0
    found: int = 0
    subnets: list[str] = field(default_factory=list)
    error: str | None = None
    last_found: list[dict[str, Any]] = field(default_factory=list)
    cancelled: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            elapsed = None
            if self.started_at:
                end = self.finished_at or time.time()
                elapsed = round(end - self.started_at, 1)
            percent = 0
            if self.total:
                percent = min(100, round(self.scanned * 100 / self.total))
            return {
                "running": self.running,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "elapsed_seconds": elapsed,
                "total": self.total,
                "scanned": self.scanned,
                "found": self.found,
                "percent": percent,
                "subnets": list(self.subnets),
                "error": self.error,
                "cancelled": self.cancelled,
                "last_found": list(self.last_found),
            }


def reverse_dns(ip: str, timeout: float = 0.5) -> str | None:
    """
    Reverse-resolve an address, guarding against a slow resolver.

    socket.gethostbyaddr has no timeout parameter, so the default resolver
    timeout applies; keep this off the critical path where possible.
    """
    previous = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(timeout)
        hostname, _aliases, _addrs = socket.gethostbyaddr(ip)
    except (OSError, UnicodeError):
        return None
    finally:
        socket.setdefaulttimeout(previous)
    if not hostname:
        return None
    # Office DNS usually hands back an FQDN; the short name reads better.
    return hostname.split(".")[0] if hostname.count(".") else hostname


def probe_web_interface(
    ip: str,
    ports: Sequence[int] = (443, 80, 8443, 8080),
    timeout: float = 0.6,
) -> str | None:
    """
    Find the printer's embedded web server by TCP-connecting to likely ports.

    Returns a URL, or None if nothing accepted a connection. Only the TCP
    handshake is done -- no HTTP request and no TLS negotiation, which keeps it
    fast and avoids tripping on the self-signed certificates printers ship.
    """
    https_ports = {443, 8443}
    for port in ports:
        try:
            with socket.create_connection((ip, port), timeout=timeout):
                pass
        except OSError:
            continue
        scheme = "https" if port in https_ports else "http"
        host = f"[{ip}]" if ":" in ip else ip
        default_port = (scheme == "https" and port == 443) or (
            scheme == "http" and port == 80
        )
        return f"{scheme}://{host}" if default_port else f"{scheme}://{host}:{port}"
    return None


def probe_host(
    ip: str,
    settings: Settings,
    resolve_dns: bool = True,
) -> DiscoveryResult | None:
    """
    Test a single address. Returns a result only if it is really a printer.

    Each configured community string is tried in turn, so a mixed fleet works
    without per-device configuration. An SNMPv2c agent silently drops a request
    carrying the wrong community, which is indistinguishable from an absent
    host, so silence cannot end the loop early -- only a reply can. With more
    than one community configured that costs one timeout per community on every
    empty address, which is the price of the fallback working at all.
    """
    for community in settings.community_list:
        client = SnmpClient(
            ip,
            community=community,
            port=settings.snmp_port,
            timeout=settings.snmp_timeout,
            retries=settings.snmp_retries,
        )
        try:
            is_printer, identity = looks_like_printer(client)
        except SnmpError:
            continue
        finally:
            client.close()

        if not identity:
            # Silence: either nothing is at this address or this community is
            # wrong. Try the next one before giving up.
            continue
        if not is_printer:
            # It answered, so the community is right; it simply has no Printer
            # MIB -- a switch, server or UPS. No other community will change
            # that, so stop here.
            return None

        sys_descr = _as_text(identity.get(SYS_DESCR))
        sys_name = _as_text(identity.get(SYS_NAME))

        # One extra round trip, only for confirmed printers, to get a clean
        # model name. Without it the card shows the sysDescr banner until the
        # first full poll replaces it.
        device_descr = None
        client = SnmpClient(
            ip,
            community=community,
            port=settings.snmp_port,
            timeout=settings.snmp_timeout,
            retries=0,
        )
        try:
            bindings = client.get_next(HR_DEVICE_DESCR)
            for binding in bindings:
                if not binding.is_exception:
                    device_descr = _as_text(binding.value)
                    break
        except SnmpError:
            pass
        finally:
            client.close()

        model = guess_model(sys_descr, device_descr, None)
        return DiscoveryResult(
            ip=ip,
            hostname=reverse_dns(ip) if resolve_dns else None,
            sys_name=sys_name,
            sys_descr=sys_descr,
            model=model,
            vendor=guess_vendor(sys_descr, device_descr, sys_name, model),
            community=community,
            web_url=probe_web_interface(
                ip,
                ports=settings.web_probe_ports,
                timeout=settings.web_probe_timeout,
            ),
        )
    return None


class DiscoveryRunner:
    """
    Runs sweeps in a thread pool, one at a time.

    `start()` is non-blocking and returns immediately; poll `progress` for
    status. A second start while one is running is rejected.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.progress = DiscoveryProgress()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())

    def cancel(self) -> bool:
        if not self.is_running:
            return False
        self._cancel.set()
        return True

    def start(
        self,
        on_found: Callable[[DiscoveryResult], None] | None = None,
        on_complete: Callable[[list[DiscoveryResult]], None] | None = None,
        targets: Sequence[str] | None = None,
        subnets: Sequence[str] | None = None,
    ) -> bool:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return False
            self._cancel.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(on_found, on_complete, targets, subnets),
                name="printer-discovery",
                daemon=True,
            )
            self._thread.start()
            return True

    def run_blocking(
        self,
        on_found: Callable[[DiscoveryResult], None] | None = None,
        targets: Sequence[str] | None = None,
        subnets: Sequence[str] | None = None,
    ) -> list[DiscoveryResult]:
        """Synchronous sweep, used by tests and the CLI."""
        return self._run(on_found, None, targets, subnets)

    def _run(
        self,
        on_found: Callable[[DiscoveryResult], None] | None,
        on_complete: Callable[[list[DiscoveryResult]], None] | None,
        targets: Sequence[str] | None,
        subnets: Sequence[str] | None = None,
    ) -> list[DiscoveryResult]:
        progress = self.progress
        addresses = list(targets) if targets is not None else self.settings.sweep_targets()
        found: list[DiscoveryResult] = []

        with progress._lock:
            progress.running = True
            progress.started_at = time.time()
            progress.finished_at = None
            progress.total = len(addresses)
            progress.scanned = 0
            progress.found = 0
            progress.error = None
            progress.cancelled = False
            # `subnets` is what to *display*; a scan of one range from the
            # dashboard covers a subset of the configured list.
            progress.subnets = list(
                subnets if subnets is not None else self.settings.subnets
            )
            progress.last_found = []

        problems = self.settings.validate()
        if problems and not addresses:
            with progress._lock:
                progress.running = False
                progress.finished_at = time.time()
                progress.error = "; ".join(problems)
            return found

        workers = max(1, min(self.settings.discovery_workers, 256))
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers, thread_name_prefix="discover"
            ) as pool:
                futures = {
                    pool.submit(self._probe_guarded, ip): ip for ip in addresses
                }
                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    with progress._lock:
                        progress.scanned += 1
                        if result is not None:
                            progress.found += 1
                            progress.last_found.append(result.as_dict())
                    if result is not None:
                        found.append(result)
                        if on_found is not None:
                            try:
                                on_found(result)
                            except Exception:
                                pass
                    if self._cancel.is_set():
                        for pending in futures:
                            pending.cancel()
                        with progress._lock:
                            progress.cancelled = True
                        break
        except Exception as exc:  # pragma: no cover - defensive
            with progress._lock:
                progress.error = f"{type(exc).__name__}: {exc}"
        finally:
            with progress._lock:
                progress.running = False
                progress.finished_at = time.time()

        found.sort(key=_ip_sort_key)
        if on_complete is not None:
            try:
                on_complete(found)
            except Exception:
                pass
        return found

    def _probe_guarded(self, ip: str) -> DiscoveryResult | None:
        if self._cancel.is_set():
            return None
        try:
            return probe_host(ip, self.settings)
        except Exception:
            # One unreachable or misbehaving host must not abort the sweep.
            return None


def _ip_sort_key(result: DiscoveryResult):
    try:
        return (0, tuple(int(part) for part in result.ip.split(".")))
    except ValueError:
        return (1, result.ip)
