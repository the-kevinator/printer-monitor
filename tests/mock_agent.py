"""
A tiny in-process SNMPv2c agent used by the test suite.

It serves a dict of {oid_tuple: value} over UDP so the client's GET / GETNEXT /
GETBULK and walk logic can be exercised without touching real hardware.
Encoding here is written independently of app.snmp's encoder where practical,
but it shares the tag constants.
"""

from __future__ import annotations

import socket
import threading

from app.snmp import (
    PDU_GET,
    PDU_GET_BULK,
    PDU_GET_NEXT,
    PDU_RESPONSE,
    TAG_COUNTER32,
    TAG_INTEGER,
    TAG_OCTET_STRING,
    TAG_OID,
    TAG_SEQUENCE,
    _decode_integer,
    _decode_oid,
    _encode_base128,
    _encode_integer,
    _parse_tlv,
    _tlv,
)


def _encode_oid_value(oid):
    body = _encode_base128(oid[0] * 40 + oid[1])
    for sub in oid[2:]:
        body += _encode_base128(sub)
    return _tlv(TAG_OID, body)


def _encode_value(value):
    if isinstance(value, bool):
        return _encode_integer(int(value))
    if isinstance(value, int):
        return _encode_integer(value)
    if isinstance(value, str):
        return _tlv(TAG_OCTET_STRING, value.encode("utf-8"))
    if isinstance(value, bytes):
        return _tlv(TAG_OCTET_STRING, value)
    if isinstance(value, tuple):
        return _encode_oid_value(value)
    if isinstance(value, Counter32):
        return _tlv(TAG_COUNTER32, value.raw())
    raise TypeError(f"mock agent cannot encode {value!r}")


class Counter32:
    def __init__(self, value: int) -> None:
        self.value = value

    def raw(self) -> bytes:
        return self.value.to_bytes(4, "big")


class MockAgent:
    """Serves a static MIB on 127.0.0.1 on an ephemeral UDP port."""

    def __init__(self, mib: dict, community: str = "public", drop_first: int = 0):
        self.mib = {k: v for k, v in mib.items()}
        self.sorted_oids = sorted(self.mib)
        self.community = community
        self.drop_first = drop_first
        self.requests_seen = 0
        self.bulk_supported = True
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "MockAgent":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- request handling -------------------------------------------------

    def _next_oid(self, oid):
        for candidate in self.sorted_oids:
            if candidate > oid:
                return candidate
        return None

    def _serve(self):
        self._sock.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data, peer = self._sock.recvfrom(65535)
            except (socket.timeout, OSError):
                continue
            self.requests_seen += 1
            if self.requests_seen <= self.drop_first:
                continue
            try:
                reply = self._build_reply(data)
            except Exception:
                continue
            if reply is not None:
                try:
                    self._sock.sendto(reply, peer)
                except OSError:
                    pass

    def _build_reply(self, data: bytes):
        _tag, envelope, _ = _parse_tlv(data, 0)
        offset = 0
        _t, _version, offset = _parse_tlv(envelope, offset)
        _t, community, offset = _parse_tlv(envelope, offset)
        if community.decode() != self.community:
            return None
        pdu_tag, pdu, _ = _parse_tlv(envelope, offset)

        inner = 0
        _t, body, inner = _parse_tlv(pdu, inner)
        request_id = _decode_integer(body)
        _t, body, inner = _parse_tlv(pdu, inner)
        field_a = _decode_integer(body)
        _t, body, inner = _parse_tlv(pdu, inner)
        field_b = _decode_integer(body)
        _t, vb_list, _ = _parse_tlv(pdu, inner)

        requested = []
        position = 0
        while position < len(vb_list):
            _t, entry, position = _parse_tlv(vb_list, position)
            e = 0
            _t, oid_body, e = _parse_tlv(entry, e)
            requested.append(_decode_oid(oid_body))

        if pdu_tag == PDU_GET_BULK and not self.bulk_supported:
            return self._error_reply(request_id, 5, 0)

        results = []
        if pdu_tag == PDU_GET:
            for oid in requested:
                if oid in self.mib:
                    results.append((oid, _encode_value(self.mib[oid])))
                else:
                    results.append((oid, b"\x80\x00"))  # noSuchObject
        elif pdu_tag == PDU_GET_NEXT:
            for oid in requested:
                nxt = self._next_oid(oid)
                if nxt is None:
                    results.append((oid, b"\x82\x00"))  # endOfMibView
                else:
                    results.append((nxt, _encode_value(self.mib[nxt])))
        elif pdu_tag == PDU_GET_BULK:
            reps = max(1, field_b)
            cursor = requested[0]
            for _ in range(reps):
                nxt = self._next_oid(cursor)
                if nxt is None:
                    results.append((cursor, b"\x82\x00"))
                    break
                results.append((nxt, _encode_value(self.mib[nxt])))
                cursor = nxt
        else:
            return None

        varbinds = b"".join(
            _tlv(TAG_SEQUENCE, _encode_oid_value(oid) + value)
            for oid, value in results
        )
        response_pdu = _tlv(
            PDU_RESPONSE,
            _encode_integer(request_id)
            + _encode_integer(0)
            + _encode_integer(0)
            + _tlv(TAG_SEQUENCE, varbinds),
        )
        return _tlv(
            TAG_SEQUENCE,
            _encode_integer(1)
            + _tlv(TAG_OCTET_STRING, self.community.encode())
            + response_pdu,
        )

    def _error_reply(self, request_id: int, status: int, index: int):
        response_pdu = _tlv(
            PDU_RESPONSE,
            _encode_integer(request_id)
            + _encode_integer(status)
            + _encode_integer(index)
            + _tlv(TAG_SEQUENCE, b""),
        )
        return _tlv(
            TAG_SEQUENCE,
            _encode_integer(1)
            + _tlv(TAG_OCTET_STRING, self.community.encode())
            + response_pdu,
        )
