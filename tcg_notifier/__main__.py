from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

import yaml

from .category import fetch_category
from .checker import check_product
from .config import DEFAULT_IN_STOCK, DEFAULT_OOS, Config, Product, load_config
from .notifier import send_in_stock_alert, send_new_listing_alert
from .state import State

log = logging.getLogger("tcg_notifier")


def _name_from_url(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1].rsplit(".", 1)[0].lstrip("_")
    return slug.replace("-", " ").replace("_", " ").title() or url


def _register_as_product(config: Config, raw: dict, category, url: str, title: str) -> bool:
    """Add a category-discovered URL to the product watchlist. No-op if already tracked."""
    if any(p.url == url for p in config.products):
        return False
    name = title.strip() if title.strip() else _name_from_url(url)
    entry: dict = {
        "name": name,
        "shop": category.shop,
        "url": url,
        "out_of_stock_text": DEFAULT_OOS,
        "in_stock_text": DEFAULT_IN_STOCK,
    }
    if category.use_browser:
        entry["use_browser"] = True
    raw.setdefault("products", []).append(entry)
    config.products.append(Product(
        name=name, url=url, shop=category.shop,
        out_of_stock_text=DEFAULT_OOS,
        in_stock_text=DEFAULT_IN_STOCK,
        use_browser=category.use_browser,
    ))
    log.info("Auto-registered product: %s (%s)", name, url)
    return True


def _check_products(config: Config, state: State) -> None:
    for product in config.products:
        result = check_product(product, config.defaults)
        if result is None:
            continue

        previously_in_stock = state.was_in_stock(product.url)
        log.info(
            "%s [%s] in_stock=%s (%s)",
            product.name,
            product.shop,
            result.in_stock,
            result.detail,
        )

        if result.in_stock and not previously_in_stock:
            log.info("Sending in-stock alert for %s", product.name)
            send_in_stock_alert(config.webhook_url, product, result.detail)

        state.update_product(product.url, result.in_stock)


def _check_categories(config: Config, state: State, raw: dict) -> bool:
    """Returns True if any new products were auto-registered into raw/config."""
    config_changed = False
    for category in config.categories:
        found = fetch_category(category, config.defaults)
        if found is None:
            continue

        current = {fp.url: fp.title for fp in found}
        known = state.known_urls(category.url)
        new_urls = sorted(set(current) - known)

        log.info(
            "%s [%s] %d listings, %d previously known, %d new",
            category.name, category.shop, len(current), len(known), len(new_urls),
        )

        # Register every visible product as a tracked product (no-op if already registered)
        for url, title in current.items():
            if _register_as_product(config, raw, category, url, title):
                config_changed = True

        if state.is_category_initialized(category.url):
            for url in new_urls:
                log.info("Sending new-listing alert for %s", url)
                send_new_listing_alert(config.webhook_url, category, url, current[url])
        elif new_urls:
            log.info(
                "%s: first run — saving %d items as baseline.",
                category.name, len(current),
            )

        state.update_category(category.url, set(current))

    return config_changed


def run_once(config: Config, state: State, config_path: Path, raw: dict) -> None:
    _check_products(config, state)
    changed = _check_categories(config, state, raw)
    if changed:
        config_path.write_text(
            yaml.dump(raw, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
        log.info("config.yaml updated with %d newly registered products.", sum(1 for _ in raw.get("products", [])))


def run_loop(config_path: Path, state_path: Path) -> None:
    while True:
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            config = load_config(config_path)
        except Exception as e:
            log.error("Failed to load config (%s); retrying in 60s.", e)
            time.sleep(60)
            continue

        state = State(state_path)
        run_once(config, state, config_path, raw)

        sleep_for = config.defaults.check_interval_seconds + random.randint(
            0, max(0, config.defaults.jitter_seconds)
        )
        log.info("Sleeping %ss until next round.", sleep_for)
        time.sleep(sleep_for)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tcg_notifier",
        description=(
            "Watch TCG product pages and category pages; "
            "alert a Discord webhook when items come in stock or new listings appear."
        ),
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML.")
    parser.add_argument("--state", default="state.json", help="Path to state file.")
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument("--test", action="store_true", help="Send a test ping to Discord and exit.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config_path = Path(args.config)
    state_path = Path(args.state)
    if not config_path.exists():
        log.error(
            "Config not found at %s. Copy config.example.yaml to config.yaml first.",
            config_path,
        )
        return 1

    if args.test:
        config = load_config(config_path)
        from .notifier import _post
        _post(config.webhook_url, {
            "username": "TCG Stock Notifier",
            "content": "Test ping — the notifier is set up and working!",
            "embeds": [{
                "title": "Connection test",
                "description": "If you see this, Discord notifications are working correctly.",
                "color": 0x3498DB,
            }],
        })
        log.info("Test ping sent.")
        return 0

    if args.once:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        config = load_config(config_path)
        state = State(state_path)
        run_once(config, state, config_path, raw)
    else:
        try:
            run_loop(config_path, state_path)
        except KeyboardInterrupt:
            log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
