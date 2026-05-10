from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

# Global lock so __main__.py and discord_commands.py never write simultaneously
_STATE_LOCK = threading.Lock()


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
            # Preserve extra keys (e.g. last_checked_at)
            for k, v in loaded.items():
                if k not in ("products", "categories"):
                    self._data[k] = v

    def save(self, last_checked_at: str | None = None) -> None:
        if not self._dirty and last_checked_at is None:
            return
        if last_checked_at:
            self._data["last_checked_at"] = last_checked_at
        # Atomic write: write to a temp file then rename so a crash never corrupts state
        with _STATE_LOCK:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=self.path.parent, prefix=".state_tmp_", suffix=".json"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
                    json.dump(self._data, fh, indent=2, sort_keys=True)
                os.replace(tmp_path, self.path)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
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
        existing_stock: dict = entry.get("stock") or {}
        entry["stock"] = {u: existing_stock[u] for u in known if u in existing_stock}
        self._dirty = True

    def was_category_url_in_stock(self, category_key: str, url: str) -> bool | None:
        entry = self._data["categories"].get(category_key) or {}
        stock = entry.get("stock") or {}
        return stock.get(url)

    def update_category_url_stock(self, category_key: str, url: str, in_stock: bool) -> None:
        entry = self._data["categories"].setdefault(category_key, {})
        entry.setdefault("stock", {})[url] = in_stock
        self._dirty = True

    def category_stock_summary(self, category_key: str) -> dict:
        entry = self._data["categories"].get(category_key) or {}
        return dict(entry.get("stock") or {})

    def last_checked_at(self) -> str | None:
        return self._data.get("last_checked_at")
