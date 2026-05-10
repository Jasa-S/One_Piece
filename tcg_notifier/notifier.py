from __future__ import annotations

import logging

import requests

from .config import Category, Product

log = logging.getLogger(__name__)


def send_in_stock_alert(webhook_url: str, product: Product, detail: str) -> None:
    embed = {
        "title": f"In stock: {product.name}",
        "url": product.url,
        "description": product.shop or "TCG product",
        "color": 0x2ECC71,
        "fields": [
            {"name": "Detection", "value": detail[:1000], "inline": False},
            {"name": "Link", "value": product.url, "inline": False},
        ],
    }
    payload = {
        "username": "TCG Stock Notifier",
        "content": (
            f"@here **{product.name}** is in stock"
            + (f" at **{product.shop}**!" if product.shop else "!")
        ),
        "embeds": [embed],
    }
    _post(webhook_url, payload)


def send_new_listing_alert(
    webhook_url: str,
    category: Category,
    product_url: str,
    product_title: str,
) -> None:
    where = f" at **{category.shop}**" if category.shop else ""
    embed = {
        "title": f"New listing: {product_title[:240]}",
        "url": product_url,
        "description": f"in {category.name}" + (f" — {category.shop}" if category.shop else ""),
        "color": 0x3498DB,
        "fields": [
            {"name": "Link", "value": product_url, "inline": False},
        ],
    }
    payload = {
        "username": "TCG Stock Notifier",
        "content": (
            f"@here new listing in **{category.name}**{where}: {product_title[:200]}"
        ),
        "embeds": [embed],
    }
    _post(webhook_url, payload)


def _post(webhook_url: str, payload: dict) -> None:
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code >= 300:
            log.warning(
                "Discord webhook returned %s: %s", r.status_code, r.text[:200]
            )
    except requests.RequestException as e:
        log.warning("Discord webhook failed: %s", e)
