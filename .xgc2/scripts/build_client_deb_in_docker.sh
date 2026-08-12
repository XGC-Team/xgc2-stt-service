#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
architecture="${TARGET_ARCH:-$(dpkg --print-architecture)}"
distribution="${PACKAGE_DISTRIBUTION:-focal}"
output_dir="${OUTPUT_DIR:-${repo_root}/debs}"
cache_dir="${XGC2_CLIENT_BUILD_CACHE_DIR:-${repo_root}/.ci/client-build-cache}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --architecture) architecture="$2"; shift 2 ;;
    --distribution) distribution="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "${architecture}" in amd64) platform=linux/amd64 ;; arm64) platform=linux/arm64 ;; *) exit 2 ;; esac
[[ "${distribution}" == focal ]] || { echo "Only focal is currently supported." >&2; exit 2; }
command -v docker >/dev/null
uv_binary="$(command -v uv)"
mkdir -p "${output_dir}"
output_dir="$(cd "${output_dir}" && pwd -P)"
mkdir -p "${cache_dir}"
cache_dir="$(cd "${cache_dir}" && pwd -P)"
source_date_epoch="$(git -C "${repo_root}" show -s --format=%ct HEAD)"
network_args=()
if [[ -n "${DOCKER_NETWORK:-}" ]]; then
  network_args=(--network "${DOCKER_NETWORK}")
fi
apt_mirror="${XGC2_CLIENT_APT_MIRROR:-}"
if [[ -n "${apt_mirror}" && ! "${apt_mirror}" =~ ^https?://[A-Za-z0-9._:/-]+$ ]]; then
  echo "XGC2_CLIENT_APT_MIRROR must be an HTTP(S) URL without query parameters." >&2
  exit 2
fi

docker run --rm --platform "${platform}" "${network_args[@]}" \
  -e DEBIAN_FRONTEND=noninteractive \
  -e PACKAGE_DISTRIBUTION="${distribution}" \
  -e TARGET_ARCH="${architecture}" \
  -e OUTPUT_DIR=/out \
  -e PYTHON_STANDALONE_CACHE_DIR=/cache \
  -e SOURCE_DATE_EPOCH="${source_date_epoch}" \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -e APT_MIRROR="${apt_mirror}" \
  -v "${repo_root}:/workspace:ro" \
  -v "${output_dir}:/out" \
  -v "${cache_dir}:/cache" \
  -v "${uv_binary}:/usr/local/bin/uv:ro" \
  ubuntu:20.04 bash -lc '
    set -euo pipefail
    restore_host_ownership() { chown -R "${HOST_UID}:${HOST_GID}" /cache /out || true; }
    trap restore_host_ownership EXIT
    if [[ -n "${APT_MIRROR}" ]]; then
      mirror="${APT_MIRROR%/}"
      sed -i -E "s#https?://(archive|security)\.ubuntu\.com/ubuntu#${mirror}#g" /etc/apt/sources.list
    fi
    apt-get -o Acquire::Retries=5 update >/dev/null
    apt-get install -y --no-install-recommends binutils build-essential ca-certificates clang curl dpkg-dev python3 ripgrep >/dev/null
    /workspace/.xgc2/scripts/build_client_deb.sh
  '
