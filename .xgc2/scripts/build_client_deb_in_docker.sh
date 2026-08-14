#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "${script_dir}/../.." && pwd)"
architecture="${TARGET_ARCH:-$(dpkg --print-architecture)}"
distribution="${PACKAGE_DISTRIBUTION:-focal}"
output_dir="${OUTPUT_DIR:-${repo_root}/debs}"
image_tag="${XGC2_BUILD_IMAGE_TAG:-1.0.0}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --architecture) architecture="$2"; shift 2 ;;
    --distribution) distribution="$2"; shift 2 ;;
    --output-dir) output_dir="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done
case "${architecture}" in amd64) platform=linux/amd64 ;; arm64) platform=linux/arm64 ;; *) exit 2 ;; esac
case "${distribution}" in focal|jammy|noble) ;; *) echo "Supported distributions: focal, jammy, noble." >&2; exit 2 ;; esac
if [[ -n "${XGC2_CLIENT_BUILD_IMAGE:-}" ]]; then
  build_image="${XGC2_CLIENT_BUILD_IMAGE}"
else
  build_image="$("${script_dir}/extra_build_image.sh" "xgc2-build-${distribution}-dev:${image_tag}")"
fi
command -v docker >/dev/null
mkdir -p "${output_dir}"
output_dir="$(cd "${output_dir}" && pwd -P)"
source_date_epoch="$(git -C "${repo_root}" show -s --format=%ct HEAD)"
network_args=()
if [[ -n "${DOCKER_NETWORK:-}" ]]; then
  network_args=(--network "${DOCKER_NETWORK}")
fi

docker run --rm --platform "${platform}" "${network_args[@]}" \
  -e DEBIAN_FRONTEND=noninteractive \
  -e PACKAGE_DISTRIBUTION="${distribution}" \
  -e TARGET_ARCH="${architecture}" \
  -e OUTPUT_DIR=/out \
  -e SOURCE_DATE_EPOCH="${source_date_epoch}" \
  -e HOST_UID="$(id -u)" \
  -e HOST_GID="$(id -g)" \
  -v "${repo_root}:/workspace:ro" \
  -v "${output_dir}:/out" \
  "${build_image}" bash -lc '
    set -euo pipefail
    restore_host_ownership() { chown -R "${HOST_UID}:${HOST_GID}" /out || true; }
    trap restore_host_ownership EXIT
    /workspace/.xgc2/scripts/build_client_deb.sh
  '
