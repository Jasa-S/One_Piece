from __future__ import annotations

import json
from pathlib import Path


class State:
    """Persistent state: per-product stock + per-category known product URLs."""

    def __init__(self, path: Path) -> None:
        self.path = path
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
                # legacy flat shape: every key was a product URL
                self._data["products"] = loaded

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
        )

    # ---- products ----
    def was_in_stock(self, url: str) -> bool:
        return bool(self._data["products"].get(url, {}).get("in_stock"))

    def update_product(self, url: str, in_stock: bool) -> None:
        self._data["products"][url] = {"in_stock": in_stock}
        self._save()

    # ---- categories ----
    def known_urls(self, category_key: str) -> set[str]:
        entry = self._data["categories"].get(category_key) or {}
        return set(entry.get("known_urls") or [])

    def is_category_initialized(self, category_key: str) -> bool:
        entry = self._data["categories"].get(category_key) or {}
        return bool(entry.get("initialized"))

    def update_category(self, category_key: str, known: set[str]) -> None:
        self._data["categories"][category_key] = {
            "initialized": True,
            "known_urls": sorted(known),
        }
        self._save()
