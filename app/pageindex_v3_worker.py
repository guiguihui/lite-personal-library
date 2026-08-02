"""Command-line entry point for one short-lived PageIndex v3 worker."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

from app.index.v3.worker import run_worker


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one PageIndex v3 task file.")
    parser.add_argument("request", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute exactly the request named on the command line."""

    arguments = _parser().parse_args(argv)
    return run_worker(arguments.request)


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["main"]
