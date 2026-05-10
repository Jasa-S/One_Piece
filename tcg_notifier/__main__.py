from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

from .browser import close_shared_browser
from .category import fetch_category
from .checker import check_product
from .config import Config, Product, load_config
from .notifier import send_in_stock_alert, send_new_listing_alert
from .state import State

log = logging.getLogger("tcg_notifier")


def _check_products(config: Config, state: State) -> None:
    """
    Check all products in parallel.

    - HTTP products share a requests.Session per shop domain (keep-alive reuse).
    - Browser products run in the shared Playwright browser.
    - Up to defaults.max_workers threads run concurrently.
    """
    if not config.products:
        return

    shop_sessions: dict[str, requests.Session] = defaultdict(requests.Session)

    def _check_one(product: Product):
        session = None if product.use_browser else shop_sessions[product.shop]
        return product, check_product(product, config.defaults, session=session)

    with ThreadPoolExecutor(max_workers=config.defaults.max_workers) as pool:
        futures = {pool.submit(_check_one, p): p for p in config.products}
        for fut in as_completed(futures):
            try:
                product, result = fut.result()
            except Exception as e:
                log.error("Unexpected error checking %s: %s", futures[fut].name, e)
                continue

            if result is None:
                log.warning("Skipping %s — all retries exhausted.", product.name)
                continue

            previously_in_stock = state.was_in_stock(product.url)
            log.info(
                "%s [%s] in_stock=%s (%s)",
                product.name, product.shop, result.in_stock, result.detail,
            )

            if result.in_stock and not previously_in_stock:
                log.info("Sending in-stock alert for %s", product.name)
                send_in_stock_alert(config.webhook_url, product, result.detail)

            state.update_product(product.url, result.in_stock)

    state.save()


def _check_categories(config: Config, state: State) -> None:
    """Check all categories in parallel and alert on new listings.

    Does NOT register found URLs as products — categories are purely for
    new-listing notifications.
    """
    if not config.categories:
        return

    def _check_one(category):
        return category, fetch_category(category, config.defaults)

    with ThreadPoolExecutor(max_workers=max(2, config.defaults.max_workers // 2)) as pool:
        futures = {pool.submit(_check_one, c): c for c in config.categories}
        for fut in as_completed(futures):
            try:
                category, found = fut.result()
            except Exception as e:
                log.error("Unexpected error fetching category %s: %s", futures[fut].name, e)
                continue

            if found is None:
                continue

            current = {fp.url: fp.title for fp in found}
            known = state.known_urls(category.url)
            new_urls = sorted(set(current) - known)

            log.info(
                "%s [%s] %d listings, %d known, %d new",
                category.name, category.shop, len(current), len(known), len(new_urls),
            )

            if state.is_category_initialized(category.url):
                for url in new_urls:
                    log.info("Sending new-listing alert for %s", url)
                    send_new_listing_alert(config.webhook_url, category, url, current[url])
            elif current:
                log.info("%s: first run — %d items baselined.", category.name, len(current))

            state.update_category(category.url, set(current))

    state.save()


def run_once(config: Config, state: State, config_path: Path, raw: dict) -> None:
    # Run products and categories concurrently
    with ThreadPoolExecutor(max_workers=2) as pool:
        prod_fut = pool.submit(_check_products, config, state)
        cat_fut = pool.submit(_check_categories, config, state)
        prod_fut.result()
        cat_fut.result()


def run_loop(config_path: Path, state_path: Path) -> None:
    try:
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
    finally:
        close_shared_browser()


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
        try:
            raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            config = load_config(config_path)
            state = State(state_path)
            run_once(config, state, config_path, raw)
        finally:
            close_shared_browser()
    else:
        try:
            run_loop(config_path, state_path)
        except KeyboardInterrupt:
            log.info("Stopped.")
            close_shared_browser()
    return 0


if __name__ == "__main__":
    sys.exit(main())
