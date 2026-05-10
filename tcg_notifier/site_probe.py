from __future__ import annotations

import logging
import re

import requests
from bs4 import BeautifulSoup

from .config import DEFAULT_USER_AGENT

log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_JS_SIGNALS = [
    "Checking your browser",
    "cf-browser-verification",
    "Just a moment...",
    "Enable JavaScript and cookies",
    "Please enable JavaScript",
    "Please enable cookies",
    "__NEXT_DATA__",
]

_KNOWN_PATTERNS: list[tuple[str, str]] = [
    ("saturn.de", "/de/product/"),
    ("mediamarkt.de", "/de/product/"),
    ("amazon.de", "/dp/"),
    ("amazon.com", "/dp/"),
    ("smythstoys.com", "/p/"),
]

_KNOWN_SHOPS: list[tuple[str, str]] = [
    ("saturn.de", "Saturn"),
    ("mediamarkt.de", "Media Markt"),
    ("amazon.de", "Amazon.de"),
    ("amazon.com", "Amazon"),
    ("jk-entertainment.biz", "JK Entertainment"),
    ("pokegeodude.de", "PokéGeoDude"),
    ("smythstoys.com", "Smyths Toys"),
    ("gate-to-the-games.de", "Gate to the Games"),
    ("spielraum.wien", "Spielraum Wien"),
]


def probe(url: str) -> dict:
    """Probe a URL and return detection results.

    Returns a dict with:
      needs_browser (bool)     – whether Playwright is required
      link_pattern  (str|None) – guessed product link filter for category pages
      shop          (str)      – human-readable shop name
      note          (str)      – explanation shown to the user
    """
    result: dict = {
        "needs_browser": False,
        "link_pattern": _guess_pattern(url),
        "shop": _guess_shop(url),
        "note": "",
    }

    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        result["needs_browser"] = True
        result["note"] = f"request failed — {e}"
        return result

    if resp.status_code != 200:
        result["needs_browser"] = True
        result["note"] = f"HTTP {resp.status_code} — likely bot-protected"
        return result

    html = resp.text

    if result["link_pattern"] is None and "cdn.shopify.com" in html:
        result["link_pattern"] = "/products/"

    for signal in _JS_SIGNALS:
        if signal in html:
            result["needs_browser"] = True
            result["note"] = f"JS-rendered or bot-protected ({signal!r} found)"
            return result

    visible = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)
    if len(visible) < 400:
        result["needs_browser"] = True
        result["note"] = "page appears JS-rendered (almost no text in HTML)"
        return result

    result["note"] = "plain HTTP works"
    return result


def _guess_pattern(url: str) -> str | None:
    for domain, pattern in _KNOWN_PATTERNS:
        if domain in url:
            return pattern
    return None


def _guess_shop(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    if not m:
        return ""
    domain = m.group(1)
    for key, name in _KNOWN_SHOPS:
        if key in domain:
            return name
    return domain
