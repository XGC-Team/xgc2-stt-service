#!/usr/bin/env bash
set -euo pipefail

image="${1:-xgc2-stt-service:smoke}"
container="xgc2-stt-smoke-${RANDOM}"
port="${STT_SMOKE_PORT:-38080}"

cleanup() {
  docker rm -f "${container}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run --rm -d \
  --name "${container}" \
  -p "127.0.0.1:${port}:8000" \
  -e STT_MANAGE_ENGINE=false \
  "${image}" >/dev/null

for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:${port}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

curl -fsS "http://127.0.0.1:${port}/healthz" | grep -q '"status":"ok"'
curl -fsS "http://127.0.0.1:${port}/api/status" | grep -q '"service":"xgc2-stt-service"'
curl -fsS "http://127.0.0.1:${port}/" | grep -q 'XGC2'
echo "Image smoke test passed: ${image}"
