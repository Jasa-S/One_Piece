from __future__ import annotations

import logging
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .config import Defaults, Product

log = logging.getLogger(__name__)


@dataclass
class CheckResult:
    in_stock: bool
    detail: str


def check_product(product: Product, defaults: Defaults) -> CheckResult | None:
    """Fetch a product page and decide whether it's in stock.

    Returns None if the page can't be fetched (network error, non-200, blocked).
    """
    if product.use_browser:
        from .browser import check_product_browser
        in_stock, detail = check_product_browser(product)
        if in_stock is None:
            return None
        return CheckResult(in_stock, detail)

    headers = {
        "User-Agent": defaults.user_agent,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(
            product.url,
            headers=headers,
            timeout=defaults.request_timeout_seconds,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.warning("Request failed for %s: %s", product.url, e)
        return None

    if resp.status_code != 200:
        log.warning("Non-200 (%s) for %s", resp.status_code, product.url)
        return None

    page_text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
    haystack = page_text.lower()

    found_oos = next(
        (s for s in product.out_of_stock_text if s.lower() in haystack), None
    )
    if found_oos:
        return CheckResult(False, f"out-of-stock phrase matched: {found_oos!r}")

    found_in = next(
        (s for s in product.in_stock_text if s.lower() in haystack), None
    )
    if found_in:
        return CheckResult(True, f"in-stock phrase matched: {found_in!r}")

    return CheckResult(False, "no configured phrase matched (assumed out of stock)")
