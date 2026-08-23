"""Entry point: agora [bot|console|doctor] [--config PATH]"""
from __future__ import annotations

import argparse
import asyncio
import sys

from . import config as config_mod

MIN_PYTHON = (3, 10)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        # macOS still ships 3.9 as `python3`; leftover needs newer.
        raise SystemExit(
            f"agora needs Python {'.'.join(map(str, MIN_PYTHON))}+, "
            f"this is {sys.version.split()[0]} at {sys.executable}.\n"
            "Try:  brew install python@3.12  (or use uv / pyenv), then rebuild "
            "the venv with that interpreter.")
    parser = argparse.ArgumentParser(prog="agora")
    parser.add_argument("command", nargs="?", default="console",
                        choices=["bot", "console", "doctor", "leftover", "macbot"])
    parser.add_argument("--config", "-c", default=None,
                        help="path to leftover.toml")
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
        try:
            from .transports import telegram
        except ModuleNotFoundError as exc:      # optional extra, frozen path
            raise SystemExit(
                f"`agora bot` needs the telegram extra ({exc.name} is missing).\n"
                "Install it with:  pip install 'leftover[telegram]'") from None
        telegram.main(cfg)
        return 0
    from .transports import console
    console.main(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
