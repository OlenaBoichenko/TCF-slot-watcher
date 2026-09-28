# Slot Watcher

A small service that watches booking pages for newly available slots and sends a
Telegram notification when one appears. It can also announce upcoming registration
openings and remind you before they start.

Monitors are configured in a web UI: a URL plus an extraction rule (CSS selector,
JSON path, regex, or substring match). 

## Tech stack

- Python, FastAPI, Jinja2
- httpx, BeautifulSoup + lxml
- APScheduler
- SQLite
- Telegram Bot API
- Docker, Fly.io
