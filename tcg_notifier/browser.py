from __future__ import annotations

import json
import logging
import re
import threading
from contextlib import contextmanager
from typing import Generator
from urllib.parse import urljoin

from .category import FoundProduct, _normalize
from .config import Category, DEFAULT_USER_AGENT, NAVER_USER_AGENT, Product, is_naver_smartstore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared Playwright browser (one process, reused across all checks)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_pw_instance = None
_browser_instance = None


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


def get_shared_browser():
    """Return (playwright, browser), launching once and reusing thereafter."""
    global _pw_instance, _browser_instance
    with _lock:
        if _browser_instance is None or not _browser_instance.is_connected():
            sync_playwright = _get_playwright()
            if sync_playwright is None:
                return None, None
            _pw_instance = sync_playwright().start()
            _browser_instance = _pw_instance.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            log.info("Shared Playwright browser launched.")
    return _pw_instance, _browser_instance


def close_shared_browser() -> None:
    global _pw_instance, _browser_instance
    with _lock:
        if _browser_instance is not None:
            try:
                _browser_instance.close()
            except Exception:
                pass
            _browser_instance = None
        if _pw_instance is not None:
            try:
                _pw_instance.stop()
            except Exception:
                pass
            _pw_instance = None
    log.info("Shared Playwright browser closed.")


@contextmanager
def _browser_page(url: str) -> Generator:
    """Open a new page in the shared browser, choosing locale/UA by URL."""
    naver = is_naver_smartstore(url)
    locale = "ko-KR" if naver else "de-DE"
    ua = NAVER_USER_AGENT if naver else DEFAULT_USER_AGENT
    accept_lang = "ko-KR,ko;q=0.9" if naver else "de-DE,de;q=0.9,en;q=0.6"

    _, browser = get_shared_browser()
    if browser is None:
        raise RuntimeError("Playwright browser not available")

    ctx = browser.new_context(
        locale=locale,
        user_agent=ua,
        extra_http_headers={"Accept-Language": accept_lang},
        java_script_enabled=True,
    )
    page = ctx.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    try:
        yield page
    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# brand.naver.com — Next.js __NEXT_DATA__ approach
# ---------------------------------------------------------------------------

# Korean sold-out phrases that only appear in the product content area,
# never in nav/header, so they are safe to match anywhere in body text.
_BRAND_NAVER_OOS_PHRASES = [
    "품절되었습니다",   # "It is sold out" — shown as the primary sold-out message
    "일시품절",         # "Temporarily out of stock"
    "품절",             # "Sold out" — shorter fallback
]

# The buy/cart phrase appears in the nav too, so we only treat it as in-stock
# when NO sold-out phrase is present.
_BRAND_NAVER_IN_STOCK_PHRASES = [
    "구매하기",   # "Buy now"
    "장바구니",   # "Add to cart"
]


def _check_naver_brand(page, product: Product) -> tuple[bool | None, str]:
    """Stock check for brand.naver.com.

    Priority order:
      1. Body text: sold-out Korean phrases (품절되었습니다 etc.) — highest
         confidence because these phrases only appear in the product content
         area when the item is genuinely unavailable.
      2. __NEXT_DATA__ JSON: walk the top-level product object for soldOut /
         stockCount / purchasable fields on a *targeted* path only (no deep
         search to avoid hitting a soldOut=false on an in-stock option variant
         while the overall product is sold out).
      3. Body text: in-stock phrases (구매하기 / 장바구니) — only if no OOS
         signal found anywhere.
      4. Fallback to the generic Naver smartstore DOM/text check.
    """
    try:
        page.wait_for_selector("#__NEXT_DATA__", timeout=15_000)
    except Exception:
        log.debug("brand.naver: __NEXT_DATA__ not found within timeout")

    # --- 1. Body text: sold-out phrases (ground truth) ---
    try:
        body_text = page.inner_text("body")
        for phrase in _BRAND_NAVER_OOS_PHRASES:
            if phrase in body_text:
                return False, f"body text oos phrase: {phrase!r}"
    except Exception as exc:
        log.debug("brand.naver: body text read failed: %s", exc)
        body_text = ""

    # --- 2. __NEXT_DATA__: targeted path only (no broad deep search) ---
    try:
        raw = page.eval_on_selector("#__NEXT_DATA__", "el => el.textContent")
        if raw:
            data = json.loads(raw)
            sold_out = _extract_sold_out_targeted(data)
            if sold_out is True:
                return False, "__NEXT_DATA__ soldOut=true (targeted)"
            if sold_out is False:
                # Don't trust this alone — only confirm in-stock once we also
                # verify no OOS phrase is present (already checked above).
                log.debug("brand.naver: __NEXT_DATA__ soldOut=false")
    except Exception as exc:
        log.debug("brand.naver: __NEXT_DATA__ parse failed: %s", exc)

    # --- 3. Body text: in-stock phrases ---
    try:
        for phrase in _BRAND_NAVER_IN_STOCK_PHRASES:
            if phrase in body_text:
                return True, f"body text in-stock phrase: {phrase!r}"
    except Exception:
        pass

    # --- 4. Fallback ---
    log.debug("brand.naver: falling back to DOM/text check for %s", product.url)
    return _check_naver(page, product)


def _extract_sold_out_targeted(data: dict) -> bool | None:
    """Walk __NEXT_DATA__ using known paths only — no broad deep search.

    A broad recursive search risks finding soldOut=false on an in-stock
    *option variant* while the parent product is sold out. Instead we only
    read the top-level product object directly under pageProps.
    """
    try:
        page_props = (data.get("props") or {}).get("pageProps") or {}

        # Primary path: pageProps.<product|item|productDetail|detail>
        for key in ("product", "item", "productDetail", "detail"):
            obj = page_props.get(key)
            if isinstance(obj, dict):
                result = _read_stock_fields(obj)
                if result is not None:
                    return result

        # Secondary: pageProps.initialState — one level down only
        for key in ("initialState", "initialData"):
            obj = page_props.get(key)
            if isinstance(obj, dict):
                for inner_key in ("product", "item", "productDetail", "detail"):
                    inner = obj.get(inner_key)
                    if isinstance(inner, dict):
                        result = _read_stock_fields(inner)
                        if result is not None:
                            return result

    except Exception:
        pass
    return None


def _read_stock_fields(obj: dict) -> bool | None:
    """Check soldOut / stockCount / purchasable fields on a single dict."""
    if "soldOut" in obj:
        return bool(obj["soldOut"])
    if "isSoldOut" in obj:
        return bool(obj["isSoldOut"])
    if "purchasable" in obj:
        return not bool(obj["purchasable"])
    if "stockCount" in obj:
        return int(obj["stockCount"]) <= 0
    if "stock" in obj and isinstance(obj["stock"], (int, float)):
        return int(obj["stock"]) <= 0
    return None


def _deep_search_sold_out(obj, depth: int) -> bool | None:
    """Recursively search a JSON tree for stock fields, up to `depth` levels.

    NOTE: This is intentionally no longer called by _check_naver_brand because
    it can return a false in-stock result by finding soldOut=false on an option
    variant. Kept for potential use elsewhere.
    """
    if depth == 0 or not isinstance(obj, dict):
        return None
    result = _read_stock_fields(obj)
    if result is not None:
        return result
    for v in obj.values():
        if isinstance(v, dict):
            result = _deep_search_sold_out(v, depth - 1)
            if result is not None:
                return result
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    result = _deep_search_sold_out(item, depth - 1)
                    if result is not None:
                        return result
    return None


# ---------------------------------------------------------------------------
# Naver Smartstore helpers
# ---------------------------------------------------------------------------

# Naver Smartstore uses hashed CSS class names that change with every deploy.
# Instead we rely on stable data-* attributes, ARIA roles, and Korean text
# content which are far less likely to change.
_NAVER_OOS_SELECTORS = [
    # data-nclick attribute contains 'soldout' on the sold-out badge
    "[data-nclick*='soldout']",
    "[data-nclick*='SoldOut']",
    # aria-disabled buy button = sold out
    "button[aria-disabled='true'][class*='btn']",
    # common class fragments still used across deploys
    "[class*='soldOut']",
    "[class*='sold-out']",
    "[class*='outOfStock']",
    "[class*='SoldOut']",
    ".sold_out",
    "#SOLD_OUT",
]

# Scoped to the main product area to avoid matching nav/header buy buttons.
_NAVER_IN_STOCK_SELECTORS = [
    # Purchase / cart buttons scoped to main content area
    "main button[class*='buy']:not([disabled]):not([aria-disabled='true'])",
    "main button[class*='purchase']:not([disabled]):not([aria-disabled='true'])",
    "main button[class*='cart']:not([disabled]):not([aria-disabled='true'])",
    # Naver's own data-nclick buy actions, scoped away from header/nav
    "main [data-nclick*='buy']:not([disabled])",
    "main [data-nclick*='addCart']:not([disabled])",
    # Fallback without main scope (less reliable)
    "#content button[class*='buy']:not([disabled]):not([aria-disabled='true'])",
    "#product-content [data-nclick*='buy']:not([disabled])",
]


def _check_naver(page, product: Product) -> tuple[bool | None, str]:
    """Naver Smartstore-specific stock check.

    Strategy:
    1. Wait for networkidle so React has fully rendered the stock state.
    2. Check DOM selectors for sold-out / in-stock indicators (reliable).
    3. Fall back to Korean phrase matching in body text.
    4. If nothing found, assume out of stock.
    """
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        log.debug("Naver: networkidle timeout, proceeding with current DOM")

    # --- 1. DOM selector checks (most reliable) ---

    for sel in _NAVER_OOS_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return False, f"sold-out selector matched: {sel!r}"
        except Exception:
            pass

    for sel in _NAVER_IN_STOCK_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.is_enabled():
                return True, f"in-stock selector matched: {sel!r}"
        except Exception:
            pass

    # --- 2. Korean text phrase matching (fallback) ---
    try:
        text = page.inner_text("body").lower()
    except Exception:
        return None, "failed to read body text"

    # OOS phrases checked before in-stock (more specific signal)
    found_oos = next((s for s in product.out_of_stock_text if s.lower() in text), None)
    if found_oos:
        return False, f"oos phrase matched: {found_oos!r}"

    found_in = next((s for s in product.in_stock_text if s.lower() in text), None)
    if found_in:
        return True, f"in-stock phrase matched: {found_in!r}"

    # --- 3. JSON-LD / script tag stock check (deep fallback) ---
    try:
        scripts = page.query_selector_all("script[type='application/ld+json']")
        for script in scripts:
            try:
                content = script.inner_html().lower()
                if "instock" in content or "in_stock" in content:
                    return True, "JSON-LD availability: InStock"
                if "outofstock" in content or "out_of_stock" in content or "soldout" in content:
                    return False, "JSON-LD availability: OutOfStock"
            except Exception:
                pass
    except Exception:
        pass

    return False, "no stock indicator found (assumed out of stock)"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_category_browser(category: Category) -> list[FoundProduct] | None:
    """Fetch a JS-rendered category page using the shared Chromium browser."""
    pattern = re.compile(category.link_pattern) if category.link_pattern else None

    try:
        with _browser_page(category.url) as page:
            try:
                page.goto(category.url, wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                log.warning("domcontentloaded timed out for %s, using partial content", category.url)

            # Scroll to load lazy/infinite-scroll content
            prev_height = 0
            for i in range(6):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1200)
                new_height = page.evaluate("document.body.scrollHeight")
                if new_height == prev_height:
                    log.debug("scroll stable after %d iterations", i + 1)
                    break
                prev_height = new_height

            final_url = page.url
            all_anchors = page.query_selector_all(category.link_selector)
            log.info("%s: %d anchors found", category.name, len(all_anchors))

            found: dict[str, str] = {}
            sample_unmatched: list[str] = []
            for el in all_anchors:
                href = el.get_attribute("href")
                if not href:
                    continue
                absolute = urljoin(final_url, href)
                if pattern and not pattern.search(absolute):
                    if len(sample_unmatched) < 5:
                        sample_unmatched.append(absolute)
                    continue
                normalized = _normalize(absolute)
                if normalized == _normalize(final_url):
                    continue
                if normalized in found:
                    continue
                found[normalized] = (el.inner_text() or "").strip()[:200] or normalized

            if pattern and not found and sample_unmatched:
                log.warning(
                    "%s: pattern %r matched 0 of %d anchors. Sample: %s",
                    category.name, category.link_pattern, len(all_anchors), sample_unmatched,
                )

        return [FoundProduct(url=u, title=t) for u, t in found.items()]

    except Exception as e:
        log.warning("Browser category fetch failed for %s: %s", category.url, e)
        return None


def check_product_browser(product: Product) -> tuple[bool | None, str]:
    """Check stock on a JS-rendered product page using the shared browser.

    Returns (in_stock, detail). in_stock is None on fetch failure.
    """
    from urllib.parse import urlsplit
    host = urlsplit(product.url).netloc.lower()

    try:
        with _browser_page(product.url) as page:
            try:
                page.goto(product.url, wait_until="domcontentloaded", timeout=20_000)
            except Exception:
                log.warning("domcontentloaded timed out for %s, using partial content", product.url)

            # brand.naver.com: Next.js app, read __NEXT_DATA__ for soldOut field
            if "brand.naver.com" in host:
                return _check_naver_brand(page, product)

            # smartstore.naver.com and other Naver shops: DOM/text strategy
            if is_naver_smartstore(product.url):
                return _check_naver(page, product)

            # Generic JS-rendered site: phrase matching only
            text = page.inner_text("body").lower()

            found_in = next((s for s in product.in_stock_text if s.lower() in text), None)
            if found_in:
                return True, f"in-stock phrase matched: {found_in!r}"

            found_oos = next((s for s in product.out_of_stock_text if s.lower() in text), None)
            if found_oos:
                return False, f"oos phrase matched: {found_oos!r}"

            return False, "no configured phrase matched (assumed out of stock)"

    except Exception as e:
        log.warning("Browser product check failed for %s: %s", product.url, e)
        return None, str(e)
