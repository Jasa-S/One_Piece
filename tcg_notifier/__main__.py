from __future__ import annotations

import argparse
import logging
import random
import sys
import time
from pathlib import Path

from .checker import check_product
from .config import Config, load_config
from .notifier import send_discord_alert
from .state import State

log = logging.getLogger("tcg_notifier")


def run_once(config: Config, state: State) -> None:
    for product in config.products:
        result = check_product(product, config.defaults)
        if result is None:
            continue

        previously_in_stock = state.was_in_stock(product.url)
        log.info(
            "%s [%s] in_stock=%s (%s)",
            product.name,
            product.shop,
            result.in_stock,
            result.detail,
        )

        if result.in_stock and not previously_in_stock:
            log.info("Sending Discord alert for %s", product.name)
            send_discord_alert(config.webhook_url, product, result.detail)

        state.update(product.url, result.in_stock)


def run_loop(config_path: Path, state_path: Path) -> None:
    while True:
        try:
            config = load_config(config_path)
        except Exception as e:
            log.error("Failed to load config (%s); retrying in 60s.", e)
            time.sleep(60)
            continue

        state = State(state_path)
        run_once(config, state)

        sleep_for = config.defaults.check_interval_seconds + random.randint(
            0, max(0, config.defaults.jitter_seconds)
        )
        log.info("Sleeping %ss until next round.", sleep_for)
        time.sleep(sleep_for)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tcg_notifier",
        description="Notifies a Discord webhook when configured TCG products come in stock.",
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config YAML.")
    parser.add_argument("--state", default="state.json", help="Path to state file.")
    parser.add_argument("--once", action="store_true", help="Check once and exit.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config_path = Path(args.config)
    state_path = Path(args.state)
    if not config_path.exists():
        log.error(
            "Config not found at %s. Copy config.example.yaml to config.yaml first.",
            config_path,
        )
        return 1

    if args.once:
        config = load_config(config_path)
        state = State(state_path)
        run_once(config, state)
    else:
        try:
            run_loop(config_path, state_path)
        except KeyboardInterrupt:
            log.info("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
