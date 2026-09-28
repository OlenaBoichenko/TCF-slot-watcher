"""Creates the TCF monitors for Alliance Française Victoria.

Run: .venv/Scripts/python.exe seed_afvictoria.py
Idempotent: existing monitors are matched by selector and updated.
"""

from __future__ import annotations

import asyncio

from app import checker, db

PAGE = "https://www.afvictoria.ca/language/exams/tcf/"
URL = PAGE + "?s8-datatable1_rows=200"

BOOKABLE_SELECTOR = (
    "tr.tableRow:not(:has(.es-status-closed)):not(:has(.es-status-full))"
    ":not(:has(.es-opens-countdown)) .es-exam-title"
)

MONITORS = [
    {
        "name": "TCF Victoria — seat available",
        "url": URL,
        "method": "GET",
        "request_headers": "",
        "request_body": "",
        "mode": "css",
        "expression": BOOKABLE_SELECTOR,
        "exclude_pattern": "",
        "interval_seconds": 60,
        "enabled": 1,
        "notify_cooldown_seconds": 0,
        "alert_on_empty": 0,
        "prefill_seen": False,
    },
    {
        "name": "TCF Victoria — new exam date",
        "url": URL,
        "method": "GET",
        "request_headers": "",
        "request_body": "",
        "mode": "css",
        "expression": ".es-exam-title",
        "exclude_pattern": "",
        "interval_seconds": 60,
        "enabled": 1,
        "notify_cooldown_seconds": 0,
        "alert_on_empty": 1,
        "prefill_seen": True,
    },
]


async def main() -> None:
    db.init_db()
    existing = {
        row["expression"]: row["id"]
        for row in db.list_monitors()
        if row["url"].startswith(PAGE)
    }

    for spec in MONITORS:
        spec = dict(spec)
        prefill = spec.pop("prefill_seen")
        name = spec["name"]

        monitor_id = existing.get(spec["expression"])
        if monitor_id is not None:
            db.update_monitor(monitor_id, spec)
            print(f"updated #{monitor_id}: {name}")
        else:
            monitor_id = db.create_monitor(spec)
            print(f"created #{monitor_id}: {name}")

        slots, status, ms = await checker.fetch_and_extract(
            spec["url"], mode=spec["mode"], expression=spec["expression"]
        )
        print(f"    check: HTTP {status} in {ms} ms, found {len(slots)}")

        if prefill and slots:
            db.diff_and_store_slots(monitor_id, slots, spec["interval_seconds"] * 3)
            print(f"    marked {len(slots)} current dates as seen")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
