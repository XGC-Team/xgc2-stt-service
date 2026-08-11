# XGC2 STT Service

One-step, GPU-native realtime speech-to-text service for the XGC2 trusted
network. The public release images include the selected model weights: pulling
an image is the only model-installation step.

This is an independent platform repository with its own API gateway, WebUI,
container images, tests, and release workflow. It is not an App Store entry and
is not deployed to a remote server in the current phase.

## Images

All images share the same API and React/GCS WebUI:

| Tag | Embedded model | Realtime behavior | Role |
| --- | --- | --- | --- |
| `base-0.1.0` | none | configurable | Common CUDA/vLLM/API/WebUI runtime |
| `qwen-0.1.0` | Qwen3-ASR-1.7B | vLLM native 5 s segments | Chinese/dialect comparison |
| `voxtral-0.1.0` | Voxtral Mini 4B Realtime 2602 | native 480 ms streaming | Recommended/default |
| `latest` | Voxtral Mini 4B Realtime 2602 | native 480 ms streaming | Current recommended release |

Public release tags are published to GHCR:

```text
ghcr.io/lxk36/xgc2-stt-service:<tag>
```

An optional private China mirror receives identical tags, but its address is
not part of this public repository. Operators select it through private local
configuration:

```bash
STT_REGISTRY_PREFIX="${PRIVATE_REGISTRY}/xgc2-stt-service" \
  ./scripts/deploy-local.sh
```

The exact model revisions are pinned in the Dockerfile. Model files are stored
under `/opt/xgc2-stt/models` in the image, not downloaded into a runtime volume.
The writable `xgc2-stt-cache` volume contains only vLLM/Triton compilation
caches and can be recreated.

## Deployment boundary

```text
browser / GCS / Agent Hub
           |
           | HTTPS + WSS
           v
internal reverse proxy
           |
           | trusted LAN
           v
RTX 4090 workstation :8000
           |
           v
one XGC2 STT container
  ├─ API + WebUI gateway :8000
  └─ vLLM Realtime       127.0.0.1:8001
```

- The container runs on this GPU workstation in the current phase.
- The internal server supplies HTTPS, client authentication, access policy,
  request limits, and WebSocket proxy timeouts.
- Port 8001 is loopback-only inside the container and is never published.
- The workstation must not be exposed directly to the public Internet.

## One-step local deployment

Prerequisites are Docker Compose v2, NVIDIA driver 570 or newer, and NVIDIA
Container Toolkit. Images use vLLM's CUDA 12.9 build so the RTX 4090 workstation
does not require a CUDA 13 host driver. From the repository, deploy the
recommended Voxtral release with one command:

```bash
./scripts/deploy-local.sh
```

The script creates `.env` on first use, pulls the image, starts/replaces the
container, waits until the embedded model is ready, and prints its status. Use
`./scripts/deploy-local.sh qwen` to deploy the embedded Qwen comparison variant.
No model download occurs during startup. If deployment fails, inspect startup
with `docker compose logs stt`.

Readiness becomes healthy after vLLM loads and compiles the embedded model:

```bash
curl http://127.0.0.1:8000/healthz
curl http://127.0.0.1:8000/readyz
docker compose exec stt nvidia-smi
```

For manual Compose operation, switch to the embedded Qwen image by changing
`STT_IMAGE` in `.env` to:

```text
ghcr.io/lxk36/xgc2-stt-service:qwen-0.1.0
```

Then run `docker compose pull && docker compose up -d --force-recreate`.

To build a fully self-contained variant locally:

```bash
STT_VARIANT=voxtral docker compose -f docker-compose.yml -f docker-compose.build.yml build
STT_VARIANT=voxtral docker compose -f docker-compose.yml -f docker-compose.build.yml up -d
```

Use `STT_VARIANT=qwen` for the Qwen image. Building a variant downloads its
weights once at image-build time.

## WebUI

Open <http://127.0.0.1:8000> on the workstation. Other LAN devices must use the
HTTPS/WSS origin supplied by the internal reverse proxy because browsers block
microphone capture on ordinary remote HTTP origins.

The WebUI records with browser Web Audio, resamples to mono PCM16LE at 16 kHz,
and streams it over WebSocket. `transcript.partial` replaces the current
uncommitted preview; `transcript.final` is authoritative after the operator
stops recording. The React interface uses the versioned `@xgc2/ui-react`
package so its shell, controls, panels, global scrollbars, compact chrome, and
skin match GCS. The topbar contains only the `XGC2 STT` title and Settings
action; engine state appears beside the capture surface only when it is not
ready and therefore affects the operator's next action.

## API

OpenAI-compatible file transcription:

```bash
curl http://127.0.0.1:8000/v1/audio/transcriptions \
  -H "Authorization: Bearer ${STT_API_KEY}" \
  -F file=@speech.wav \
  -F model=stt-1
```

Supported response formats are `json`, `text`, and `verbose_json`. `stt-1`,
`whisper-1`, the configured model name, and its basename are accepted aliases.
The OpenAI-compatible `language` and `prompt` fields are accepted for client
compatibility; vLLM's current native realtime protocol performs model-level
language detection and does not expose those controls.

Streaming endpoint:

```text
ws://127.0.0.1:8000/v1/audio/transcriptions/stream?sample_rate=16000
```

After `session.started`, send binary mono PCM16LE chunks. The gateway maps
vLLM's native realtime delta stream to replacement-friendly events:

```json
{"type":"transcript.partial","text":"正在识别"}
{"type":"transcript.final","text":"最终结果"}
```

Send `{"type":"commit"}` to stop and finalize. `reset` cancels without
committing. `/v1/stream` is an alias and `/docs` exposes OpenAPI documentation.

## Authentication and proxying

`STT_API_KEY` is optional for workstation-local use. HTTP accepts
`Authorization: Bearer` or `X-API-Key`; browser WebSockets use the
`access_token` query parameter. Before LAN use, configure a long API key and an
HTTPS reverse proxy that preserves WebSocket upgrades and long-lived sessions.

## Development and acceptance

Run source gates:

```bash
scripts/test.sh
```

Build and smoke only the common runtime without starting a GPU engine:

```bash
docker build --target base -t xgc2-stt-service:base .
scripts/smoke-test-image.sh xgc2-stt-service:base
```

After deploying an embedded-weight variant, run real GPU and Chinese audio
acceptance:

```bash
scripts/verify-gpu.sh
```

This requires GPU visibility, a ready model, a non-empty Chinese HTTP result,
and both partial and final native WebSocket transcripts.

## Publication

The release workflow publishes versioned `base`, `qwen`, and `voxtral` tags to
GHCR; `latest` points to Voxtral. If all four `XGC_CN_*` repository secrets are
configured, the same tags are mirrored to the China registry. No workflow
deploys the service to a server.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for model/runtime license
and pinned-source information.
