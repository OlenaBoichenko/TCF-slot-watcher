"""Settings from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _str(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class Settings:
    db_path: str
    tick_seconds: int
    request_timeout: int
    user_agent: str
    telegram_bot_token: str
    telegram_chat_id: str
    ntfy_server: str
    ntfy_topic: str
    admin_user: str
    admin_password: str
    public_url: str
    timezone: str

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def ntfy_enabled(self) -> bool:
        return bool(self.ntfy_topic)

    @property
    def auth_enabled(self) -> bool:
        return bool(self.admin_password)


def load_settings() -> Settings:
    db_path = _str("DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "slots.db"))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return Settings(
        db_path=db_path,
        # Scheduler tick; each monitor has its own polling interval.
        tick_seconds=_int("TICK_SECONDS", 15),
        request_timeout=_int("REQUEST_TIMEOUT", 30),
        user_agent=_str("USER_AGENT", DEFAULT_USER_AGENT),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=_str("TELEGRAM_CHAT_ID"),
        ntfy_server=_str("NTFY_SERVER", "https://ntfy.sh"),
        ntfy_topic=_str("NTFY_TOPIC"),
        admin_user=_str("ADMIN_USER", "admin"),
        admin_password=_str("ADMIN_PASSWORD"),
        public_url=_str("PUBLIC_URL"),
        # Time zone for opening times in messages.
        timezone=_str("TIMEZONE", "America/Vancouver"),
    )


settings = load_settings()
