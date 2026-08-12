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
grep -F './opt/xgc2-stt-client/xgc2-stt-client' "${contents}" >/dev/null
grep -F './usr/share/applications/xgc2-stt-client.desktop' "${contents}" >/dev/null
grep -F './usr/share/metainfo/io.xgc2.stt-client.metainfo.xml' "${contents}" >/dev/null
dpkg-deb --extract "${deb}" "${extract_root}"
binary="${extract_root}/opt/xgc2-stt-client/xgc2-stt-client"
docs_root="${extract_root}/usr/share/doc/${package}"
provenance_root="${docs_root}/python-build-standalone"
qt_provenance_root="${docs_root}/qt-runtime"
for required_file in \
  "${docs_root}/THIRD_PARTY_NOTICES.md" \
  "${provenance_root}/PROVENANCE.md" \
  "${provenance_root}/LICENSE.python-build-standalone.txt" \
  "${provenance_root}/python-licenses.rst" \
  "${provenance_root}/downloads.py" \
  "${provenance_root}/licenses/LICENSE.openssl-3.txt" \
  "${provenance_root}/licenses/LICENSE.sqlite.txt" \
  "${provenance_root}/licenses/LICENSE.liblzma.txt" \
  "${provenance_root}/licenses/LICENSE.zlib.txt"; do
  [[ -s "${required_file}" ]] || {
    echo "Client package is missing provenance or license material: ${required_file}" >&2
    exit 1
  }
done
for required_file in \
  "${qt_provenance_root}/PROVENANCE.md" \
  "${qt_provenance_root}/qtbase/LICENSES/LGPL-3.0-only.txt" \
  "${qt_provenance_root}/pyside/sources/pyside6/COPYING" \
  "${qt_provenance_root}/icu/LICENSE" \
  "${qt_provenance_root}/icu/license.html"; do
  [[ -s "${required_file}" ]] || {
    echo "Client package is missing Qt/ICU provenance: ${required_file}" >&2
    exit 1
  }
done
grep -F '20251217' "${provenance_root}/PROVENANCE.md" >/dev/null
grep -F 'PySide6_Essentials 6.7.3' "${qt_provenance_root}/PROVENANCE.md" >/dev/null
grep -F '| OpenSSL | 3.5.4 |' "${docs_root}/THIRD_PARTY_NOTICES.md" >/dev/null
for required_dependency in \
  libdbus-1-3 libportaudio2 libx11-xcb1 libxcb-cursor0 libxcb-icccm4 libxcb-image0 \
  libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 libxcb-shape0; do
  dpkg-deb -f "${deb}" Depends | grep -Eq "(^|, )${required_dependency}([ ,(]|$)" || {
    echo "Client package is missing a direct runtime dependency: ${required_dependency}" >&2
    exit 1
  }
done
while IFS= read -r -d '' candidate; do
  unresolved="$(ldd "${candidate}" 2>/dev/null | grep -F 'not found' || true)"
  if [[ -n "${unresolved}" ]]; then
    printf '%s\n' "${unresolved}" >&2
    echo "Client runtime has unresolved shared-library dependencies: ${candidate}" >&2
    exit 1
  fi
done < <(find "$(dirname "${binary}")" -type f \
  \( -name 'xgc2-stt-client' -o -name '*.so' -o -name '*.so.*' \) -print0)

launch=("${binary}")
if command -v xvfb-run >/dev/null 2>&1 && command -v dbus-run-session >/dev/null 2>&1; then
  launch=(xvfb-run -a dbus-run-session -- "${binary}")
elif [[ -z "${DISPLAY:-}" ]]; then
  echo "xvfb-run is required when DISPLAY is unavailable." >&2
  exit 1
fi
set +e
  QT_QPA_PLATFORM=xcb \
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

if dpkg-query -W -f='${db:Status-Abbrev}' "${package}" 2>/dev/null | grep -q '^ii'; then
  [[ -x /usr/bin/xgc2-stt-client ]]
  [[ -f /usr/share/applications/xgc2-stt-client.desktop ]]
  [[ -f /usr/share/metainfo/io.xgc2.stt-client.metainfo.xml ]]
  dpkg --verify "${package}"
fi
echo "Client Deb smoke test passed."
