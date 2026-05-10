from __future__ import annotations

import logging
import re
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

from .config import Category
from .category import FoundProduct, _normalize

log = logging.getLogger(__name__)


def fetch_category_browser(category: Category) -> list[FoundProduct] | None:
    """Fetch a JS-rendered category page using a headless Chromium browser."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        log.error(
            "playwright is not installed. Run: pip install playwright && "
            "playwright install chromium"
        )
        return None

    pattern = re.compile(category.link_pattern) if category.link_pattern else None

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                locale="de-DE",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            try:
                page.goto(category.url, wait_until="networkidle", timeout=30_000)
            except PWTimeout:
                # networkidle timed out — grab whatever loaded
                log.warning("networkidle timed out for %s, using partial content", category.url)

            final_url = page.url

            # Collect all matching anchors
            elements = page.query_selector_all(category.link_selector)
            found: dict[str, str] = {}
            for el in elements:
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
                title = (el.inner_text() or "").strip()[:200] or normalized
                found[normalized] = title

            browser.close()

        return [FoundProduct(url=u, title=t) for u, t in found.items()]

    except Exception as e:
        log.warning("Browser fetch failed for %s: %s", category.url, e)
        return None
