from __future__ import annotations

import argparse
import logging
from collections.abc import Callable, Sequence

from app.config.logging import configure_logging
from app.config.runtime import prepare_windows_runtime
from app.config.settings import load_settings

logger = logging.getLogger(__name__)

type CommandHandler = Callable[[argparse.Namespace], int]


def _run_client(_: argparse.Namespace) -> int:
    prepare_windows_runtime(logger)
    from app.launcher import main as launcher_main

    return launcher_main()


def _run_auth(_: argparse.Namespace) -> int:
    prepare_windows_runtime(logger)
    from app.auth.cli import main as auth_main

    return auth_main()


def _run_bot(_: argparse.Namespace) -> int:
    prepare_windows_runtime(logger)
    from app.bot.cli import main as bot_main

    return bot_main()


def _run_worker(_: argparse.Namespace) -> int:
    prepare_windows_runtime(logger)
    from app.worker.cli import main as worker_main

    return worker_main()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="stories",
        description="Unified CLI for the Telegram Stories scheduler runtimes.",
    )
    parser.set_defaults(command="client", handler=_run_client)
    subparsers = parser.add_subparsers(dest="command", required=False)

    auth_parser = subparsers.add_parser("auth", help="Authorize the Telegram user session.")
    auth_parser.set_defaults(handler=_run_auth)

    bot_parser = subparsers.add_parser("bot", help="Run the Telegram bot control plane.")
    bot_parser.set_defaults(handler=_run_bot)

    worker_parser = subparsers.add_parser("worker", help="Run the Telegram Stories worker.")
    worker_parser.set_defaults(handler=_run_worker)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    prepare_windows_runtime()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "client"

    settings = load_settings()
    configure_logging(
        settings.runtime.log_level,
        settings.paths.logs_dir,
        connectivity_summary_interval_seconds=settings.runtime.connectivity_summary_interval_seconds,
    )
    prepare_windows_runtime(logger)
    logger.info("Unified CLI selected mode=%s", args.command)
    logger.debug("Unified CLI parsed args=%s", args)

    handler = getattr(args, "handler", None)
    if handler is None:
        logger.error("Unified CLI parsed no handler for command=%s", args.command)
        parser.error("No handler configured for the selected command")

    try:
        return handler(args)
    except KeyboardInterrupt:
        logger.warning("Unified CLI interrupted by operator for mode=%s", args.command)
        return 130
