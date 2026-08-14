#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

registry="${STT_REGISTRY_PREFIX:-ghcr.io/xgc-team/xgc2-stt-service}"
image="${registry}:qwen-0.1.0"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created ${repo_dir}/.env; review the LAN bind address and API key before wider access."
fi

STT_IMAGE="${image}" docker compose pull
STT_IMAGE="${image}" docker compose up -d --wait \
  --wait-timeout "${STT_DEPLOY_TIMEOUT:-1800}" --remove-orphans
docker compose ps
