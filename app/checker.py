"""Core: fetch the page, extract slots, diff against history, notify."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from dataclasses import dataclass, field

import httpx

from . import db, extractors, notifier
from .config import settings

log = logging.getLogger(__name__)

FAILURE_ALERT_THRESHOLD = 3
# A slot missing for this many intervals is forgotten.
STALE_INTERVALS = 3
# Reminder lead times before registration opens, in seconds.
REMINDER_LEADS = (24 * 3600, 15 * 60)
# A slot appearing within this many seconds after an opening time counts as "registration opened".
JUST_OPENED_WINDOW = 3600


@dataclass
class CheckOutcome:
    ok: bool
    slots: list[str] = field(default_factory=list)
    new_slots: list[str] = field(default_factory=list)
    error: str = ""
    status_code: int | None = None
    duration_ms: int = 0
    notified: bool = False
    notify_errors: list[str] = field(default_factory=list)


def _parse_headers(raw: str) -> dict[str, str]:
    """Accepts a JSON object or `Name: value` lines."""
    raw = (raw or "").strip()
    if not raw:
        return {}
    if raw.startswith("{"):
        parsed = json.loads(raw)
        return {str(k): str(v) for k, v in parsed.items()}
    headers: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip()] = value.strip()
    return headers


# Monitors watching the same URL share one response per tick.
SHARED_FETCH_TTL = 10.0
_fetch_cache: dict[tuple, tuple[float, str, int]] = {}
_inflight: dict[tuple, "asyncio.Future"] = {}


async def _fetch_text(
    url: str, method: str, headers: dict[str, str], body: str
) -> tuple[str, int, int, bool]:
    """Returns (text, status code, duration in ms, from_cache)."""
    key = (url, method, body, tuple(sorted(headers.items())))
    now = time.monotonic()

    cached = _fetch_cache.get(key)
    if cached is not None and now - cached[0] < SHARED_FETCH_TTL:
        return cached[1], cached[2], 0, True

    pending = _inflight.get(key)
    if pending is not None:
        text, status, error = await pending
        if error is not None:
            raise error
        return text, status, 0, True

    for stale_key in [k for k, v in _fetch_cache.items() if now - v[0] >= SHARED_FETCH_TTL]:
        _fetch_cache.pop(stale_key, None)

    future: asyncio.Future = asyncio.get_running_loop().create_future()
    _inflight[key] = future
    try:
        started = time.perf_counter()
        async with httpx.AsyncClient(timeout=settings.request_timeout, follow_redirects=True) as client:
            response = await client.request(
                method, url, headers=headers,
                content=(body or None) if method != "GET" else None,
            )
        duration_ms = int((time.perf_counter() - started) * 1000)

        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code} from site")
    except Exception as exc:
        future.set_result((None, None, exc))
        raise
    finally:
        _inflight.pop(key, None)

    _fetch_cache[key] = (time.monotonic(), response.text, response.status_code)
    future.set_result((response.text, response.status_code, None))
    return response.text, response.status_code, duration_ms, False


async def fetch_and_extract(
    url: str,
    *,
    method: str = "GET",
    request_headers: str = "",
    request_body: str = "",
    mode: str = "css",
    expression: str = "",
    exclude_pattern: str = "",
) -> tuple[list[str], int | None, int]:
    """Returns (slots, status code, duration in ms). Raises on error."""
    slots, status_code, duration_ms, _ = await _fetch_and_extract_with_text(
        url, method=method, request_headers=request_headers, request_body=request_body,
        mode=mode, expression=expression, exclude_pattern=exclude_pattern,
    )
    return slots, status_code, duration_ms


async def _fetch_and_extract_with_text(
    url: str,
    *,
    method: str,
    request_headers: str,
    request_body: str,
    mode: str,
    expression: str,
    exclude_pattern: str,
) -> tuple[list[str], int | None, int, str]:
    headers = {"User-Agent": settings.user_agent, "Accept-Language": "en,fr;q=0.8"}
    headers.update(_parse_headers(request_headers))

    text, status_code, duration_ms, _ = await _fetch_text(
        url, method.upper() or "GET", headers, request_body
    )

    slots = extractors.extract(mode, expression, text)
    slots = extractors.apply_filters(slots, exclude_pattern)
    return slots, status_code, duration_ms, text


async def run_check(monitor: sqlite3.Row, *, dry_run: bool = False) -> CheckOutcome:
    """Runs one check. dry_run=True skips state updates and notifications."""
    monitor_id = int(monitor["id"])
    name = monitor["name"]

    try:
        slots, status_code, duration_ms, text = await _fetch_and_extract_with_text(
            monitor["url"],
            method=monitor["method"],
            request_headers=monitor["request_headers"],
            request_body=monitor["request_body"],
            mode=monitor["mode"],
            expression=monitor["expression"],
            exclude_pattern=monitor["exclude_pattern"],
        )
    except Exception as exc:
        outcome = CheckOutcome(ok=False, error=f"{type(exc).__name__}: {exc}")
        if not dry_run:
            await _handle_failure(monitor, outcome)
        return outcome

    outcome = CheckOutcome(ok=True, slots=slots, status_code=status_code, duration_ms=duration_ms)
    if dry_run:
        return outcome

    openings = extractors.find_openings(text)
    if openings:
        db.store_openings(monitor["url"], openings)
    opens_at_by_title = {title: ts for ts, titles in openings.items() for title in titles}

    stale_after = max(int(monitor["interval_seconds"]), 60) * STALE_INTERVALS
    outcome.new_slots = db.diff_and_store_slots(monitor_id, slots, stale_after)

    if outcome.new_slots and _cooldown_passed(monitor):
        just_opened = db.recently_opened_titles(
            monitor["url"], int(time.time()), JUST_OPENED_WINDOW
        )
        title, body = notifier.format_slots_message(
            name, outcome.new_slots, opens_at_by_title, just_opened
        )
        outcome.notify_errors = await notifier.notify(title, body, monitor["url"])
        outcome.notified = not outcome.notify_errors
        if not outcome.notified:
            # Forget the slots so the next check retries the notification.
            for slot in outcome.new_slots:
                _forget_single_slot(monitor_id, slot)
    elif outcome.new_slots:
        log.info("Monitor %s: %d new slots suppressed by cooldown", name, len(outcome.new_slots))

    # Non-empty to empty usually means the selector broke.
    previous_found = int(monitor["last_found_count"] or 0)
    markup_alarm = bool(monitor["alert_on_empty"]) and previous_found > 0 and not slots

    recovered = bool(monitor["consecutive_failures"] and monitor["consecutive_failures"] >= FAILURE_ALERT_THRESHOLD)
    db.record_check_state(
        monitor_id,
        ok=True,
        error="",
        found_count=len(slots),
        consecutive_failures=0,
        notified=outcome.notified,
    )
    db.add_log(
        monitor_id,
        ok=True,
        status_code=status_code,
        found_count=len(slots),
        new_count=len(outcome.new_slots),
        duration_ms=duration_ms,
        error="; ".join(outcome.notify_errors),
        sample=" | ".join(slots[:5]),
    )
    if recovered:
        await notifier.notify(f"✅ Monitor recovered: {name}", "The last check succeeded.", monitor["url"])
    if markup_alarm:
        await notifier.notify(
            f"⚠️ Monitor found nothing: {name}",
            f"The previous check found {previous_found} items, this one found 0.\n"
            f"The site layout has probably changed and the selector needs fixing.",
            monitor["url"],
        )
        log.warning("Monitor %s: result dropped from %d to 0", name, previous_found)
    if openings:
        await _send_reminders(monitor["url"], openings)
    return outcome


async def _send_reminders(url: str, openings: dict[int, list[str]]) -> None:
    """Sends only the closest due reminder per opening time."""
    now = int(time.time())
    for opens_at, titles in sorted(openings.items()):
        seconds_left = opens_at - now
        due = [lead for lead in REMINDER_LEADS if 0 < seconds_left <= lead]
        if not due:
            continue
        lead = min(due)
        claimed, previous = db.claim_reminder(url, opens_at, lead)
        if not claimed:
            continue
        title, body = notifier.format_reminder_message(opens_at, titles, seconds_left)
        if await notifier.notify(title, body, url):
            db.release_reminder(url, opens_at, previous)


async def _handle_failure(monitor: sqlite3.Row, outcome: CheckOutcome) -> None:
    monitor_id = int(monitor["id"])
    failures = int(monitor["consecutive_failures"] or 0) + 1
    db.record_check_state(
        monitor_id,
        ok=False,
        error=outcome.error,
        found_count=int(monitor["last_found_count"] or 0),
        consecutive_failures=failures,
        notified=False,
    )
    db.add_log(
        monitor_id, ok=False, status_code=None, found_count=0, new_count=0,
        duration_ms=0, error=outcome.error, sample="",
    )
    log.warning("Monitor %s failed (%d in a row): %s", monitor["name"], failures, outcome.error)

    # Alert on the 3rd failure, then every 20th.
    if failures == FAILURE_ALERT_THRESHOLD or (failures > FAILURE_ALERT_THRESHOLD and failures % 20 == 0):
        await notifier.notify(
            f"⚠️ Monitor is failing: {monitor['name']}",
            f"{failures} failed checks in a row.\nLast error: {outcome.error}",
            monitor["url"],
        )


def _cooldown_passed(monitor: sqlite3.Row) -> bool:
    cooldown = int(monitor["notify_cooldown_seconds"] or 0)
    if cooldown <= 0:
        return True
    last = db.parse_ts(monitor["last_notified_at"])
    if last is None:
        return True
    from datetime import datetime, timezone
    return (datetime.now(timezone.utc) - last).total_seconds() >= cooldown


def _forget_single_slot(monitor_id: int, slot: str) -> None:
    with db.connect() as conn:
        conn.execute(
            "DELETE FROM seen_slots WHERE monitor_id = ? AND slot_key = ?", (monitor_id, slot)
        )
