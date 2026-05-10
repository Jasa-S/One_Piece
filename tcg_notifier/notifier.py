from __future__ import annotations

import logging

import requests

from .config import Product

log = logging.getLogger(__name__)


def send_discord_alert(webhook_url: str, product: Product, detail: str) -> None:
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
    try:
        r = requests.post(webhook_url, json=payload, timeout=10)
        if r.status_code >= 300:
            log.warning(
                "Discord webhook returned %s: %s", r.status_code, r.text[:200]
            )
    except requests.RequestException as e:
        log.warning("Discord webhook failed: %s", e)
