"""Headless-safe command entry point for the desktop client."""

from __future__ import annotations

import sys

from .desktop_support import (
    format_desktop_version,
    parse_desktop_cli,
    send_running_instance,
)


def main(argv: list[str] | None = None) -> int:
    """Handle control-plane commands before loading GUI or X11 backends."""

    args = parse_desktop_cli(sys.argv[1:] if argv is None else argv)
    if args.version:
        sys.stdout.write(f"{format_desktop_version()}\n")
        return 0
    if args.toggle_capture and send_running_instance("toggle"):
        return 0
    if args.settings and send_running_instance("settings"):
        return 0
    if not args.toggle_capture and not args.settings and send_running_instance("activate"):
        return 0

    from .desktop import run_desktop

    return run_desktop(
        start_capture=args.toggle_capture,
        open_settings=args.settings,
    )


if __name__ == "__main__":
    raise SystemExit(main())
