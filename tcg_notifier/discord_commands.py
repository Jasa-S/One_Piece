from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests
import yaml

from .site_probe import probe

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

DEFAULT_OOS = [
    "Ausverkauft", "Nicht verfügbar", "Vergriffen",
    "Derzeit nicht verfügbar", "Sold out", "Out of stock",
]
DEFAULT_IN_STOCK = [
    "In den Warenkorb", "In den Einkaufswagen",
    "Auf Lager", "Lieferbar", "Sofort lieferbar", "Add to cart",
]

HELP_TEXT = """\
**TCG Notifier commands:**

Single entry:
`!add product <url> <name>`
`!add category <url> <name>`

Multiple entries in one message (block format):
```
!add product
https://shop.de/product/1 One Piece OP-09 Display
https://shop.de/product/2 Pokemon 151 TTB
```
```
!add category
https://shop.de/collections/op One Piece @ JK
https://shop.de/collections/pokemon Pokemon @ Shop
```

Other commands:
`!list` — show everything currently tracked
`!remove <name>` — stop tracking (partial name match)
`!help` — show this message

The bot auto-detects whether a site needs a headless browser."""


# ---------------------------------------------------------------------------
# Discord REST client
# ---------------------------------------------------------------------------

class _Discord:
    def __init__(self, token: str) -> None:
        self._s = requests.Session()
        self._s.headers["Authorization"] = f"Bot {token}"

    def messages(self, channel_id: str, after: str | None) -> list[dict]:
        params: dict[str, Any] = {"limit": 100}
        if after:
            params["after"] = after
        r = self._s.get(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()

    def reply(self, channel_id: str, content: str, reply_to: str) -> None:
        r = self._s.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            json={"content": content[:2000], "message_reference": {"message_id": reply_to}},
            timeout=10,
        )
        if r.status_code == 400:
            # Reply failed (e.g. original message deleted) — send as plain message
            log.warning("Reply failed (%s), sending as plain message", r.status_code)
            self._s.post(
                f"{DISCORD_API}/channels/{channel_id}/messages",
                json={"content": content[:2000]},
                timeout=10,
            )
        elif r.status_code >= 300:
            log.error("Discord send failed: %s %s", r.status_code, r.text[:200])

    def react(self, channel_id: str, message_id: str, emoji: str) -> None:
        r = self._s.put(
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}"
            f"/reactions/{quote(emoji)}/@me",
            timeout=10,
        )
        if r.status_code >= 300:
            log.warning("React failed: %s %s", r.status_code, r.text[:200])


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_add_product(data: dict, url: str, name: str) -> str:
    products: list = data.setdefault("products", []) or []
    if any(p.get("url") == url for p in products):
        return f"Already tracking `{url}`."

    info = probe(url)
    entry: dict = {
        "name": name,
        "shop": info["shop"],
        "url": url,
        "out_of_stock_text": DEFAULT_OOS,
        "in_stock_text": DEFAULT_IN_STOCK,
    }
    if info["needs_browser"]:
        entry["use_browser"] = True

    products.append(entry)
    data["products"] = products

    browser_note = " — ⚠️ site needs headless browser, set automatically." if info["needs_browser"] else " — plain HTTP."
    return f"✅ Added product **{name}**\nProbe: {info['note']}{browser_note}"


def _cmd_add_category(data: dict, url: str, name: str) -> str:
    categories: list = data.setdefault("categories", []) or []
    if any(c.get("url") == url for c in categories):
        return f"Already tracking `{url}`."

    info = probe(url)
    entry: dict = {
        "name": name,
        "shop": info["shop"],
        "url": url,
    }
    if info["link_pattern"]:
        entry["link_pattern"] = info["link_pattern"]
    if info["needs_browser"]:
        entry["use_browser"] = True

    categories.append(entry)
    data["categories"] = categories

    pattern_note = f" Link pattern: `{info['link_pattern']}`." if info["link_pattern"] else " No link pattern detected — add one manually in config.yaml if needed."
    browser_note = " ⚠️ site needs headless browser, set automatically." if info["needs_browser"] else ""
    return f"✅ Added category **{name}**\nProbe: {info['note']}.{pattern_note}{browser_note}"


def _cmd_list(data: dict) -> str:
    lines: list[str] = []
    for p in (data.get("products") or []):
        lines.append(f"📦 **{p.get('name','?')}** ({p.get('shop','?')}) — stock watch")
    for c in (data.get("categories") or []):
        lines.append(f"🗂️ **{c.get('name','?')}** ({c.get('shop','?')}) — new listings")
    return "\n".join(lines) if lines else "Nothing is being tracked yet."


def _cmd_remove(data: dict, query: str) -> str:
    q = query.lower()
    removed: list[str] = []
    for key in ("products", "categories"):
        before = data.get(key) or []
        after = [i for i in before if q not in i.get("name", "").lower()]
        for i in before:
            if q in i.get("name", "").lower():
                removed.append(i["name"])
        data[key] = after
    if removed:
        return "✅ Removed: " + ", ".join(f"**{n}**" for n in removed)
    return f"Nothing found matching `{query}`."


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _parse_commands(content: str) -> list[tuple[str, ...]]:
    """Parse a (possibly multi-line) message into a list of command tuples.

    Single-line:
        !add product https://... Name
        !add category https://... Name

    Block (type on first line, one url+name per subsequent line):
        !add product
        https://... Name 1
        https://... Name 2
    """
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    commands: list[tuple[str, ...]] = []
    i = 0
    while i < len(lines):
        parts = lines[i].split(None, 3)
        cmd = parts[0].lower() if parts else ""

        if cmd == "!add" and len(parts) == 2:
            # Block format: following non-command lines are "url name" pairs
            sub = parts[1].lower()
            i += 1
            while i < len(lines) and not lines[i].startswith("!"):
                pair = lines[i].split(None, 1)
                if len(pair) == 2:
                    commands.append(("!add", sub, pair[0], pair[1]))
                elif len(pair) == 1:
                    commands.append(("!add", sub, pair[0], pair[0]))
                i += 1
        else:
            commands.append(tuple(parts))
            i += 1

    return commands


def _dispatch(data: dict, parts: tuple[str, ...]) -> tuple[str, bool]:
    """Execute one parsed command. Returns (reply_text, config_changed)."""
    cmd = parts[0].lower() if parts else ""

    if cmd == "!help":
        return HELP_TEXT, False
    if cmd == "!list":
        return _cmd_list(data), False
    if cmd == "!remove":
        if len(parts) < 2:
            return "Usage: `!remove <name>`", False
        return _cmd_remove(data, " ".join(parts[1:])), True
    if cmd == "!add":
        if len(parts) < 4:
            return "Usage: `!add product <url> <name>` or `!add category <url> <name>`", False
        sub, url, name = parts[1].lower(), parts[2], parts[3]
        if sub == "product":
            return _cmd_add_product(data, url, name), True
        if sub == "category":
            return _cmd_add_category(data, url, name), True
        return f"Unknown type `{sub}`. Use `product` or `category`.", False

    return f"Unknown command `{cmd}`. Type `!help` for usage.", False


def run(config_path: Path, state_path: Path) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        log.error("DISCORD_BOT_TOKEN environment variable not set.")
        return

    raw: dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    channel_id = str((raw.get("discord") or {}).get("command_channel_id", "")).strip()
    if not channel_id:
        log.error("discord.command_channel_id not configured in config.yaml.")
        return

    discord = _Discord(token)

    state: dict = {}
    if state_path.exists():
        import json
        try:
            state = __import__("json").loads(state_path.read_text())
        except Exception:
            pass

    last_id: str | None = state.get("last_message_id")

    try:
        messages = discord.messages(channel_id, after=last_id)
    except Exception as e:
        log.error("Failed to fetch Discord messages: %s", e)
        return

    # API returns newest-first; process oldest-first
    messages = [m for m in reversed(messages) if m["content"].strip().startswith("!")]
    config_changed = False

    for msg in messages:
        mid = msg["id"]
        content = msg["content"].strip()
        log.info("Command message: %s", content[:120])

        reply_lines: list[str] = []
        changed = False
        try:
            for cmd_parts in _parse_commands(content):
                line_reply, line_changed = _dispatch(raw, cmd_parts)
                reply_lines.append(line_reply)
                changed = changed or line_changed
        except Exception as e:
            reply_lines.append(f"⚠️ Error: {e}")
            log.exception("Command error")

        config_changed = config_changed or changed
        reply = "\n".join(reply_lines) or "Done."

        try:
            discord.reply(channel_id, reply, reply_to=mid)
            discord.react(channel_id, mid, "✅")
        except Exception as e:
            log.warning("Failed to send Discord reply: %s", e)

        state["last_message_id"] = mid

    import json
    state_path.write_text(json.dumps(state, indent=2))

    if config_changed:
        config_path.write_text(
            yaml.dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        log.info("config.yaml updated.")


if __name__ == "__main__":
    import logging
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stdout,
    )
    run(Path("config.yaml"), Path("discord_state.json"))
