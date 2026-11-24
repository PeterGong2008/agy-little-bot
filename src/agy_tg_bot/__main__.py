"""CLI entrypoint for running AGY Telegram Bot."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .agy_client import AgyRunner
from .bot import AgyTelegramBot
from .config import ConfigError, load_config
from .storage import SessionStore


def _configure_logging(level_name: str) -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noisy HTTP polling logs from httpx / telegram
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agy-tg-bot",
        description="Telegram Bot bridge for Google Antigravity CLI (agy) on VPS",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional path to .env configuration file (defaults to .env in current directory)",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(env_file=args.env_file)
    except ConfigError as exc:
        print(f"[CONFIG ERROR] {exc}", file=sys.stderr)
        return 2

    _configure_logging(config.log_level)
    logger = logging.getLogger(__name__)

    # Ensure default workspace exists
    ws = Path(config.default_workspace)
    try:
        ws.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("Cannot create default workspace %s: %s", ws, exc)
        return 1

    store = SessionStore(config.db_path)
    runner = AgyRunner(config)
    bot = AgyTelegramBot(config, store, runner)
    app = bot.build_application()

    logger.info(
        "Starting AGY Telegram Bot | agy=%s | workspace=%s | allowed_users=%d | mode=%s | perm=%s",
        config.agy_bin,
        config.default_workspace,
        len(config.allowed_user_ids),
        config.runner_mode,
        config.default_permission_mode,
    )

    app.run_polling(drop_pending_updates=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
