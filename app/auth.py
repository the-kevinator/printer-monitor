"""
Accounts and sessions.

The dashboard is readable by anyone who can reach it; changing anything --
subnets, discovery, the inventory -- requires signing in. Everything here is
standard library, so the Windows install stays at three pure-Python wheels:
PBKDF2 from hashlib, HMAC-signed cookies from hmac/secrets.

Sessions are stateless. The cookie carries the username, an expiry and a short
fingerprint of the stored password hash, all signed with a server secret. That
makes a password change invalidate that user's existing sessions without
keeping a session table, and rotating the secret file signs everyone out.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .jsonfile import quarantine, read_json, write_atomic

SCHEMA_VERSION = 1

# OWASP's floor for PBKDF2-HMAC-SHA256 is 600k iterations; roughly 0.2s on the
# kind of hardware an office file server has, which is the point.
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
SESSION_COOKIE = "pm_session"
CSRF_COOKIE = "pm_csrf"
CSRF_HEADER = "X-PM-CSRF"
DEFAULT_SESSION_HOURS = 12

MIN_PASSWORD_LENGTH = 10


class AuthError(Exception):
    """A sign-in or account change that failed, with a user-facing reason."""


# ---------------------------------------------------------------------------
# Password hashing
# ---------------------------------------------------------------------------


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Return a self-describing PBKDF2 hash: algo$iterations$salt$digest."""
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join([
        "pbkdf2_sha256",
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ])


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of a password against a stored hash."""
    try:
        algo, raw_iterations, raw_salt, raw_digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        salt = base64.b64decode(raw_salt)
        expected = base64.b64decode(raw_digest)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return hmac.compare_digest(candidate, expected)


def check_password_quality(password: str) -> None:
    """Raise AuthError if a new password is too weak to accept."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@dataclass
class User:
    username: str
    password_hash: str
    created_at: float = field(default_factory=time.time)
    last_login: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public(self) -> dict[str, Any]:
        """Everything except the hash -- safe to put in an API response."""
        data = self.as_dict()
        data.pop("password_hash", None)
        return data


def normalise_username(username: str) -> str:
    cleaned = (username or "").strip().lower()
    if not cleaned:
        raise AuthError("A username is required.")
    if len(cleaned) > 64 or not all(c.isalnum() or c in "._-@" for c in cleaned):
        raise AuthError(
            "Usernames may contain letters, digits, dot, dash, underscore and @."
        )
    return cleaned


class UserStore:
    """Thread-safe, JSON-backed account list with write-through saves."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._users: dict[str, User] = {}

    def load(self) -> None:
        with self._lock:
            raw = read_json(self.path)
            if raw is None:
                if self.path.is_file():
                    quarantine(self.path)
                self._users = {}
                return
            entries = raw.get("users", []) if isinstance(raw, dict) else raw
            known = set(User.__dataclass_fields__)
            loaded: dict[str, User] = {}
            for entry in entries if isinstance(entries, list) else []:
                if not isinstance(entry, dict) or not entry.get("username"):
                    continue
                cleaned = {k: v for k, v in entry.items() if k in known}
                try:
                    user = User(**cleaned)
                except TypeError:
                    continue
                loaded[user.username.lower()] = user
            self._users = loaded

    def save(self) -> None:
        with self._lock:
            write_atomic(self.path, {
                "schema_version": SCHEMA_VERSION,
                "saved_at": time.time(),
                "users": [u.as_dict() for u in self._sorted()],
            })

    def _sorted(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.username)

    # -- reads -------------------------------------------------------------

    def get(self, username: str) -> User | None:
        with self._lock:
            return self._users.get((username or "").strip().lower())

    def all(self) -> list[User]:
        with self._lock:
            return self._sorted()

    def count(self) -> int:
        with self._lock:
            return len(self._users)

    @property
    def is_unconfigured(self) -> bool:
        """
        True when no account exists yet.

        While this holds the app stays wide open, exactly as it behaved before
        accounts existed -- upgrading an install must not lock the admin out of
        their own dashboard. The UI shows a standing banner until it is false.
        """
        return self.count() == 0

    # -- writes ------------------------------------------------------------

    def add(self, username: str, password: str) -> User:
        name = normalise_username(username)
        check_password_quality(password)
        with self._lock:
            if name in self._users:
                raise AuthError(f"User {name!r} already exists.")
            user = User(username=name, password_hash=hash_password(password))
            self._users[name] = user
            self.save()
            return user

    def set_password(self, username: str, password: str) -> User:
        name = normalise_username(username)
        check_password_quality(password)
        with self._lock:
            user = self._users.get(name)
            if user is None:
                raise AuthError(f"No such user {name!r}.")
            user.password_hash = hash_password(password)
            self.save()
            return user

    def delete(self, username: str) -> bool:
        name = normalise_username(username)
        with self._lock:
            if name not in self._users:
                return False
            if len(self._users) == 1:
                raise AuthError(
                    "This is the only account; deleting it would leave the "
                    "dashboard unprotected. Create another account first."
                )
            del self._users[name]
            self.save()
            return True

    def authenticate(self, username: str, password: str) -> User:
        user = self.get(username)
        # Hash even when the user is unknown, so a missing account and a wrong
        # password take the same time and cannot be told apart by timing.
        stored = user.password_hash if user else hash_password("_")
        ok = verify_password(password or "", stored)
        if not user or not ok:
            raise AuthError("Incorrect username or password.")
        with self._lock:
            user.last_login = time.time()
            try:
                self.save()
            except OSError:
                pass  # a lost timestamp must not block a valid sign-in
        return user


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def load_or_create_secret(path: str | Path) -> bytes:
    """
    Read the cookie-signing key, generating one on first run.

    It has to survive restarts or every app-pool recycle would sign everyone
    out. Deleting the file is the documented way to force exactly that.
    """
    key_path = Path(path)
    if key_path.is_file():
        try:
            raw = key_path.read_text(encoding="ascii").strip()
            if len(raw) >= 32:
                return raw.encode("ascii")
        except OSError:
            pass
    secret = secrets.token_urlsafe(48)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    # Written directly rather than via write_atomic: it is a bare token, not
    # JSON, and it is created owner-only.
    fd = os.open(str(key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as handle:
        handle.write(secret)
    return secret.encode("ascii")


def _fingerprint(password_hash: str) -> str:
    """8 chars derived from the stored hash, to bind a cookie to a password."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:8]


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class SessionManager:
    """Issues and validates the signed session cookie."""

    def __init__(self, secret: bytes, hours: int = DEFAULT_SESSION_HOURS) -> None:
        self.secret = secret
        self.lifetime = max(1, hours) * 3600

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(
            self.secret, payload.encode("utf-8"), hashlib.sha256
        ).digest())

    def issue(self, user: User) -> str:
        expires = int(time.time() + self.lifetime)
        payload = f"{user.username}|{expires}|{_fingerprint(user.password_hash)}"
        return f"{_b64(payload.encode('utf-8'))}.{self._sign(payload)}"

    def verify(self, token: str | None, users: UserStore) -> User | None:
        """Return the signed-in user, or None for absent/invalid/expired."""
        if not token or "." not in token:
            return None
        encoded, _, signature = token.rpartition(".")
        try:
            payload = _unb64(encoded).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
        if not hmac.compare_digest(self._sign(payload), signature):
            return None
        parts = payload.split("|")
        if len(parts) != 3:
            return None
        username, raw_expires, fingerprint = parts
        try:
            if int(raw_expires) < time.time():
                return None
        except ValueError:
            return None
        user = users.get(username)
        if user is None:
            return None
        # The password changed (or the account was recreated) since this cookie
        # was issued, so it is no longer good.
        if not hmac.compare_digest(_fingerprint(user.password_hash), fingerprint):
            return None
        return user


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)
