from __future__ import annotations

import logging
from datetime import datetime, timezone

import requests

from .config import Category, Product

log = logging.getLogger(__name__)


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def send_in_stock_alert(webhook_url: str, product: Product, detail: str) -> None:
    shop_line = f"`{product.shop}`" if product.shop else "unknown shop"
    embed = {
        "color": 0x57F287,  # Discord green
        "author": {
            "name": "\u2705  Back in Stock",
        },
        "title": product.name,
        "url": product.url,
        "fields": [
            {"name": "\U0001f3ea  Shop", "value": shop_line, "inline": True},
            {"name": "\U0001f517  Link", "value": f"[Open product page]({product.url})", "inline": True},
        ],
        "footer": {"text": "TCG Stock Notifier"},
        "timestamp": _timestamp(),
    }
    payload = {
        "username": "TCG Stock Notifier",
        "content": f"@here **{product.name}** is back in stock!",
        "embeds": [embed],
    }
    _post(webhook_url, payload)


def send_category_in_stock_alert(
    webhook_url: str,
    category: Category,
    product_url: str,
    product_title: str,
    detail: str,
) -> None:
    shop_line = f"`{category.shop}`" if category.shop else "unknown shop"
    embed = {
        "color": 0x57F287,
        "author": {
            "name": "\u2705  In Stock — Category Alert",
        },
        "title": product_title[:256],
        "url": product_url,
        "fields": [
            {"name": "\U0001f4c2  Category", "value": f"`{category.name}`", "inline": True},
            {"name": "\U0001f3ea  Shop", "value": shop_line, "inline": True},
            {"name": "\U0001f517  Link", "value": f"[Open product page]({product_url})", "inline": False},
        ],
        "footer": {"text": "TCG Stock Notifier"},
        "timestamp": _timestamp(),
    }
    payload = {
        "username": "TCG Stock Notifier",
        "content": f"@here **{product_title[:200]}** is in stock!",
        "embeds": [embed],
    }
    _post(webhook_url, payload)


def send_new_listing_alert(
    webhook_url: str,
    category: Category,
    product_url: str,
    product_title: str,
) -> None:
    shop_line = f"`{category.shop}`" if category.shop else "unknown shop"
    embed = {
        "color": 0x5865F2,  # Discord blurple — new/informational
        "author": {
            "name": "\U0001f195  New Listing Detected",
        },
        "title": product_title[:256],
        "url": product_url,
        "fields": [
            {"name": "\U0001f4c2  Category", "value": f"`{category.name}`", "inline": True},
            {"name": "\U0001f3ea  Shop", "value": shop_line, "inline": True},
            {"name": "\U0001f517  Link", "value": f"[Open product page]({product_url})", "inline": False},
        ],
        "footer": {"text": "TCG Stock Notifier"},
        "timestamp": _timestamp(),
    }
    payload = {
        "username": "TCG Stock Notifier",
        "content": f"@here new listing spotted: **{product_title[:200]}**",
        "embeds": [embed],
    }
    _post(webhook_url, payload)


def _post(webhook_url: str, payload: dict) -> None:
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code >= 300:
            log.warning("Discord webhook returned %s: %s", r.status_code, r.text[:200])
    except requests.RequestException as e:
        log.warning("Discord webhook failed: %s", e)
