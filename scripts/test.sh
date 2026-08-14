#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

if ! command -v ruff >/dev/null || ! command -v pytest >/dev/null; then
  echo "Run this inside ghcr.io/xgc-team/xgc2-images/xgc2-stt-dev:1.0.0" >&2
  echo "Dependencies are installed in xgc2-images, not in this repository." >&2
  exit 1
fi

export PYTHONPATH="${repo_dir}/src${PYTHONPATH:+:${PYTHONPATH}}"
if [[ ! -e web/node_modules && -d /opt/xgc2-stt/web/node_modules ]]; then
  ln -sfn /opt/xgc2-stt/web/node_modules web/node_modules
fi

ruff check src tests scripts/verify-stream.py
env -u DISPLAY -u WAYLAND_DISPLAY pytest
npm --prefix web run test
npm --prefix web run typecheck
npm --prefix web run build

docker compose config >/dev/null
STT_VARIANT=qwen docker compose -f docker-compose.yml -f docker-compose.build.yml config >/dev/null
bash -n scripts/*.sh
