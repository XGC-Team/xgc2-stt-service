#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created ${repo_dir}/.env; review the LAN bind address and API key before wider access."
fi

docker compose pull
docker compose up -d --remove-orphans
docker compose ps
