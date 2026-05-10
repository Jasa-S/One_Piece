from __future__ import annotations

import logging
import re
from urllib.parse import urljoin

from .category import FoundProduct, _normalize
from .config import Category, Product

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _get_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        log.error(
            "playwright is not installed. Run: pip install playwright && "
            "playwright install chromium"
        )
        return None


def fetch_category_browser(category: Category) -> list[FoundProduct] | None:
    """Fetch a JS-rendered category page using a headless Chromium browser."""
    sync_playwright = _get_playwright()
    if sync_playwright is None:
        return None

    pattern = re.compile(category.link_pattern) if category.link_pattern else None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(locale="de-DE", user_agent=_UA).new_page()
            try:
                from playwright.sync_api import TimeoutError as PWTimeout
                page.goto(category.url, wait_until="networkidle", timeout=30_000)
            except Exception:
                log.warning("networkidle timed out for %s, using partial content", category.url)

            final_url = page.url
            found: dict[str, str] = {}
            for el in page.query_selector_all(category.link_selector):
                href = el.get_attribute("href")
                if not href:
                    continue
                absolute = urljoin(final_url, href)
                if pattern and not pattern.search(absolute):
                    continue
                normalized = _normalize(absolute)
                if normalized == _normalize(final_url):
                    continue
                if normalized in found:
                    continue
                found[normalized] = (el.inner_text() or "").strip()[:200] or normalized

            browser.close()
        return [FoundProduct(url=u, title=t) for u, t in found.items()]

    except Exception as e:
        log.warning("Browser category fetch failed for %s: %s", category.url, e)
        return None


def check_product_browser(product: Product) -> tuple[bool | None, str]:
    """Check stock on a JS-rendered product page.

    Returns (in_stock, detail). in_stock is None on fetch failure.
    """
    sync_playwright = _get_playwright()
    if sync_playwright is None:
        return None, "playwright not installed"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_context(locale="de-DE", user_agent=_UA).new_page()
            try:
                page.goto(product.url, wait_until="networkidle", timeout=30_000)
            except Exception:
                log.warning("networkidle timed out for %s, using partial content", product.url)

            text = page.inner_text("body").lower()
            browser.close()

        found_oos = next((s for s in product.out_of_stock_text if s.lower() in text), None)
        if found_oos:
            return False, f"out-of-stock phrase matched: {found_oos!r}"

        found_in = next((s for s in product.in_stock_text if s.lower() in text), None)
        if found_in:
            return True, f"in-stock phrase matched: {found_in!r}"

        return False, "no configured phrase matched (assumed out of stock)"

    except Exception as e:
        log.warning("Browser product check failed for %s: %s", product.url, e)
        return None, str(e)
