from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_OOS = [
    "Ausverkauft", "Nicht verfügbar", "Vergriffen",
    "Derzeit nicht verfügbar", "Sold out", "Out of stock",
]
DEFAULT_IN_STOCK = [
    "In den Warenkorb", "In den Einkaufswagen",
    "Auf Lager", "Lieferbar", "Sofort lieferbar", "Add to cart",
]


@dataclass
class Product:
    name: str
    url: str
    shop: str = ""
    in_stock_text: list[str] = field(default_factory=list)
    out_of_stock_text: list[str] = field(default_factory=list)
    use_browser: bool = False  # set True for JS-rendered shops


@dataclass
class Category:
    name: str
    url: str
    shop: str = ""
    link_selector: str = "a[href]"
    link_pattern: str | None = None  # optional regex matched against absolute URL
    use_browser: bool = False         # set True for JS-rendered shops (Saturn, MediaMarkt…)


@dataclass
class Defaults:
    check_interval_seconds: int = 300
    jitter_seconds: int = 60
    request_timeout_seconds: int = 20
    user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )


@dataclass
class Config:
    webhook_url: str
    defaults: Defaults
    products: list[Product]
    categories: list[Category]
    command_channel_id: str = ""


def load_config(path: Path) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL") or (
        (data.get("discord") or {}).get("webhook_url", "")
    )
    if not webhook_url or "REPLACE_ME" in webhook_url:
        raise ValueError(
            f"Webhook URL not set. Either add discord.webhook_url to {path} "
            "or set the DISCORD_WEBHOOK_URL environment variable."
        )

    defaults = Defaults(**(data.get("defaults") or {}))

    products = [Product(**p) for p in (data.get("products") or [])]
    for p in products:
        if not p.in_stock_text and not p.out_of_stock_text:
            raise ValueError(
                f"Product {p.name!r} needs at least one of "
                "in_stock_text or out_of_stock_text."
            )

    categories = [Category(**c) for c in (data.get("categories") or [])]

    if not products and not categories:
        raise ValueError(f"No products or categories configured in {path}.")

    command_channel_id = str((data.get("discord") or {}).get("command_channel_id", "") or "")

    return Config(
        webhook_url=webhook_url,
        defaults=defaults,
        products=products,
        categories=categories,
        command_channel_id=command_channel_id,
    )
