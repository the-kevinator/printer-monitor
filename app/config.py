"""
Configuration loading.

Order of precedence, lowest to highest: built-in defaults, then the JSON config
file, then environment variables. The config file path comes from
PRINTER_MONITOR_CONFIG, else ./config.json next to the app.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent

# A sweep bigger than this is almost certainly a misconfigured mask, so refuse
# rather than hammer the network for hours.
MAX_SWEEP_HOSTS = 8192


@dataclass
class Settings:
    # -- what to scan ----------------------------------------------------
    subnets: list[str] = field(default_factory=list)
    exclude_ips: list[str] = field(default_factory=list)
    manual_printers: list[str] = field(default_factory=list)

    # -- SNMP ------------------------------------------------------------
    community: str = "public"
    communities: list[str] = field(default_factory=list)
    snmp_port: int = 161
    snmp_timeout: float = 1.5
    snmp_retries: int = 1

    # -- scheduling ------------------------------------------------------
    discovery_workers: int = 64
    poll_workers: int = 16
    poll_interval_seconds: int = 300
    discovery_interval_seconds: int = 3600
    auto_discover_on_start: bool = True
    # Set false to serve the dashboard without any background SNMP traffic
    # (a read-only viewer, or a test run).
    poller_enabled: bool = True

    # -- presentation ----------------------------------------------------
    low_toner_threshold: int = 20
    web_probe_ports: list[int] = field(default_factory=lambda: [443, 80, 8443, 8080])
    web_probe_timeout: float = 0.6

    # -- storage ---------------------------------------------------------
    data_dir: str = str(APP_ROOT / "data")

    # -- populated at load time -----------------------------------------
    config_path: str | None = None
    subnets_autodetected: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def community_list(self) -> list[str]:
        """Every community to try, primary first, de-duplicated."""
        ordered = [self.community, *self.communities]
        seen: set[str] = set()
        out: list[str] = []
        for item in ordered:
            if item and item not in seen:
                seen.add(item)
                out.append(item)
        return out or ["public"]

    @property
    def printers_file(self) -> Path:
        return Path(self.data_dir) / "printers.json"

    def sweep_targets(self) -> list[str]:
        """Expand the configured subnets into host addresses to probe."""
        excluded = _expand_exclusions(self.exclude_ips)
        targets: list[str] = []
        seen: set[str] = set()
        for entry in self.subnets:
            for address in _expand_subnet(entry):
                if address in seen or address in excluded:
                    continue
                seen.add(address)
                targets.append(address)
        return targets

    def validate(self) -> list[str]:
        """Return human-readable problems; an empty list means it is usable."""
        problems: list[str] = []
        for entry in self.subnets:
            try:
                network = ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError as exc:
                problems.append(f"subnet {entry!r} is not valid: {exc}")
                continue
            if network.num_addresses > MAX_SWEEP_HOSTS:
                problems.append(
                    f"subnet {entry!r} covers {network.num_addresses} addresses, "
                    f"which is above the {MAX_SWEEP_HOSTS} limit -- "
                    f"use narrower ranges"
                )
        for entry in self.exclude_ips:
            try:
                ipaddress.ip_network(entry.strip(), strict=False)
            except ValueError as exc:
                problems.append(f"exclude_ips entry {entry!r} is not valid: {exc}")
        if not 0 <= self.low_toner_threshold <= 100:
            problems.append("low_toner_threshold must be between 0 and 100")
        if self.poll_interval_seconds < 30:
            problems.append("poll_interval_seconds must be at least 30")
        if self.snmp_timeout <= 0:
            problems.append("snmp_timeout must be positive")
        if self.discovery_workers < 1:
            problems.append("discovery_workers must be at least 1")
        return problems


def _expand_subnet(entry: str) -> list[str]:
    """Expand '10.0.0.0/24' or a bare '10.0.0.5' into host addresses."""
    text = entry.strip()
    if not text:
        return []
    try:
        network = ipaddress.ip_network(text, strict=False)
    except ValueError:
        return []
    if network.num_addresses > MAX_SWEEP_HOSTS:
        return []
    if network.num_addresses <= 2:
        # /31, /32 and the IPv6 equivalents have no reserved host addresses.
        return [str(address) for address in network]
    return [str(address) for address in network.hosts()]


def _expand_exclusions(entries: list[str]) -> set[str]:
    excluded: set[str] = set()
    for entry in entries:
        text = entry.strip()
        if not text:
            continue
        try:
            network = ipaddress.ip_network(text, strict=False)
        except ValueError:
            continue
        if network.num_addresses > MAX_SWEEP_HOSTS:
            continue
        excluded.update(str(address) for address in network)
    return excluded


def primary_ipv4() -> str | None:
    """
    Best-effort local IPv4 of the interface that reaches the default gateway.

    Uses a connectionless UDP socket, so nothing is actually sent.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("198.51.100.1", 9))  # TEST-NET-2, never routed
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def detect_local_subnets() -> list[str]:
    """
    Guess the local /24 so a fresh install can discover something immediately.

    A /24 guess is deliberate: it is the common case on office LANs and it
    keeps the first sweep small. Anything else should be configured explicitly.
    """
    address = primary_ipv4()
    if not address:
        return []
    try:
        interface = ipaddress.ip_interface(f"{address}/24")
    except ValueError:
        return []
    return [str(interface.network)]


_ENV_PREFIX = "PRINTER_MONITOR_"

_ENV_FIELDS: dict[str, str] = {
    "SUBNETS": "subnets",
    "EXCLUDE_IPS": "exclude_ips",
    "MANUAL_PRINTERS": "manual_printers",
    "COMMUNITY": "community",
    "COMMUNITIES": "communities",
    "SNMP_PORT": "snmp_port",
    "SNMP_TIMEOUT": "snmp_timeout",
    "SNMP_RETRIES": "snmp_retries",
    "DISCOVERY_WORKERS": "discovery_workers",
    "POLL_WORKERS": "poll_workers",
    "POLL_INTERVAL_SECONDS": "poll_interval_seconds",
    "DISCOVERY_INTERVAL_SECONDS": "discovery_interval_seconds",
    "AUTO_DISCOVER_ON_START": "auto_discover_on_start",
    "POLLER_ENABLED": "poller_enabled",
    "LOW_TONER_THRESHOLD": "low_toner_threshold",
    "WEB_PROBE_PORTS": "web_probe_ports",
    "WEB_PROBE_TIMEOUT": "web_probe_timeout",
    "DATA_DIR": "data_dir",
}

_LIST_FIELDS = {
    "subnets", "exclude_ips", "manual_printers", "communities", "web_probe_ports",
}
_INT_FIELDS = {
    "snmp_port", "snmp_retries", "discovery_workers", "poll_workers",
    "poll_interval_seconds", "discovery_interval_seconds", "low_toner_threshold",
}
_FLOAT_FIELDS = {"snmp_timeout", "web_probe_timeout"}
_BOOL_FIELDS = {"auto_discover_on_start", "poller_enabled"}


def _coerce(name: str, value: Any) -> Any:
    if name in _LIST_FIELDS:
        if isinstance(value, str):
            items = [part.strip() for part in value.replace(";", ",").split(",")]
            value = [item for item in items if item]
        elif not isinstance(value, list):
            value = [value]
        if name == "web_probe_ports":
            return [int(item) for item in value]
        return [str(item) for item in value]
    if name in _BOOL_FIELDS:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)
    if name in _INT_FIELDS:
        return int(value)
    if name in _FLOAT_FIELDS:
        return float(value)
    return str(value)


def config_file_path() -> Path:
    override = os.environ.get(f"{_ENV_PREFIX}CONFIG")
    if override:
        return Path(override)
    return APP_ROOT / "config.json"


def load_settings(path: str | Path | None = None) -> Settings:
    """Load settings, falling back to an autodetected local /24 if unset."""
    settings = Settings()
    config_path = Path(path) if path else config_file_path()

    if config_path.is_file():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read config {config_path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"config {config_path} must contain a JSON object")
        known = {f.name for f in Settings.__dataclass_fields__.values()}
        for key, value in raw.items():
            if key in {"config_path", "subnets_autodetected"}:
                continue
            if key in known and value is not None:
                setattr(settings, key, _coerce(key, value))
        settings.config_path = str(config_path)

    for env_suffix, field_name in _ENV_FIELDS.items():
        env_value = os.environ.get(_ENV_PREFIX + env_suffix)
        if env_value is not None and env_value != "":
            setattr(settings, field_name, _coerce(field_name, env_value))

    if not settings.subnets:
        detected = detect_local_subnets()
        if detected:
            settings.subnets = detected
            settings.subnets_autodetected = True

    return settings
