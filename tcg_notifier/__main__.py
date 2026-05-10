from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

from .browser import close_shared_browser
from .category import fetch_category
from .checker import check_product, CheckResult
from .config import Config, Defaults, Product, load_config
from .notifier import send_in_stock_alert, send_new_listing_alert, send_category_in_stock_alert
from .state import State

log = logging.getLogger("tcg_notifier")


def _check_products(config: Config, state: State) -> None:
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
                state.record_product_unknown(product.url)
                continue

            previously_in_stock = state.was_in_stock(product.url)
            log.info("%s [%s] in_stock=%s (%s)", product.name, product.shop, result.in_stock, result.detail)

            if result.in_stock and not previously_in_stock:
                log.info("Sending in-stock alert for %s", product.name)
                send_in_stock_alert(config.webhook_url, product, result.detail)

            state.update_product(product.url, result.in_stock)

    state.save()


def _check_category_stocks(
    category,
    urls: set[str],
    defaults: Defaults,
) -> dict[str, CheckResult | None]:
    from .config import DEFAULT_IN_STOCK, DEFAULT_OOS, is_naver_smartstore
    session = requests.Session()
    results: dict[str, CheckResult | None] = {}

    def _check_one(url: str):
        stub = Product(
            name=url,
            url=url,
            shop=category.shop,
            in_stock_text=list(DEFAULT_IN_STOCK),
            out_of_stock_text=list(DEFAULT_OOS),
            use_browser=category.use_browser or is_naver_smartstore(url),
        )
        sess = None if stub.use_browser else session
        return url, check_product(stub, defaults, session=sess)

    with ThreadPoolExecutor(max_workers=defaults.max_workers) as pool:
        futures = {pool.submit(_check_one, u): u for u in urls}
        for fut in as_completed(futures):
            try:
                url, result = fut.result()
                results[url] = result
            except Exception as e:
                log.error("Error checking category URL %s: %s", futures[fut], e)
                results[futures[fut]] = None

    return results


def _check_categories(config: Config, state: State) -> None:
    if not config.categories:
        return

    def _fetch_one(category):
        return category, fetch_category(category, config.defaults)

    with ThreadPoolExecutor(max_workers=max(2, config.defaults.max_workers // 2)) as pool:
        futures = {pool.submit(_fetch_one, c): c for c in config.categories}
        for fut in as_completed(futures):
            try:
                category, found = fut.result()
            except Exception as e:
                log.error("Error fetching category %s: %s", futures[fut].name, e)
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
                    log.info("New listing in %s: %s", category.name, url)
                    send_new_listing_alert(config.webhook_url, category, url, current[url])
            elif current:
                log.info("%s: first run — %d items baselined.", category.name, len(current))

            state.update_category(category.url, set(current))

            if not current:
                continue

            log.info("%s: checking stock on %d URLs\u2026", category.name, len(current))
            stock_results = _check_category_stocks(category, set(current), config.defaults)

            for url, result in stock_results.items():
                if result is None:
                    state.record_category_url_unknown(category.url, url)
                    continue
                title = current.get(url, url)
                previously = state.was_category_url_in_stock(category.url, url)

                if result.in_stock and previously is not True:
                    log.info("Category in-stock alert: %s — %s", category.name, url)
                    send_category_in_stock_alert(
                        config.webhook_url, category, url, title, result.detail
                    )

                state.update_category_url_stock(category.url, url, result.in_stock)

    state.save()


def run_once(config: Config, state: State) -> None:
    with ThreadPoolExecutor(max_workers=2) as pool:
        prod_fut = pool.submit(_check_products, config, state)
        cat_fut = pool.submit(_check_categories, config, state)
        prod_fut.result()
        cat_fut.result()
    state.save(last_checked_at=datetime.now(timezone.utc).isoformat())


def run_loop(config_path: Path, state_path: Path) -> None:
    try:
        while True:
            try:
                config = load_config(config_path)
            except Exception as e:
                log.error("Failed to load config (%s); retrying in 60s.", e)
                time.sleep(60)
                continue

            state = State(state_path)
            run_once(config, state)

            sleep_for = config.defaults.check_interval_seconds + random.randint(
                0, max(0, config.defaults.jitter_seconds)
            )
            log.info("Sleeping %ss until next round.", sleep_for)
            time.sleep(sleep_for)
    finally:
        close_shared_browser()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tcg_notifier")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--reset-url",
        metavar="URL",
        help="Clear the cached stock state for a product URL so the next "
             "run treats it as unseen (useful after fixing a false sold-out).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config_path = Path(args.config)
    state_path = Path(args.state)

    # --reset-url: wipe the cached entry so the next run re-checks from scratch
    if args.reset_url:
        if not state_path.exists():
            log.error("State file not found at %s.", state_path)
            return 1
        state = State(state_path)
        url = args.reset_url
        products = state._data.get("products", {})
        if url in products:
            del products[url]
            state._dirty = True
            state.save()
            log.info("Cleared cached state for: %s", url)
        else:
            log.warning("URL not found in state: %s", url)
            log.info("Known product URLs in state:")
            for u in sorted(products):
                log.info("  %s", u)
        return 0

    if not config_path.exists():
        log.error("Config not found at %s.", config_path)
        return 1

    if args.test:
        config = load_config(config_path)
        from .notifier import _post
        _post(config.webhook_url, {
            "username": "TCG Stock Notifier",
            "content": "Test ping \u2014 the notifier is set up and working!",
            "embeds": [{"title": "Connection test", "description": "Discord notifications are working.", "color": 0x3498DB}],
        })
        log.info("Test ping sent.")
        return 0

    if args.once:
        try:
            config = load_config(config_path)
            state = State(state_path)
            run_once(config, state)
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
