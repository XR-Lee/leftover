"""Entry point: agora [bot|console|doctor] [--config PATH]"""
from __future__ import annotations

import argparse
import asyncio
import sys

MIN_PYTHON = (3, 10)

from . import config as config_mod


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        # macOS still ships 3.9 as `python3`; agora needs newer.
        raise SystemExit(
            f"agora needs Python {'.'.join(map(str, MIN_PYTHON))}+, "
            f"this is {sys.version.split()[0]} at {sys.executable}.\n"
            "Try:  brew install python@3.12  (or use uv / pyenv), then rebuild "
            "the venv with that interpreter.")
    parser = argparse.ArgumentParser(prog="agora")
    parser.add_argument("command", nargs="?", default="console",
                        choices=["bot", "console", "doctor", "leftover", "macbot"])
    parser.add_argument("--config", "-c", default=None,
                        help="path to agora.toml")
    args = parser.parse_args(argv)
    cfg = config_mod.load(args.config)

    if args.command in {"leftover", "macbot"}:
        from . import macbot as leftover_mod
        return leftover_mod.main(sys.argv[2:])
    if args.command == "doctor":
        from . import doctor
        print(asyncio.run(doctor.run(cfg)))
        return 0
    if args.command == "bot":
        from .transports import telegram
        telegram.main(cfg)
        return 0
    from .transports import console
    console.main(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
