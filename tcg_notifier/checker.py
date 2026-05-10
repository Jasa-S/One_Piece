from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .config import DEFAULT_USER_AGENT, Defaults, Product

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    in_stock: bool
    detail: str


def _classify(text: str, product: Product) -> CheckResult:
    """Classify page text as in-stock or out-of-stock.

    In-stock phrases take priority: if ANY in-stock phrase is found
    the product is considered available, even if an OOS phrase also appears
    (e.g. a page listing multiple variants where some are sold out).
    """
    haystack = text.lower()

    found_in = next((s for s in product.in_stock_text if s.lower() in haystack), None)
    if found_in:
        return CheckResult(True, f"in-stock phrase matched: {found_in!r}")

    found_oos = next((s for s in product.out_of_stock_text if s.lower() in haystack), None)
    if found_oos:
        return CheckResult(False, f"oos phrase matched: {found_oos!r}")

    return CheckResult(False, "no configured phrase matched (assumed out of stock)")


def check_product(
    product: Product,
    defaults: Defaults,
    session: requests.Session | None = None,
) -> CheckResult | None:
    """Fetch a product page and decide whether it's in stock.

    Retries up to defaults.max_retries times on transient errors.
    Returns None only if all retries are exhausted.
    """
    if product.use_browser:
        from .browser import check_product_browser
        for attempt in range(1, defaults.max_retries + 1):
            in_stock, detail = check_product_browser(product)
            if in_stock is not None:
                return CheckResult(in_stock, detail)
            if attempt < defaults.max_retries:
                log.warning(
                    "Browser check failed for %s (attempt %d/%d), retrying in %.0fs…",
                    product.name, attempt, defaults.max_retries, defaults.retry_delay_seconds,
                )
                time.sleep(defaults.retry_delay_seconds)
        log.error("All %d browser retries exhausted for %s", defaults.max_retries, product.name)
        return None

    headers = {
        "User-Agent": defaults.user_agent,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    _session = session or requests.Session()

    for attempt in range(1, defaults.max_retries + 1):
        try:
            resp = _session.get(
                product.url,
                headers=headers,
                timeout=defaults.request_timeout_seconds,
                allow_redirects=True,
            )
        except requests.RequestException as e:
            log.warning(
                "Request failed for %s (attempt %d/%d): %s",
                product.name, attempt, defaults.max_retries, e,
            )
            if attempt < defaults.max_retries:
                time.sleep(defaults.retry_delay_seconds)
            continue

        if resp.status_code == 429:
            retry_after = float(resp.headers.get("Retry-After", defaults.retry_delay_seconds * 2))
            log.warning("%s: rate-limited (429), waiting %.0fs", product.name, retry_after)
            time.sleep(retry_after)
            continue

        if resp.status_code != 200:
            log.warning(
                "Non-200 (%s) for %s (attempt %d/%d)",
                resp.status_code, product.name, attempt, defaults.max_retries,
            )
            if attempt < defaults.max_retries:
                time.sleep(defaults.retry_delay_seconds)
            continue

        page_text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        return _classify(page_text, product)

    log.error("All %d retries exhausted for %s", defaults.max_retries, product.name)
    return None
