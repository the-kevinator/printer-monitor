"""
The subnet list: what the discovery sweep is allowed to scan.

This used to live in config.json. It now lives in `data/subnets.json` so it can
be edited from the dashboard: config.json seeds the file once, on the first
start that finds no file, and after that this store is the only authority.
`data/` is the one directory the IIS app pool identity already needs write
access to, so nothing new has to be granted.

Entries are kept rather than deleted-and-retyped: each one carries a label, an
enabled flag and its origin, so a range can be parked without losing the note
explaining what it is.
"""

from __future__ import annotations

import ipaddress
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Literal

from .jsonfile import quarantine, read_json, write_atomic

SCHEMA_VERSION = 1

# A sweep bigger than this is almost certainly a mistyped mask, so refuse it
# rather than hammer the network for hours. Mirrors config.MAX_SWEEP_HOSTS;
# imported there to keep a single number.
from .config import MAX_SWEEP_HOSTS  # noqa: E402

Kind = Literal["include", "exclude"]

#: Origins, in the order the seeder prefers them. "manual" means typed into
#: the dashboard, which is everything added after the first start.
SOURCES = {"manual", "config", "env", "autodetected"}


@dataclass
class SubnetEntry:
    cidr: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    label: str | None = None
    enabled: bool = True
    source: str = "manual"
    added_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class SubnetError(ValueError):
    """A subnet the user typed cannot be used, with a reason they can act on."""


def _is_scannable(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    """
    True when the range is not routable on the public internet.

    `is_global` is the exact predicate wanted here: it is False for RFC1918,
    CGNAT, loopback, link-local and the documentation ranges, and True for
    anything reachable from outside. It is evaluated on the network rather than
    per address, so a block that straddles the boundary -- 203.0.112.0/23, half
    documentation range and half not -- counts as global and is refused whole.
    """
    return not network.is_global


def normalise(
    text: str,
    kind: Kind = "include",
    allow_public: bool = False,
) -> tuple[str, int]:
    """
    Validate one user-supplied range and return (canonical CIDR, host count).

    Raises SubnetError with a message meant to be shown in the dashboard. A
    bare address is accepted and canonicalised to a /32, which is how someone
    naturally types "just this one printer" or "never touch the gateway".
    """
    cleaned = (text or "").strip()
    if not cleaned:
        raise SubnetError("Enter a network range, for example 10.10.20.0/24.")
    try:
        network = ipaddress.ip_network(cleaned, strict=False)
    except ValueError as exc:
        raise SubnetError(f"{cleaned!r} is not a valid address or CIDR range: {exc}")

    hosts = _host_count(network)
    if hosts > MAX_SWEEP_HOSTS:
        raise SubnetError(
            f"{network} covers {network.num_addresses:,} addresses, above the "
            f"{MAX_SWEEP_HOSTS:,} limit. Split it into narrower ranges "
            f"(a /19 is the widest allowed)."
        )
    # Excluding a public range only ever narrows the sweep, so the guard is on
    # includes alone -- it exists to stop the dashboard being used to scan the
    # internet, not to stop someone protecting an address.
    if kind == "include" and not allow_public and not _is_scannable(network):
        raise SubnetError(
            f"{network} is routable on the public internet. Printer Monitor "
            f"sweeps only internal ranges by default -- 10/8, 172.16/12, "
            f"192.168/16, 100.64/10 and link-local. Set "
            f"\"allow_public_subnets\": true in config.json if this really is "
            f"an internal network."
        )
    return str(network), hosts


def _host_count(network: ipaddress.IPv4Network | ipaddress.IPv6Network) -> int:
    # /31 and /32 (and the IPv6 equivalents) have no reserved network or
    # broadcast address, so every address in them is a host.
    if network.num_addresses <= 2:
        return network.num_addresses
    return network.num_addresses - 2


def describe(cidr: str, kind: Kind = "include", allow_public: bool = False) -> dict[str, Any]:
    """Validation result for an entry already in the store, for the UI."""
    try:
        canonical, hosts = normalise(cidr, kind=kind, allow_public=allow_public)
    except SubnetError as exc:
        return {"valid": False, "problem": str(exc), "host_count": 0}
    return {"valid": True, "problem": None, "host_count": hosts,
            "canonical": canonical}


class SubnetStore:
    """
    Thread-safe, JSON-backed include/exclude lists with write-through saves.

    Request handlers mutate it while the poller thread reads it, so every
    public method takes the lock and reads hand back copies.
    """

    def __init__(self, path: str | Path, allow_public: bool = False) -> None:
        self.path = Path(path)
        self.allow_public = allow_public
        self._lock = threading.RLock()
        self._entries: dict[str, list[SubnetEntry]] = {"include": [], "exclude": []}
        self.seeded_from: str | None = None

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        with self._lock:
            raw = read_json(self.path)
            if raw is None:
                if self.path.is_file():
                    # Present but unreadable: keep it for inspection rather
                    # than silently overwriting whatever the admin typed.
                    quarantine(self.path)
                self._entries = {"include": [], "exclude": []}
                return
            self._entries = {
                "include": _parse(raw.get("subnets") if isinstance(raw, dict) else raw),
                "exclude": _parse(raw.get("exclusions") if isinstance(raw, dict) else []),
            }
            if isinstance(raw, dict):
                self.seeded_from = raw.get("seeded_from")

    def save(self) -> None:
        with self._lock:
            write_atomic(self.path, {
                "schema_version": SCHEMA_VERSION,
                "saved_at": time.time(),
                "seeded_from": self.seeded_from,
                "subnets": [e.as_dict() for e in self._entries["include"]],
                "exclusions": [e.as_dict() for e in self._entries["exclude"]],
            })

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def _touch(self) -> None:
        try:
            self.save()
        except OSError:
            # A dashboard edit that cannot be persisted must still say so, so
            # unlike the inventory this one is not swallowed.
            raise

    # -- reads -------------------------------------------------------------

    def list(self, kind: Kind = "include") -> list[SubnetEntry]:
        with self._lock:
            return list(self._entries[kind])

    def get(self, entry_id: str) -> tuple[Kind, SubnetEntry] | None:
        with self._lock:
            for kind in ("include", "exclude"):
                for entry in self._entries[kind]:
                    if entry.id == entry_id:
                        return kind, entry  # type: ignore[return-value]
            return None

    def active(self, kind: Kind = "include") -> list[str]:
        """Enabled, valid CIDRs -- what the sweep should actually use."""
        out: list[str] = []
        for entry in self.list(kind):
            if not entry.enabled:
                continue
            try:
                canonical, _ = normalise(
                    entry.cidr, kind=kind, allow_public=self.allow_public
                )
            except SubnetError:
                continue
            out.append(canonical)
        return out

    def sweep_target_count(self) -> int:
        """
        How many addresses a full sweep would actually probe.

        Summing the ranges would overstate it wherever an exclusion falls
        inside one, and that number is shown next to a button that starts real
        network traffic, so it is worth computing exactly. Addresses are
        counted as integers rather than strings: a handful of /19s is tens of
        thousands of them and the set arithmetic stays cheap.
        """
        included: set[int] = set()
        for cidr in self.active("include"):
            included.update(_expand_ints(cidr, hosts_only=True))
        if not included:
            return 0
        for cidr in self.active("exclude"):
            # Exclusions cover every address in the block, network and
            # broadcast included -- "never touch 10.10.20.240/28" means the
            # whole block. This matches Settings.sweep_targets().
            included.difference_update(_expand_ints(cidr, hosts_only=False))
        return len(included)

    def payload(self) -> dict[str, Any]:
        """Everything the dashboard's subnet panel needs, in one shape."""
        def rows(kind: Kind) -> list[dict[str, Any]]:
            result = []
            for entry in self.list(kind):
                row = entry.as_dict()
                row.update(describe(entry.cidr, kind=kind,
                                    allow_public=self.allow_public))
                result.append(row)
            return result

        include = rows("include")
        return {
            "subnets": include,
            "exclusions": rows("exclude"),
            "sweep_target_count": self.sweep_target_count(),
            "allow_public_subnets": self.allow_public,
            "max_hosts": MAX_SWEEP_HOSTS,
            "seeded_from": self.seeded_from,
            "path": str(self.path),
        }

    # -- writes ------------------------------------------------------------

    def add(
        self,
        cidr: str,
        kind: Kind = "include",
        label: str | None = None,
        source: str = "manual",
    ) -> SubnetEntry:
        canonical, _ = normalise(cidr, kind=kind, allow_public=self.allow_public)
        with self._lock:
            for existing in self._entries[kind]:
                if existing.cidr == canonical:
                    raise SubnetError(
                        f"{canonical} is already in the list"
                        + (" (currently switched off)" if not existing.enabled else "")
                        + "."
                    )
            entry = SubnetEntry(
                cidr=canonical,
                label=(label or "").strip() or None,
                source=source if source in SOURCES else "manual",
            )
            self._entries[kind].append(entry)
            self._touch()
            return entry

    def update(
        self, entry_id: str, label: Any = None, enabled: Any = None
    ) -> SubnetEntry | None:
        """Only the label and the enabled flag are editable; the range is not.

        Changing a range in place would silently repoint a sweep while keeping
        the note that describes the old one, so the UI deletes and re-adds.
        """
        with self._lock:
            found = self.get(entry_id)
            if found is None:
                return None
            _kind, entry = found
            if label is not None:
                entry.label = str(label).strip() or None
            if enabled is not None:
                entry.enabled = bool(enabled)
            self._touch()
            return entry

    def delete(self, entry_id: str) -> SubnetEntry | None:
        with self._lock:
            found = self.get(entry_id)
            if found is None:
                return None
            kind, entry = found
            self._entries[kind] = [
                e for e in self._entries[kind] if e.id != entry_id
            ]
            self._touch()
            return entry

    def seed(
        self,
        subnets: Iterable[str],
        exclusions: Iterable[str] = (),
        source: str = "config",
    ) -> int:
        """
        Populate an empty store from config.json / env / autodetection.

        Runs only on the first start that finds no `subnets.json`. Invalid
        seeds are kept but left switched off, so a bad value in config.json
        surfaces in the dashboard instead of vanishing.
        """
        added = 0
        with self._lock:
            for kind, values in (("include", subnets), ("exclude", exclusions)):
                for value in values:
                    text = str(value).strip()
                    if not text:
                        continue
                    try:
                        canonical, _ = normalise(
                            text, kind=kind, allow_public=self.allow_public  # type: ignore[arg-type]
                        )
                    except SubnetError:
                        # Keep it visible but inert.
                        origin = {
                            "env": "from PRINTER_MONITOR_SUBNETS",
                            "autodetected": "autodetected",
                        }.get(source, "from config.json")
                        self._entries[kind].append(SubnetEntry(  # type: ignore[index]
                            cidr=text, source=source, enabled=False,
                            label=f"{origin} -- not usable",
                        ))
                        added += 1
                        continue
                    if any(e.cidr == canonical for e in self._entries[kind]):  # type: ignore[index]
                        continue
                    self._entries[kind].append(  # type: ignore[index]
                        SubnetEntry(cidr=canonical, source=source)
                    )
                    added += 1
            self.seeded_from = source
            self._touch()
        return added


def _expand_ints(cidr: str, hosts_only: bool) -> Iterable[int]:
    """
    The addresses of a range, as integers.

    `hosts_only` drops the network and broadcast addresses, which is right for
    something being scanned and wrong for something being protected.
    """
    try:
        network = ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return ()
    if network.num_addresses > MAX_SWEEP_HOSTS:
        return ()
    # /31 and /32 have no reserved addresses, so every address is a host.
    if not hosts_only or network.num_addresses <= 2:
        return (int(a) for a in network)
    return (int(a) for a in network.hosts())


def _parse(entries: Any) -> list[SubnetEntry]:
    if not isinstance(entries, list):
        return []
    known = set(SubnetEntry.__dataclass_fields__)
    out: list[SubnetEntry] = []
    seen_ids: set[str] = set()
    for raw in entries:
        if isinstance(raw, str):
            raw = {"cidr": raw}
        if not isinstance(raw, dict) or not raw.get("cidr"):
            continue
        cleaned = {k: v for k, v in raw.items() if k in known}
        try:
            entry = SubnetEntry(**cleaned)
        except TypeError:
            continue
        # A hand-edited file can repeat or omit ids; the UI addresses entries
        # by id, so they have to be unique.
        if entry.id in seen_ids:
            entry.id = uuid.uuid4().hex[:12]
        seen_ids.add(entry.id)
        out.append(entry)
    return out
