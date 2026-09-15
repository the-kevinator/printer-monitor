"""
Minimal, dependency-free SNMP v2c client.

Implements just enough BER/ASN.1 to talk SNMPv2c GET / GETNEXT / GETBULK over
UDP. This exists instead of pysnmp so the whole app installs on a locked-down
Windows/IIS box with nothing but `pip install fastapi uvicorn jinja2`.

Only read operations are supported -- there is deliberately no SET.
"""

from __future__ import annotations

import os
import random
import socket
import threading
from dataclasses import dataclass
from typing import Iterable, Iterator, Sequence

# ---------------------------------------------------------------------------
# BER tags
# ---------------------------------------------------------------------------

TAG_INTEGER = 0x02
TAG_OCTET_STRING = 0x04
TAG_NULL = 0x05
TAG_OID = 0x06
TAG_SEQUENCE = 0x30

# SNMP application-specific types
TAG_IP_ADDRESS = 0x40
TAG_COUNTER32 = 0x41
TAG_GAUGE32 = 0x42
TAG_TIMETICKS = 0x43
TAG_OPAQUE = 0x44
TAG_COUNTER64 = 0x46

# SNMPv2 context-specific "exception" values that appear in place of a value
TAG_NO_SUCH_OBJECT = 0x80
TAG_NO_SUCH_INSTANCE = 0x81
TAG_END_OF_MIB_VIEW = 0x82

# PDU types
PDU_GET = 0xA0
PDU_GET_NEXT = 0xA1
PDU_RESPONSE = 0xA2
PDU_GET_BULK = 0xA5

ERROR_STATUS = {
    0: "noError",
    1: "tooBig",
    2: "noSuchName",
    3: "badValue",
    4: "readOnly",
    5: "genErr",
    6: "noAccess",
    7: "wrongType",
    8: "wrongLength",
    9: "wrongEncoding",
    10: "wrongValue",
    11: "noCreation",
    12: "inconsistentValue",
    13: "resourceUnavailable",
    14: "commitFailed",
    15: "undoFailed",
    16: "authorizationError",
    17: "notWritable",
    18: "inconsistentName",
}


class SnmpError(Exception):
    """Any SNMP-level failure (encoding, decoding, or an error-status reply)."""


class SnmpTimeout(SnmpError):
    """The agent did not answer within the timeout, across all retries."""


class _Sentinel:
    """Marker for the SNMPv2 no-such-object / end-of-mib pseudo-values."""

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{self.name}>"

    def __bool__(self) -> bool:
        return False


NO_SUCH_OBJECT = _Sentinel("noSuchObject")
NO_SUCH_INSTANCE = _Sentinel("noSuchInstance")
END_OF_MIB_VIEW = _Sentinel("endOfMibView")
NULL = _Sentinel("null")

_EXCEPTION_TAGS = {
    TAG_NO_SUCH_OBJECT: NO_SUCH_OBJECT,
    TAG_NO_SUCH_INSTANCE: NO_SUCH_INSTANCE,
    TAG_END_OF_MIB_VIEW: END_OF_MIB_VIEW,
}


# ---------------------------------------------------------------------------
# OID helpers
# ---------------------------------------------------------------------------

OID = tuple[int, ...]


def parse_oid(value: str | Sequence[int]) -> OID:
    """Accept "1.3.6.1.2.1.1.1.0" or an int sequence and return a tuple."""
    if isinstance(value, str):
        text = value.strip().lstrip(".")
        if not text:
            raise SnmpError("empty OID")
        try:
            return tuple(int(part) for part in text.split("."))
        except ValueError as exc:
            raise SnmpError(f"invalid OID {value!r}") from exc
    return tuple(int(part) for part in value)


def oid_str(oid: Sequence[int]) -> str:
    return ".".join(str(part) for part in oid)


def oid_starts_with(oid: Sequence[int], prefix: Sequence[int]) -> bool:
    return tuple(oid[: len(prefix)]) == tuple(prefix)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def _encode_length(length: int) -> bytes:
    if length < 0x80:
        return bytes((length,))
    raw = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes((0x80 | len(raw),)) + raw


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes((tag,)) + _encode_length(len(body)) + body


def _encode_integer(value: int) -> bytes:
    # Shortest two's-complement representation, per BER.
    size = 1
    while True:
        try:
            return _tlv(TAG_INTEGER, value.to_bytes(size, "big", signed=True))
        except OverflowError:
            size += 1


def _encode_base128(value: int) -> bytes:
    if value == 0:
        return b"\x00"
    if value < 0:
        raise SnmpError("OID sub-identifiers cannot be negative")
    chunks = bytearray()
    while value:
        chunks.insert(0, value & 0x7F)
        value >>= 7
    for index in range(len(chunks) - 1):
        chunks[index] |= 0x80
    return bytes(chunks)


def _encode_oid(oid: Sequence[int]) -> bytes:
    if len(oid) < 2:
        raise SnmpError(f"OID too short to encode: {oid_str(oid)}")
    body = _encode_base128(oid[0] * 40 + oid[1])
    for sub in oid[2:]:
        body += _encode_base128(sub)
    return _tlv(TAG_OID, body)


def _encode_varbind(oid: Sequence[int]) -> bytes:
    return _tlv(TAG_SEQUENCE, _encode_oid(oid) + _tlv(TAG_NULL, b""))


def _encode_message(
    community: str,
    pdu_type: int,
    request_id: int,
    oids: Sequence[Sequence[int]],
    non_repeaters: int = 0,
    max_repetitions: int = 0,
) -> bytes:
    varbinds = b"".join(_encode_varbind(oid) for oid in oids)
    if pdu_type == PDU_GET_BULK:
        field_a, field_b = non_repeaters, max_repetitions
    else:
        field_a, field_b = 0, 0
    pdu = _tlv(
        pdu_type,
        _encode_integer(request_id)
        + _encode_integer(field_a)
        + _encode_integer(field_b)
        + _tlv(TAG_SEQUENCE, varbinds),
    )
    return _tlv(
        TAG_SEQUENCE,
        _encode_integer(1)  # version 1 == SNMPv2c
        + _tlv(TAG_OCTET_STRING, community.encode("utf-8"))
        + pdu,
    )


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def _parse_tlv(data: bytes, offset: int) -> tuple[int, bytes, int]:
    if offset >= len(data):
        raise SnmpError("truncated response: expected a tag")
    tag = data[offset]
    offset += 1
    if offset >= len(data):
        raise SnmpError("truncated response: expected a length")
    first = data[offset]
    offset += 1
    if first < 0x80:
        length = first
    else:
        count = first & 0x7F
        if count == 0:
            raise SnmpError("indefinite BER lengths are not supported")
        if offset + count > len(data):
            raise SnmpError("truncated response: truncated long-form length")
        length = int.from_bytes(data[offset : offset + count], "big")
        offset += count
    end = offset + length
    if end > len(data):
        raise SnmpError("truncated response: value runs past end of packet")
    return tag, data[offset:end], end


def _decode_integer(body: bytes) -> int:
    if not body:
        return 0
    return int.from_bytes(body, "big", signed=True)


def _decode_unsigned(body: bytes) -> int:
    if not body:
        return 0
    return int.from_bytes(body, "big", signed=False)


def _decode_oid(body: bytes) -> OID:
    subs: list[int] = []
    current = 0
    started = False
    for byte in body:
        current = (current << 7) | (byte & 0x7F)
        started = True
        if not byte & 0x80:
            subs.append(current)
            current = 0
            started = False
    if started:
        raise SnmpError("malformed OID encoding")
    if not subs:
        return ()
    first = subs[0]
    if first < 40:
        head: OID = (0, first)
    elif first < 80:
        head = (1, first - 40)
    else:
        head = (2, first - 80)
    return head + tuple(subs[1:])


class SnmpString(str):
    """
    The text reading of an OCTET STRING, with the original bytes on `.raw`.

    Printers use OCTET STRING for both human-readable fields and packed binary
    (MAC addresses, BITS values such as hrPrinterDetectedErrorState). Decoding
    straight to text would corrupt the binary ones -- NUL bytes are significant
    there but are padding in the text ones. Subclassing `str` keeps the common
    case ergonomic (it compares and formats as the cleaned text) while binary
    callers reach for `.raw`.
    """

    __slots__ = ("raw",)

    def __new__(cls, raw: bytes) -> "SnmpString":
        text = None
        for encoding in ("utf-8", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if text is None:  # pragma: no cover - latin-1 always succeeds
            text = raw.decode("latin-1", errors="replace")
        # Agents commonly pad with NULs and whitespace; strip for display only.
        cleaned = text.replace("\x00", "").strip()
        obj = super().__new__(cls, cleaned)
        obj.raw = bytes(raw)
        return obj


def raw_bytes(value: object) -> bytes | None:
    """Recover the on-the-wire bytes of an OCTET STRING value, if available."""
    raw = getattr(value, "raw", None)
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    return None


def _decode_octet_string(body: bytes) -> SnmpString:
    return SnmpString(body)


def _decode_value(tag: int, body: bytes):
    if tag in _EXCEPTION_TAGS:
        return _EXCEPTION_TAGS[tag]
    if tag == TAG_INTEGER:
        return _decode_integer(body)
    if tag in (TAG_COUNTER32, TAG_GAUGE32, TAG_TIMETICKS, TAG_COUNTER64):
        return _decode_unsigned(body)
    if tag == TAG_OCTET_STRING:
        return _decode_octet_string(body)
    if tag == TAG_OID:
        return _decode_oid(body)
    if tag == TAG_NULL:
        return NULL
    if tag == TAG_IP_ADDRESS:
        if len(body) == 4:
            return ".".join(str(part) for part in body)
        return _decode_octet_string(body)
    if tag == TAG_OPAQUE:
        return body
    # Unknown type: hand back the raw bytes rather than exploding.
    return body


@dataclass(frozen=True)
class VarBind:
    oid: OID
    value: object

    @property
    def oid_text(self) -> str:
        return oid_str(self.oid)

    @property
    def is_exception(self) -> bool:
        return isinstance(self.value, _Sentinel)


def _decode_message(data: bytes) -> tuple[int, int, int, list[VarBind]]:
    """Return (request_id, error_status, error_index, varbinds)."""
    tag, envelope, _ = _parse_tlv(data, 0)
    if tag != TAG_SEQUENCE:
        raise SnmpError(f"expected an outer SEQUENCE, got tag 0x{tag:02x}")

    offset = 0
    tag, body, offset = _parse_tlv(envelope, offset)
    if tag != TAG_INTEGER:
        raise SnmpError("expected the version field")
    tag, body, offset = _parse_tlv(envelope, offset)
    if tag != TAG_OCTET_STRING:
        raise SnmpError("expected the community field")

    pdu_tag, pdu, _ = _parse_tlv(envelope, offset)
    if pdu_tag != PDU_RESPONSE:
        raise SnmpError(f"expected a GetResponse PDU, got tag 0x{pdu_tag:02x}")

    offset = 0
    tag, body, offset = _parse_tlv(pdu, offset)
    request_id = _decode_integer(body)
    tag, body, offset = _parse_tlv(pdu, offset)
    error_status = _decode_integer(body)
    tag, body, offset = _parse_tlv(pdu, offset)
    error_index = _decode_integer(body)

    tag, varbind_list, _ = _parse_tlv(pdu, offset)
    if tag != TAG_SEQUENCE:
        raise SnmpError("expected the variable-bindings SEQUENCE")

    varbinds: list[VarBind] = []
    position = 0
    while position < len(varbind_list):
        tag, entry, position = _parse_tlv(varbind_list, position)
        if tag != TAG_SEQUENCE:
            raise SnmpError("expected a variable-binding SEQUENCE")
        inner = 0
        tag, body, inner = _parse_tlv(entry, inner)
        if tag != TAG_OID:
            raise SnmpError("expected an OID in the variable binding")
        oid = _decode_oid(body)
        tag, body, inner = _parse_tlv(entry, inner)
        varbinds.append(VarBind(oid, _decode_value(tag, body)))

    return request_id, error_status, error_index, varbinds


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_request_counter = threading.Lock()
_next_request_id = random.randint(1, 0x3FFFFFFF)


def _allocate_request_id() -> int:
    global _next_request_id
    with _request_counter:
        _next_request_id = (_next_request_id + 1) & 0x7FFFFFFF
        if _next_request_id == 0:
            _next_request_id = 1
        return _next_request_id


class SnmpClient:
    """
    A blocking SNMPv2c client for a single agent.

    Instances are cheap and not thread-safe; create one per host per worker.
    Use as a context manager, or call `close()`, to release the socket.
    """

    def __init__(
        self,
        host: str,
        community: str = "public",
        port: int = 161,
        timeout: float = 1.5,
        retries: int = 1,
    ) -> None:
        self.host = host
        self.community = community
        self.port = port
        self.timeout = timeout
        self.retries = max(0, retries)
        self._socket: socket.socket | None = None

    # -- plumbing ----------------------------------------------------------

    def __enter__(self) -> "SnmpClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _get_socket(self) -> socket.socket:
        if self._socket is None:
            family = socket.AF_INET
            try:
                info = socket.getaddrinfo(
                    self.host, self.port, 0, socket.SOCK_DGRAM
                )
                family = info[0][0]
            except socket.gaierror as exc:
                raise SnmpError(f"cannot resolve {self.host}: {exc}") from exc
            sock = socket.socket(family, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            self._socket = sock
        return self._socket

    def _exchange(
        self,
        pdu_type: int,
        oids: Sequence[Sequence[int]],
        non_repeaters: int = 0,
        max_repetitions: int = 0,
    ) -> list[VarBind]:
        request_id = _allocate_request_id()
        packet = _encode_message(
            self.community,
            pdu_type,
            request_id,
            oids,
            non_repeaters=non_repeaters,
            max_repetitions=max_repetitions,
        )
        sock = self._get_socket()
        address = (self.host, self.port)
        last_error: Exception | None = None

        for _ in range(self.retries + 1):
            try:
                sock.sendto(packet, address)
            except OSError as exc:
                last_error = exc
                continue

            # Loop so that a stale datagram from an earlier, timed-out request
            # does not get mistaken for this one's answer.
            deadline_socket_timeout = self.timeout
            while True:
                try:
                    sock.settimeout(deadline_socket_timeout)
                    data, _peer = sock.recvfrom(65535)
                except socket.timeout as exc:
                    last_error = exc
                    break
                except OSError as exc:
                    last_error = exc
                    break
                try:
                    reply_id, status, index, varbinds = _decode_message(data)
                except SnmpError as exc:
                    last_error = exc
                    continue
                if reply_id != request_id:
                    continue
                if status != 0:
                    name = ERROR_STATUS.get(status, f"unknown({status})")
                    raise SnmpError(
                        f"{self.host} returned error-status {name} "
                        f"at index {index}"
                    )
                return varbinds

        if isinstance(last_error, socket.timeout) or last_error is None:
            raise SnmpTimeout(
                f"no SNMP response from {self.host}:{self.port} "
                f"after {self.retries + 1} attempt(s)"
            )
        raise SnmpError(f"SNMP exchange with {self.host} failed: {last_error}")

    # -- operations --------------------------------------------------------

    def get(self, *oids: str | Sequence[int]) -> list[VarBind]:
        parsed = [parse_oid(oid) for oid in oids]
        if not parsed:
            return []
        return self._exchange(PDU_GET, parsed)

    def get_one(self, oid: str | Sequence[int]):
        """Return a single value, or None if the agent has no such object."""
        result = self.get(oid)
        if not result:
            return None
        binding = result[0]
        if binding.is_exception:
            return None
        return binding.value

    def get_many(self, oids: Sequence[str]) -> dict[str, object]:
        """
        GET several OIDs at once, returning {requested_oid: value}.

        Missing objects are omitted. Falls back to single GETs if the agent
        rejects the combined request (some printers cap varbinds per PDU).
        """
        if not oids:
            return {}
        try:
            bindings = self.get(*oids)
        except SnmpError:
            out: dict[str, object] = {}
            for oid in oids:
                try:
                    value = self.get_one(oid)
                except SnmpError:
                    continue
                if value is not None:
                    out[oid] = value
            return out

        out = {}
        for requested, binding in zip(oids, bindings):
            if not binding.is_exception:
                out[requested] = binding.value
        return out

    def get_next(self, *oids: str | Sequence[int]) -> list[VarBind]:
        parsed = [parse_oid(oid) for oid in oids]
        if not parsed:
            return []
        return self._exchange(PDU_GET_NEXT, parsed)

    def get_bulk(
        self, oid: str | Sequence[int], max_repetitions: int = 25
    ) -> list[VarBind]:
        return self._exchange(
            PDU_GET_BULK,
            [parse_oid(oid)],
            non_repeaters=0,
            max_repetitions=max(1, max_repetitions),
        )

    def walk(
        self,
        base: str | Sequence[int],
        max_rows: int = 5000,
        use_bulk: bool = True,
    ) -> Iterator[VarBind]:
        """
        Walk everything under `base`, yielding VarBinds in OID order.

        Tries GETBULK first (far fewer round trips) and transparently falls
        back to GETNEXT for agents that only speak the older request.
        """
        prefix = parse_oid(base)
        current: OID = prefix
        produced = 0
        bulk = use_bulk

        while produced < max_rows:
            if bulk:
                try:
                    bindings = self.get_bulk(current, max_repetitions=25)
                except SnmpTimeout:
                    raise
                except SnmpError:
                    bulk = False
                    continue
            else:
                bindings = self.get_next(current)

            if not bindings:
                return

            advanced = False
            for binding in bindings:
                if binding.value is END_OF_MIB_VIEW:
                    return
                if not oid_starts_with(binding.oid, prefix):
                    return
                if binding.oid <= current and advanced is False and produced:
                    # Agent is not making progress; stop rather than spin.
                    return
                current = binding.oid
                advanced = True
                if binding.is_exception:
                    continue
                yield binding
                produced += 1
                if produced >= max_rows:
                    return

            if not advanced:
                return

    def walk_table(
        self, base: str | Sequence[int], max_rows: int = 5000
    ) -> dict[str, object]:
        """Walk a subtree into {full_oid_string: value}."""
        prefix = parse_oid(base)
        rows: dict[str, object] = {}
        for binding in self.walk(prefix, max_rows=max_rows):
            rows[oid_str(binding.oid)] = binding.value
        return rows

    def walk_column(
        self, column: str | Sequence[int], max_rows: int = 2000
    ) -> dict[str, object]:
        """
        Walk one table column, returning {row_index_suffix: value}.

        The key is whatever follows the column OID, e.g. "1.1" for
        prtMarkerSuppliesLevel.1.1.
        """
        prefix = parse_oid(column)
        out: dict[str, object] = {}
        for binding in self.walk(prefix, max_rows=max_rows):
            suffix = binding.oid[len(prefix) :]
            out[oid_str(suffix)] = binding.value
        return out
