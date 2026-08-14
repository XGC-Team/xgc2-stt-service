#!/usr/bin/env bash
set -euo pipefail

# Resolve an xgc2-build image on the extra registry. The host comes from
# EXTRA_REGISTRY and must never be hardcoded in this repository.
name="${1:?usage: extra_build_image.sh IMAGE_NAME[:TAG]}"
registry="${EXTRA_REGISTRY:?EXTRA_REGISTRY is required}"
namespace="${EXTRA_NAMESPACE:?EXTRA_NAMESPACE is required}"
registry="${registry#https://}"
registry="${registry#http://}"
registry="${registry%/}"
namespace="${namespace#/}"
namespace="${namespace%/}"
[[ -n "${registry}" && -n "${namespace}" && "${name}" == xgc2-build-* ]]
printf '%s\n' "${registry}/${namespace}/${name}"
