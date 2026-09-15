"""
Account and session tests.

These cover the promises the login makes: a password is never recoverable from
what is stored, a cookie cannot be forged or outlived, and changing a password
ends the sessions it was issued for.
"""

import time

import pytest

from app.auth import (
    AuthError,
    SessionManager,
    UserStore,
    hash_password,
    load_or_create_secret,
    normalise_username,
    verify_password,
)


# ----------------------------------------------------------------- hashing


def test_a_password_round_trips_but_is_not_stored_in_the_clear():
    stored = hash_password("correct horse battery staple")
    assert "correct horse battery staple" not in stored
    assert stored.startswith("pbkdf2_sha256$")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("Correct horse battery staple", stored)


def test_the_same_password_hashes_differently_every_time():
    """A shared salt would let one cracked hash reveal every reuse of it."""
    assert hash_password("a-long-password") != hash_password("a-long-password")


@pytest.mark.parametrize("stored", [
    "", "garbage", "pbkdf2_sha256$notanumber$c2FsdA==$aGFzaA==",
    "md5$1$c2FsdA==$aGFzaA==",
])
def test_a_malformed_hash_never_verifies(stored):
    assert not verify_password("anything", stored)


def test_usernames_are_normalised_and_junk_refused():
    assert normalise_username("  KMendez ") == "kmendez"
    for bad in ["", "   ", "has space", "semi;colon", "x" * 65]:
        with pytest.raises(AuthError):
            normalise_username(bad)


# ------------------------------------------------------------------- users


@pytest.fixture
def users(tmp_path):
    store = UserStore(tmp_path / "users.json")
    store.load()
    return store


def test_a_fresh_install_has_no_accounts(users):
    assert users.is_unconfigured
    assert users.count() == 0


def test_add_persists_and_survives_a_reload(users, tmp_path):
    users.add("kmendez", "a-long-enough-password")

    reopened = UserStore(tmp_path / "users.json")
    reopened.load()
    assert reopened.count() == 1
    assert not reopened.is_unconfigured
    assert reopened.authenticate("kmendez", "a-long-enough-password")


def test_usernames_are_case_insensitive(users):
    users.add("KMendez", "a-long-enough-password")
    assert users.get("kmendez") is not None
    with pytest.raises(AuthError, match="already exists"):
        users.add("kmendez", "another-long-password")


def test_short_passwords_are_refused(users):
    with pytest.raises(AuthError, match="at least 10"):
        users.add("kmendez", "short")


def test_authenticate_rejects_a_wrong_password_and_an_unknown_user(users):
    users.add("kmendez", "a-long-enough-password")
    with pytest.raises(AuthError):
        users.authenticate("kmendez", "wrong-password-here")
    with pytest.raises(AuthError):
        users.authenticate("nobody", "a-long-enough-password")


def test_the_last_account_cannot_be_deleted(users):
    """Deleting it would silently reopen the dashboard to everyone."""
    users.add("kmendez", "a-long-enough-password")
    with pytest.raises(AuthError, match="only account"):
        users.delete("kmendez")

    users.add("second", "a-long-enough-password")
    assert users.delete("kmendez") is True
    assert users.count() == 1


# ---------------------------------------------------------------- sessions


@pytest.fixture
def sessions(tmp_path):
    return SessionManager(load_or_create_secret(tmp_path / "secret_key"))


def test_a_cookie_identifies_the_user_who_signed_in(users, sessions):
    user = users.add("kmendez", "a-long-enough-password")
    token = sessions.issue(user)
    assert sessions.verify(token, users).username == "kmendez"


@pytest.mark.parametrize("mangle", [
    lambda t: None,
    lambda t: "",
    lambda t: "not-a-token",
    lambda t: t + "x",                       # broken signature
    lambda t: t.split(".")[0],               # signature stripped
    lambda t: "AAAA." + t.split(".")[1],     # payload swapped
])
def test_a_forged_or_damaged_cookie_is_rejected(users, sessions, mangle):
    user = users.add("kmendez", "a-long-enough-password")
    assert sessions.verify(mangle(sessions.issue(user)), users) is None


def test_a_cookie_signed_with_another_key_is_rejected(users, tmp_path):
    user = users.add("kmendez", "a-long-enough-password")
    token = SessionManager(load_or_create_secret(tmp_path / "a")).issue(user)
    other = SessionManager(load_or_create_secret(tmp_path / "b"))
    assert other.verify(token, users) is None


def test_a_cookie_expires(users, tmp_path, monkeypatch):
    user = users.add("kmendez", "a-long-enough-password")
    manager = SessionManager(load_or_create_secret(tmp_path / "secret_key"), hours=1)
    real_now = time.time()
    token = manager.issue(user)
    assert manager.verify(token, users) is not None

    # Jump past the expiry rather than sleep through it.
    monkeypatch.setattr(time, "time", lambda: real_now + 7200)
    assert manager.verify(token, users) is None


def test_changing_a_password_invalidates_existing_cookies(users, sessions):
    """
    Sessions are stateless, so this is the mechanism that makes "change the
    password" actually eject whoever was signed in with the old one.
    """
    user = users.add("kmendez", "a-long-enough-password")
    token = sessions.issue(user)
    assert sessions.verify(token, users) is not None

    users.set_password("kmendez", "a-different-long-password")
    assert sessions.verify(token, users) is None


def test_deleting_a_user_invalidates_their_cookie(users, sessions):
    users.add("kmendez", "a-long-enough-password")
    second = users.add("second", "a-long-enough-password")
    token = sessions.issue(second)
    users.delete("second")
    assert sessions.verify(token, users) is None


def test_the_signing_key_is_stable_across_restarts(tmp_path):
    """Otherwise every app-pool recycle would sign everyone out."""
    first = load_or_create_secret(tmp_path / "secret_key")
    assert load_or_create_secret(tmp_path / "secret_key") == first
    assert len(first) >= 32
