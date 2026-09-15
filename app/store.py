"""
The printer registry: a thread-safe, JSON-backed list of known printers.

Discovery adds to it, the poller updates it, and the API reads it. Persisting
to JSON (rather than a database) keeps the IIS deployment to "copy the folder",
and the file doubles as an editable inventory.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1


@dataclass
class PrinterRecord:
    ip: str
    hostname: str | None = None
    label: str | None = None  # user-supplied friendly name, wins over the rest
    sys_name: str | None = None
    model: str | None = None
    vendor: str | None = None
    serial_number: str | None = None
    location: str | None = None
    mac_address: str | None = None
    web_url: str | None = None
    community: str = "public"
    source: str = "discovered"  # discovered | manual
    enabled: bool = True
    first_seen: float = field(default_factory=time.time)
    last_seen: float | None = None  # last time it answered SNMP
    last_polled: float | None = None  # last poll attempt, success or not
    online: bool = False
    health: str = "unknown"
    consecutive_failures: int = 0
    stats: dict[str, Any] | None = None
    notes: str | None = None

    @property
    def display_name(self) -> str:
        for candidate in (self.label, self.sys_name, self.hostname, self.model):
            if candidate:
                return candidate
        return self.ip

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["display_name"] = self.display_name
        return data


class PrinterStore:
    """
    In-memory registry with write-through JSON persistence.

    Every public method takes the lock, so callers can use it freely from the
    poller's worker threads and from request handlers.
    """

    def __init__(self, path: str | Path, autosave: bool = True) -> None:
        self.path = Path(path)
        self.autosave = autosave
        self._lock = threading.RLock()
        self._printers: dict[str, PrinterRecord] = {}
        self._dirty = False

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self._printers = {}
                return
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # A corrupt inventory should not stop the service from coming
                # up; keep the bad file aside so it can be inspected.
                self._quarantine_bad_file()
                self._printers = {}
                return

            entries: Iterable[dict[str, Any]]
            if isinstance(raw, dict):
                entries = raw.get("printers", [])
            elif isinstance(raw, list):
                entries = raw
            else:
                entries = []

            known = set(PrinterRecord.__dataclass_fields__)
            loaded: dict[str, PrinterRecord] = {}
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("ip"):
                    continue
                cleaned = {k: v for k, v in entry.items() if k in known}
                try:
                    record = PrinterRecord(**cleaned)
                except TypeError:
                    continue
                loaded[record.ip] = record
            self._printers = loaded

    def _quarantine_bad_file(self) -> None:
        try:
            backup = self.path.with_suffix(
                self.path.suffix + f".corrupt-{int(time.time())}"
            )
            self.path.replace(backup)
        except OSError:
            pass

    def save(self, force: bool = False) -> None:
        """Atomically write the inventory, so a crash cannot truncate it."""
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "schema_version": SCHEMA_VERSION,
                "saved_at": time.time(),
                "printers": [record.as_dict() for record in self._sorted()],
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(self.path.parent),
                prefix=self.path.name + ".",
                suffix=".tmp",
                delete=False,
            )
            temp_name = handle.name
            try:
                with handle:
                    json.dump(payload, handle, indent=2, default=str)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp_name, self.path)
                self._dirty = False
            except OSError:
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass
                raise

    def _touch(self) -> None:
        self._dirty = True
        if self.autosave:
            try:
                self.save()
            except OSError:
                # Losing a save is survivable; losing the process is not.
                pass

    # -- reads -------------------------------------------------------------

    def _sorted(self) -> list[PrinterRecord]:
        def key(record: PrinterRecord):
            try:
                octets = tuple(int(part) for part in record.ip.split("."))
            except ValueError:
                octets = (0,)
            return (len(octets) != 4, octets, record.ip)

        return sorted(self._printers.values(), key=key)

    def all(self) -> list[PrinterRecord]:
        with self._lock:
            return self._sorted()

    def get(self, ip: str) -> PrinterRecord | None:
        with self._lock:
            return self._printers.get(ip)

    def enabled(self) -> list[PrinterRecord]:
        with self._lock:
            return [r for r in self._sorted() if r.enabled]

    def count(self) -> int:
        with self._lock:
            return len(self._printers)

    # -- writes ------------------------------------------------------------

    def upsert(
        self,
        ip: str,
        /,
        source: str = "discovered",
        **fields: Any,
    ) -> PrinterRecord:
        """
        Create or update a printer.

        Fields explicitly set by the user (`label`, `notes`, `enabled`) are
        never overwritten by a rediscovery, and a record first added manually
        keeps `source == "manual"`.
        """
        with self._lock:
            record = self._printers.get(ip)
            if record is None:
                record = PrinterRecord(ip=ip, source=source)
                self._printers[ip] = record
            known = set(PrinterRecord.__dataclass_fields__)
            for key, value in fields.items():
                if key not in known or key == "ip":
                    continue
                if value is None:
                    continue
                setattr(record, key, value)
            self._touch()
            return record

    def add_manual(self, ip: str, label: str | None = None,
                   community: str | None = None) -> PrinterRecord:
        with self._lock:
            record = self._printers.get(ip)
            if record is None:
                record = PrinterRecord(ip=ip, source="manual")
                self._printers[ip] = record
            else:
                record.source = "manual"
            if label:
                record.label = label
            if community:
                record.community = community
            record.enabled = True
            self._touch()
            return record

    def delete(self, ip: str) -> bool:
        with self._lock:
            existed = self._printers.pop(ip, None) is not None
            if existed:
                self._touch()
            return existed

    def set_fields(self, ip: str, /, **fields: Any) -> PrinterRecord | None:
        """
        Apply user edits (label, notes, enabled, web_url, community).

        Anything else in `fields` is ignored, so a request body can be splatted
        in directly without letting a caller rewrite device facts or the key.
        """
        editable = {"label", "notes", "enabled", "web_url", "community"}
        with self._lock:
            record = self._printers.get(ip)
            if record is None:
                return None
            for key, value in fields.items():
                if key in editable and value is not None:
                    setattr(record, key, value)
            self._touch()
            return record

    def record_poll(
        self,
        ip: str,
        stats: dict[str, Any],
        low_threshold: int = 20,
        save: bool = False,
    ) -> PrinterRecord | None:
        """
        Store a poll result and refresh the derived identity fields.

        This only marks the inventory dirty by default: a poll cycle touches
        every printer, and writing the whole file once per printer would mean
        one rewrite per device per cycle. The poller saves once when the cycle
        finishes; pass save=True for a one-off poll.
        """
        with self._lock:
            record = self._printers.get(ip)
            if record is None:
                return None
            now = time.time()
            record.last_polled = now
            record.stats = stats
            record.online = bool(stats.get("reachable"))
            record.health = stats.get("health") or (
                "ok" if record.online else "offline"
            )
            if record.online:
                record.last_seen = now
                record.consecutive_failures = 0
                # Refresh identity from the device, but never clobber a value
                # we have with a blank one.
                for field_name, stat_key in (
                    ("sys_name", "sys_name"),
                    ("model", "model"),
                    ("vendor", "vendor"),
                    ("serial_number", "serial_number"),
                    ("location", "sys_location"),
                    ("mac_address", "mac_address"),
                ):
                    value = stats.get(stat_key)
                    if value:
                        setattr(record, field_name, value)
            else:
                record.consecutive_failures += 1
            self._dirty = True
            if save:
                try:
                    self.save()
                except OSError:
                    pass
            return record

    # -- aggregate ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        with self._lock:
            records = list(self._printers.values())
        counts = {"ok": 0, "warning": 0, "critical": 0, "offline": 0, "unknown": 0}
        for record in records:
            key = record.health if record.health in counts else "unknown"
            if not record.enabled:
                continue
            counts[key] += 1
        return {
            "total": len(records),
            "enabled": sum(1 for r in records if r.enabled),
            "online": sum(1 for r in records if r.online and r.enabled),
            "by_health": counts,
            "needs_attention": [
                r.ip for r in records
                if r.enabled and r.health in {"critical", "warning", "offline"}
            ],
        }
