from __future__ import annotations

import json
import logging
import queue
import re
import threading
import time
from contextlib import contextmanager
from typing import Callable, Generator
from urllib.parse import urljoin, urlsplit

from .category import FoundProduct, _normalize
from .config import Category, DEFAULT_USER_AGENT, NAVER_USER_AGENT, Product, is_naver_smartstore

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Browser thread
# ---------------------------------------------------------------------------
# Playwright's sync API is built on greenlets and is NOT thread-safe.
# All Playwright operations must run on the single thread that called
# sync_playwright().start(). We enforce this by running one permanent daemon
# thread (_browser_thread) that owns the Playwright instance and processes
# work items from _BROWSER_QUEUE one at a time.
#
# Public callers (check_product_browser / fetch_category_browser) submit a
# callable to the queue and block on a threading.Event until the result is
# ready. Worker threads in ThreadPoolExecutor never touch Playwright directly.
# ---------------------------------------------------------------------------

_SENTINEL = object()  # signals the browser thread to shut down

class _WorkItem:
    __slots__ = ("fn", "event", "result", "exc")
    def __init__(self, fn: Callable):
        self.fn = fn
        self.event = threading.Event()
        self.result = None
        self.exc: BaseException | None = None


_BROWSER_QUEUE: queue.Queue = queue.Queue()
_browser_thread_handle: threading.Thread | None = None
_browser_thread_lock = threading.Lock()

# Playwright objects — only ever accessed from _browser_thread
_pw = None
_browser = None

_NAVER_QUEUE_RETRIES = 4
_NAVER_QUEUE_RETRY_DELAY = 8


def _browser_thread_main() -> None:
    global _pw, _browser
    sync_playwright = _get_playwright()
    if sync_playwright is None:
        log.error("Browser thread: playwright not available, exiting.")
        return

    _pw = sync_playwright().start()
    _browser = _pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    log.info("Browser thread: Playwright browser launched.")

    while True:
        item = _BROWSER_QUEUE.get()
        if item is _SENTINEL:
            break
        try:
            item.result = item.fn()
        except Exception as exc:
            item.exc = exc
        finally:
            item.event.set()

    try:
        _browser.close()
    except Exception:
        pass
    try:
        _pw.stop()
    except Exception:
        pass
    log.info("Browser thread: Playwright browser closed.")


def _ensure_browser_thread() -> None:
    global _browser_thread_handle
    with _browser_thread_lock:
        if _browser_thread_handle is None or not _browser_thread_handle.is_alive():
            _browser_thread_handle = threading.Thread(
                target=_browser_thread_main, daemon=True, name="playwright-browser"
            )
            _browser_thread_handle.start()


def _run_in_browser_thread(fn: Callable):
    """Submit fn to the browser thread and block until it completes."""
    _ensure_browser_thread()
    item = _WorkItem(fn)
    _BROWSER_QUEUE.put(item)
    item.event.wait()
    if item.exc is not None:
        raise item.exc
    return item.result


def close_shared_browser() -> None:
    """Signal the browser thread to shut down and wait for it to finish."""
    global _browser_thread_handle
    _BROWSER_QUEUE.put(_SENTINEL)
    if _browser_thread_handle is not None:
        _browser_thread_handle.join(timeout=15)
        _browser_thread_handle = None
    log.info("Browser thread shut down.")


# kept for any external callers that might import it
def get_shared_browser():
    return _pw, _browser


# ---------------------------------------------------------------------------
# Playwright helpers (MUST only be called from the browser thread)
# ---------------------------------------------------------------------------

_UC_ACCEPT_SELECTOR = "button[data-testid='uc-accept-all-button']"
_UC_WAIT_MS = 4_000

_CONSENT_SELECTORS = [
    "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "button#CybotCookiebotDialogBodyButtonAccept",
    "#onetrust-accept-btn-handler",
    "button.onetrust-close-btn-handler",
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
    # 1. Usercentrics — wait for its async accept button
    try:
        page.wait_for_selector(_UC_ACCEPT_SELECTOR, timeout=_UC_WAIT_MS)
        el = page.query_selector(_UC_ACCEPT_SELECTOR)
        if el and el.is_visible():
            el.click(timeout=3_000)
            log.debug("Usercentrics consent dismissed")
            page.wait_for_timeout(800)
            return
    except Exception:
        pass

    # 2. Other CMP selectors
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

    # 3. Text-based fallback
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


@contextmanager
def _browser_page(url: str) -> Generator:
    """Open a new Playwright page. Must only be called from the browser thread."""
    korean = is_naver_smartstore(url)
    locale = "ko-KR" if korean else "de-DE"
    ua = NAVER_USER_AGENT if korean else DEFAULT_USER_AGENT
    accept_lang = "ko-KR,ko;q=0.9" if korean else "de-DE,de;q=0.9,en;q=0.6"

    if _browser is None:
        raise RuntimeError("Playwright browser not initialised")

    ctx = _browser.new_context(
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


def _navigate_with_consent(page, url: str) -> None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception:
        log.warning("domcontentloaded timed out for %s, using partial content", url)
    _dismiss_consent(page)
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# brand.naver.com
# ---------------------------------------------------------------------------

_BRAND_NAVER_IN_STOCK_PHRASES = [
    "\uad6c\ub9e4\ud558\uae30",
    "\uc7a5\ubc14\uad6c\ub2c8",
]
_BRAND_NAVER_OOS_PHRASES_EXACT = [
    "\ud488\uc808\ub418\uc5c8\uc2b5\ub2c8\ub2e4",
    "\uc77c\uc2dc\ud488\uc808",
]
_BRAND_NAVER_OOS_STANDALONE = re.compile(
    r"(?<![\uAC00-\uD7A3\w])\ud488\uc808(?![\uAC00-\uD7A3\w])"
)


def _fetch_next_data(page, url: str) -> str | None:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except Exception:
        log.debug("brand.naver: domcontentloaded timed out")
    try:
        page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception:
        log.debug("brand.naver: networkidle timed out")
    try:
        return page.eval_on_selector("#__NEXT_DATA__", "el => el.textContent")
    except Exception:
        return None


def _check_naver_brand(page, product: Product) -> tuple[bool | None, str]:
    next_data_raw = _fetch_next_data(page, product.url)

    attempt = 0
    while not next_data_raw and attempt < _NAVER_QUEUE_RETRIES:
        attempt += 1
        log.warning("brand.naver: no __NEXT_DATA__ — retry %d/%d in %ds",
                    attempt, _NAVER_QUEUE_RETRIES, _NAVER_QUEUE_RETRY_DELAY)
        time.sleep(_NAVER_QUEUE_RETRY_DELAY)
        next_data_raw = _fetch_next_data(page, product.url)

    next_data_sold_out: bool | None = None
    if next_data_raw:
        try:
            data = json.loads(next_data_raw)
            next_data_sold_out = _extract_sold_out_targeted(data)
            if next_data_sold_out is True:
                return False, "__NEXT_DATA__ soldOut=true"
        except Exception as exc:
            log.debug("brand.naver: __NEXT_DATA__ parse error: %s", exc)
    else:
        log.warning("brand.naver: no __NEXT_DATA__ after retries for %s", product.url)

    body_text = ""
    try:
        body_text = page.inner_text("body")
    except Exception as exc:
        log.debug("brand.naver: body text read failed: %s", exc)

    for phrase in _BRAND_NAVER_IN_STOCK_PHRASES:
        if phrase in body_text:
            return True, f"body in-stock phrase: {phrase!r}"

    for phrase in _BRAND_NAVER_OOS_PHRASES_EXACT:
        if phrase in body_text:
            return False, f"body OOS phrase: {phrase!r}"
    if _BRAND_NAVER_OOS_STANDALONE.search(body_text):
        return False, "body standalone \ud488\uc808 matched"

    if next_data_sold_out is False:
        return True, "__NEXT_DATA__ soldOut=false"

    return True, "no OOS signal found (defaulting to in-stock)"


def _extract_sold_out_targeted(data: dict) -> bool | None:
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


# ---------------------------------------------------------------------------
# Naver Smartstore
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
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        log.debug("Naver: networkidle timeout")

    for sel in _NAVER_OOS_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return False, f"sold-out selector: {sel!r}"
        except Exception:
            pass

    for sel in _NAVER_IN_STOCK_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible() and el.is_enabled():
                return True, f"in-stock selector: {sel!r}"
        except Exception:
            pass

    try:
        body_text = page.inner_text("body")
    except Exception:
        return None, "failed to read body text"

    text = body_text.lower()
    found_oos = next((s for s in product.out_of_stock_text if s.lower() in text), None)
    if found_oos:
        return False, f"oos phrase: {found_oos!r}"

    found_in = next((s for s in product.in_stock_text if s.lower() in text), None)
    if found_in:
        return True, f"in-stock phrase: {found_in!r}"

    try:
        scripts = page.query_selector_all("script[type='application/ld+json']")
        for script in scripts:
            try:
                content = script.inner_html().lower()
                if "instock" in content or "in_stock" in content:
                    return True, "JSON-LD: InStock"
                if "outofstock" in content or "out_of_stock" in content or "soldout" in content:
                    return False, "JSON-LD: OutOfStock"
            except Exception:
                pass
    except Exception:
        pass

    try:
        raw = page.eval_on_selector("#__NEXT_DATA__", "el => el.textContent")
        if raw:
            data = json.loads(raw)
            sold_out = _extract_sold_out_targeted(data)
            if sold_out is True:
                return False, "__NEXT_DATA__ soldOut=true"
            if sold_out is False:
                return True, "__NEXT_DATA__ soldOut=false"
    except Exception:
        pass

    return True, "no OOS signal found (defaulting to in-stock)"


# ---------------------------------------------------------------------------
# Public API — these are called from worker threads and dispatch to browser
# ---------------------------------------------------------------------------

def check_product_browser(product: Product) -> tuple[bool | None, str]:
    """Check stock on a JS-rendered product page.

    Safe to call from any thread; actual Playwright work runs on the
    dedicated browser thread via _run_in_browser_thread.
    """
    def _work():
        host = urlsplit(product.url).netloc.lower()
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
            log.debug("Generic: no phrase matched for %s — defaulting to in-stock", product.url)
            return True, "no configured phrase matched (defaulting to in-stock)"

    try:
        return _run_in_browser_thread(_work)
    except Exception as e:
        log.warning("Browser product check failed for %s: %s", product.url, e)
        return None, str(e)


def fetch_category_browser(category: Category) -> list[FoundProduct] | None:
    """Fetch a JS-rendered category page.

    Safe to call from any thread; dispatches to the browser thread.
    """
    def _work():
        pattern = re.compile(category.link_pattern) if category.link_pattern else None
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

    try:
        return _run_in_browser_thread(_work)
    except Exception as e:
        log.warning("Browser category fetch failed for %s: %s", category.url, e)
        return None
