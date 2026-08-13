#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
output_dir="${OUTPUT_DIR:-${repo_root}/debs}"
distribution="${PACKAGE_DISTRIBUTION:-focal}"
architecture="${TARGET_ARCH:-$(dpkg --print-architecture)}"
package_name=xgc2-stt-client
maintainer='XGC2 Packaging <lxk36@users.noreply.github.com>'

for command_name in curl dpkg-deb readelf sha256sum tar uv; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Required build command is unavailable: ${command_name}" >&2
    exit 1
  }
done

case "${distribution}" in focal) ;; *) echo "Only focal is currently supported." >&2; exit 2 ;; esac
case "${architecture}" in amd64|arm64) ;; *) echo "Only amd64 and arm64 are supported." >&2; exit 2 ;; esac
[[ "$(dpkg --print-architecture)" == "${architecture}" ]] || {
  echo "Build host architecture does not match ${architecture}." >&2
  exit 1
}

# shellcheck disable=SC1090
source "${repo_root}/.xgc2/desktop.lock"
package_version="$(awk -F': *' '/^version:/ {print $2; exit}' "${repo_root}/.xgc2/product.yml")"
work_dir="$(mktemp -d)"
cleanup() { rm -rf -- "${work_dir}"; }
trap cleanup EXIT

case "${architecture}" in
  amd64)
    python_standalone_url="${PYTHON_STANDALONE_AMD64_URL}"
    python_standalone_sha256="${PYTHON_STANDALONE_AMD64_SHA256}"
    pyside6_essentials_sha256="${PYSIDE6_ESSENTIALS_AMD64_SHA256}"
    pyside6_essentials_url="${PYSIDE6_ESSENTIALS_AMD64_URL}"
    shiboken6_sha256="${SHIBOKEN6_AMD64_SHA256}"
    shiboken6_url="${SHIBOKEN6_AMD64_URL}"
    ;;
  arm64)
    python_standalone_url="${PYTHON_STANDALONE_ARM64_URL}"
    python_standalone_sha256="${PYTHON_STANDALONE_ARM64_SHA256}"
    pyside6_essentials_sha256="${PYSIDE6_ESSENTIALS_ARM64_SHA256}"
    pyside6_essentials_url="${PYSIDE6_ESSENTIALS_ARM64_URL}"
    shiboken6_sha256="${SHIBOKEN6_ARM64_SHA256}"
    shiboken6_url="${SHIBOKEN6_ARM64_URL}"
    ;;
esac

download_locked() {
  local url="$1"
  local expected_sha256="$2"
  local target="$3"
  if [[ -f "${target}" ]] && printf '%s  %s\n' "${expected_sha256}" "${target}" | sha256sum --check --status; then
    return
  fi
  local partial="${target}.partial.$$"
  rm -f -- "${partial}"
  curl --fail --location --http1.1 \
    --connect-timeout 30 --max-time 1200 \
    --retry 8 --retry-delay 2 --retry-connrefused \
    --output "${partial}" "${url}"
  printf '%s  %s\n' "${expected_sha256}" "${partial}" | sha256sum --check --status || {
    rm -f -- "${partial}"
    echo "Downloaded archive failed SHA-256 verification: ${url}" >&2
    exit 1
  }
  mv -- "${partial}" "${target}"
}

"${script_dir}/check_client_privacy.sh" source
install -d "${output_dir}"
find "${output_dir}" -maxdepth 1 -type f -name "${package_name}_*.deb" -delete

python_cache_dir="${PYTHON_STANDALONE_CACHE_DIR:-${work_dir}/python-cache}"
install -d "${python_cache_dir}"
python_archive="${python_cache_dir}/cpython-${PYTHON_VERSION}+${PYTHON_STANDALONE_RELEASE}-${architecture}-install_only_stripped.tar.gz"
python_source_archive="${python_cache_dir}/python-build-standalone-${PYTHON_STANDALONE_COMMIT}.tar.gz"
pyside6_essentials_wheel="${python_cache_dir}/${pyside6_essentials_url##*/}"
shiboken6_wheel="${python_cache_dir}/${shiboken6_url##*/}"
download_locked "${python_standalone_url}" "${python_standalone_sha256}" "${python_archive}"
download_locked "${PYTHON_STANDALONE_SOURCE_URL}" "${PYTHON_STANDALONE_SOURCE_SHA256}" "${python_source_archive}"
download_locked "${pyside6_essentials_url}" "${pyside6_essentials_sha256}" "${pyside6_essentials_wheel}"
download_locked "${shiboken6_url}" "${shiboken6_sha256}" "${shiboken6_wheel}"
tar -xzf "${python_archive}" -C "${work_dir}"
python_root="${work_dir}/python"
[[ -x "${python_root}/bin/python3.12" ]] || { echo "Pinned CPython runtime is missing." >&2; exit 1; }
[[ "$("${python_root}/bin/python3.12" --version)" == "Python ${PYTHON_VERSION}" ]] || {
  echo "Pinned CPython runtime version does not match the lock file." >&2
  exit 1
}
python_source_root="${work_dir}/python-build-standalone-source"
install -d "${python_source_root}"
tar -xzf "${python_source_archive}" --strip-components=1 -C "${python_source_root}"

build_venv="${work_dir}/venv"
uv venv --python "${python_root}/bin/python3.12" "${build_venv}" >/dev/null
uv pip install --python "${build_venv}/bin/python" --no-deps \
  "${pyside6_essentials_wheel}" "${shiboken6_wheel}" >/dev/null
uv pip install --python "${build_venv}/bin/python" \
  "pyinstaller==${PYINSTALLER_VERSION}" \
  "pyinstaller-hooks-contrib==${PYINSTALLER_HOOKS_VERSION}" \
  "altgraph==${ALTGRAPH_VERSION}" \
  "packaging==${PACKAGING_VERSION}" \
  "setuptools==${SETUPTOOLS_VERSION}" \
  "sounddevice==${SOUNDDEVICE_VERSION}" \
  "cffi==${CFFI_VERSION}" \
  "pycparser==${PYCPARSER_VERSION}" \
  "pynput==${PYNPUT_VERSION}" \
  "evdev==${EVDEV_VERSION}" \
  "python-xlib==${PYTHON_XLIB_VERSION}" \
  "six==${SIX_VERSION}" \
  "websockets==${WEBSOCKETS_VERSION}" >/dev/null

python_license="$("${build_venv}/bin/python" - <<'PY'
import pathlib
import sys
print(pathlib.Path(sys.base_prefix) / "lib" / "python3.12" / "LICENSE.txt")
PY
)"
[[ -f "${python_license}" ]] || { echo "CPython license is missing." >&2; exit 1; }
# The install-only runtime carries CPython's top-level license while the
# python-build-standalone source bundle additionally carries incorporated
# software acknowledgements. They are intentionally not byte-identical; both
# are archived below and the runtime/source archives are independently pinned.

source_date_epoch="${SOURCE_DATE_EPOCH:-$(git -C "${repo_root}" show -s --format=%ct HEAD)}"
export SOURCE_DATE_EPOCH="${source_date_epoch}"
export PYTHONHASHSEED=0
"${build_venv}/bin/pyinstaller" \
  --clean --noconfirm \
  --distpath "${work_dir}/dist" \
  --workpath "${work_dir}/pyinstaller" \
  "${repo_root}/.xgc2/desktop/xgc2-stt-client.spec" >/dev/null

# Qt uses zstd through its stable shared-library ABI. Prefer Focal's maintained
# system libzstd1 over PyInstaller's copied wheel library; the Deb dependency
# below makes that resolution explicit and the installed-package smoke runs
# ldd after dependency installation.
find "${work_dir}/dist/xgc2-stt-client" -name 'libzstd.so*' -delete

# Berkeley DB's Sleepycat license and Tcl/Tk's bundled subcomponents require a
# different source-compliance path. The desktop client does not use them, so
# keep that boundary machine-verifiable instead of relying on PyInstaller's
# current import analysis.
dist_root="${work_dir}/dist/xgc2-stt-client"
forbidden="$(find "${dist_root}" \
  \( -iname '*_dbm*' -o -iname 'dbm' -o -iname 'dbm.*' \
     -o -iname 'libdb.so*' -o -iname 'libdb-*.so*' -o -iname 'libdb*.a' \
     -o -iname '*_tkinter*' -o -iname 'tkinter' -o -iname 'tkinter.*' \
     -o -iname 'libtcl*' -o -iname 'libtk*.so*' -o -iname 'libitcl*' \
     -o -iname 'libthread*' -o -iname 'itcl4*' -o -iname 'thread2*' \
     -o -iname '_tcl_data' -o -iname '_tk_data' -o -iname 'tcl8*' -o -iname 'tk8*' \
     -o -iname 'libzstd*' -o -iname '*ffmpeg*' -o -iname '*gstreamer*' \
     -o -iname '*Qt6Multimedia*' -o -iname '*Qt6VirtualKeyboard*' \
     -o -iname '*Qt6WebEngine*' -o -iname '*virtualkeyboard*' \
     -o -iname 'libav*' \) -print -quit)"
[[ -z "${forbidden}" ]] || {
  echo "Unexpected optional runtime requires additional license handling: ${forbidden}" >&2
  exit 1
}

archive_listing="$("${build_venv}/bin/pyi-archive_viewer" -r -b \
  "${dist_root}/xgc2-stt-client")"
if printf '%s\n' "${archive_listing}" | grep -Eq \
  '(^|[./[:space:]])(_dbm|dbm|_tkinter|tkinter)([./[:space:]]|$)'; then
  echo "Excluded DBM or Tkinter module found in the PyInstaller archive." >&2
  exit 1
fi
"${build_venv}/bin/python" - "${dist_root}" <<'PY'
from pathlib import Path
import sys
import zipfile

for archive in Path(sys.argv[1]).rglob("*.zip"):
    with zipfile.ZipFile(archive) as stream:
        for name in stream.namelist():
            parts = {part.lower() for part in Path(name).parts}
            if parts.intersection({"_dbm", "dbm", "_tkinter", "tkinter"}):
                raise SystemExit(f"excluded module found in {archive}: {name}")
PY
while IFS= read -r -d '' candidate; do
  needed="$(readelf -d "${candidate}" 2>/dev/null | sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p' || true)"
  if printf '%s\n' "${needed}" | grep -Eq \
    '^(libdb([.-].*)?|libtcl.*|libtk8.*|libitcl.*|libthread.*)$'; then
    echo "Unexpected optional native dependency in ${candidate}: ${needed}" >&2
    exit 1
  fi
done < <(find "${dist_root}" -type f -print0)

pkg_root="${work_dir}/package"
install -d \
  "${pkg_root}/DEBIAN" \
  "${pkg_root}/opt/xgc2-stt-client" \
  "${pkg_root}/usr/bin" \
  "${pkg_root}/usr/share/applications" \
  "${pkg_root}/usr/share/metainfo" \
  "${pkg_root}/usr/share/doc/${package_name}"
cp -a "${work_dir}/dist/xgc2-stt-client/." "${pkg_root}/opt/xgc2-stt-client/"
# PyInstaller retains the websockets wheel RECORD although its console script
# isn't shipped. That entry hashes a temporary-venv shebang and would make two
# otherwise identical packages differ byte-for-byte.
while IFS= read -r record; do
  sed -i '\#^../../../bin/websockets,#d' "${record}"
done < <(find "${pkg_root}/opt/xgc2-stt-client" -type f \
  -path '*/websockets-*.dist-info/RECORD' -print | LC_ALL=C sort)
chmod -R go-w "${pkg_root}/opt/xgc2-stt-client"
ln -s /opt/xgc2-stt-client/xgc2-stt-client "${pkg_root}/usr/bin/xgc2-stt-client"
install -m 0644 "${repo_root}/.xgc2/desktop/xgc2-stt-client.desktop" \
  "${pkg_root}/usr/share/applications/xgc2-stt-client.desktop"
install -m 0644 "${repo_root}/.xgc2/desktop/io.xgc2.stt-client.metainfo.xml" \
  "${pkg_root}/usr/share/metainfo/io.xgc2.stt-client.metainfo.xml"
install -m 0644 "${repo_root}/LICENSE" "${pkg_root}/usr/share/doc/${package_name}/copyright"
install -m 0644 "${repo_root}/README.md" "${pkg_root}/usr/share/doc/${package_name}/README.md"
install -m 0644 "${repo_root}/THIRD_PARTY_NOTICES.md" \
  "${pkg_root}/usr/share/doc/${package_name}/THIRD_PARTY_NOTICES.md"
qt_provenance_dir="${pkg_root}/usr/share/doc/${package_name}/qt-runtime"
install -d "${qt_provenance_dir}"
install -m 0644 "${repo_root}/.xgc2/desktop/QT_RUNTIME_PROVENANCE.md" \
  "${qt_provenance_dir}/PROVENANCE.md"
cp -a "${repo_root}/.xgc2/desktop/licenses/." "${qt_provenance_dir}/"
chmod -R go-w "${qt_provenance_dir}"
install -m 0644 "${python_license}" \
  "${pkg_root}/usr/share/doc/${package_name}/LICENSE.CPython.txt"
python_provenance_dir="${pkg_root}/usr/share/doc/${package_name}/python-build-standalone"
install -d "${python_provenance_dir}/licenses"
install -m 0644 "${repo_root}/.xgc2/desktop/PYTHON_STANDALONE_PROVENANCE.md" \
  "${python_provenance_dir}/PROVENANCE.md"
install -m 0644 "${python_source_root}/LICENSE" \
  "${python_provenance_dir}/LICENSE.python-build-standalone.txt"
install -m 0644 "${python_source_root}/python-licenses.rst" \
  "${python_provenance_dir}/python-licenses.rst"
install -m 0644 "${python_source_root}/pythonbuild/downloads.py" \
  "${python_provenance_dir}/downloads.py"
while IFS= read -r license; do
  install -m 0644 "${license}" "${python_provenance_dir}/licenses/$(basename "${license}")"
done < <(find "${python_source_root}" -maxdepth 1 -type f -name 'LICENSE*.txt' -print | LC_ALL=C sort)
install -m 0644 /usr/share/common-licenses/LGPL-3 \
  "${pkg_root}/usr/share/doc/${package_name}/LICENSE.Qt-LGPL-3.txt"
install -m 0644 /usr/share/common-licenses/GPL-3 \
  "${pkg_root}/usr/share/doc/${package_name}/LICENSE.Qt-GPL-3.txt"
install -m 0644 /usr/share/common-licenses/LGPL-2.1 \
  "${pkg_root}/usr/share/doc/${package_name}/LICENSE.LGPL-2.1.txt"
install -m 0644 /usr/share/common-licenses/GPL-2 \
  "${pkg_root}/usr/share/doc/${package_name}/LICENSE.GPL-2.txt"
licenses_dir="${pkg_root}/usr/share/doc/${package_name}/licenses"
install -d "${licenses_dir}"
while IFS= read -r license; do
  relative="${license#${build_venv}/lib/python3.12/site-packages/}"
  safe_name="${relative//\//__}"
  install -m 0644 "${license}" "${licenses_dir}/${safe_name}"
done < <(find "${build_venv}/lib/python3.12/site-packages" -maxdepth 4 -type f \
  \( -iname 'LICENSE*' -o -iname 'COPYING*' \) -print | LC_ALL=C sort)
ln -s /usr/share/common-licenses/GPL-2 "${licenses_dir}/GPL-2"
ln -s /usr/share/common-licenses/GPL-3 "${licenses_dir}/GPL-3"
ln -s /usr/share/common-licenses/LGPL-2.1 "${licenses_dir}/LGPL-2.1"
ln -s /usr/share/common-licenses/LGPL-3 "${licenses_dir}/LGPL-3"

installed_size="$(du -sk "${pkg_root}" | awk '{print $1}')"
cat >"${pkg_root}/DEBIAN/control" <<EOF
Package: ${package_name}
Version: ${package_version}
Section: sound
Priority: optional
Architecture: ${architecture}
Installed-Size: ${installed_size}
Maintainer: ${maintainer}
Depends: libasound2 (>= 1.0.16), libc6 (>= 2.31), libdbus-1-3, libegl1, libfontconfig1, libgl1, libglib2.0-0, libportaudio2, libx11-6, libx11-xcb1, libxcb1, libxcb-cursor0, libxcb-icccm4, libxcb-image0, libxcb-keysyms1, libxcb-randr0, libxcb-render-util0, libxcb-shape0, libxext6, libxkbcommon-x11-0, libxkbcommon0, libzstd1 (>= 1.4.4), xclip, xdotool
Recommends: libayatana-appindicator3-1 | libappindicator3-1
Description: Desktop client for a self-hosted streaming STT API
 Provides an X11 status-area client with microphone capture, a configurable
 global shortcut, live transcript preview, and focused text insertion. The
 service URL and API key are supplied by the user after installation.
EOF
chmod 0644 "${pkg_root}/DEBIAN/control"

# Normalize the complete package tree, including generated PyInstaller files,
# so repeated builds of the same source produce the same Deb bytes.
find "${pkg_root}" -exec touch --no-dereference --date="@${SOURCE_DATE_EPOCH}" {} +

deb="${output_dir}/${package_name}_${package_version}_${architecture}.deb"
dpkg-deb --root-owner-group --build "${pkg_root}" "${deb}" >/dev/null
"${script_dir}/check_client_privacy.sh" deb "${deb}"
echo "${deb}"
