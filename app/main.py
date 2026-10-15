"""
FastAPI application: JSON API plus the dashboard page.

Hosted under IIS via HttpPlatformHandler (see web.config), or run directly
with `python -m uvicorn app.main:app` for development.

Reading is open to anyone who can reach the site; every route that changes
something -- subnets, discovery, the inventory -- requires a signed-in user.
Until the first account exists the app behaves exactly as it did before
accounts were added, so upgrading an install cannot lock its admin out.
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import time
from urllib.parse import parse_qs, urlparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import (
    CSRF_COOKIE,
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthError,
    SessionManager,
    User,
    UserStore,
    load_or_create_secret,
    new_csrf_token,
)
from secrets import token_urlsafe

from .config import Settings, load_settings
from .discovery import DiscoveryRunner
from . import export
from .poller import PollerService
from .store import PRINTER_COLORS, PrinterStore
from .subnets import SubnetError, SubnetStore

log = logging.getLogger("printer_monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

APP_DIR = Path(__file__).resolve().parent
VERSION = "1.1.0"


class AppState:
    """Container for the long-lived objects, built once at startup."""

    settings: Settings
    store: PrinterStore
    poller: PollerService
    subnets: SubnetStore
    users: UserStore
    sessions: SessionManager

    def __init__(self) -> None:
        self.settings = load_settings()
        Path(self.settings.data_dir).mkdir(parents=True, exist_ok=True)

        # Subnets moved out of config.json and into data/subnets.json so they
        # can be edited here. config.json seeds the file once, on the first
        # start that finds none; after that this store is the only authority.
        self.subnets = SubnetStore(
            self.settings.subnets_file,
            allow_public=self.settings.allow_public_subnets,
        )
        self.subnets.load()
        if not self.subnets.exists:
            source = self.settings.subnets_source or "config"
            try:
                seeded = self.subnets.seed(
                    self.settings.subnets, self.settings.exclude_ips, source=source
                )
                log.info(
                    "seeded %d subnet entr(ies) into %s from %s",
                    seeded, self.settings.subnets_file, source,
                )
            except OSError as exc:
                # A data directory the app cannot write to is a deployment
                # mistake (on IIS, the app pool identity is missing Modify),
                # but failing to start leaves the admin with a bare 502 and
                # nothing to read. Come up on the seeded list in memory and
                # say why editing will not stick.
                log.error(
                    "cannot write %s (%s) -- subnets are in memory only and "
                    "edits will fail until the data directory is writable",
                    self.settings.subnets_file, exc,
                )
        self.sync_subnets()

        problems = self.settings.validate()
        for problem in problems:
            log.warning("config problem: %s", problem)
        self.config_problems = problems

        self.store = PrinterStore(self.settings.printers_file)
        self.store.load()
        self.poller = PollerService(
            self.settings, self.store, DiscoveryRunner(self.settings)
        )

        self.users = UserStore(self.settings.users_file)
        self.users.load()
        try:
            secret = load_or_create_secret(self.settings.secret_key_file)
        except OSError as exc:
            # Same reasoning as above. A per-process key still signs cookies
            # correctly; it just does not outlive a restart, so everyone is
            # signed out whenever the app pool recycles.
            log.error(
                "cannot write %s (%s) -- using a temporary signing key, so "
                "sign-ins will not survive a restart",
                self.settings.secret_key_file, exc,
            )
            secret = token_urlsafe(48).encode("ascii")
        self.sessions = SessionManager(secret, hours=self.settings.session_hours)
        if self.users.is_unconfigured:
            log.warning(
                "no accounts exist yet -- anyone who can reach the dashboard "
                "can change it. Create one with: python manage.py user add <name>"
            )

        log.info(
            "loaded %d printer(s); sweeping %s",
            self.store.count(), self.settings.subnets or "(nothing configured)",
        )

    def sync_subnets(self) -> None:
        """
        Push the stored lists into Settings, which is what the sweep reads.

        Settings stays the single runtime source so discovery, the poller and
        manage.py need no knowledge of where the list is kept. The lists are
        replaced rather than mutated in place, so a sweep running on another
        thread sees either the old list or the new one, never a half-built one.
        """
        self.settings.subnets = self.subnets.active("include")
        self.settings.exclude_ips = self.subnets.active("exclude")
        self.settings.subnets_autodetected = (
            self.subnets.seeded_from == "autodetected"
            and all(e.source == "autodetected" for e in self.subnets.list("include"))
            and bool(self.settings.subnets)
        )


state = AppState()


@asynccontextmanager
async def lifespan(app: FastAPI):
    if state.settings.poller_enabled:
        state.poller.start()
    else:
        log.warning("background polling is disabled (poller_enabled=false)")
    try:
        yield
    finally:
        state.poller.stop()


app = FastAPI(
    title="Printer Monitor",
    description="Discovers network printers over SNMP and reports toner, "
                "tray and status information.",
    version=VERSION,
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def current_user(request: Request) -> User | None:
    """The signed-in user for this request, or None."""
    return state.sessions.verify(request.cookies.get(SESSION_COOKIE), state.users)


def _check_csrf(request: Request) -> None:
    """
    Double-submit check on every state-changing request.

    The session cookie is SameSite=Lax, which already blocks cross-site form
    posts, but the dashboard's own writes go through fetch() and it costs
    almost nothing to require a header no other origin can read.
    """
    cookie = request.cookies.get(CSRF_COOKIE)
    header = request.headers.get(CSRF_HEADER) or ""
    if not cookie or not hmac.compare_digest(cookie, header):
        raise HTTPException(
            status_code=403,
            detail="Stale page -- reload the dashboard and try again.",
        )


def require_auth(request: Request) -> User | None:
    """
    Dependency for every route that changes something.

    Returns None while no account exists at all: a fresh or just-upgraded
    install must stay usable until someone sets up the first login, and the
    dashboard shows a standing banner for exactly as long as that is true.
    """
    if state.users.is_unconfigured:
        return None
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=401, detail="Sign in to change anything here."
        )
    _check_csrf(request)
    return user


def _csrf_for(request: Request) -> str:
    """The token this request should use: the one it already has, or a new one."""
    return request.cookies.get(CSRF_COOKIE) or new_csrf_token()


def _issue_csrf(request: Request, response: Response, token: str | None = None) -> str:
    """
    Make sure the browser ends up holding `token`.

    Pages that render the token into a hidden form field have to know its value
    *before* the response exists, so they call `_csrf_for` first and pass the
    result in here; everything else lets this pick one.
    """
    token = token or _csrf_for(request)
    response.set_cookie(
        CSRF_COOKIE, token,
        max_age=state.settings.session_hours * 3600,
        httponly=False,  # the dashboard's JS has to read it to echo it back
        samesite="lax",
        secure=request.url.scheme == "https",
        path=(_base_path(request) or "/"),
    )
    return token


def _set_session_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=state.settings.session_hours * 3600,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path=(_base_path(request) or "/"),
    )


def _safe_next(request: Request, target: str | None) -> str:
    """
    Only ever redirect back to a path on this site.

    `next` comes from the query string, so an absolute URL here would make the
    login page an open redirect.
    """
    base = _base_path(request) or ""
    if not target or not target.startswith("/") or target.startswith("//"):
        return base + "/"
    return target


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_path(request: Request) -> str:
    """
    The prefix this app is mounted under, e.g. "/printers".

    IIS often hosts an app as a virtual application below a site, in which case
    HttpPlatformHandler forwards the full path and the app is started with
    --root-path. Templates and the browser-side code must build their URLs on
    this prefix, or every link and API call would resolve against the site root.
    """
    root = request.scope.get("root_path", "") or ""
    return root.rstrip("/")


def _valid_ip(ip: str) -> str:
    try:
        return str(ipaddress.ip_address(ip))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{ip!r} is not a valid IP address")


def _validate_web_url(url: str | None) -> str | None:
    """
    Accept only http(s) URLs for the printer's web interface.

    This value is user-editable and is rendered straight into an href and used
    as a redirect target, so a "javascript:" or "data:" URL would be clickable
    script. Anything else is rejected outright rather than sanitised.
    """
    if url is None:
        return None
    text = str(url).strip()
    if not text:
        return ""  # an explicit clear
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(
            status_code=400,
            detail="web_url must be an absolute http:// or https:// URL",
        )
    return text


def _validate_color(color: Any) -> str:
    """A colour tag must be one of the named swatches; "" or null clears it."""
    if color is None or color == "":
        return ""
    text = str(color).strip().lower()
    if text not in PRINTER_COLORS:
        raise HTTPException(
            status_code=400,
            detail=f"color must be one of {sorted(PRINTER_COLORS)}, or empty",
        )
    return text


def _record_payload(record) -> dict[str, Any]:
    data = record.as_dict()
    stats = data.get("stats") or {}
    # Promote the handful of figures the dashboard sorts and filters on.
    data["summary"] = {
        "health": record.health,
        "online": record.online,
        "page_count": stats.get("page_count"),
        "lowest_toner_percent": stats.get("lowest_toner_percent"),
        "printer_status": stats.get("printer_status"),
        "device_status": stats.get("device_status"),
        "errors": stats.get("errors") or [],
        "console_text": stats.get("console_text"),
        "supplies": stats.get("supplies") or [],
        "trays": stats.get("trays") or [],
        "polled_at": stats.get("polled_at"),
        "error": stats.get("error"),
    }
    return data


def _auth_payload(user: User | None) -> dict[str, Any]:
    return {
        "signed_in": user is not None,
        "username": user.username if user else None,
        "unconfigured": state.users.is_unconfigured,
        "user_count": state.users.count(),
    }


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = current_user(request)
    csrf_token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "base": _base_path(request),
            "version": VERSION,
            "subnets": state.settings.subnets,
            "subnets_autodetected": state.settings.subnets_autodetected,
            "low_threshold": state.settings.low_toner_threshold,
            "poll_interval": state.settings.poll_interval_seconds,
            "config_path": state.settings.config_path,
            "config_problems": state.config_problems,
            "auth": _auth_payload(user),
            "csrf_token": csrf_token,
            "colors": list(PRINTER_COLORS),
        },
    )
    _issue_csrf(request, response, csrf_token)
    return response


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str | None = Query(None)):
    if current_user(request) is not None:
        return RedirectResponse(url=_safe_next(request, next), status_code=303)
    csrf_token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "base": _base_path(request),
            "version": VERSION,
            "next": next or "",
            "error": None,
            "unconfigured": state.users.is_unconfigured,
            "csrf_token": csrf_token,
        },
    )
    _issue_csrf(request, response, csrf_token)
    return response


@app.post("/login", response_class=HTMLResponse)
async def login_submit(request: Request):
    # The body is parsed by hand rather than with FastAPI's Form() or
    # Starlette's request.form(): both require python-multipart, and this app
    # deliberately installs as three pure-Python wheels. The sign-in form is
    # plain urlencoded, which the standard library already handles.
    fields = parse_qs((await request.body()).decode("utf-8", "replace"))
    username = (fields.get("username") or [""])[0]
    password = (fields.get("password") or [""])[0]
    csrf_token = (fields.get("csrf_token") or [""])[0]
    next = (fields.get("next") or [""])[0]

    cookie = request.cookies.get(CSRF_COOKIE) or ""
    if not cookie or not hmac.compare_digest(cookie, csrf_token):
        error = "That form had gone stale. Try again."
    else:
        try:
            user = state.users.authenticate(username, password)
        except AuthError as exc:
            log.warning("failed sign-in for %r from %s", username,
                        request.client.host if request.client else "?")
            error = str(exc)
        else:
            log.info("%s signed in", user.username)
            response = RedirectResponse(url=_safe_next(request, next), status_code=303)
            _set_session_cookie(request, response, state.sessions.issue(user))
            _issue_csrf(request, response, cookie)
            return response

    csrf_token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "login.html",
        {
            "base": _base_path(request),
            "version": VERSION,
            "next": next or "",
            "error": error,
            "unconfigured": state.users.is_unconfigured,
            "csrf_token": csrf_token,
        },
        status_code=401,
    )
    _issue_csrf(request, response, csrf_token)
    return response


@app.post("/logout")
async def logout(request: Request):
    response = RedirectResponse(url=(_base_path(request) or "") + "/", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path=(_base_path(request) or "/"))
    return response


@app.get("/printer/{ip}", response_class=HTMLResponse)
async def printer_page(request: Request, ip: str):
    _valid_ip(ip)
    record = state.store.get(ip)
    if record is None:
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    csrf_token = _csrf_for(request)
    response = templates.TemplateResponse(
        request,
        "printer.html",
        {
            "base": _base_path(request),
            "version": VERSION,
            "printer": _record_payload(record),
            "low_threshold": state.settings.low_toner_threshold,
            "auth": _auth_payload(current_user(request)),
            "csrf_token": csrf_token,
            "colors": list(PRINTER_COLORS),
        },
    )
    _issue_csrf(request, response, csrf_token)
    return response


@app.get("/open/{ip}")
async def open_web_interface(ip: str):
    """Redirect straight to the printer's own embedded web interface."""
    _valid_ip(ip)
    record = state.store.get(ip)
    if record is None:
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    target = record.web_url or f"http://{ip}"
    return RedirectResponse(url=target, status_code=307)


# ---------------------------------------------------------------------------
# API -- printers
# ---------------------------------------------------------------------------


@app.get("/api/printers")
async def list_printers(
    request: Request,
    health: str | None = Query(None, description="Filter by health state"),
    enabled_only: bool = Query(False),
    q: str | None = Query(None, description="Substring match on name/model/IP"),
):
    records = state.store.all()
    if enabled_only:
        records = [r for r in records if r.enabled]
    if health:
        wanted = {part.strip() for part in health.split(",") if part.strip()}
        records = [r for r in records if r.health in wanted]
    if q:
        needle = q.lower()
        records = [
            r for r in records
            if needle in " ".join(
                str(value).lower() for value in (
                    r.ip, r.hostname, r.label, r.sys_name,
                    r.model, r.vendor, r.serial_number, r.location,
                ) if value
            )
        ]
    return {
        "printers": [_record_payload(r) for r in records],
        "summary": state.store.summary(),
        "poller": state.poller.status(),
        "server_time": time.time(),
        "low_toner_threshold": state.settings.low_toner_threshold,
        "auth": _auth_payload(current_user(request)),
    }


@app.get("/api/printers/{ip}")
async def get_printer(ip: str):
    _valid_ip(ip)
    record = state.store.get(ip)
    if record is None:
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    return _record_payload(record)


@app.post("/api/printers", status_code=201)
async def add_printer(
    payload: dict[str, Any] = Body(...),
    _user: User | None = Depends(require_auth),
):
    """Add a printer by hand, for anything discovery cannot see."""
    ip = str(payload.get("ip", "")).strip()
    if not ip:
        raise HTTPException(status_code=400, detail="'ip' is required")
    _valid_ip(ip)
    record = state.store.add_manual(
        ip,
        label=payload.get("label") or None,
        community=payload.get("community") or None,
    )
    state.poller.enrich_record(ip)
    stats = state.poller.poll_single(ip)
    record = state.store.get(ip) or record
    return {
        "printer": _record_payload(record),
        "reachable": bool(stats and stats.get("reachable")),
    }


@app.patch("/api/printers/{ip}")
async def update_printer(
    ip: str,
    payload: dict[str, Any] = Body(...),
    _user: User | None = Depends(require_auth),
):
    _valid_ip(ip)
    allowed = {"label", "notes", "color", "enabled", "web_url", "community"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if "web_url" in fields:
        fields["web_url"] = _validate_web_url(fields["web_url"])
    if "color" in fields:
        fields["color"] = _validate_color(fields["color"])
    if not fields:
        raise HTTPException(
            status_code=400,
            detail=f"nothing to update; allowed fields are {sorted(allowed)}",
        )
    record = state.store.set_fields(ip, **fields)
    if record is None:
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    return _record_payload(record)


@app.delete("/api/printers/{ip}")
async def delete_printer(ip: str, _user: User | None = Depends(require_auth)):
    _valid_ip(ip)
    if not state.store.delete(ip):
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    return {"deleted": ip}


@app.post("/api/printers/{ip}/refresh")
async def refresh_printer(ip: str, _user: User | None = Depends(require_auth)):
    _valid_ip(ip)
    if state.store.get(ip) is None:
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    stats = state.poller.poll_single(ip)
    record = state.store.get(ip)
    return {"printer": _record_payload(record), "stats": stats}


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

# Open like every other read: the same fields, notes included, are already
# served by /api/printers to anyone who can reach the dashboard.


@app.get("/export/printers.csv")
async def export_csv():
    return Response(
        content=export.to_csv(state.store.all()),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{export.filename("csv")}"',
        },
    )


@app.get("/export/printers.pdf")
async def export_pdf():
    return Response(
        content=export.to_pdf(state.store.all(), version=VERSION),
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'attachment; filename="{export.filename("pdf")}"',
        },
    )


# ---------------------------------------------------------------------------
# API -- subnets
# ---------------------------------------------------------------------------


@app.get("/api/subnets")
async def list_subnets():
    """The include and exclude lists, each entry with its validation result."""
    return state.subnets.payload()


@app.post("/api/subnets", status_code=201)
async def add_subnet(
    payload: dict[str, Any] = Body(...),
    _user: User | None = Depends(require_auth),
):
    kind = str(payload.get("kind") or "include")
    if kind not in {"include", "exclude"}:
        raise HTTPException(status_code=400, detail="kind must be include or exclude")
    try:
        entry = state.subnets.add(
            str(payload.get("cidr", "")),
            kind=kind,  # type: ignore[arg-type]
            label=payload.get("label"),
        )
    except SubnetError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"could not write {state.settings.subnets_file}: {exc}",
        )
    state.sync_subnets()
    log.info("subnet %s added to the %s list", entry.cidr, kind)
    return {"entry": entry.as_dict(), **state.subnets.payload()}


@app.patch("/api/subnets/{entry_id}")
async def update_subnet(
    entry_id: str,
    payload: dict[str, Any] = Body(...),
    _user: User | None = Depends(require_auth),
):
    if "label" not in payload and "enabled" not in payload:
        raise HTTPException(
            status_code=400,
            detail="nothing to update; send 'label' or 'enabled'",
        )
    try:
        entry = state.subnets.update(
            entry_id, label=payload.get("label"), enabled=payload.get("enabled")
        )
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"could not write {state.settings.subnets_file}: {exc}",
        )
    if entry is None:
        raise HTTPException(status_code=404, detail="no such subnet entry")
    state.sync_subnets()
    return {"entry": entry.as_dict(), **state.subnets.payload()}


@app.delete("/api/subnets/{entry_id}")
async def delete_subnet(entry_id: str, _user: User | None = Depends(require_auth)):
    try:
        entry = state.subnets.delete(entry_id)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"could not write {state.settings.subnets_file}: {exc}",
        )
    if entry is None:
        raise HTTPException(status_code=404, detail="no such subnet entry")
    state.sync_subnets()
    log.info("subnet %s removed", entry.cidr)
    return {"deleted": entry.as_dict(), **state.subnets.payload()}


@app.post("/api/subnets/{entry_id}/scan")
async def scan_subnet(entry_id: str, _user: User | None = Depends(require_auth)):
    """Sweep one range on its own, rather than the whole configured list."""
    found = state.subnets.get(entry_id)
    if found is None:
        raise HTTPException(status_code=404, detail="no such subnet entry")
    kind, entry = found
    if kind != "include":
        raise HTTPException(
            status_code=400, detail="exclusions cannot be scanned on their own"
        )
    scoped = Settings(**{
        **state.settings.as_dict(),
        "subnets": [entry.cidr],
        "exclude_ips": list(state.settings.exclude_ips),
    })
    targets = scoped.sweep_targets()
    if not targets:
        raise HTTPException(
            status_code=400,
            detail=f"{entry.cidr} expands to no addresses to scan",
        )
    if not state.poller.start_discovery(targets=targets, subnets=[entry.cidr]):
        return JSONResponse(
            status_code=409,
            content={
                "detail": "a discovery sweep is already running",
                "progress": state.poller.discovery.progress.snapshot(),
            },
        )
    return {
        "started": True,
        "subnet": entry.cidr,
        "target_count": len(targets),
        "progress": state.poller.discovery.progress.snapshot(),
    }


# ---------------------------------------------------------------------------
# API -- accounts
# ---------------------------------------------------------------------------


@app.get("/api/account")
async def account(request: Request):
    payload = _auth_payload(current_user(request))
    payload["users"] = [u.public() for u in state.users.all()]
    return payload


@app.post("/api/account/setup", status_code=201)
async def setup_first_account(request: Request, payload: dict[str, Any] = Body(...)):
    """
    Create the very first account.

    Open on purpose, and only while no account exists: someone has to be able
    to claim the dashboard, and until they do it is unprotected anyway. Once
    one account exists this returns 409 and further accounts need a sign-in.
    """
    if not state.users.is_unconfigured:
        raise HTTPException(
            status_code=409,
            detail="An account already exists; sign in to add more.",
        )
    _check_csrf(request)
    try:
        user = state.users.add(
            str(payload.get("username", "")), str(payload.get("password", ""))
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    log.info("first account %r created", user.username)
    response = JSONResponse(status_code=201, content={"user": user.public()})
    _set_session_cookie(request, response, state.sessions.issue(user))
    return response


@app.post("/api/account/users", status_code=201)
async def add_user(
    payload: dict[str, Any] = Body(...),
    _user: User | None = Depends(require_auth),
):
    try:
        user = state.users.add(
            str(payload.get("username", "")), str(payload.get("password", ""))
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"user": user.public()}


@app.post("/api/account/password")
async def change_password(
    request: Request,
    payload: dict[str, Any] = Body(...),
    user: User | None = Depends(require_auth),
):
    if user is None:
        raise HTTPException(status_code=401, detail="Sign in first.")
    try:
        state.users.authenticate(user.username, str(payload.get("current_password", "")))
    except AuthError:
        raise HTTPException(status_code=403, detail="Current password is wrong.")
    try:
        updated = state.users.set_password(
            user.username, str(payload.get("new_password", ""))
        )
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    # The old cookie is bound to the old hash and is now dead; re-issue so the
    # person who just changed their own password is not signed out by it.
    response = JSONResponse(content={"user": updated.public()})
    _set_session_cookie(request, response, state.sessions.issue(updated))
    return response


@app.delete("/api/account/users/{username}")
async def remove_user(username: str, _user: User | None = Depends(require_auth)):
    try:
        removed = state.users.delete(username)
    except AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not removed:
        raise HTTPException(status_code=404, detail=f"no such user {username!r}")
    return {"deleted": username}


# ---------------------------------------------------------------------------
# API -- discovery and status
# ---------------------------------------------------------------------------


@app.post("/api/discover")
async def start_discovery(_user: User | None = Depends(require_auth)):
    if not state.settings.subnets:
        raise HTTPException(
            status_code=400,
            detail="No subnets are configured. Add one before running a sweep.",
        )
    if not state.poller.start_discovery():
        return JSONResponse(
            status_code=409,
            content={
                "detail": "a discovery sweep is already running",
                "progress": state.poller.discovery.progress.snapshot(),
            },
        )
    return {
        "started": True,
        "progress": state.poller.discovery.progress.snapshot(),
    }


@app.get("/api/discover")
async def discovery_status():
    return state.poller.discovery.progress.snapshot()


@app.post("/api/discover/cancel")
async def cancel_discovery(_user: User | None = Depends(require_auth)):
    return {"cancelled": state.poller.discovery.cancel()}


@app.post("/api/poll")
async def poll_now(_user: User | None = Depends(require_auth)):
    state.poller.request_poll_now()
    return {"queued": True, "poller": state.poller.status()}


@app.get("/api/summary")
async def summary():
    return {
        "summary": state.store.summary(),
        "poller": state.poller.status(),
        "server_time": time.time(),
    }


@app.get("/api/config")
async def get_config(request: Request):
    """Expose the effective settings, with the community strings masked."""
    data = state.settings.as_dict()
    data["community"] = "***"
    data["communities"] = ["***" for _ in state.settings.communities]
    data["sweep_target_count"] = len(state.settings.sweep_targets())
    data["problems"] = state.config_problems
    data["version"] = VERSION
    data["subnets_file"] = str(state.settings.subnets_file)
    data["auth"] = _auth_payload(current_user(request))
    return data


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "version": VERSION,
        "printers": state.store.count(),
        "poller_running": state.poller.status()["running"],
        "accounts_configured": not state.users.is_unconfigured,
    }
