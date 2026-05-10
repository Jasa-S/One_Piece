from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Product:
    name: str
    url: str
    shop: str = ""
    in_stock_text: list[str] = field(default_factory=list)
    out_of_stock_text: list[str] = field(default_factory=list)


@dataclass
class Category:
    name: str
    url: str
    shop: str = ""
    link_selector: str = "a[href]"
    link_pattern: str | None = None  # optional regex matched against absolute URL


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


def load_config(path: Path) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    webhook_url = (data.get("discord") or {}).get("webhook_url", "")
    if not webhook_url or "REPLACE_ME" in webhook_url:
        raise ValueError(
            f"Set discord.webhook_url in {path}. "
            "Get one from Discord: Server Settings -> Integrations -> Webhooks."
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

    return Config(
        webhook_url=webhook_url,
        defaults=defaults,
        products=products,
        categories=categories,
    )
