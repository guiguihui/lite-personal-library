"""Command-line entry point for the short-lived PageIndex v2 worker."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from app.index.v2.worker import run_worker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one PageIndex v2 task file.")
    parser.add_argument("request", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute the request named on the command line."""

    arguments = _parser().parse_args(argv)
    return run_worker(arguments.request)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
