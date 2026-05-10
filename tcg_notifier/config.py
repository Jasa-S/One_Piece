from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import yaml

DEFAULT_OOS = [
    "Ausverkauft", "Nicht verfügbar", "Vergriffen",
    "Derzeit nicht verfügbar", "Sold out", "Out of stock",
    "품절", "일시품절",  # Korean: sold out / temporarily sold out
]
DEFAULT_IN_STOCK = [
    "In den Warenkorb", "In den Einkaufswagen",
    "Auf Lager", "Lieferbar", "Sofort lieferbar", "Add to cart",
    "구매하기", "장바구니",  # Korean: buy now / add to cart
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

NAVER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def is_naver_smartstore(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return "smartstore.naver.com" in host


@dataclass
class Product:
    name: str
    url: str
    shop: str = ""
    in_stock_text: list[str] = field(default_factory=list)
    out_of_stock_text: list[str] = field(default_factory=list)
    use_browser: bool = False


@dataclass
class Category:
    name: str
    url: str
    shop: str = ""
    link_selector: str = "a[href]"
    link_pattern: str | None = None
    use_browser: bool = False


@dataclass
class Defaults:
    check_interval_seconds: int = 300
    jitter_seconds: int = 60
    request_timeout_seconds: int = 20
    user_agent: str = DEFAULT_USER_AGENT
    max_workers: int = 6          # parallel HTTP product checks
    max_retries: int = 3          # transient-error retries per product
    retry_delay_seconds: float = 4.0  # wait between retries


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

    raw_defaults = data.get("defaults") or {}
    known_defaults = {f for f in Defaults.__dataclass_fields__}
    filtered_defaults = {k: v for k, v in raw_defaults.items() if k in known_defaults}
    defaults = Defaults(**filtered_defaults)

    products = []
    for p in (data.get("products") or []):
        p = dict(p)
        p.setdefault("in_stock_text", list(DEFAULT_IN_STOCK))
        p.setdefault("out_of_stock_text", list(DEFAULT_OOS))
        # Auto-enable browser for Naver Smartstore
        if is_naver_smartstore(p.get("url", "")):
            p["use_browser"] = True
        products.append(Product(**p))

    for p in products:
        if not p.in_stock_text and not p.out_of_stock_text:
            raise ValueError(
                f"Product {p.name!r} needs at least one of "
                "in_stock_text or out_of_stock_text."
            )

    categories = []
    for c in (data.get("categories") or []):
        c = dict(c)
        if is_naver_smartstore(c.get("url", "")):
            c["use_browser"] = True
        categories.append(Category(**c))

    command_channel_id = str((data.get("discord") or {}).get("command_channel_id", "") or "")

    return Config(
        webhook_url=webhook_url,
        defaults=defaults,
        products=products,
        categories=categories,
        command_channel_id=command_channel_id,
    )
