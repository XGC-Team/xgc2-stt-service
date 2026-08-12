"""PyInstaller entry point that preserves the xgc2_stt package context."""

from xgc2_stt.desktop import main


if __name__ == "__main__":
    raise SystemExit(main())
