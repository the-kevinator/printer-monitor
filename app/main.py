"""
FastAPI application: JSON API plus the dashboard page.

Hosted under IIS via HttpPlatformHandler (see web.config), or run directly
with `python -m uvicorn app.main:app` for development.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import Settings, load_settings
from .discovery import DiscoveryRunner
from .poller import PollerService
from .store import PrinterStore

log = logging.getLogger("printer_monitor")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)

APP_DIR = Path(__file__).resolve().parent
VERSION = "1.0.0"


class AppState:
    """Container for the long-lived objects, built once at startup."""

    settings: Settings
    store: PrinterStore
    poller: PollerService

    def __init__(self) -> None:
        self.settings = load_settings()
        problems = self.settings.validate()
        for problem in problems:
            log.warning("config problem: %s", problem)
        Path(self.settings.data_dir).mkdir(parents=True, exist_ok=True)
        self.store = PrinterStore(self.settings.printers_file)
        self.store.load()
        self.poller = PollerService(
            self.settings, self.store, DiscoveryRunner(self.settings)
        )
        self.config_problems = problems
        log.info(
            "loaded %d printer(s); subnets=%s%s",
            self.store.count(),
            self.settings.subnets,
            " (autodetected)" if self.settings.subnets_autodetected else "",
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


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse(
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
        },
    )


@app.get("/printer/{ip}", response_class=HTMLResponse)
async def printer_page(request: Request, ip: str):
    _valid_ip(ip)
    record = state.store.get(ip)
    if record is None:
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    return templates.TemplateResponse(
        request,
        "printer.html",
        {
            "base": _base_path(request),
            "version": VERSION,
            "printer": _record_payload(record),
            "low_threshold": state.settings.low_toner_threshold,
        },
    )


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
    }


@app.get("/api/printers/{ip}")
async def get_printer(ip: str):
    _valid_ip(ip)
    record = state.store.get(ip)
    if record is None:
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    return _record_payload(record)


@app.post("/api/printers", status_code=201)
async def add_printer(payload: dict[str, Any] = Body(...)):
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
async def update_printer(ip: str, payload: dict[str, Any] = Body(...)):
    _valid_ip(ip)
    allowed = {"label", "notes", "enabled", "web_url", "community"}
    fields = {k: v for k, v in payload.items() if k in allowed}
    if "web_url" in fields:
        fields["web_url"] = _validate_web_url(fields["web_url"])
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
async def delete_printer(ip: str):
    _valid_ip(ip)
    if not state.store.delete(ip):
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    return {"deleted": ip}


@app.post("/api/printers/{ip}/refresh")
async def refresh_printer(ip: str):
    _valid_ip(ip)
    if state.store.get(ip) is None:
        raise HTTPException(status_code=404, detail=f"{ip} is not a known printer")
    stats = state.poller.poll_single(ip)
    record = state.store.get(ip)
    return {"printer": _record_payload(record), "stats": stats}


# ---------------------------------------------------------------------------
# API -- discovery and status
# ---------------------------------------------------------------------------


@app.post("/api/discover")
async def start_discovery():
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
async def cancel_discovery():
    return {"cancelled": state.poller.discovery.cancel()}


@app.post("/api/poll")
async def poll_now():
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
async def get_config():
    """Expose the effective settings, with the community strings masked."""
    data = state.settings.as_dict()
    data["community"] = "***"
    data["communities"] = ["***" for _ in state.settings.communities]
    data["sweep_target_count"] = len(state.settings.sweep_targets())
    data["problems"] = state.config_problems
    data["version"] = VERSION
    return data


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "version": VERSION,
        "printers": state.store.count(),
        "poller_running": state.poller.status()["running"],
    }
