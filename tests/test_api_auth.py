"""
End-to-end tests for the subnet editor and the login that guards it.

`app.main` builds its state at import time, so each fixture here sets the
environment up first and reloads the module to get a clean app.
"""

import importlib
import json
import os

import pytest
from fastapi.testclient import TestClient

PASSWORD = "a-long-enough-password"


def _build(tmp_path, config_extra=None):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "subnets": ["10.10.20.0/24"],
        "data_dir": str(data_dir),
        "poller_enabled": False,
        "auto_discover_on_start": False,
        "discovery_interval_seconds": 0,
        **(config_extra or {}),
    }))
    os.environ["PRINTER_MONITOR_CONFIG"] = str(config)
    # A stale env override would silently outrank the config file.
    os.environ.pop("PRINTER_MONITOR_SUBNETS", None)
    import app.main as main
    importlib.reload(main)
    return main


@pytest.fixture
def open_app(tmp_path):
    """An install with no accounts yet -- writes are still allowed."""
    main = _build(tmp_path)
    with TestClient(main.app) as client:
        client.app_module = main
        yield client


@pytest.fixture
def secured(tmp_path):
    """An install with one account, signed out."""
    main = _build(tmp_path)
    main.state.users.add("jdoe", PASSWORD)
    with TestClient(main.app) as client:
        client.app_module = main
        yield client


def sign_in(client, username="jdoe", password=PASSWORD):
    client.get("/")  # collect the CSRF cookie the form needs
    token = client.cookies.get("pm_csrf")
    response = client.post(
        "/login",
        data={"username": username, "password": password,
              "csrf_token": token, "next": ""},
        follow_redirects=False,
    )
    return response


def writes(client):
    """Every request that changes something, as (method, path, body)."""
    return [
        ("POST", "/api/subnets", {"cidr": "10.10.90.0/24"}),
        ("POST", "/api/discover", None),
        ("POST", "/api/discover/cancel", None),
        ("POST", "/api/poll", None),
        ("POST", "/api/printers", {"ip": "10.10.20.9"}),
        ("DELETE", "/api/printers/10.10.20.9", None),
    ]


# ------------------------------------------------------- seeding from config


def test_config_subnets_seed_the_store_on_first_start(open_app, tmp_path):
    payload = open_app.get("/api/subnets").json()
    assert [row["cidr"] for row in payload["subnets"]] == ["10.10.20.0/24"]
    assert payload["subnets"][0]["source"] == "config"
    assert (tmp_path / "data" / "subnets.json").is_file()


def test_the_store_wins_over_config_on_later_starts(tmp_path):
    """
    The whole point of moving subnets into the app: once the file exists,
    editing config.json must not quietly undo what someone typed here.
    """
    main = _build(tmp_path)
    with TestClient(main.app) as client:
        client.post("/api/subnets", json={"cidr": "10.10.55.0/24"})
        client.delete(
            "/api/subnets/"
            + client.get("/api/subnets").json()["subnets"][0]["id"]
        )

    # Restart with a config.json that still lists the original range.
    main = _build(tmp_path)
    with TestClient(main.app) as client:
        cidrs = [r["cidr"] for r in client.get("/api/subnets").json()["subnets"]]
        assert cidrs == ["10.10.55.0/24"]
        assert main.state.settings.subnets == ["10.10.55.0/24"]


# ---------------------------------------------------------- editing subnets


def test_adding_a_subnet_takes_effect_without_a_restart(open_app):
    before = open_app.get("/api/config").json()["sweep_target_count"]
    open_app.post("/api/subnets", json={"cidr": "10.10.30.0/24", "label": "Floor 3"})

    after = open_app.get("/api/config").json()
    assert after["sweep_target_count"] == before + 254
    assert "10.10.30.0/24" in after["subnets"]


def test_a_disabled_subnet_leaves_the_sweep(open_app):
    entry = open_app.get("/api/subnets").json()["subnets"][0]
    open_app.patch("/api/subnets/" + entry["id"], json={"enabled": False})

    config = open_app.get("/api/config").json()
    assert config["subnets"] == []
    assert config["sweep_target_count"] == 0
    # Still listed, so it can be switched back on.
    assert len(open_app.get("/api/subnets").json()["subnets"]) == 1


def test_an_exclusion_narrows_the_sweep(open_app):
    open_app.post("/api/subnets", json={"cidr": "10.10.20.1", "kind": "exclude"})
    assert open_app.get("/api/config").json()["sweep_target_count"] == 253


@pytest.mark.parametrize("cidr, fragment", [
    ("8.8.8.0/24", "public internet"),
    ("10.0.0.0/8", "8,192"),
    ("nonsense", "not a valid"),
    ("", "Enter a network range"),
])
def test_a_bad_subnet_is_refused_with_a_usable_message(open_app, cidr, fragment):
    response = open_app.post("/api/subnets", json={"cidr": cidr})
    assert response.status_code == 400
    assert fragment in response.json()["detail"]


def test_a_duplicate_subnet_is_refused(open_app):
    assert open_app.post("/api/subnets", json={"cidr": "10.10.20.0/24"}).status_code == 400


def test_deleting_an_unknown_entry_is_a_404(open_app):
    assert open_app.delete("/api/subnets/nope").status_code == 404
    assert open_app.patch("/api/subnets/nope", json={"enabled": False}).status_code == 404


def test_exclusions_cannot_be_scanned_on_their_own(open_app):
    created = open_app.post(
        "/api/subnets", json={"cidr": "10.10.20.1", "kind": "exclude"}
    ).json()
    response = open_app.post("/api/subnets/" + created["entry"]["id"] + "/scan")
    assert response.status_code == 400


def test_discovery_is_refused_when_nothing_is_configured(open_app):
    entry = open_app.get("/api/subnets").json()["subnets"][0]
    open_app.delete("/api/subnets/" + entry["id"])
    response = open_app.post("/api/discover")
    assert response.status_code == 400
    assert "No subnets" in response.json()["detail"]


# ------------------------------------------------------------ access control


def test_without_accounts_everything_stays_open(open_app):
    """An upgrade must not lock the existing admin out of their dashboard."""
    assert open_app.get("/api/printers").json()["auth"]["unconfigured"] is True
    for method, path, body in writes(open_app):
        response = open_app.request(method, path, json=body)
        assert response.status_code != 401, f"{method} {path} was blocked"


def test_once_an_account_exists_writes_need_a_session(secured):
    assert secured.get("/api/printers").json()["auth"]["unconfigured"] is False
    for method, path, body in writes(secured):
        response = secured.request(method, path, json=body)
        assert response.status_code == 401, f"{method} {path} was not protected"


def test_reading_stays_open_to_everyone(secured):
    for path in ["/", "/api/printers", "/api/subnets", "/api/summary",
                 "/api/config", "/api/discover", "/healthz"]:
        assert secured.get(path).status_code == 200, path


def test_exports_are_open_but_tagging_a_printer_is_a_write(secured):
    """Exports read what /api/printers already serves; a colour tag is an edit."""
    for path in ("/export/printers.csv", "/export/printers.pdf"):
        assert secured.get(path).status_code == 200
    assert secured.patch("/api/printers/192.0.2.10",
                         json={"color": "blue"}).status_code == 401
    assert secured.get("/api/tags").status_code == 200
    assert secured.put("/api/tags",
                       json={"labels": {"blue": "x"}}).status_code == 401


def test_signing_in_then_writing_works(secured):
    assert sign_in(secured).status_code == 303
    assert secured.get("/api/printers").json()["auth"]["username"] == "jdoe"

    response = secured.post(
        "/api/subnets",
        json={"cidr": "10.10.77.0/24"},
        headers={"X-PM-CSRF": secured.cookies.get("pm_csrf")},
    )
    assert response.status_code == 201


def test_a_wrong_password_does_not_sign_anyone_in(secured):
    response = sign_in(secured, password="wrong-password-entirely")
    assert response.status_code == 401
    assert "Incorrect username or password" in response.text
    assert secured.get("/api/printers").json()["auth"]["signed_in"] is False


def test_a_signed_in_write_without_the_csrf_header_is_refused(secured):
    sign_in(secured)
    response = secured.post("/api/subnets", json={"cidr": "10.10.88.0/24"})
    assert response.status_code == 403


def test_signing_out_ends_the_session(secured):
    sign_in(secured)
    secured.post("/logout", follow_redirects=False)
    assert secured.post("/api/subnets", json={"cidr": "10.10.99.0/24"}).status_code == 401


def test_the_first_account_can_be_claimed_from_the_browser(open_app):
    open_app.get("/")
    response = open_app.post(
        "/api/account/setup",
        json={"username": "jdoe", "password": PASSWORD},
        headers={"X-PM-CSRF": open_app.cookies.get("pm_csrf")},
    )
    assert response.status_code == 201
    # ...and that same request signs them in, so they are not locked out of
    # the dashboard they just secured.
    assert open_app.get("/api/printers").json()["auth"]["username"] == "jdoe"


def test_the_setup_route_closes_once_an_account_exists(secured):
    secured.get("/")
    response = secured.post(
        "/api/account/setup",
        json={"username": "intruder", "password": PASSWORD},
        headers={"X-PM-CSRF": secured.cookies.get("pm_csrf")},
    )
    assert response.status_code == 409


def test_the_login_page_will_not_redirect_off_site(secured):
    """`next` comes from the query string, so it is an open-redirect risk."""
    secured.get("/")
    token = secured.cookies.get("pm_csrf")
    response = secured.post(
        "/login",
        data={"username": "jdoe", "password": PASSWORD,
              "csrf_token": token, "next": "https://evil.example/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


# --------------------------------------------- the detail page's own writes


def test_the_printer_page_carries_the_csrf_token(secured):
    """
    The detail page saves notes with fetch(), so it needs the token in the
    document the same way the dashboard does. Shipping it without one made
    every save fail with a bare "Forbidden".
    """
    secured.app_module.state.store.add_manual("10.10.20.41")
    sign_in(secured)

    page = secured.get("/printer/10.10.20.41")
    assert page.status_code == 200
    assert f'data-csrf="{secured.cookies.get("pm_csrf")}"' in page.text


def test_saving_a_note_needs_the_token_the_page_was_given(secured):
    secured.app_module.state.store.add_manual("10.10.20.41")
    sign_in(secured)
    secured.get("/printer/10.10.20.41")
    token = secured.cookies.get("pm_csrf")

    assert secured.patch(
        "/api/printers/10.10.20.41", json={"notes": "behind reception"},
    ).status_code == 403

    saved = secured.patch(
        "/api/printers/10.10.20.41",
        json={"notes": "behind reception"},
        headers={"X-PM-CSRF": token},
    )
    assert saved.status_code == 200
    assert saved.json()["notes"] == "behind reception"
