#!/usr/bin/env bash
set -euo pipefail

deb="$(realpath "${1:?usage: smoke_client_deb.sh PACKAGE.deb}")"
package=xgc2-stt-client
[[ "$(dpkg-deb -f "${deb}" Package)" == "${package}" ]]
architecture="$(dpkg-deb -f "${deb}" Architecture)"
[[ "${architecture}" == amd64 || "${architecture}" == arm64 ]]
[[ -z "${TARGET_ARCH:-}" || "${architecture}" == "${TARGET_ARCH}" ]]
contents="$(mktemp)"
extract_root="$(mktemp -d)"
smoke_home="$(mktemp -d)"
cleanup() { rm -rf -- "${contents}" "${extract_root}" "${smoke_home}"; }
trap cleanup EXIT
dpkg-deb --contents "${deb}" >"${contents}"
grep -F './usr/bin/xgc2-stt-client' "${contents}" >/dev/null
grep -F './usr/lib/python3/dist-packages/xgc2_stt/desktop.py' "${contents}" >/dev/null
grep -F './usr/lib/python3/dist-packages/xgc2_stt/desktop_cli.py' "${contents}" >/dev/null
grep -F './usr/share/applications/xgc2-stt-client.desktop' "${contents}" >/dev/null
grep -F './usr/share/metainfo/io.xgc2.stt-client.metainfo.xml' "${contents}" >/dev/null
if grep -E './opt/xgc2-stt-client|PySide|PyInstaller|libQt6' "${contents}" >/dev/null; then
  echo "Client package still contains a bundled Qt/Python runtime." >&2
  exit 1
fi
dpkg-deb --extract "${deb}" "${extract_root}"
binary="${extract_root}/usr/bin/xgc2-stt-client"
[[ -x "${binary}" ]]
head -n 1 "${binary}" | grep -Fq '#!/usr/bin/python3'
export PYTHONPATH="${extract_root}/usr/lib/python3/dist-packages${PYTHONPATH:+:${PYTHONPATH}}"
docs_root="${extract_root}/usr/share/doc/${package}"
[[ -s "${docs_root}/THIRD_PARTY_NOTICES.md" ]]
[[ -s "${docs_root}/copyright" ]]
if grep -Eqi 'PySide6|python-build-standalone|PyInstaller' "${docs_root}/THIRD_PARTY_NOTICES.md"; then
  echo "Client package notices still describe a bundled Qt/CPython runtime." >&2
  exit 1
fi
depends="$(dpkg-deb -f "${deb}" Depends)"
for required_dependency in python3 python3-gi gir1.2-gtk-3.0 python3-websocket python3-pyaudio python3-xlib; do
  printf '%s\n' "${depends}" | grep -Eq "(^|, )${required_dependency}([ ,(]|$)" || {
    echo "Client package is missing a direct runtime dependency: ${required_dependency}" >&2
    exit 1
  }
done
printf '%s\n' "${depends}" | grep -Eq 'gir1.2-ayatanaappindicator3-0.1|gir1.2-appindicator3-0.1' || {
  echo "Client package is missing an AppIndicator runtime dependency." >&2
  exit 1
}
if printf '%s\n' "${depends}" | grep -Eq 'libqt|pyside|pyinstaller'; then
  echo "Client package still depends on Qt." >&2
  exit 1
fi
installed_size="$(dpkg-deb -f "${deb}" Installed-Size)"
[[ "${installed_size}" -lt 5000 ]] || {
  echo "Client package Installed-Size ${installed_size} KiB is too large for a system-Python GTK client." >&2
  exit 1
}

launch=("${binary}")
if command -v xvfb-run >/dev/null 2>&1 && command -v dbus-run-session >/dev/null 2>&1; then
  launch=(xvfb-run -a dbus-run-session -- "${binary}")
elif [[ -z "${DISPLAY:-}" ]]; then
  echo "xvfb-run is required when DISPLAY is unavailable." >&2
  exit 1
fi
set +e
  HOME="${smoke_home}" timeout --signal=TERM --kill-after=3 8 \
    "${launch[@]}" \
    >"${smoke_home}/client.log" 2>&1
status=$?
set -e
if [[ "${status}" != 124 ]]; then
  cat "${smoke_home}/client.log" >&2
  echo "Client exited before the smoke window (status ${status})." >&2
  exit 1
fi

version_log="${smoke_home}/version.log"
if ! HOME="${smoke_home}" timeout --signal=TERM --kill-after=3 8 \
  "${binary}" --version >"${version_log}" 2>&1; then
  cat "${version_log}" >&2
  echo "Client --version failed." >&2
  exit 1
fi
grep -Eq '^xgc2-stt-client [0-9]' "${version_log}" || {
  cat "${version_log}" >&2
  echo "Client --version did not print the package identity." >&2
  exit 1
}

if dpkg-query -W -f='${db:Status-Abbrev}' "${package}" 2>/dev/null | grep -q '^ii'; then
  [[ -x /usr/bin/xgc2-stt-client ]]
  [[ -f /usr/share/applications/xgc2-stt-client.desktop ]]
  [[ -f /usr/share/metainfo/io.xgc2.stt-client.metainfo.xml ]]
  dpkg --verify "${package}"
fi
echo "Client Deb smoke test passed."
