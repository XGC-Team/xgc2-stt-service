#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
cd "${repo_root}"

required=(
  .xgc2/product.yml
  .xgc2/desktop/xgc2-stt-client.desktop
  .xgc2/desktop/io.xgc2.stt-client.metainfo.xml
  .xgc2/scripts/build_client_deb.sh
  .xgc2/scripts/build_client_deb_in_docker.sh
  .xgc2/scripts/check_client_package.sh
  .xgc2/scripts/check_client_privacy.sh
  .xgc2/scripts/extra_build_image.sh
  .xgc2/scripts/extra_registry_login.sh
  .xgc2/scripts/physical_client_x11.py
  .xgc2/scripts/smoke_client_deb.sh
  .xgc2/scripts/xgc2_artifact_manifest.py
  src/xgc2_stt/desktop.py
  src/xgc2_stt/desktop_audio.py
  src/xgc2_stt/desktop_cli.py
  src/xgc2_stt/desktop_support.py
  .github/workflows/client-deb.yml
  .github/workflows/client-deb-ci.yml
)
for path in "${required[@]}"; do [[ -f "${path}" ]] || { echo "Missing ${path}" >&2; exit 1; }; done
for script in .xgc2/scripts/*.sh; do bash -n "${script}"; done

[[ ! -e .xgc2/desktop.lock ]]
[[ ! -e .xgc2/desktop/xgc2-stt-client.spec ]]
[[ ! -e .xgc2/desktop/launcher.py ]]
[[ ! -e .xgc2/desktop/QT_RUNTIME_PROVENANCE.md ]]
[[ ! -e .xgc2/desktop/PYTHON_STANDALONE_PROVENANCE.md ]]

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
grep -Fq 'Exec=xgc2-stt-client' .xgc2/desktop/xgc2-stt-client.desktop
grep -Fq '<binary>xgc2-stt-client</binary>' .xgc2/desktop/io.xgc2.stt-client.metainfo.xml
grep -Fq 'xgc2-stt-client = "xgc2_stt.desktop_cli:main"' pyproject.toml
grep -Fq "CLIENT_VERSION = \"${product_version%-*}\"" src/xgc2_stt/desktop_support.py
grep -Fq 'gi.require_version("Gtk", "3.0")' src/xgc2_stt/desktop.py
grep -Fq 'AyatanaAppIndicator3' src/xgc2_stt/desktop.py
if grep -Eq 'PySide6|pynput|PyInstaller|QT_QPA_PLATFORM' src/xgc2_stt/desktop.py src/xgc2_stt/desktop_cli.py src/xgc2_stt/desktop_support.py; then
  echo "Desktop client still imports a bundled Qt/PyInstaller stack." >&2
  exit 1
fi
grep -Fq 'python3-gi' .xgc2/scripts/build_client_deb.sh
grep -Fq 'python3-pyaudio' .xgc2/scripts/build_client_deb.sh
grep -Fq 'needs: [release-guard, source-tests]' .github/workflows/client-deb.yml
grep -Fq 'secrets.EXTRA_REGISTRY' .github/workflows/client-deb.yml
grep -Fq 'secrets.EXTRA_REGISTRY' .github/workflows/client-deb-ci.yml
grep -Fq 'xgc2-build-noble-dev:1.0.0' .github/workflows/client-deb.yml
grep -Fq 'xgc2-build-focal-dev:1.0.0' .github/workflows/client-deb-ci.yml
if grep -Eq 'run_cpp_quality|run_source_tests|--clobber|gh release upload' \
  .github/workflows/client-deb.yml; then
  echo "Desktop release workflow contains a bypass or overwrite path." >&2
  exit 1
fi
if grep -Eq 'astral-sh/setup-uv|actions/setup-node|ubuntu:20\.04|ubuntu:22\.04|ubuntu:24\.04' \
  .github/workflows/client-deb.yml .github/workflows/client-deb-ci.yml; then
  echo "Desktop workflows must use xgc2-build images without toolchain bootstrap." >&2
  exit 1
fi
if grep -Fq 'ghcr.io/xgc-team/xgc2-images/' \
  .github/workflows/client-deb.yml .github/workflows/client-deb-ci.yml; then
  echo "Desktop workflows must pull xgc2-build images from EXTRA_REGISTRY." >&2
  exit 1
fi
./.xgc2/scripts/check_client_privacy.sh source
git diff --check
echo "Client package compliance passed."
