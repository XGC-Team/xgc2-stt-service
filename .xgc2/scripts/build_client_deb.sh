#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
output_dir="${OUTPUT_DIR:-${repo_root}/debs}"
distribution="${PACKAGE_DISTRIBUTION:-focal}"
architecture="${TARGET_ARCH:-$(dpkg --print-architecture)}"
package_name=xgc2-stt-client
maintainer='XGC2 Packaging <lxk36@users.noreply.github.com>'

for command_name in dpkg-deb python3; do
  command -v "${command_name}" >/dev/null 2>&1 || {
    echo "Required build command is unavailable: ${command_name}" >&2
    exit 1
  }
done

case "${distribution}" in
  focal) ubuntu_release="20.04" ;;
  jammy) ubuntu_release="22.04" ;;
  noble) ubuntu_release="24.04" ;;
  *)
    echo "Supported distributions: focal, jammy, noble." >&2
    exit 2
    ;;
esac
case "${architecture}" in amd64|arm64) ;; *) echo "Only amd64 and arm64 are supported." >&2; exit 2 ;; esac
[[ "$(dpkg --print-architecture)" == "${architecture}" ]] || {
  echo "Build host architecture does not match ${architecture}." >&2
  exit 1
}

package_version="$(awk -F': *' '/^version:/ {print $2; exit}' "${repo_root}/.xgc2/product.yml")"
work_dir="$(mktemp -d)"
cleanup() { rm -rf -- "${work_dir}"; }
trap cleanup EXIT

"${script_dir}/check_client_privacy.sh" source
install -d "${output_dir}"
find "${output_dir}" -maxdepth 1 -type f -name "${package_name}_*.deb" -delete

source_date_epoch="${SOURCE_DATE_EPOCH:-$(git -C "${repo_root}" show -s --format=%ct HEAD)}"
export SOURCE_DATE_EPOCH="${source_date_epoch}"

pkg_root="${work_dir}/package"
python_pkg="${pkg_root}/usr/lib/python3/dist-packages/xgc2_stt"
install -d \
  "${pkg_root}/DEBIAN" \
  "${pkg_root}/usr/bin" \
  "${python_pkg}" \
  "${pkg_root}/usr/share/applications" \
  "${pkg_root}/usr/share/metainfo" \
  "${pkg_root}/usr/share/doc/${package_name}"

install -m 0644 "${repo_root}/src/xgc2_stt/__init__.py" "${python_pkg}/__init__.py"
install -m 0644 "${repo_root}/src/xgc2_stt/desktop.py" "${python_pkg}/desktop.py"
install -m 0644 "${repo_root}/src/xgc2_stt/desktop_audio.py" "${python_pkg}/desktop_audio.py"
install -m 0644 "${repo_root}/src/xgc2_stt/desktop_cli.py" "${python_pkg}/desktop_cli.py"
install -m 0644 "${repo_root}/src/xgc2_stt/desktop_support.py" "${python_pkg}/desktop_support.py"

cat >"${pkg_root}/usr/bin/${package_name}" <<'EOF'
#!/usr/bin/python3
import sys

from xgc2_stt.desktop_cli import main

if __name__ == "__main__":
    sys.exit(main())
EOF
chmod 0755 "${pkg_root}/usr/bin/${package_name}"

install -m 0644 "${repo_root}/.xgc2/desktop/xgc2-stt-client.desktop" \
  "${pkg_root}/usr/share/applications/xgc2-stt-client.desktop"
install -m 0644 "${repo_root}/.xgc2/desktop/io.xgc2.stt-client.metainfo.xml" \
  "${pkg_root}/usr/share/metainfo/io.xgc2.stt-client.metainfo.xml"
install -m 0644 "${repo_root}/LICENSE" "${pkg_root}/usr/share/doc/${package_name}/copyright"
install -m 0644 "${repo_root}/README.md" "${pkg_root}/usr/share/doc/${package_name}/README.md"
install -m 0644 "${repo_root}/THIRD_PARTY_NOTICES.md" \
  "${pkg_root}/usr/share/doc/${package_name}/THIRD_PARTY_NOTICES.md"

installed_size="$(du -sk "${pkg_root}" | awk '{print $1}')"
cat >"${pkg_root}/DEBIAN/control" <<EOF
Package: ${package_name}
Version: ${package_version}
Section: sound
Priority: optional
Architecture: ${architecture}
Installed-Size: ${installed_size}
Maintainer: ${maintainer}
Depends: python3 (>= 3.8), python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1, python3-websocket, python3-pyaudio, python3-xlib
Recommends: gir1.2-notify-0.7, python3-sounddevice, wl-clipboard, xclip, xdotool
Suggests: wtype, ydotool
Description: Desktop client for a self-hosted streaming STT API
 Provides a status-area client with microphone capture, a configurable
 global shortcut, live transcript preview, and focused text insertion.
 On Wayland, insertion falls back to the clipboard when a paste keystroke
 cannot be synthesized. The service URL and API key are supplied by the
 user after installation. Autostart is optional and off by default.
 This package uses the distribution Python and GTK 3 stack.
EOF
chmod 0644 "${pkg_root}/DEBIAN/control"

find "${pkg_root}" -exec touch --no-dereference --date="@${SOURCE_DATE_EPOCH}" {} +

deb="${output_dir}/${package_name}_${package_version}_${architecture}.ubuntu-${ubuntu_release}.deb"
dpkg-deb --root-owner-group --build "${pkg_root}" "${deb}" >/dev/null
"${script_dir}/check_client_privacy.sh" deb "${deb}"
echo "${deb}"
