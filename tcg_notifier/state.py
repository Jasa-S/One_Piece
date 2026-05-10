from __future__ import annotations

import json
from pathlib import Path


class State:
    """Persistent state: per-product stock + per-category known/stock data."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._dirty = False
        self._data: dict = {"products": {}, "categories": {}}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                loaded = {}
            if "products" in loaded or "categories" in loaded:
                self._data["products"] = loaded.get("products") or {}
                self._data["categories"] = loaded.get("categories") or {}
            else:
                self._data["products"] = loaded

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
        )
        self._dirty = False

    # ---- explicit products ----

    def was_in_stock(self, url: str) -> bool:
        return bool(self._data["products"].get(url, {}).get("in_stock"))

    def update_product(self, url: str, in_stock: bool) -> None:
        self._data["products"][url] = {"in_stock": in_stock}
        self._dirty = True

    # ---- categories ----

    def known_urls(self, category_key: str) -> set[str]:
        entry = self._data["categories"].get(category_key) or {}
        return set(entry.get("known_urls") or [])

    def is_category_initialized(self, category_key: str) -> bool:
        entry = self._data["categories"].get(category_key) or {}
        return bool(entry.get("initialized"))

    def update_category(self, category_key: str, known: set[str]) -> None:
        entry = self._data["categories"].setdefault(category_key, {})
        entry["initialized"] = True
        entry["known_urls"] = sorted(known)
        # Preserve existing stock data for URLs that are still present;
        # remove stock data for URLs that have disappeared.
        existing_stock: dict = entry.get("stock") or {}
        entry["stock"] = {u: existing_stock[u] for u in known if u in existing_stock}
        self._dirty = True

    def was_category_url_in_stock(self, category_key: str, url: str) -> bool | None:
        """Return True/False if we have a previous result, None if never checked."""
        entry = self._data["categories"].get(category_key) or {}
        stock = entry.get("stock") or {}
        return stock.get(url)  # None if missing

    def update_category_url_stock(self, category_key: str, url: str, in_stock: bool) -> None:
        entry = self._data["categories"].setdefault(category_key, {})
        entry.setdefault("stock", {})[url] = in_stock
        self._dirty = True

    def category_stock_summary(self, category_key: str) -> dict:
        """Return {url: in_stock} for all URLs with a known stock status."""
        entry = self._data["categories"].get(category_key) or {}
        return dict(entry.get("stock") or {})
