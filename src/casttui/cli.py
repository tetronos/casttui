"""Command line: parse the arguments, then run a headless command or start the
paned interface."""
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
    # Headless commands: no UI, one line of output, exit code 0 or 1. Flags
    # rather than subcommands, so a file named "stop" or "page" stays castable.
    h = p.add_mutually_exclusive_group()
    h.add_argument("--page", metavar="URL",
                   help="show a web page on the device (DashCast) and exit")
    h.add_argument("--desktop", action="store_true",
                   help="cast the live screen until Ctrl+C or casttui --stop "
                        "(desktop_* settings)")
    h.add_argument("--cells", action="store_true",
                   help="serve and cast a tetron-wm cell stream read from stdin "
                        "(JSON lines) until the pipe closes or casttui --stop")
    h.add_argument("--stop", action="store_true",
                   help="stop whatever the device is showing and exit")
    h.add_argument("--status", action="store_true",
                   help="print the device name and running app, and exit")
    p.add_argument("--no-cast", action="store_true",
                   help="with --cells: do not use a device, print the URL for a browser on this LAN")
    return p


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Exits with a usage error before any UI starts on a missing file, so a
    bad right-click never opens an empty UI. File paths are made absolute."""
    p = parser()
    args = p.parse_args(argv)
    if (args.page or args.desktop or args.cells or args.stop or args.status) and args.source is not None:
        p.error("a source can not be combined with --page, --desktop, --cells, --stop or --status")
    if args.no_cast and not args.cells:
        p.error("--no-cast needs --cells")
    if args.page is not None and not is_url(args.page):
        p.error("--page needs an http:// or https:// URL")
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
    # Headless first, before importing the UI: fast, and no terminal needed.
    if args.page or args.desktop or args.cells or args.stop or args.status:
        from . import headless

        if args.cells:
            headless.exit_now(headless.cells(device=args.device, no_cast=args.no_cast))

        if args.desktop:
            headless.exit_now(headless.desktop(device=args.device))

        action = "page" if args.page else "stop" if args.stop else "status"
        headless.exit_now(headless.run(action, url=args.page, device=args.device))
    cfg = Config.load()
    from .panes import run

    run(config=cfg, source=args.source, device=args.device)
