#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

variant="${1:-voxtral}"
registry="${STT_REGISTRY_PREFIX:-ghcr.io/lxk36/xgc2-stt-service}"
case "${variant}" in
  voxtral)
    image="${registry}:voxtral-0.1.0"
    ;;
  qwen)
    image="${registry}:qwen-0.1.0"
    ;;
  *)
    echo "Usage: $0 [voxtral|qwen]" >&2
    exit 2
    ;;
esac

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created ${repo_dir}/.env; review the LAN bind address and API key before wider access."
fi

STT_IMAGE="${image}" docker compose pull
STT_IMAGE="${image}" docker compose up -d --wait \
  --wait-timeout "${STT_DEPLOY_TIMEOUT:-1800}" --remove-orphans
docker compose ps
