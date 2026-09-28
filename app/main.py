"""Web UI: monitor list, create/edit, check log, playground."""

from __future__ import annotations

import base64
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import checker, db, extractors, notifier, scheduler
from .config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    scheduler.start()
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(title="Slot Watcher", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if not settings.auth_enabled or request.url.path in ("/healthz", "/static"):
        return await call_next(request)
    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            user, _, password = decoded.partition(":")
            if (secrets.compare_digest(user, settings.admin_user)
                    and secrets.compare_digest(password, settings.admin_password)):
                return await call_next(request)
        except Exception:
            pass
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Slot Watcher"'})


def _form_payload(form) -> dict:
    def clean(key: str, default: str = "") -> str:
        return (form.get(key) or default).strip()

    interval = max(30, int(clean("interval_seconds", "600") or 600))
    return {
        "name": clean("name") or "Untitled",
        "url": clean("url"),
        "method": clean("method", "GET").upper(),
        "request_headers": clean("request_headers"),
        "request_body": clean("request_body"),
        "mode": clean("mode", "css"),
        "expression": form.get("expression") or "",
        "exclude_pattern": clean("exclude_pattern"),
        "interval_seconds": interval,
        "enabled": 1 if form.get("enabled") else 0,
        "notify_cooldown_seconds": max(0, int(clean("notify_cooldown_seconds", "0") or 0)),
        "alert_on_empty": 1 if form.get("alert_on_empty") else 0,
    }


@app.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok", "monitors": len(db.list_monitors())})


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(request, "index.html", {
        "monitors": db.list_monitors(),
        "settings": settings,
        "channels": _channel_summary(),
    })


def _channel_summary() -> list[str]:
    channels = []
    if settings.telegram_enabled:
        channels.append("Telegram")
    if settings.ntfy_enabled:
        channels.append(f"ntfy ({settings.ntfy_topic})")
    return channels


@app.get("/monitors/new", response_class=HTMLResponse)
async def new_monitor(request: Request):
    return templates.TemplateResponse(request, "form.html", {
        "monitor": None, "modes": extractors.MODES, "settings": settings,
    })


@app.post("/monitors")
async def create_monitor(request: Request):
    data = _form_payload(await request.form())
    monitor_id = db.create_monitor(data)
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)


@app.get("/monitors/{monitor_id}", response_class=HTMLResponse)
async def monitor_detail(request: Request, monitor_id: int):
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "detail.html", {
        "monitor": monitor,
        "logs": db.list_logs(monitor_id),
        "seen": db.list_seen_slots(monitor_id),
        "modes": extractors.MODES,
        "settings": settings,
    })


@app.get("/monitors/{monitor_id}/edit", response_class=HTMLResponse)
async def edit_monitor(request: Request, monitor_id: int):
    monitor = db.get_monitor(monitor_id)
    if monitor is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "form.html", {
        "monitor": monitor, "modes": extractors.MODES, "settings": settings,
    })


@app.post("/monitors/{monitor_id}")
async def save_monitor(request: Request, monitor_id: int):
    db.update_monitor(monitor_id, _form_payload(await request.form()))
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)


@app.post("/monitors/{monitor_id}/delete")
async def remove_monitor(monitor_id: int):
    db.delete_monitor(monitor_id)
    return RedirectResponse("/", status_code=303)


@app.post("/monitors/{monitor_id}/toggle")
async def toggle_monitor(monitor_id: int):
    monitor = db.get_monitor(monitor_id)
    if monitor is not None:
        db.set_enabled(monitor_id, not monitor["enabled"])
    return RedirectResponse("/", status_code=303)


@app.post("/monitors/{monitor_id}/check")
async def check_now(monitor_id: int):
    monitor = db.get_monitor(monitor_id)
    if monitor is not None:
        await checker.run_check(monitor)
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)


@app.post("/monitors/{monitor_id}/forget")
async def forget(monitor_id: int):
    """Forget seen slots so the next check notifies again."""
    db.forget_slots(monitor_id)
    return RedirectResponse(f"/monitors/{monitor_id}", status_code=303)


@app.get("/playground", response_class=HTMLResponse)
async def playground_form(request: Request):
    return templates.TemplateResponse(request, "playground.html", {
        "modes": extractors.MODES, "result": None, "form": {}, "settings": settings,
    })


@app.post("/playground", response_class=HTMLResponse)
async def playground_run(
    request: Request,
    url: str = Form(...),
    mode: str = Form("css"),
    expression: str = Form(""),
    exclude_pattern: str = Form(""),
):
    result: dict = {}
    try:
        slots, status_code, duration_ms = await checker.fetch_and_extract(
            url, mode=mode, expression=expression, exclude_pattern=exclude_pattern
        )
        result = {"ok": True, "slots": slots, "status_code": status_code, "duration_ms": duration_ms}
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return templates.TemplateResponse(request, "playground.html", {
        "modes": extractors.MODES,
        "result": result,
        "form": {"url": url, "mode": mode, "expression": expression,
                 "exclude_pattern": exclude_pattern},
        "settings": settings,
    })


@app.post("/test-notification")
async def test_notification():
    errors = await notifier.notify(
        "🔔 Test notification",
        "If you can see this, the channel is set up correctly.",
        settings.public_url or None,
    )
    log.info("Test notification, errors: %s", errors or "none")
    return RedirectResponse("/?test=" + ("ok" if not errors else "fail"), status_code=303)
