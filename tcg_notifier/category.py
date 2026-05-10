from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from .config import Category, Defaults

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FoundProduct:
    url: str
    title: str


def _normalize(url: str) -> str:
    """Strip fragment + query so trivial URL variations don't look like new items."""
    url, _ = urldefrag(url)
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def fetch_category(category: Category, defaults: Defaults) -> list[FoundProduct] | None:
    headers = {
        "User-Agent": defaults.user_agent,
        "Accept-Language": "de-DE,de;q=0.9,en;q=0.6",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        resp = requests.get(
            category.url,
            headers=headers,
            timeout=defaults.request_timeout_seconds,
            allow_redirects=True,
        )
    except requests.RequestException as e:
        log.warning("Request failed for category %s: %s", category.url, e)
        return None
    if resp.status_code != 200:
        log.warning("Non-200 (%s) for category %s", resp.status_code, category.url)
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    pattern = re.compile(category.link_pattern) if category.link_pattern else None

    found: dict[str, str] = {}
    for a in soup.select(category.link_selector):
        href = a.get("href")
        if not href:
            continue
        absolute = urljoin(resp.url, href)
        if pattern and not pattern.search(absolute):
            continue
        normalized = _normalize(absolute)
        if normalized == _normalize(resp.url):
            continue  # skip self-link
        if normalized in found:
            continue
        title = a.get_text(" ", strip=True) or normalized
        found[normalized] = title[:200]
    return [FoundProduct(url=u, title=t) for u, t in found.items()]
