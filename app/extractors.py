"""Slot extraction strategies.

Each strategy takes the response text and returns a list of strings;
strings not seen before trigger a notification.
"""

from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

MODES: dict[str, str] = {
    "css": "CSS selector — each matched element is a slot",
    "css_absent": "CSS selector DISAPPEARED — alert when e.g. a \"no seats\" block is gone",
    "json": "Path in a JSON response, e.g. data.slots[*].date",
    "regex": "Regular expression over the response text",
    "contains": "Substring APPEARED in the response",
    "not_contains": "Substring DISAPPEARED from the response",
}

# Single slot key for yes/no modes, so the alert fires once while the condition holds.
SENTINEL = "SLOTS MAY BE AVAILABLE"

_WHITESPACE_RE = re.compile(r"\s+")


def _soup(text: str) -> BeautifulSoup:
    try:
        return BeautifulSoup(text, "lxml")
    except Exception:  # lxml unavailable
        return BeautifulSoup(text, "html.parser")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    elif not isinstance(value, str):
        value = str(value)
    return _WHITESPACE_RE.sub(" ", value).strip()


_PATH_TOKEN_RE = re.compile(r"([^.\[\]]+)|\[(\*|-?\d+)\]")


def json_path(data: Any, path: str) -> list[Any]:
    """Minimal JSONPath: `data.slots[*].start`, `[0].date`, `items[*]`."""
    values: list[Any] = [data]
    for match in _PATH_TOKEN_RE.finditer(path or ""):
        key, index = match.group(1), match.group(2)
        nxt: list[Any] = []
        for value in values:
            if key is not None:
                if isinstance(value, dict) and key in value:
                    nxt.append(value[key])
            elif isinstance(value, list):
                if index == "*":
                    nxt.extend(value)
                else:
                    position = int(index)
                    if -len(value) <= position < len(value):
                        nxt.append(value[position])
        values = nxt
        if not values:
            break
    return values


def extract(mode: str, expression: str, text: str) -> list[str]:
    expression = (expression or "").strip()

    if mode == "css":
        if not expression:
            raise ValueError("css mode requires a CSS selector")
        return [_clean(el.get_text(" ")) or _clean(el.get("value")) or "(empty element)"
                for el in _soup(text).select(expression)]

    if mode == "css_absent":
        if not expression:
            raise ValueError("css_absent mode requires a CSS selector")
        return [] if _soup(text).select(expression) else [SENTINEL]

    if mode == "json":
        data = json.loads(text)
        return [_clean(value) for value in json_path(data, expression)]

    if mode == "regex":
        if not expression:
            raise ValueError("regex mode requires a regular expression")
        pattern = re.compile(expression, re.IGNORECASE | re.DOTALL)
        found: list[str] = []
        for match in pattern.finditer(text):
            # First non-empty group, or the whole match.
            groups = [g for g in match.groups() if g]
            found.append(_clean(groups[0] if groups else match.group(0)))
        return found

    if mode in ("contains", "not_contains"):
        if not expression:
            raise ValueError("this mode requires a substring")
        present = expression.lower() in text.lower()
        target = present if mode == "contains" else not present
        return [SENTINEL] if target else []

    raise ValueError(f"unknown mode: {mode}")


def find_openings(text: str) -> dict[int, list[str]]:
    """Rows whose registration hasn't opened yet: {opens_at unix time: [titles]}.

    Titles come from the row's first cell, matching `.es-exam-title` slot text.
    """
    if "data-opens-at" not in text:
        return {}
    openings: dict[int, list[str]] = {}
    for element in _soup(text).select("[data-opens-at]"):
        try:
            opens_at = int(element.get("data-opens-at", ""))
        except ValueError:
            continue
        row = element.find_parent("tr")
        cell = row.find("td") if row is not None else None
        title = _clean(cell.get_text(" ")) if cell is not None else ""
        if opens_at and title:
            openings.setdefault(opens_at, []).append(title)
    return openings


def apply_filters(slots: list[str], exclude_pattern: str) -> list[str]:
    """Drops empty values, duplicates, and anything matching the exclude regex."""
    result: list[str] = []
    excluder = re.compile(exclude_pattern, re.IGNORECASE) if exclude_pattern.strip() else None
    for slot in slots:
        slot = _clean(slot)
        if not slot:
            continue
        if excluder and excluder.search(slot):
            continue
        result.append(slot)
    return list(dict.fromkeys(result))
