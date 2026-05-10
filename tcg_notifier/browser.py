from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from typing import Generator
from urllib.parse import urljoin, urlsplit

from .category import FoundProduct, _normalize
from .config import Category, DEFAULT_USER_AGENT, NAVER_USER_AGENT, Product, is_naver_smartstore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared Playwright browser (one process, reused across all checks)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_pw_instance = None
_browser_instance = None

_NAVER_QUEUE_RETRIES = 4
_NAVER_QUEUE_RETRY_DELAY = 8

# ---------------------------------------------------------------------------
# Borlabs Cookie — localStorage injection
# ---------------------------------------------------------------------------

_BORLABS_PAYLOAD = json.dumps({
    "consents": {
        "statistics": True,
        "marketing": True,
        "preferences": True,
        "essential": True,
    },
    "domainPath": "/",
    "expiry": 365,
    "uid": "borlabs-bypass",
    "version": "3",
})


def _is_borlabs_site(url: str) -> bool:
    host = urlsplit(url).netloc.lower()
    return "gate-to-the-games.de" in host


def _inject_borlabs_consent(page, url: str) -> None:
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    origin = f"{parts.scheme}://{parts.netloc}"
    try:
        page.goto(origin, wait_until="domcontentloaded", timeout=15_000)
    except Exception:
        pass
    try:
        page.evaluate(
            "(payload) => { localStorage.setItem('borlabs-cookie', payload); }",
            _BORLABS_PAYLOAD,
        )
        log.debug("Borlabs consent injected into localStorage for %s", origin)
    except Exception as exc:
        log.debug("Borlabs localStorage injection failed: %s", exc)


# ---------------------------------------------------------------------------
# Generic cookie consent click-through
# ---------------------------------------------------------------------------
_CONSENT_SELECTORS = [
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "button#CybotCookiebotDialogBodyButtonAccept",
    "#onetrust-accept-btn-handler",
    "button.onetrust-close-btn-handler",
    "button[data-testid='uc-accept-all-button']",
    "button[class*='accept-all']",
    "button[class*='acceptAll']",
    "button[class*='cookie-accept']",
    "button[id*='accept-all']",
    "button[id*='acceptAll']",
    "a[id*='accept-all']",
]

_CONSENT_BUTTON_TEXTS = [
    "Alle akzeptieren",
    "Alle Cookies akzeptieren",
    "Akzeptieren",
    "Accept all",
    "Accept All",
    "Allow all",
    "Allow All",
    "Agree",
    "I agree",
    "OK",
]


def _dismiss_consent(page) -> None:
    for sel in _CONSENT_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click(timeout=3_000)
                log.debug("Consent dismissed via selector: %s", sel)
                page.wait_for_timeout(600)
                return
        except Exception:
            pass
    for text in _CONSENT_BUTTON_TEXTS:
        try:
            btn = page.get_by_role("button", name=re.compile(re.escape(text), re.IGNORECASE)).first
            if btn and btn.is_visible():
                btn.click(timeout=3_000)
                log.debug("Consent dismissed via button text: %r", text)
                page.wait_for_timeout(600)
                return
        except Exception:
            pass


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
    korean = is_naver_smartstore(url)  # True for brand.naver.com too (see config.py)
    locale = "ko-KR" if korean else "de-DE"
    ua = NAVER_USER_AGENT if korean else DEFAULT_USER_AGENT
    accept_lang = "ko-KR,ko;q=0.9" if korean else "de-DE,de;q=0.9,en;q=0.6"

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
# brand.naver.com — combined __NEXT_DATA__ + DOM/phrase check
# ---------------------------------------------------------------------------

_BRAND_NAVER_OOS_PHRASES = [
    "\ud488\uc808\ub418\uc5c8\uc2b5\ub2c8\ub2e4",   # 품절되었습니다
    "\uc77c\uc2dc\ud488\uc808",                       # 일시품절
    "\ud488\uc808",                                   # 품절
]

_BRAND_NAVER_IN_STOCK_PHRASES = [
    "\uad6c\ub9e4\ud558\uae30",   # 구매하기
    "\uc7a5\ubc14\uad6c\ub2c8",   # 장바구니
]


def _fetch_next_data(page, url: str) -> str | None:
    """Navigate to url, wait for networkidle, return #__NEXT_DATA__ text or None."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception:
        log.debug("brand.naver: domcontentloaded timed out, using partial content")
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        log.debug("brand.naver: networkidle timed out, proceeding with current DOM")
    try:
        return page.eval_on_selector("#__NEXT_DATA__", "el => el.textContent")
    except Exception:
        return None


def _check_naver_brand(page, product: Product) -> tuple[bool | None, str]:
    """Stock check for brand.naver.com.

    Strategy (in order):
    1. Try to get a definitive answer from __NEXT_DATA__ (soldOut / stockCount fields).
       Retry up to _NAVER_QUEUE_RETRIES times if the page shows a queue/error.
    2. Check body text for OOS phrases (품절 etc.) — reliable even without __NEXT_DATA__.
    3. Check body text for in-stock phrases (구매하기 etc.).
    4. Fall through to the same DOM-selector + phrase check used for Smartstore.
       This is the same logic that correctly detects stock on other Naver products.

    We never return None (unknown) unless the page genuinely failed to load at all.
    """
    next_data_raw: str | None = _fetch_next_data(page, product.url)

    # Retry only if __NEXT_DATA__ is completely absent (queue/error page).
    attempt = 0
    while not next_data_raw and attempt < _NAVER_QUEUE_RETRIES:
        attempt += 1
        log.warning(
            "brand.naver: no __NEXT_DATA__ for %s — retry %d/%d in %ds",
            product.url, attempt, _NAVER_QUEUE_RETRIES, _NAVER_QUEUE_RETRY_DELAY,
        )
        time.sleep(_NAVER_QUEUE_RETRY_DELAY)
        next_data_raw = _fetch_next_data(page, product.url)

    # --- Step 1: __NEXT_DATA__ structured check (conclusive only) ---
    if next_data_raw:
        if attempt:
            log.info("brand.naver: real page loaded after %d retry/retries", attempt)
        try:
            data = json.loads(next_data_raw)
            sold_out = _extract_sold_out_targeted(data)
            if sold_out is True:
                return False, "__NEXT_DATA__ soldOut=true"
            if sold_out is False:
                # Confirmed in stock via structured data — still verify with body text
                # before returning, to catch edge cases where the flag is stale.
                log.debug("brand.naver: __NEXT_DATA__ soldOut=false, confirming via body")
        except Exception as exc:
            log.debug("brand.naver: __NEXT_DATA__ parse error: %s", exc)
    else:
        log.warning(
            "brand.naver: no __NEXT_DATA__ after %d retries for %s — continuing to DOM check",
            _NAVER_QUEUE_RETRIES, product.url,
        )

    # --- Step 2 & 3: Body text phrases ---
    body_text = ""
    try:
        body_text = page.inner_text("body")
    except Exception as exc:
        log.debug("brand.naver: body text read failed: %s", exc)

    for phrase in _BRAND_NAVER_OOS_PHRASES:
        if phrase in body_text:
            return False, f"body OOS phrase: {phrase!r}"

    for phrase in _BRAND_NAVER_IN_STOCK_PHRASES:
        if phrase in body_text:
            return True, f"body in-stock phrase: {phrase!r}"

    # --- Step 4: Full Naver DOM selector + JSON-LD fallback ---
    # Same logic that works reliably for smartstore.naver.com.
    log.debug("brand.naver: falling through to _check_naver DOM check for %s", product.url)
    return _check_naver(page, product)


def _extract_sold_out_targeted(data: dict) -> bool | None:
    """Walk __NEXT_DATA__ using known paths only."""
    try:
        page_props = (data.get("props") or {}).get("pageProps") or {}

        for key in ("product", "item", "productDetail", "detail"):
            obj = page_props.get(key)
            if isinstance(obj, dict):
                result = _read_stock_fields(obj)
                if result is not None:
                    return result

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

_NAVER_OOS_SELECTORS = [
    "[data-nclick*='soldout']",
    "[data-nclick*='SoldOut']",
    "button[aria-disabled='true'][class*='btn']",
    "[class*='soldOut']",
    "[class*='sold-out']",
    "[class*='outOfStock']",
    "[class*='SoldOut']",
    ".sold_out",
    "#SOLD_OUT",
]

_NAVER_IN_STOCK_SELECTORS = [
    "main button[class*='buy']:not([disabled]):not([aria-disabled='true'])",
    "main button[class*='purchase']:not([disabled]):not([aria-disabled='true'])",
    "main button[class*='cart']:not([disabled]):not([aria-disabled='true'])",
    "main [data-nclick*='buy']:not([disabled])",
    "main [data-nclick*='addCart']:not([disabled])",
    "#content button[class*='buy']:not([disabled]):not([aria-disabled='true'])",
    "#product-content [data-nclick*='buy']:not([disabled])",
]


def _check_naver(page, product: Product) -> tuple[bool | None, str]:
    """Naver Smartstore-specific stock check."""
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        log.debug("Naver: networkidle timeout, proceeding with current DOM")

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

    try:
        body_text = page.inner_text("body")
    except Exception:
        return None, "failed to read body text"

    text = body_text.lower()
    found_oos = next((s for s in product.out_of_stock_text if s.lower() in text), None)
    if found_oos:
        return False, f"oos phrase matched: {found_oos!r}"

    found_in = next((s for s in product.in_stock_text if s.lower() in text), None)
    if found_in:
        return True, f"in-stock phrase matched: {found_in!r}"

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
# Shared consent + navigation helper
# ---------------------------------------------------------------------------

def _navigate_with_consent(page, url: str) -> None:
    if _is_borlabs_site(url):
        _inject_borlabs_consent(page, url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception:
        log.warning("domcontentloaded timed out for %s, using partial content", url)
    _dismiss_consent(page)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_category_browser(category: Category) -> list[FoundProduct] | None:
    """Fetch a JS-rendered category page using the shared Chromium browser."""
    pattern = re.compile(category.link_pattern) if category.link_pattern else None

    try:
        with _browser_page(category.url) as page:
            _navigate_with_consent(page, category.url)

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
    """Check stock on a JS-rendered product page using the shared browser."""
    host = urlsplit(product.url).netloc.lower()

    try:
        with _browser_page(product.url) as page:
            if "brand.naver.com" in host:
                return _check_naver_brand(page, product)

            _navigate_with_consent(page, product.url)

            if is_naver_smartstore(product.url):
                return _check_naver(page, product)

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
