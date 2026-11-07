"""Command line: parse the arguments, then start the paned interface."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .settings import help_text

def is_url(text: str) -> bool:
    return text.lower().startswith(("http://", "https://"))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="casttui",
        description="A terminal remote for casting to a Chromecast.",
        epilog=help_text() + "\n\nenvironment: "
        "CASTTUI_ICONS=ascii|unicode, CASTTUI_COLORS=mono|16|256, NO_COLOR",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-V", "--version", action="version", version=f"casttui {__version__}")
    p.add_argument("source", nargs="?",
                   help="a media file or URL to cast as soon as a device is connected")
    p.add_argument("-d", "--device", metavar="NAME_OR_IP",
                   help="connect to this device (a name or an address) without scanning first")
    return p


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Exits with a usage error before any UI starts on a missing file, so a
    bad right-click never opens an empty UI. File paths are made absolute."""
    p = parser()
    args = p.parse_args(argv)
    if args.source is not None and not is_url(args.source):
        path = Path(args.source).expanduser()
        if not path.is_file():
            p.error(f"not a file: {args.source}")
        args.source = str(path.resolve())
    return args


def parse_source(argv: list[str]) -> str | None:
    return parse_args(argv).source


def main() -> None:
    from .config import Config

    args = parse_args(sys.argv[1:])
    cfg = Config.load()
    from .panes import run

    run(config=cfg, source=args.source, device=args.device)
