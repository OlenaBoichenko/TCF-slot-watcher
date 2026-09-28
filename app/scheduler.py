"""Scheduler: every TICK_SECONDS, runs the monitors that are due."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from . import checker, db
from .config import settings

log = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None
# Monitors currently being checked, to prevent overlapping runs.
_running: set[int] = set()


# Poll on every tick around a registration opening time.
FAST_INTERVAL_SECONDS = 10
FAST_WINDOW_BEFORE = 2 * 60
FAST_WINDOW_AFTER = 5 * 60


def _is_due(monitor) -> bool:
    if not monitor["enabled"]:
        return False
    last = db.parse_ts(monitor["last_checked_at"])
    if last is None:
        return True
    now = datetime.now(timezone.utc)
    interval = int(monitor["interval_seconds"])
    if interval > FAST_INTERVAL_SECONDS and db.near_opening(
        monitor["url"], int(now.timestamp()), FAST_WINDOW_BEFORE, FAST_WINDOW_AFTER
    ):
        interval = FAST_INTERVAL_SECONDS
    return (now - last).total_seconds() >= interval


async def _check_one(monitor) -> None:
    monitor_id = int(monitor["id"])
    _running.add(monitor_id)
    try:
        outcome = await checker.run_check(monitor)
        if outcome.new_slots:
            log.info("Monitor %s: %d NEW SLOTS", monitor["name"], len(outcome.new_slots))
    except Exception:
        log.exception("Unexpected error in monitor %s", monitor["name"])
    finally:
        _running.discard(monitor_id)


async def tick() -> None:
    due = [m for m in db.list_monitors() if _is_due(m) and int(m["id"]) not in _running]
    if due:
        await asyncio.gather(*(_check_one(m) for m in due))


def start() -> AsyncIOScheduler:
    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="UTC")
    _scheduler.add_job(
        tick,
        "interval",
        seconds=settings.tick_seconds,
        id="tick",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(timezone.utc),
    )
    _scheduler.start()
    log.info("Scheduler started, tick every %d s", settings.tick_seconds)
    return _scheduler


def shutdown() -> None:
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
