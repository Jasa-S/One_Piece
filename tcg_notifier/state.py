from __future__ import annotations

import json
from pathlib import Path


class State:
    """Tracks last-known stock status per product URL to avoid duplicate alerts."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, dict] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._data = {}

    def was_in_stock(self, url: str) -> bool:
        return bool(self._data.get(url, {}).get("in_stock"))

    def update(self, url: str, in_stock: bool) -> None:
        self._data[url] = {"in_stock": in_stock}
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
        )
