"""SQLite storage: monitors, seen slots, check log, registration openings."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS monitors (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    name                    TEXT    NOT NULL,
    url                     TEXT    NOT NULL,
    method                  TEXT    NOT NULL DEFAULT 'GET',
    request_headers         TEXT    NOT NULL DEFAULT '',
    request_body            TEXT    NOT NULL DEFAULT '',
    mode                    TEXT    NOT NULL DEFAULT 'css',
    expression              TEXT    NOT NULL DEFAULT '',
    exclude_pattern         TEXT    NOT NULL DEFAULT '',
    interval_seconds        INTEGER NOT NULL DEFAULT 600,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    notify_cooldown_seconds INTEGER NOT NULL DEFAULT 0,
    alert_on_empty          INTEGER NOT NULL DEFAULT 0,
    last_checked_at         TEXT,
    last_notified_at        TEXT,
    last_ok                 INTEGER,
    last_error              TEXT    NOT NULL DEFAULT '',
    last_found_count        INTEGER NOT NULL DEFAULT 0,
    consecutive_failures    INTEGER NOT NULL DEFAULT 0,
    created_at              TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_slots (
    monitor_id   INTEGER NOT NULL,
    slot_key     TEXT    NOT NULL,
    first_seen_at TEXT   NOT NULL,
    last_seen_at TEXT    NOT NULL,
    PRIMARY KEY (monitor_id, slot_key),
    FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS check_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id   INTEGER NOT NULL,
    checked_at   TEXT    NOT NULL,
    ok           INTEGER NOT NULL,
    status_code  INTEGER,
    found_count  INTEGER NOT NULL DEFAULT 0,
    new_count    INTEGER NOT NULL DEFAULT 0,
    duration_ms  INTEGER NOT NULL DEFAULT 0,
    error        TEXT    NOT NULL DEFAULT '',
    sample       TEXT    NOT NULL DEFAULT '',
    FOREIGN KEY (monitor_id) REFERENCES monitors(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_check_log_monitor ON check_log(monitor_id, id DESC);

-- Registration opening times seen on the page ("Opens in ..." buttons).
CREATE TABLE IF NOT EXISTS openings (
    url           TEXT    NOT NULL,
    opens_at      INTEGER NOT NULL,
    titles        TEXT    NOT NULL DEFAULT '',
    reminded_lead INTEGER,
    last_seen_at  TEXT    NOT NULL,
    PRIMARY KEY (url, opens_at)
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release; migrated in place.
LATER_COLUMNS = {
    "alert_on_empty": "INTEGER NOT NULL DEFAULT 0",
}


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(monitors)")}
        for column, definition in LATER_COLUMNS.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE monitors ADD COLUMN {column} {definition}")


# --- monitors ---------------------------------------------------------------

MONITOR_FIELDS = (
    "name", "url", "method", "request_headers", "request_body", "mode",
    "expression", "exclude_pattern", "interval_seconds", "enabled",
    "notify_cooldown_seconds", "alert_on_empty",
)


def list_monitors() -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute("SELECT * FROM monitors ORDER BY id").fetchall()


def get_monitor(monitor_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM monitors WHERE id = ?", (monitor_id,)).fetchone()


def create_monitor(data: dict[str, Any]) -> int:
    payload = {field: data.get(field) for field in MONITOR_FIELDS}
    columns = ", ".join((*MONITOR_FIELDS, "created_at"))
    placeholders = ", ".join("?" for _ in range(len(MONITOR_FIELDS) + 1))
    with connect() as conn:
        cur = conn.execute(
            f"INSERT INTO monitors ({columns}) VALUES ({placeholders})",
            (*payload.values(), utcnow()),
        )
        return int(cur.lastrowid)


def update_monitor(monitor_id: int, data: dict[str, Any]) -> None:
    payload = {field: data.get(field) for field in MONITOR_FIELDS}
    assignments = ", ".join(f"{field} = ?" for field in MONITOR_FIELDS)
    with connect() as conn:
        conn.execute(
            f"UPDATE monitors SET {assignments} WHERE id = ?",
            (*payload.values(), monitor_id),
        )


def delete_monitor(monitor_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM seen_slots WHERE monitor_id = ?", (monitor_id,))
        conn.execute("DELETE FROM check_log WHERE monitor_id = ?", (monitor_id,))
        conn.execute("DELETE FROM monitors WHERE id = ?", (monitor_id,))


def set_enabled(monitor_id: int, enabled: bool) -> None:
    with connect() as conn:
        conn.execute("UPDATE monitors SET enabled = ? WHERE id = ?", (int(enabled), monitor_id))


def record_check_state(
    monitor_id: int,
    *,
    ok: bool,
    error: str,
    found_count: int,
    consecutive_failures: int,
    notified: bool,
) -> None:
    now = utcnow()
    with connect() as conn:
        conn.execute(
            """
            UPDATE monitors
               SET last_checked_at = ?, last_ok = ?, last_error = ?,
                   last_found_count = ?, consecutive_failures = ?,
                   last_notified_at = CASE WHEN ? THEN ? ELSE last_notified_at END
             WHERE id = ?
            """,
            (now, int(ok), error, found_count, consecutive_failures, int(notified), now, monitor_id),
        )


# --- slots ------------------------------------------------------------------

def diff_and_store_slots(monitor_id: int, slots: Iterable[str], stale_after_seconds: int) -> list[str]:
    """Stores current slots and returns the ones not seen before.

    Slots missing for longer than `stale_after_seconds` are forgotten.
    """
    now = utcnow()
    slots = list(dict.fromkeys(slots))
    with connect() as conn:
        known = {row["slot_key"] for row in conn.execute(
            "SELECT slot_key FROM seen_slots WHERE monitor_id = ?", (monitor_id,)
        )}
        fresh = [slot for slot in slots if slot not in known]
        conn.executemany(
            """
            INSERT INTO seen_slots (monitor_id, slot_key, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(monitor_id, slot_key) DO UPDATE SET last_seen_at = excluded.last_seen_at
            """,
            [(monitor_id, slot, now, now) for slot in slots],
        )
        conn.execute(
            """
            DELETE FROM seen_slots
             WHERE monitor_id = ?
               AND (julianday(?) - julianday(last_seen_at)) * 86400.0 > ?
            """,
            (monitor_id, now, stale_after_seconds),
        )
    return fresh


def list_seen_slots(monitor_id: int, limit: int = 50) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM seen_slots WHERE monitor_id = ? ORDER BY last_seen_at DESC LIMIT ?",
            (monitor_id, limit),
        ).fetchall()


def forget_slots(monitor_id: int) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM seen_slots WHERE monitor_id = ?", (monitor_id,))


# --- registration openings ---------------------------------------------------

OPENING_KEEP_SECONDS = 2 * 86400


def store_openings(url: str, openings: dict[int, list[str]]) -> None:
    now = utcnow()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO openings (url, opens_at, titles, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(url, opens_at) DO UPDATE
               SET titles = excluded.titles, last_seen_at = excluded.last_seen_at
            """,
            [(url, opens_at, "\n".join(titles), now) for opens_at, titles in openings.items()],
        )
        conn.execute(
            "DELETE FROM openings WHERE opens_at < ?",
            (int(datetime.now(timezone.utc).timestamp()) - OPENING_KEEP_SECONDS,),
        )


def claim_reminder(url: str, opens_at: int, lead_seconds: int) -> tuple[bool, int | None]:
    """Atomically marks a reminder as sent, so concurrent monitors send it once.

    Returns (claimed, previous value for release_reminder).
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT reminded_lead FROM openings WHERE url = ? AND opens_at = ?", (url, opens_at)
        ).fetchone()
        if row is None:
            return False, None
        cur = conn.execute(
            """
            UPDATE openings SET reminded_lead = ?
             WHERE url = ? AND opens_at = ?
               AND (reminded_lead IS NULL OR reminded_lead > ?)
            """,
            (lead_seconds, url, opens_at, lead_seconds),
        )
        return cur.rowcount == 1, row["reminded_lead"]


def release_reminder(url: str, opens_at: int, previous_lead: int | None) -> None:
    """Undoes claim_reminder after a failed send."""
    with connect() as conn:
        conn.execute(
            "UPDATE openings SET reminded_lead = ? WHERE url = ? AND opens_at = ?",
            (previous_lead, url, opens_at),
        )


def near_opening(url: str, now_ts: int, before: int, after: int) -> bool:
    """Whether now is within [opens_at - before, opens_at + after] of any opening for this URL."""
    with connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM openings WHERE url = ? AND opens_at BETWEEN ? AND ? LIMIT 1",
            (url, now_ts - after, now_ts + before),
        ).fetchone()
    return row is not None


def recently_opened_titles(url: str, now_ts: int, window: int) -> set[str]:
    """Titles whose registration opened within the last `window` seconds."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT titles FROM openings WHERE url = ? AND opens_at BETWEEN ? AND ?",
            (url, now_ts - window, now_ts),
        ).fetchall()
    return {title for row in rows for title in row["titles"].split("\n") if title}


# --- check log -----------------------------------------------------------------

def add_log(
    monitor_id: int,
    *,
    ok: bool,
    status_code: int | None,
    found_count: int,
    new_count: int,
    duration_ms: int,
    error: str,
    sample: str,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO check_log
                (monitor_id, checked_at, ok, status_code, found_count, new_count,
                 duration_ms, error, sample)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (monitor_id, utcnow(), int(ok), status_code, found_count, new_count,
             duration_ms, error, sample),
        )
        # Keep the last 200 entries per monitor.
        conn.execute(
            """
            DELETE FROM check_log
             WHERE monitor_id = ?
               AND id NOT IN (
                   SELECT id FROM check_log WHERE monitor_id = ? ORDER BY id DESC LIMIT 200
               )
            """,
            (monitor_id, monitor_id),
        )


def list_logs(monitor_id: int, limit: int = 40) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM check_log WHERE monitor_id = ? ORDER BY id DESC LIMIT ?",
            (monitor_id, limit),
        ).fetchall()
