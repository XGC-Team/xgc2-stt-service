#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${repo_dir}"

port="${STT_PORT:-8000}"
base="http://127.0.0.1:${port}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT

docker compose exec -T stt nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
curl -fsS "${base}/readyz" >/dev/null
curl -fsSL \
  https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-ASR-Repo/asr_zh.wav \
  -o "${tmp_dir}/asr-zh.wav"

curl_args=(-fsS "${base}/v1/audio/transcriptions")
if [ -n "${STT_API_KEY:-}" ]; then
  curl_args+=(-H "Authorization: Bearer ${STT_API_KEY}")
fi
response="$(curl "${curl_args[@]}" \
  -F "file=@${tmp_dir}/asr-zh.wav" \
  -F model=stt-1 \
  -F language=zh)"
printf '%s\n' "${response}"
grep -Pq '[\x{4e00}-\x{9fff}]' <<<"${response}"

ffmpeg -hide_banner -loglevel error -y \
  -i "${tmp_dir}/asr-zh.wav" \
  -ac 1 -ar 16000 -f s16le "${tmp_dir}/asr-zh.pcm"
uv run --frozen --python 3.12 --extra dev \
  python scripts/verify-stream.py "${base}" "${tmp_dir}/asr-zh.pcm"
