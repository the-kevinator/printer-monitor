"""
Subnet store tests.

The store is what the dashboard edits, so these cover the validation messages
a user will actually read, the seeding hand-off from config.json, and the
promise that an edit survives a restart.
"""

import json

import pytest

from app.subnets import SubnetError, SubnetStore, describe, normalise


# --------------------------------------------------------------- validation


@pytest.mark.parametrize("text, expected", [
    ("10.10.20.0/24", "10.10.20.0/24"),
    ("  10.10.20.0/24  ", "10.10.20.0/24"),
    # A host address inside a range is the natural way to type "this /24".
    ("10.10.20.57/24", "10.10.20.0/24"),
    ("192.168.4.20", "192.168.4.20/32"),
    ("172.16.5.0/24", "172.16.5.0/24"),
])
def test_valid_ranges_are_canonicalised(text, expected):
    assert normalise(text)[0] == expected


def test_host_count_excludes_network_and_broadcast():
    assert normalise("10.10.20.0/24")[1] == 254
    assert normalise("10.10.20.0/30")[1] == 2
    # /31 and /32 have no reserved addresses, so every address is a host.
    assert normalise("10.10.20.0/31")[1] == 2
    assert normalise("10.10.20.5")[1] == 1


def test_oversized_range_is_refused_with_the_limit_named():
    with pytest.raises(SubnetError, match="8,192"):
        normalise("10.0.0.0/8")
    # A /12 is private and looks innocuous, but it is a million addresses.
    with pytest.raises(SubnetError, match="8,192"):
        normalise("172.16.0.0/12")
    # A /19 is exactly at the cap and must still be allowed.
    assert normalise("10.10.0.0/19")[1] == 8190


def test_public_range_is_refused_by_default():
    with pytest.raises(SubnetError, match="public internet"):
        normalise("8.8.8.0/24")


def test_public_range_is_allowed_when_configured():
    assert normalise("8.8.8.0/24", allow_public=True)[0] == "8.8.8.0/24"


def test_a_range_straddling_the_private_boundary_is_refused():
    # 203.0.112.0/23 is half documentation range and half globally routable;
    # accepting it because part of it looks internal would be the bug.
    with pytest.raises(SubnetError, match="public internet"):
        normalise("203.0.112.0/23")


def test_exclusions_may_name_public_addresses():
    # Excluding only ever narrows a sweep, so the guard does not apply.
    assert normalise("8.8.8.8", kind="exclude")[0] == "8.8.8.8/32"


@pytest.mark.parametrize("text", ["", "   ", "nonsense", "10.10.20.0/33", "10.10.20.999"])
def test_junk_is_refused(text):
    with pytest.raises(SubnetError):
        normalise(text)


# -------------------------------------------------------------------- store


@pytest.fixture
def store(tmp_path):
    store = SubnetStore(tmp_path / "subnets.json")
    store.load()
    return store


def test_add_persists_and_survives_a_reload(store, tmp_path):
    store.add("10.10.20.0/24", label="Floor 2")

    reopened = SubnetStore(tmp_path / "subnets.json")
    reopened.load()
    entries = reopened.list("include")
    assert [e.cidr for e in entries] == ["10.10.20.0/24"]
    assert entries[0].label == "Floor 2"
    assert entries[0].source == "manual"


def test_adding_the_same_range_twice_is_refused(store):
    store.add("10.10.20.0/24")
    with pytest.raises(SubnetError, match="already in the list"):
        store.add("10.10.20.0/24")
    # ...including when typed as a host address inside the same range.
    with pytest.raises(SubnetError, match="already in the list"):
        store.add("10.10.20.99/24")


def test_a_disabled_range_is_kept_but_not_swept(store):
    entry = store.add("10.10.20.0/24", label="Floor 2")
    store.update(entry.id, enabled=False)

    assert store.active("include") == []
    # The entry itself -- and the label explaining it -- is still there.
    kept = store.list("include")
    assert len(kept) == 1 and kept[0].label == "Floor 2"


def test_update_changes_only_the_label_and_the_flag(store):
    entry = store.add("10.10.20.0/24")
    store.update(entry.id, label="  Renamed  ")
    assert store.list("include")[0].label == "Renamed"
    assert store.list("include")[0].cidr == "10.10.20.0/24"


def test_delete_removes_the_entry(store):
    entry = store.add("10.10.20.0/24")
    assert store.delete(entry.id) is not None
    assert store.list("include") == []
    assert store.delete(entry.id) is None


def test_sweep_target_count_subtracts_exclusions(store):
    store.add("10.10.20.0/24")
    assert store.sweep_target_count() == 254
    store.add("10.10.20.1", kind="exclude")
    assert store.sweep_target_count() == 253
    # The /28 covers .240-.255; .255 is the /24's broadcast and was never in
    # the sweep, so 15 addresses actually come out.
    store.add("10.10.20.240/28", kind="exclude")
    assert store.sweep_target_count() == 253 - 15


def test_overlapping_ranges_are_not_double_counted(store):
    store.add("10.10.20.0/24")
    store.add("10.10.20.0/25")
    # The /25's hosts (.1-.126) all sit inside the /24's, so the sweep is
    # still 254 addresses rather than 254 + 126.
    assert store.sweep_target_count() == 254


def test_an_exclusion_outside_every_range_changes_nothing(store):
    store.add("10.10.20.0/24")
    store.add("192.168.9.9", kind="exclude")
    assert store.sweep_target_count() == 254


# ------------------------------------------------------------------ seeding


def test_seed_populates_an_empty_store_from_config(store):
    store.seed(["10.10.20.0/24", "10.10.30.0/24"], ["10.10.20.1"], source="config")
    assert store.active("include") == ["10.10.20.0/24", "10.10.30.0/24"]
    assert store.active("exclude") == ["10.10.20.1/32"]
    assert all(e.source == "config" for e in store.list("include"))
    assert store.seeded_from == "config"


def test_a_bad_seed_is_kept_visible_but_switched_off(store):
    store.seed(["10.10.20.0/24", "not-a-subnet"], source="config")

    # The usable one is live...
    assert store.active("include") == ["10.10.20.0/24"]
    # ...and the broken one is still on screen, so it can be understood and
    # fixed rather than silently disappearing.
    entries = {e.cidr: e for e in store.list("include")}
    assert entries["not-a-subnet"].enabled is False
    assert describe("not-a-subnet")["valid"] is False


def test_seeding_ignores_blanks_and_duplicates(store):
    store.seed(["10.10.20.0/24", "", "  ", "10.10.20.0/24"], source="config")
    assert len(store.list("include")) == 1


def test_a_hand_written_file_of_plain_strings_still_loads(tmp_path):
    # The file is meant to be readable, and someone will hand-edit it.
    path = tmp_path / "subnets.json"
    path.write_text(json.dumps({
        "subnets": ["10.10.20.0/24", {"cidr": "10.10.30.0/24", "label": "Floor 3"}],
        "exclusions": ["10.10.20.1"],
    }))
    store = SubnetStore(path)
    store.load()
    assert store.active("include") == ["10.10.20.0/24", "10.10.30.0/24"]
    assert store.active("exclude") == ["10.10.20.1/32"]


def test_duplicate_ids_in_a_hand_edited_file_are_made_unique(tmp_path):
    # The dashboard addresses entries by id; two rows sharing one would make
    # deleting the second delete the first.
    path = tmp_path / "subnets.json"
    path.write_text(json.dumps({"subnets": [
        {"cidr": "10.10.20.0/24", "id": "same"},
        {"cidr": "10.10.30.0/24", "id": "same"},
    ]}))
    store = SubnetStore(path)
    store.load()
    ids = [e.id for e in store.list("include")]
    assert len(set(ids)) == 2


def test_a_corrupt_file_is_quarantined_rather_than_obeyed(tmp_path):
    path = tmp_path / "subnets.json"
    path.write_text("{not json at all")
    store = SubnetStore(path)
    store.load()

    assert store.list("include") == []
    assert list(tmp_path.glob("subnets.json.corrupt-*"))


def test_the_reported_count_matches_what_the_sweep_really_probes(store):
    """The number sits next to a button that starts real network traffic."""
    from app.config import Settings

    store.add("10.10.20.0/24")
    store.add("10.10.30.0/28")
    store.add("10.10.20.1", kind="exclude")
    store.add("10.10.30.0/29", kind="exclude")

    settings = Settings(
        subnets=store.active("include"), exclude_ips=store.active("exclude")
    )
    assert store.sweep_target_count() == len(settings.sweep_targets())


def test_payload_reports_each_entry_with_its_validation_result(store):
    store.add("10.10.20.0/24", label="Floor 2")
    payload = store.payload()

    assert payload["sweep_target_count"] == 254
    assert payload["allow_public_subnets"] is False
    assert payload["max_hosts"] == 8192
    row = payload["subnets"][0]
    assert row["valid"] is True and row["host_count"] == 254
    assert row["label"] == "Floor 2" and row["problem"] is None
