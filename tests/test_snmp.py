"""BER/wire-format tests plus end-to-end tests against the mock agent."""

import pytest

from app import snmp
from app.snmp import (
    NO_SUCH_OBJECT,
    SnmpClient,
    SnmpTimeout,
    oid_str,
    parse_oid,
)
from tests.mock_agent import MockAgent

SYS_DESCR = (1, 3, 6, 1, 2, 1, 1, 1, 0)


# -- fixed wire vectors -----------------------------------------------------


def test_oid_encoding_matches_known_bytes():
    # 1.3.6.1.2.1.1.1.0 is the canonical example: first two arcs collapse to
    # 1*40+3 = 43 = 0x2b, then one byte per remaining arc.
    assert snmp._encode_oid(SYS_DESCR) == bytes.fromhex("06082b06010201010100")


def test_integer_encoding_is_minimal_and_signed():
    assert snmp._encode_integer(0) == bytes.fromhex("020100")
    assert snmp._encode_integer(1) == bytes.fromhex("020101")
    assert snmp._encode_integer(127) == bytes.fromhex("02017f")
    assert snmp._encode_integer(128) == bytes.fromhex("0202" "0080")
    assert snmp._encode_integer(-1) == bytes.fromhex("0201ff")
    assert snmp._encode_integer(-128) == bytes.fromhex("020180")
    assert snmp._encode_integer(-129) == bytes.fromhex("0202ff7f")


def test_base128_multibyte_subidentifier():
    # 2620 -> 20, 60 in 7-bit groups -> 0x94 0x3c
    assert snmp._encode_base128(2620) == bytes.fromhex("943c")
    assert snmp._encode_base128(0) == b"\x00"
    assert snmp._encode_base128(127) == b"\x7f"
    assert snmp._encode_base128(128) == bytes.fromhex("8100")


def test_long_form_length_encoding():
    assert snmp._encode_length(5) == b"\x05"
    assert snmp._encode_length(127) == b"\x7f"
    assert snmp._encode_length(128) == bytes.fromhex("8180")
    assert snmp._encode_length(300) == bytes.fromhex("82012c")


@pytest.mark.parametrize(
    "oid",
    [
        (1, 3, 6, 1, 2, 1, 1, 1, 0),
        (1, 3, 6, 1, 2, 1, 43, 11, 1, 1, 9, 1, 1),
        (1, 3, 6, 1, 4, 1, 2620, 1, 1),
        (0, 0),
        (2, 999, 1),
    ],
)
def test_oid_roundtrip(oid):
    encoded = snmp._encode_oid(oid)
    tag, body, _ = snmp._parse_tlv(encoded, 0)
    assert tag == snmp.TAG_OID
    assert snmp._decode_oid(body) == oid


def test_parse_oid_accepts_leading_dot_and_sequences():
    assert parse_oid(".1.3.6.1") == (1, 3, 6, 1)
    assert parse_oid("1.3.6.1") == (1, 3, 6, 1)
    assert parse_oid([1, 3, 6, 1]) == (1, 3, 6, 1)
    with pytest.raises(snmp.SnmpError):
        parse_oid("1.3.six")


def test_decode_message_rejects_truncated_packets():
    good = snmp._encode_message("public", snmp.PDU_GET, 1, [SYS_DESCR])
    with pytest.raises(snmp.SnmpError):
        snmp._decode_message(good[:6])


def test_octet_string_strips_nul_padding():
    assert snmp._decode_octet_string(b"HP LaserJet\x00\x00") == "HP LaserJet"
    assert snmp._decode_octet_string(b"  spaced  ") == "spaced"


# -- client behaviour against the mock agent --------------------------------


def _client(agent, **kwargs):
    return SnmpClient(
        "127.0.0.1", community=agent.community, port=agent.port,
        timeout=0.6, retries=1, **kwargs
    )


def test_get_one_returns_value():
    mib = {SYS_DESCR: "HP LaserJet MFP M428fdw"}
    with MockAgent(mib) as agent, _client(agent) as client:
        assert client.get_one(SYS_DESCR) == "HP LaserJet MFP M428fdw"


def test_get_missing_object_yields_exception_sentinel():
    with MockAgent({SYS_DESCR: "x"}) as agent, _client(agent) as client:
        bindings = client.get((1, 3, 6, 1, 2, 1, 99, 0))
        assert bindings[0].value is NO_SUCH_OBJECT
        assert client.get_one((1, 3, 6, 1, 2, 1, 99, 0)) is None


def test_get_many_maps_requested_oids():
    mib = {
        SYS_DESCR: "printer",
        (1, 3, 6, 1, 2, 1, 1, 5, 0): "PRN-01",
    }
    with MockAgent(mib) as agent, _client(agent) as client:
        out = client.get_many(["1.3.6.1.2.1.1.1.0", "1.3.6.1.2.1.1.5.0"])
        assert out == {
            "1.3.6.1.2.1.1.1.0": "printer",
            "1.3.6.1.2.1.1.5.0": "PRN-01",
        }


def test_walk_returns_subtree_in_order_and_stops_at_prefix():
    base = (1, 3, 6, 1, 2, 1, 43, 11, 1, 1, 9, 1)
    mib = {
        base + (1,): 82,
        base + (2,): 45,
        base + (3,): 7,
        # outside the walked prefix -- must not be returned
        (1, 3, 6, 1, 2, 1, 44, 1): 999,
    }
    with MockAgent(mib) as agent, _client(agent) as client:
        rows = list(client.walk(base))
        assert [oid_str(r.oid) for r in rows] == [
            "1.3.6.1.2.1.43.11.1.1.9.1.1",
            "1.3.6.1.2.1.43.11.1.1.9.1.2",
            "1.3.6.1.2.1.43.11.1.1.9.1.3",
        ]
        assert [r.value for r in rows] == [82, 45, 7]


def test_walk_falls_back_to_getnext_when_bulk_unsupported():
    base = (1, 3, 6, 1, 2, 1, 43, 11, 1, 1, 9, 1)
    mib = {base + (1,): 82, base + (2,): 45}
    with MockAgent(mib) as agent:
        agent.bulk_supported = False
        with _client(agent) as client:
            rows = list(client.walk(base))
    assert [r.value for r in rows] == [82, 45]


def test_walk_column_keys_by_row_suffix():
    column = (1, 3, 6, 1, 2, 1, 43, 11, 1, 1, 9)
    mib = {column + (1, 1): 82, column + (1, 2): 45}
    with MockAgent(mib) as agent, _client(agent) as client:
        assert client.walk_column(column) == {"1.1": 82, "1.2": 45}


def test_walk_of_empty_subtree_is_empty():
    with MockAgent({SYS_DESCR: "x"}) as agent, _client(agent) as client:
        assert list(client.walk((1, 3, 6, 1, 2, 1, 43))) == []


def test_timeout_raises_when_agent_silent():
    with MockAgent({SYS_DESCR: "x"}, drop_first=99) as agent:
        client = SnmpClient(
            "127.0.0.1", port=agent.port, timeout=0.25, retries=1
        )
        with client, pytest.raises(SnmpTimeout):
            client.get_one(SYS_DESCR)


def test_retry_recovers_after_one_dropped_datagram():
    with MockAgent({SYS_DESCR: "recovered"}, drop_first=1) as agent:
        client = SnmpClient(
            "127.0.0.1", port=agent.port, timeout=0.4, retries=2
        )
        with client:
            assert client.get_one(SYS_DESCR) == "recovered"


def test_wrong_community_times_out():
    with MockAgent({SYS_DESCR: "x"}, community="private") as agent:
        client = SnmpClient(
            "127.0.0.1", community="public", port=agent.port,
            timeout=0.25, retries=0,
        )
        with client, pytest.raises(SnmpTimeout):
            client.get_one(SYS_DESCR)
