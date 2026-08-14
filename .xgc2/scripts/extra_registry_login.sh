#!/usr/bin/env bash
set -euo pipefail

registry="${EXTRA_REGISTRY:?EXTRA_REGISTRY is required}"
username="${EXTRA_USERNAME:?EXTRA_USERNAME is required}"
password="${EXTRA_PASSWORD:?EXTRA_PASSWORD is required}"
registry="${registry#https://}"
registry="${registry#http://}"
registry="${registry%/}"
printf '%s' "${password}" | docker login --username "${username}" --password-stdin "${registry}" >/dev/null
