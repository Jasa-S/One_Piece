from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import requests
import yaml

from .checker import check_product
from .config import DEFAULT_IN_STOCK, DEFAULT_OOS, Defaults, Product, is_naver_smartstore, load_config
from .site_probe import probe

log = logging.getLogger(__name__)

DISCORD_API = "https://discord.com/api/v10"

HELP_TEXT = """\
**TCG Notifier commands:**

Single entry:
`!add product <url> <name>`
`!add category <url> <name>`
`!add category <url> <product_example_url> <name>` ← auto-detects pattern
`!add category <url> /link_pattern/ <name>` ← explicit pattern

Multiple entries in one message (block format):
!add product
https://shop.de/product/1 One Piece OP-09 Display
https://shop.de/product/2 Pokemon 151 TTB

!add category
https://shop.de/collections/op https://shop.de/products/op09 One Piece @ JK
https://shop.de/collections/pokemon /de/product/ Pokemon @ Shop


Other commands:
`!list` — live stock check + full status of everything tracked
`!remove <name>` — stop tracking (partial name match)
`!setpattern <name> /pattern/` — set or update link pattern for a category
`!reset` — deletes all tracked items and purges ALL channel messages
`!help` — show this message

The bot auto-detects whether a site needs a headless browser.
Link pattern is optional — auto-detected when possible."""


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
        r = self._s.get(f"{DISCORD_API}/channels/{channel_id}/messages", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def reply(self, channel_id: str, content: str, reply_to: str) -> None:
        r = self._s.post(
            f"{DISCORD_API}/channels/{channel_id}/messages",
            json={"content": content[:2000], "message_reference": {"message_id": reply_to}},
            timeout=10,
        )
        if r.status_code == 400:
            log.warning("Reply failed (%s), sending as plain message", r.status_code)
            self._s.post(f"{DISCORD_API}/channels/{channel_id}/messages", json={"content": content[:2000]}, timeout=10)
        elif r.status_code >= 300:
            log.error("Discord send failed: %s %s", r.status_code, r.text[:200])

    def react(self, channel_id: str, message_id: str, emoji: str) -> None:
        r = self._s.put(
            f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/{quote(emoji)}/@me",
            timeout=10,
        )
        if r.status_code >= 300:
            log.warning("React failed: %s %s", r.status_code, r.text[:200])

    def delete_message(self, channel_id: str, message_id: str) -> None:
        r = self._s.delete(f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}", timeout=10)
        if r.status_code == 429:
            retry_after = r.json().get("retry_after", 1)
            log.warning("Rate limited on delete, sleeping %.1fs", retry_after)
            time.sleep(retry_after)
            self.delete_message(channel_id, message_id)
        elif r.status_code not in (200, 204):
            log.warning("Delete failed: %s %s", r.status_code, r.text[:200])

    def post(self, channel_id: str, content: str) -> dict:
        r = self._s.post(f"{DISCORD_API}/channels/{channel_id}/messages", json={"content": content[:2000]}, timeout=10)
        r.raise_for_status()
        return r.json()

    def delete_all_messages(self, channel_id: str) -> int:
        total = 0
        before: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if before:
                params["before"] = before
            r = self._s.get(f"{DISCORD_API}/channels/{channel_id}/messages", params=params, timeout=10)
            r.raise_for_status()
            batch: list[dict] = r.json()
            if not batch:
                break
            ids = [m["id"] for m in batch]
            if len(ids) >= 2:
                bulk_r = self._s.post(
                    f"{DISCORD_API}/channels/{channel_id}/messages/bulk-delete",
                    json={"messages": ids}, timeout=10,
                )
                if bulk_r.status_code in (200, 204):
                    total += len(ids)
                else:
                    for mid in ids:
                        self.delete_message(channel_id, mid)
                        total += 1
                        time.sleep(0.3)
            else:
                self.delete_message(channel_id, ids[0])
                total += 1
            before = ids[-1]
            time.sleep(0.5)
        return total


# ---------------------------------------------------------------------------
# Live stock check helper (used by !list)
# ---------------------------------------------------------------------------

def _live_check_all(data: dict, defaults: Defaults) -> dict:
    """
    Run a live stock check on every explicit product and every known category URL.
    Returns an updated stock_state dict (same shape as state.json) with fresh results.
    """
    session = requests.Session()
    tasks: list[tuple[str, str, Product]] = []  # (kind, key, stub)

    # Explicit products
    for p in (data.get("products") or []):
        url = p.get("url", "")
        if not url:
            continue
        stub = Product(
            name=p.get("name", url),
            url=url,
            shop=p.get("shop", ""),
            in_stock_text=p.get("in_stock_text") or list(DEFAULT_IN_STOCK),
            out_of_stock_text=p.get("out_of_stock_text") or list(DEFAULT_OOS),
            use_browser=p.get("use_browser", False) or is_naver_smartstore(url),
        )
        tasks.append(("product", url, stub))

    # Category URLs (all known_urls from state)
    cat_url_map: dict[str, str] = {}  # product_url -> category_url
    for c in (data.get("categories") or []):
        cat_url = c.get("url", "")
        known = data.get("_state_known_urls", {}).get(cat_url, [])
        for url in known:
            stub = Product(
                name=url,
                url=url,
                shop=c.get("shop", ""),
                in_stock_text=list(DEFAULT_IN_STOCK),
                out_of_stock_text=list(DEFAULT_OOS),
                use_browser=c.get("use_browser", False) or is_naver_smartstore(url),
            )
            tasks.append(("category", cat_url, stub))
            cat_url_map[url] = cat_url

    if not tasks:
        return {"products": {}, "categories": {}}

    results: dict = {"products": {}, "categories": {}}

    def _run(kind: str, key: str, stub: Product):
        sess = None if stub.use_browser else session
        result = check_product(stub, defaults, session=sess)
        return kind, key, stub.url, result

    with ThreadPoolExecutor(max_workers=defaults.max_workers) as pool:
        futures = [pool.submit(_run, kind, key, stub) for kind, key, stub in tasks]
        for fut in as_completed(futures):
            try:
                kind, key, url, result = fut.result()
            except Exception as e:
                log.warning("Live check failed for a URL: %s", e)
                continue
            if result is None:
                continue
            if kind == "product":
                results["products"][url] = {"in_stock": result.in_stock}
            else:  # category
                results["categories"].setdefault(key, {"stock": {}})["stock"][url] = result.in_stock

    return results


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

def _cmd_add_product(data: dict, url: str, name: str) -> str:
    products: list = data.setdefault("products", []) or []
    if any(p.get("url") == url for p in products):
        return f"Already tracking `{url}`."
    info = probe(url)
    entry: dict = {
        "name": name, "shop": info["shop"], "url": url,
        "out_of_stock_text": list(DEFAULT_OOS),
        "in_stock_text": list(DEFAULT_IN_STOCK),
    }
    if info["needs_browser"]:
        entry["use_browser"] = True
    products.append(entry)
    data["products"] = products
    browser_note = " — ⚠️ site needs headless browser." if info["needs_browser"] else " — plain HTTP."
    return f"✅ Added product **{name}**\nProbe: {info['note']}{browser_note}"


def _derive_link_pattern(category_url: str, product_url: str) -> str | None:
    cat_segs = [s for s in urlparse(category_url).path.strip("/").split("/") if s]
    prod_path = urlparse(product_url).path
    prod_segs = [s for s in prod_path.strip("/").split("/") if s]
    diverge_idx = len(cat_segs)
    for i, (cs, ps) in enumerate(zip(cat_segs, prod_segs)):
        if cs != ps:
            diverge_idx = i
            break
    if diverge_idx < len(prod_segs):
        diverging_seg = prod_segs[diverge_idx]
        if not re.search(r'\d', diverging_seg) and len(diverging_seg) <= 20:
            return "/" + "/".join(prod_segs[:diverge_idx + 1]) + "/"
    prod_filename = prod_path.split("/")[-1]
    if "." in prod_filename:
        ext = prod_filename.rsplit(".", 1)[-1]
        return rf"\.{ext}$"
    return None


def _cmd_add_category(data: dict, url: str, name: str, link_pattern_override: str | None = None) -> str:
    categories: list = data.setdefault("categories", []) or []
    if any(c.get("url") == url for c in categories):
        return f"Already tracking `{url}`."
    info = probe(url)
    effective_pattern = link_pattern_override or info["link_pattern"]
    entry: dict = {"name": name, "shop": info["shop"], "url": url}
    if effective_pattern:
        entry["link_pattern"] = effective_pattern
    if info["needs_browser"]:
        entry["use_browser"] = True
    categories.append(entry)
    data["categories"] = categories
    if effective_pattern:
        source = "provided" if link_pattern_override else "auto-detected"
        pattern_note = f" Link pattern: `{effective_pattern}` ({source})."
    else:
        pattern_note = " No link pattern — send `!setpattern {name} /pattern/` to add one."
    browser_note = " ⚠️ site needs headless browser." if info["needs_browser"] else ""
    return f"✅ Added category **{name}**\nProbe: {info['note']}.{pattern_note}{browser_note}"


def _cmd_list(data: dict, live_stock: dict) -> str:
    """Render the full tracking list using freshly-checked stock data."""
    lines: list[str] = []
    products_stock = live_stock.get("products") or {}
    categories_stock = live_stock.get("categories") or {}

    # ---- explicit products ----
    products = data.get("products") or []
    if products:
        available = sold_out = unknown = 0
        product_lines = []
        for p in products:
            st = products_stock.get(p.get("url", ""))
            if st is None or "in_stock" not in st:
                status, unknown = "⚪ unknown", unknown + 1
            elif st["in_stock"]:
                status, available = "🟢 **in stock**", available + 1
            else:
                status, sold_out = "🔴 sold out", sold_out + 1
            product_lines.append(f"  📦 **{p.get('name','?')}** — {status}")
        parts = []
        if available: parts.append(f"🟢 {available} available")
        if sold_out:  parts.append(f"🔴 {sold_out} sold out")
        if unknown:   parts.append(f"⚪ {unknown} unknown")
        lines.append(f"**📦 Products — {len(products)} tracked — {' · '.join(parts) or 'checking…'}**")
        lines.extend(product_lines)

    # ---- categories ----
    categories = data.get("categories") or []
    if categories:
        if lines:
            lines.append("")
        lines.append(f"**🗂️ Categories — {len(categories)} tracked**")
        for c in categories:
            cat_url = c.get("url", "")
            # known_urls come from _state_known_urls injected before this call
            known_urls: list = data.get("_state_known_urls", {}).get(cat_url) or []
            stock: dict = (categories_stock.get(cat_url) or {}).get("stock") or {}
            total = len(known_urls)

            if total == 0:
                lines.append(f"  🗂️ **{c.get('name','?')}** ({c.get('shop','?')}) — ⚪ not yet baselined")
                continue

            in_stock_count  = sum(1 for v in stock.values() if v is True)
            out_stock_count = sum(1 for v in stock.values() if v is False)

            parts = []
            if in_stock_count:  parts.append(f"🟢 {in_stock_count} in stock")
            if out_stock_count: parts.append(f"🔴 {out_stock_count} sold out")
            summary = " · ".join(parts) if parts else "🔴 all sold out"

            lines.append(f"  🗂️ **{c.get('name','?')}** ({c.get('shop','?')}) — {total} listings — {summary}")

            in_stock_urls = [u for u, v in stock.items() if v is True]
            for url in in_stock_urls[:10]:
                lines.append(f"    🟢 {url}")
            if len(in_stock_urls) > 10:
                lines.append(f"    … and {len(in_stock_urls) - 10} more in stock")

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


def _cmd_setpattern(data: dict, query: str, pattern: str) -> str:
    q = query.lower()
    updated: list[str] = []
    for c in (data.get("categories") or []):
        if q in c.get("name", "").lower():
            c["link_pattern"] = pattern
            updated.append(c["name"])
    if updated:
        return "✅ Pattern set to `" + pattern + "` for: " + ", ".join(f"**{n}**" for n in updated)
    return f"No category found matching `{query}`."


# ---------------------------------------------------------------------------
# Parsing + dispatch
# ---------------------------------------------------------------------------

def _parse_commands(content: str) -> list[tuple[str, ...]]:
    lines = [l.strip() for l in content.splitlines() if l.strip()]
    commands: list[tuple[str, ...]] = []
    i = 0
    while i < len(lines):
        parts = lines[i].split(None, 3)
        cmd = parts[0].lower() if parts else ""
        if cmd == "!add" and len(parts) == 2:
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


def _dispatch(
    data: dict,
    stock_state: dict,
    parts: tuple[str, ...],
    defaults: Defaults | None = None,
) -> tuple[str, bool]:
    cmd = parts[0].lower() if parts else ""
    if cmd == "!help":
        return HELP_TEXT, False
    if cmd == "!list":
        if defaults is not None:
            log.info("!list: running live stock check…")
            live_stock = _live_check_all(data, defaults)
        else:
            live_stock = stock_state
        return _cmd_list(data, live_stock), False
    if cmd == "!remove":
        if len(parts) < 2:
            return "Usage: `!remove <name>`", False
        return _cmd_remove(data, " ".join(parts[1:])), True
    if cmd == "!add":
        if len(parts) < 4:
            return "Usage: `!add product <url> <name>` or `!add category <url> <name>`", False
        sub, url = parts[1].lower(), parts[2]
        if sub == "product":
            return _cmd_add_product(data, url, parts[3]), True
        if sub == "category":
            name_field = parts[3]
            if name_field.startswith("http"):
                pp = name_field.split(None, 1)
                example_url = pp[0]
                name = pp[1] if len(pp) > 1 else example_url
                link_pattern = _derive_link_pattern(url, example_url)
            elif name_field.startswith("/"):
                pp = name_field.split(None, 1)
                link_pattern = pp[0]
                name = pp[1] if len(pp) > 1 else name_field
            else:
                link_pattern, name = None, name_field
            return _cmd_add_category(data, url, name, link_pattern), True
        return f"Unknown type `{sub}`. Use `product` or `category`.", False
    if cmd == "!setpattern":
        if len(parts) < 3:
            return "Usage: `!setpattern <name> /pattern/`", False
        return _cmd_setpattern(data, " ".join(parts[1:-1]), parts[-1]), True
    return f"Unknown command `{cmd}`. Type `!help` for usage.", False


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(config_path: Path, state_path: Path, stock_state_path: Path = Path("state.json")) -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not token:
        log.error("DISCORD_BOT_TOKEN environment variable not set.")
        return

    raw: dict = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    channel_id = str((raw.get("discord") or {}).get("command_channel_id", "")).strip()
    if not channel_id:
        log.error("discord.command_channel_id not configured in config.yaml.")
        return

    # Load defaults so !list can do a live stock check
    try:
        cfg = load_config(config_path)
        defaults: Defaults | None = cfg.defaults
    except Exception:
        defaults = None

    discord_client = _Discord(token)

    # Load bot state (last_message_id only)
    bot_state: dict = {}
    if state_path.exists():
        try:
            bot_state = json.loads(state_path.read_text())
        except Exception:
            pass

    # Load stock state (written by __main__.py) for non-!list commands
    stock_state: dict = {}
    if stock_state_path.exists():
        try:
            raw_stock = json.loads(stock_state_path.read_text())
            stock_state = raw_stock
            # Inject known_urls from state.json into raw (config data) so
            # _cmd_list/_live_check_all can look them up via _state_known_urls
            known_map: dict[str, list] = {}
            for cat_key, cat_entry in (raw_stock.get("categories") or {}).items():
                known_map[cat_key] = cat_entry.get("known_urls") or []
            raw["_state_known_urls"] = known_map
        except Exception:
            pass

    last_id: str | None = bot_state.get("last_message_id")

    try:
        messages = discord_client.messages(channel_id, after=last_id)
    except Exception as e:
        log.error("Failed to fetch Discord messages: %s", e)
        return

    messages = [m for m in reversed(messages) if m["content"].strip().startswith("!")]

    if not messages:
        return

    config_changed = False

    def _save_state(last_message_id: str) -> None:
        bot_state["last_message_id"] = last_message_id
        state_path.write_text(json.dumps(bot_state, indent=2))

    for msg in messages:
        mid = msg["id"]
        content = msg["content"].strip()
        log.info("Command message: %s", content[:120])

        if content.lower() == "!reset":
            log.info("Running !reset: clearing config and purging ALL messages.")
            raw["products"] = []
            raw["categories"] = []
            raw.pop("_state_known_urls", None)
            deleted = discord_client.delete_all_messages(channel_id)
            log.info("Deleted %d messages.", deleted)
            try:
                confirm = discord_client.post(channel_id, f"✅ **Bot reset successfully.** Config cleared and {deleted} messages deleted.")
                _save_state(confirm["id"])
            except Exception:
                bot_state.pop("last_message_id", None)
                state_path.write_text(json.dumps(bot_state, indent=2))
            config_path.write_text(
                yaml.dump(
                    {k: v for k, v in raw.items() if not k.startswith("_")},
                    allow_unicode=True, sort_keys=False, default_flow_style=False,
                ),
                encoding="utf-8",
            )
            log.info("Reset complete.")
            return

        reply_lines: list[str] = []
        changed = False
        try:
            for cmd_parts in _parse_commands(content):
                line_reply, line_changed = _dispatch(raw, stock_state, cmd_parts, defaults=defaults)
                reply_lines.append(line_reply)
                changed = changed or line_changed
        except Exception as e:
            reply_lines.append(f"⚠️ Error: {e}")
            log.exception("Command error")

        config_changed = config_changed or changed
        reply = "\n".join(reply_lines) or "Done."

        try:
            discord_client.reply(channel_id, reply, reply_to=mid)
            discord_client.react(channel_id, mid, "✅")
        except Exception as e:
            log.warning("Failed to send Discord reply: %s", e)

        _save_state(mid)

    if config_changed:
        config_path.write_text(
            yaml.dump(
                {k: v for k, v in raw.items() if not k.startswith("_")},
                allow_unicode=True, sort_keys=False, default_flow_style=False,
            ),
            encoding="utf-8",
        )
        log.info("config.yaml updated.")


if __name__ == "__main__":
    import logging
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    run(Path("config.yaml"), Path("discord_state.json"))
