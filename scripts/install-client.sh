#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required: https://docs.astral.sh/uv/" >&2
  exit 1
fi

uv tool install --force --refresh "${repo_dir}[desktop]"
echo "Installed xgc2-stt-client. Run it once, then configure API URL, key, hotkey, auto-enter, and optional autostart."
echo "Optional helpers: xclip/xdotool (X11 paste), wl-clipboard/wtype (Wayland paste)."
