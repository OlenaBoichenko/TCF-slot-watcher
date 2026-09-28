"""Notifications: Telegram (primary) and ntfy (optional push)."""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

from .config import settings

log = logging.getLogger(__name__)

MAX_SLOTS_IN_MESSAGE = 15


class NotifyError(RuntimeError):
    pass


async def _send_telegram(title: str, body: str, url: str | None) -> None:
    text = f"<b>{html.escape(title)}</b>\n\n{html.escape(body)}"
    if url:
        text += f'\n\n<a href="{html.escape(url, quote=True)}">Open booking page</a>'
    endpoint = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(endpoint, json={
            "chat_id": settings.telegram_chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })
    if response.status_code != 200:
        raise NotifyError(f"Telegram returned {response.status_code}: {response.text[:300]}")


async def _send_ntfy(title: str, body: str, url: str | None) -> None:
    headers = {"Title": title.encode("utf-8"), "Priority": "high", "Tags": "bell"}
    if url:
        headers["Click"] = url
    endpoint = f"{settings.ntfy_server.rstrip('/')}/{settings.ntfy_topic}"
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(endpoint, content=body.encode("utf-8"), headers=headers)
    if response.status_code >= 300:
        raise NotifyError(f"ntfy returned {response.status_code}: {response.text[:300]}")


async def notify(title: str, body: str, url: str | None = None) -> list[str]:
    """Sends to every configured channel. Returns a list of errors (empty = success)."""
    errors: list[str] = []
    sent_anywhere = False

    for enabled, sender, name in (
        (settings.telegram_enabled, _send_telegram, "telegram"),
        (settings.ntfy_enabled, _send_ntfy, "ntfy"),
    ):
        if not enabled:
            continue
        sent_anywhere = True
        try:
            await sender(title, body, url)
            log.info("Notification sent via %s: %s", name, title)
        except Exception as exc:
            log.exception("Failed to send via %s", name)
            errors.append(f"{name}: {exc}")

    if not sent_anywhere:
        errors.append("no notification channel configured (set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)")
    return errors


# Explicit names so output doesn't depend on the system locale.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def format_moment(unix_ts: int) -> str:
    """E.g. "Tue Oct 6, 15:00 PDT" in the configured timezone."""
    try:
        tz = ZoneInfo(settings.timezone)
    except Exception:
        tz = timezone.utc
    moment = datetime.fromtimestamp(unix_ts, tz)
    return (f"{_WEEKDAYS[moment.weekday()]} {_MONTHS[moment.month - 1]} {moment.day}, "
            f"{moment:%H:%M} {moment.tzname()}")


def format_duration(seconds: int) -> str:
    minutes = max(seconds, 0) // 60
    if minutes >= 24 * 60:
        return f"{minutes // (24 * 60)}d {minutes % (24 * 60) // 60}h"
    if minutes >= 60:
        return f"{minutes // 60}h {minutes % 60}m"
    return f"{max(minutes, 1)} min"


def format_slots_message(
    monitor_name: str,
    slots: list[str],
    opens_at: dict[str, int] | None = None,
    just_opened: set[str] | None = None,
) -> tuple[str, str]:
    """opens_at: slots whose registration is upcoming; just_opened: slots whose registration just opened."""
    opens_at = opens_at or {}
    just_opened = just_opened or set()

    shown = slots[:MAX_SLOTS_IN_MESSAGE]
    lines = []
    for slot in shown:
        if slot in opens_at:
            lines.append(f"• {slot} — registration opens {format_moment(opens_at[slot])}")
        else:
            lines.append(f"• {slot}")
    if len(slots) > len(shown):
        lines.append(f"…and {len(slots) - len(shown)} more")

    if any(slot in just_opened for slot in slots):
        title = f"🟢 Registration is open: {monitor_name}"
    elif all(slot in opens_at for slot in slots):
        title = f"📅 New dates, registration not open yet: {monitor_name}"
        lines.append("")
        lines.append("Reminders will follow 24 hours and 15 minutes before opening, "
                     "plus a separate message when registration opens.")
    else:
        title = f"🎯 New slots: {monitor_name}"
    return title, "\n".join(lines)


def format_reminder_message(opens_at: int, titles: list[str], seconds_left: int) -> tuple[str, str]:
    shown = titles[:MAX_SLOTS_IN_MESSAGE]
    lines = [f"Opens: {format_moment(opens_at)}", ""]
    lines += [f"• {title}" for title in shown]
    if len(titles) > len(shown):
        lines.append(f"…and {len(titles) - len(shown)} more")
    lines += ["", "You'll get another message when registration opens."]
    title = f"⏰ Registration opens in {format_duration(seconds_left)}"
    return title, "\n".join(lines)
