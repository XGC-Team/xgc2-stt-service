"""PyInstaller entry point that preserves the xgc2_stt package context."""

from xgc2_stt.desktop_support import run_desktop_cli


if __name__ == "__main__":
    raise SystemExit(run_desktop_cli())
