#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

need_apt=()
python3 -c 'import gi, gi.repository' >/dev/null 2>&1 || need_apt+=(python3-gi python3-gi-cairo gir1.2-gtk-3.0)
python3 -c 'import pyaudio' >/dev/null 2>&1 || need_apt+=(python3-pyaudio)
python3 -c 'import Xlib' >/dev/null 2>&1 || need_apt+=(python3-xlib)
python3 -c 'import websocket' >/dev/null 2>&1 || need_apt+=(python3-websocket)
python3 -c '
import gi
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
except ValueError:
    gi.require_version("AppIndicator3", "0.1")
' >/dev/null 2>&1 || need_apt+=("gir1.2-ayatanaappindicator3-0.1")

if [[ ${#need_apt[@]} -gt 0 ]]; then
  echo "Install GTK/system Python packages first:" >&2
  echo "  sudo apt-get install -y --no-install-recommends ${need_apt[*]}" >&2
  exit 1
fi

install -d "${HOME}/.local/bin"
cat >"${HOME}/.local/bin/xgc2-stt-client" <<EOF
#!/usr/bin/python3
import sys
sys.path.insert(0, "${repo_dir}/src")
from xgc2_stt.desktop_cli import main
raise SystemExit(main())
EOF
chmod 0755 "${HOME}/.local/bin/xgc2-stt-client"
echo "Installed ${HOME}/.local/bin/xgc2-stt-client (system Python + GTK 3)."
echo "Run it once, then configure API URL, key, hotkey, auto-enter, and optional autostart."
echo "Optional helpers: xclip/xdotool (X11 paste), wl-clipboard/wtype (Wayland paste)."
