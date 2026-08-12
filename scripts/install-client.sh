#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 1
fi

if ! command -v xdotool >/dev/null 2>&1; then
  echo "xdotool is required for reliable X11 text injection (Ubuntu: sudo apt install xdotool)" >&2
  exit 1
fi

uv tool install --force "${repo_dir}[desktop]"
echo "Installed xgc2-stt-client. Run it once, then configure API URL, key, hotkey, auto-enter, and autostart."
