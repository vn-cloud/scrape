"""Command line entry point:  python -m scout [--dry-run] [--preview] [--only NAME ...]"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_settings
from .runner import run
from .telegram import TelegramError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scout", description="Daily project scouting report for ChainGPT Pad")
    parser.add_argument("--dry-run", action="store_true", help="print the report instead of sending it; do not save history")
    parser.add_argument("--preview", action="store_true", help="ignore history and show recent entries (for testing)")
    parser.add_argument("--only", nargs="+", metavar="SOURCE", help="run only these sources, e.g. --only RootData")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = load_settings()
    try:
        run(settings, only=args.only, dry_run=args.dry_run, preview=args.preview)
    except TelegramError as exc:
        logging.error("Could not send the report: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
