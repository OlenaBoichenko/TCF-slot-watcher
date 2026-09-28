"""Finds the Telegram chat_id and writes the credentials to .env.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import httpx

ENV_PATH = Path(__file__).resolve().parent / ".env"
TOKEN_RE = re.compile(r"^\d{6,}:[A-Za-z0-9_-]{30,}$")

CHAT_KINDS = {
    "private": "private chat",
    "group": "group",
    "supergroup": "supergroup",
    "channel": "channel",
}


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    """Updates the given keys, leaving the rest of the file intact."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    remaining = dict(updates)

    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"

    for key, value in remaining.items():
        lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def clean_token(raw: str) -> str:
    return raw.strip().strip("<>").strip()


def parse_args(argv: list[str]) -> tuple[str, str | None]:
    token, wanted = "", None
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--chat":
            index += 1
            wanted = argv[index].strip() if index < len(argv) else None
        elif item.startswith("--chat="):
            wanted = item.partition("=")[2].strip()
        elif not token:
            token = item
        index += 1
    return token, wanted


def describe(chat: dict) -> str:
    kind = CHAT_KINDS.get(chat.get("type", ""), chat.get("type", "?"))
    label = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
    return f"{kind}: {label}".strip().rstrip(":")


def main() -> int:
    token, wanted = parse_args(sys.argv[1:])
    token = clean_token(token or read_env().get("TELEGRAM_BOT_TOKEN", ""))

    if not token:
        print("Pass the token as an argument:  python get_chat_id.py 8594738597:AAG...")
        return 1

    if not TOKEN_RE.match(token):
        print(f"The token looks wrong: {token!r}")
        print("Expected format: 8594738597:AAGjhr... (digits, colon, long string).")
        print("Don't include the angle brackets < >.")
        return 1

    base = f"https://api.telegram.org/bot{token}"

    with httpx.Client(timeout=20) as client:
        me = client.get(f"{base}/getMe").json()
        if not me.get("ok"):
            print(f"Telegram rejected the token: {me.get('description')}")
            return 1
        username = me["result"].get("username")
        print(f"Bot found: @{username}")

        updates = client.get(f"{base}/getUpdates").json()
        if not updates.get("ok"):
            print(f"getUpdates error: {updates.get('description')}")
            return 1

        payload_keys = (
            "message", "edited_message", "channel_post", "edited_channel_post",
            "my_chat_member", "chat_member", "callback_query",
        )

        chats: dict[str, str] = {}
        for update in updates.get("result", []):
            for key in payload_keys:
                payload = update.get(key)
                if not isinstance(payload, dict):
                    continue
                chat = payload.get("chat")
                if not isinstance(chat, dict):
                    chat = (payload.get("message") or {}).get("chat")
                if isinstance(chat, dict) and chat.get("id") is not None:
                    chats[str(chat["id"])] = describe(chat)

        if not chats:
            print("\nThe bot has no messages yet.")
            print(f"Message @{username} directly, or send /start in a group it belongs to.")
            print("Then run this script again.")
            return 1

        if wanted is None and len(chats) > 1:
            print("\nSeveral chats found. Pick one and run the script again:\n")
            for chat_id, label in chats.items():
                print(f"  --chat {chat_id:<18} {label}")
            print("\nTo notify all members, use the group id (it starts with a minus).")
            return 0

        if wanted is not None:
            chat_id = wanted
            label = chats.get(wanted, "not found among recent messages")
            if wanted not in chats:
                print(f"\nWarning: {wanted} is not among recent messages. Saving it as is.")
        else:
            chat_id, label = next(iter(chats.items()))

        print(f"\nSelected chat_id: {chat_id}  ({label})")

        write_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat_id})
        print(f"Saved to {ENV_PATH}")

        test = client.post(f"{base}/sendMessage", json={
            "chat_id": chat_id,
            "text": "✅ Slot Watcher connected. TCF slot notifications will arrive here.",
        }).json()

    if test.get("ok"):
        print("\nTest message sent, check the chat.")
        print("Now push the new value to the server:  .\\push_secrets.ps1")
        return 0

    print(f"\nFailed to send the message: {test.get('description')}")
    print("If this is a group, make sure the bot is a member and allowed to post.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
