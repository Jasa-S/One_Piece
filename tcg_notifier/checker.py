from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

from .config import Defaults, Product

log = logging.getLogger(__name__)

# schema.org availability values that mean "you can buy it now"
_SCHEMA_IN_STOCK = {
    "instock", "limitedavailability", "instoreonly", "onlineonly",
}
# values that mean "not available"
_SCHEMA_OOS = {
    "outofstock", "soldout", "discontinued", "backorder",
    "preorder", "presale",
}


@dataclass
class CheckResult:
    in_stock: bool
    detail: str


# ---------------------------------------------------------------------------
# Multi-signal availability detection (shared between HTTP and browser paths)
# ---------------------------------------------------------------------------

def _schema_availability(obj) -> tuple[bool, str] | None:
    """Recursively find a schema.org availability value in a JSON-LD object."""
    if isinstance(obj, list):
        for item in obj:
            r = _schema_availability(item)
            if r is not None:
                return r
        return None
    if not isinstance(obj, dict):
        return None

    # Recurse into offers first (can be dict or list)
    offers = obj.get("offers")
    if isinstance(offers, dict):
        r = _schema_availability(offers)
        if r is not None:
            return r
    elif isinstance(offers, list):
        for o in offers:
            r = _schema_availability(o)
            if r is not None:
                return r

    avail = obj.get("availability")
    if avail:
        # Normalise: strip schema.org URI prefix and lowercase
        key = (
            str(avail)
            .lower()
            .replace("https://schema.org/", "")
            .replace("http://schema.org/", "")
        )
        if key in _SCHEMA_IN_STOCK:
            return True, f"schema.org: {avail}"
        if key in _SCHEMA_OOS:
            return False, f"schema.org: {avail}"

    return None


def detect_availability(
    soup: BeautifulSoup,
    in_stock_texts: list[str],
    oos_texts: list[str],
) -> tuple[bool | None, str]:
    """Multi-signal stock detection.  Returns (True/False/None, detail).

    None means no signal was found; caller should assume out-of-stock.
    Priority: schema.org JSON-LD > microdata > phrase matching.
    """
    # 1. schema.org JSON-LD — language-agnostic, most reliable
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            obj = json.loads(script.string or "")
            r = _schema_availability(obj)
            if r is not None:
                return r
        except (json.JSONDecodeError, ValueError):
            pass

    # 2. Microdata itemprop="availability"  (<meta content=...>, <link href=...>, or text)
    for el in soup.find_all(attrs={"itemprop": "availability"}):
        val = (el.get("content") or el.get("href") or el.get_text(" ", strip=True)).strip()
        key = (
            val.lower()
            .replace("https://schema.org/", "")
            .replace("http://schema.org/", "")
        )
        if key in _SCHEMA_IN_STOCK or key in ("available", "in stock", "auf lager"):
            return True, f'itemprop availability: "{val}"'
        if key in _SCHEMA_OOS or key in ("out of stock", "sold out", "nicht verfügbar", "품절"):
            return False, f'itemprop availability: "{val}"'

    # 3. Phrase matching against visible text
    haystack = soup.get_text(" ", strip=True).lower()
    found_oos = next((s for s in oos_texts if s.lower() in haystack), None)
    if found_oos:
        return False, f"out-of-stock phrase matched: {found_oos!r}"
    found_in = next((s for s in in_stock_texts if s.lower() in haystack), None)
    if found_in:
        return True, f"in-stock phrase matched: {found_in!r}"

    return None, "no signal matched"


# ---------------------------------------------------------------------------
# Plain-HTTP product check
# ---------------------------------------------------------------------------

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
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.6,ko;q=0.4",
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

    soup = BeautifulSoup(resp.text, "html.parser")
    in_stock, detail = detect_availability(soup, product.in_stock_text, product.out_of_stock_text)
    if in_stock is not None:
        return CheckResult(in_stock, detail)

    log.debug("No availability signal for %s — assuming out of stock", product.url)
    return CheckResult(False, "no signal matched (assumed out of stock)")
