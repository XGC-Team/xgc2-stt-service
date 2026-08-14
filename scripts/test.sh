#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

uv run --frozen --python 3.12 --extra dev --extra desktop ruff check src tests scripts/verify-stream.py
uv run --frozen --python 3.12 --extra dev --extra desktop pytest
npm --prefix web ci
npm --prefix web run test
npm --prefix web run typecheck
npm --prefix web run build

docker compose config >/dev/null
STT_VARIANT=qwen docker compose -f docker-compose.yml -f docker-compose.build.yml config >/dev/null
bash -n scripts/*.sh
