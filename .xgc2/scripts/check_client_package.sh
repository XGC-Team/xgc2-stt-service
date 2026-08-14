#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
cd "${repo_root}"

required=(
  .xgc2/product.yml
  .xgc2/desktop.lock
  .xgc2/desktop/launcher.py
  .xgc2/desktop/PYTHON_STANDALONE_PROVENANCE.md
  .xgc2/desktop/QT_RUNTIME_PROVENANCE.md
  .xgc2/desktop/xgc2-stt-client.spec
  .xgc2/desktop/xgc2-stt-client.desktop
  .xgc2/desktop/io.xgc2.stt-client.metainfo.xml
  .xgc2/scripts/build_client_deb.sh
  .xgc2/scripts/build_client_deb_in_docker.sh
  .xgc2/scripts/check_client_package.sh
  .xgc2/scripts/check_client_privacy.sh
  .xgc2/scripts/physical_client_x11.py
  .xgc2/scripts/smoke_client_deb.sh
  .xgc2/scripts/xgc2_artifact_manifest.py
  .xgc2/scripts/publish_client_apt.py
  .github/workflows/client-deb.yml
  .github/workflows/client-deb-ci.yml
)
for path in "${required[@]}"; do [[ -f "${path}" ]] || { echo "Missing ${path}" >&2; exit 1; }; done
for script in .xgc2/scripts/*.sh; do bash -n "${script}"; done
[[ -f .xgc2/desktop/licenses/qtbase/LICENSES/LGPL-3.0-only.txt ]]
[[ -f .xgc2/desktop/licenses/pyside/sources/pyside6/COPYING ]]
[[ -f .xgc2/desktop/licenses/icu/LICENSE ]]
[[ -f .xgc2/desktop/licenses/icu/license.html ]]

[[ "$(awk -F': *' '/^id:/ {print $2; exit}' .xgc2/product.yml)" == xgc2-stt-client ]]
product_version="$(awk -F': *' '/^version:/ {print $2; exit}' .xgc2/product.yml)"
[[ "${product_version}" =~ ^[0-9]+\.[0-9]+\.[0-9]+-[1-9][0-9]*$ ]]
for distro in focal jammy noble; do
  distro_version="$(awk -F': *' -v distro="${distro}" '
    /^[[:space:]]+apt_versions:/ { seen = 1; next }
    seen && $1 ~ "^[[:space:]]+" distro "$" { print $2; exit }
  ' .xgc2/product.yml)"
  [[ "${distro_version}" == "${product_version}" ]] || {
    echo "product.yml apt_versions.${distro} must match version ${product_version}" >&2
    exit 1
  }
done
grep -Eq '^PYTHON_STANDALONE_RELEASE=[0-9]{8}$' .xgc2/desktop.lock
grep -Eq '^PYTHON_STANDALONE_COMMIT=[0-9a-f]{40}$' .xgc2/desktop.lock
[[ "$(grep -Ec '^PYTHON_STANDALONE_(SOURCE|AMD64|ARM64)_SHA256=[0-9a-f]{64}$' .xgc2/desktop.lock)" == 3 ]]
grep -Fq 'Exec=xgc2-stt-client' .xgc2/desktop/xgc2-stt-client.desktop
grep -Fq '<binary>xgc2-stt-client</binary>' .xgc2/desktop/io.xgc2.stt-client.metainfo.xml
grep -Fq '"libQt6OpenGL.so.6"' .xgc2/desktop/xgc2-stt-client.spec
grep -Fq '"libQt6WlShellIntegration.so.6"' .xgc2/desktop/xgc2-stt-client.spec
./.xgc2/scripts/check_client_privacy.sh source
python3 -m py_compile .xgc2/scripts/publish_client_apt.py
git diff --check
echo "Client package compliance passed."
