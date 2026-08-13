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
| `qwen-0.1.0` | Qwen3-ASR-1.7B | revision-capable 1 s chunks | Chinese/dialect comparison |
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
workstation browser ── 127.0.0.1:34896 ── WebUI / local management

GCS / Agent Hub / LAN clients
           |
           | HTTPS + WSS through the internal reverse proxy
           v
RTX 4090 workstation :34897
           |
           v
API-only gateway :8002 ── GPU STT service :8000
                                  |
                                  └─ model runtime 127.0.0.1:8001
```

- The GPU model container runs on this workstation in the current phase.
- Host port 34896 is bound to `127.0.0.1` and serves the local WebUI. It is not
  an API reverse-proxy target and is not reachable from the LAN by default.
- Host port 34897 is the API-only LAN/reverse-proxy target. It never serves the
  WebUI. Compose runs this as a lightweight, non-GPU gateway beside the single
  GPU model process; both services reuse the same image layers.
- The internal server supplies HTTPS, client authentication, access policy,
  request limits, and WebSocket proxy timeouts.
- Port 8001 is loopback-only inside the container and is never published.
- The workstation must not be exposed directly to the public Internet.

## One-step local deployment

Prerequisites are Docker Compose v2, NVIDIA Container Toolkit, and NVIDIA Linux
driver `575.57.08` or newer; the 580 series is recommended and `580.126.09` is
the validated RTX 4090 configuration. Images use vLLM's CUDA 12.9 Update 1 build, so no host
CUDA Toolkit and no CUDA 13 host runtime are required. From the repository,
deploy the recommended Voxtral release with one command:

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
curl http://127.0.0.1:34897/healthz
curl http://127.0.0.1:34897/readyz
docker compose exec stt nvidia-smi
```

Validated cold starts on this RTX 4090 are about 36 seconds for Qwen and 44
seconds for Voxtral. With the current 80% vLLM budget, Qwen's model process
reserves about 18.2 GB and the whole GPU currently reports about 21.4 GB in use;
this is a runtime reservation, not 1.7B model weight size. Voxtral's model
process uses about 17.1 GB. The validated profiles therefore target a 24 GB GPU
and do not support an 8 GB RTX 4060 configuration without a separately
tuned runtime profile. If the driver modules are loaded after an Ubuntu driver upgrade but
`/dev/nvidia0` is missing, run `sudo /sbin/ub-device-create --verbose` once and
verify `nvidia-smi` before starting Compose.

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

Open <http://127.0.0.1:34896> on the workstation. This management UI is
loopback-only. Other devices use their GCS/client through the HTTPS/WSS origin
that proxies the API-only port 34897.

The management UI includes cached NVML GPU utilization, VRAM, temperature, and
power history. NVML is sampled inside the server every two seconds; the browser
only reads cached `/api/status` data every five seconds and never starts or
polls `nvidia-smi`. It also creates, rotates, and revokes API keys and shows
request count, stream count, active streams, and audio duration for each key.
Generated secrets are returned once; only SHA-256 digests are persisted in the
`xgc2-stt-cache` volume.
Because the management origin is loopback-only, a browser with no saved Key
automatically provisions its own `webui-local` Key after managed authentication
has been enabled. Other API clients never receive this bootstrap behavior and
must present an explicitly issued Key.

The WebUI records with browser Web Audio, resamples to mono PCM16LE at 16 kHz,
and streams it over WebSocket. `transcript.partial` replaces the current
uncommitted preview; with Qwen, the stable prefix is normal text and its
revisable suffix is rendered as gray text on a light background.
`transcript.final` is authoritative after a silence boundary or after the operator
stops recording. By default the gateway drops initial silence before the first
speech frame and normalizes Chinese output to Simplified Chinese; both are
operator settings in the WebUI. English and other Latin text is not translated.
Three seconds of silence finalizes the current segment and removes its gray
revisable tail without stopping the microphone or WebSocket. Speech after a
long pause starts the next segment and appends to the existing transcript.
Clearing while capture is active replaces only the model session: the current
text is removed while the microphone keeps recording into the fresh session.
The React interface uses the versioned `@xgc2/ui-react`
package so its shell, controls, panels, global scrollbars, compact chrome, and
skin match GCS. The topbar contains only the `XGC2 STT` title and Settings
action; engine state appears beside the capture surface only when it is not
ready and therefore affects the operator's next action.

Service parameters are editable in Settings and persisted in the data volume.
They have explicit application boundaries:

| Setting | Apply action |
| --- | --- |
| Active-stream admission limit | Immediate |
| Silence finalization delay | New WebSocket session; no process restart |
| GPU memory ratio, model limits, eager mode, Qwen chunk/revision window | **Restart model** in WebUI; container stays up |
| Host binds/ports, image/model variant, volume and GPU device policy | `docker compose up -d --force-recreate` |

No supported setting requires entering the container or rebuilding the image.

## Desktop client

The repository includes a native PySide6 client for Linux/X11. It is one local
process, not another browser frontend/backend. A small always-on-top status
window and tray icon expose settings; the default global shortcut is
`F8`. It deliberately avoids `Ctrl+Shift` because common Linux Chinese input
methods use that modifier family for input-method and simplified/traditional
switching. The first press connects and starts microphone capture, and
the next press commits and stops. The default API URL is
`http://127.0.0.1:34897`; users enter their own IP/port or HTTPS URL and API key.

Install it for the current user without `sudo`. The X11 host needs `xdotool`
and `xclip` (`sudo apt install xdotool xclip` on Ubuntu). `xclip` serves the
clipboard from an independent process while the client emits a paste chord, so
the Qt event loop cannot leak a literal `V` into the focused input:

```bash
./scripts/install-client.sh
xgc2-stt-client
```

The client streams PCM to the service and shows each replacement hypothesis in
a non-activating overlay near the focused window. Stable text is bright and the
still-revisable tail is highlighted. Only a server-finalized segment is pasted
once into the terminal/input field that held focus when recording began, so
model revisions never churn the target and Chinese input methods cannot turn a
paste chord into transcript text. Terminal `Ctrl+Shift+V` and desktop `Ctrl+V`
are selectable. The client never presses Enter by default.
When **Auto Enter** is enabled, each non-empty three-second silence-finalized
segment is submitted with Enter, while the same microphone/WebSocket continues
for the next spoken request. Focus changes suppress both injection and Enter.
Autostart is optional and creates a per-user desktop entry; client settings and
the API key are stored in a mode-`0600` user configuration file.

The current global-hotkey/focus injection implementation targets X11, which is
the validated Ubuntu session. Wayland intentionally needs a future desktop
portal implementation rather than bypassing compositor security.

## API

OpenAI-compatible file transcription:

```bash
curl http://127.0.0.1:34897/v1/audio/transcriptions \
  -H "Authorization: Bearer ${XGC2_STT_API_KEY}" \
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
ws://127.0.0.1:34897/v1/audio/transcriptions/stream?sample_rate=16000&output_script=simplified&trim_leading_silence=1
```

After `session.started`, send binary mono PCM16LE chunks. The gateway maps
vLLM's native realtime delta stream to replacement-friendly events:

```json
{"type":"transcript.partial","text":"正在识别","stable_text":"正在","unstable_text":"识别"}
{"type":"transcript.final","text":"最终结果","reason":"silence","session_complete":false}
```

After a silence final, the connection remains open and later speech starts the
next segment. Send `{"type":"commit"}` to finalize the current segment and end
the client session; its final event has `session_complete:true`. `reset` cancels
without committing. `output_script` accepts `simplified` or `original`, while
`trim_leading_silence` accepts a boolean. These are gateway controls; Voxtral
Realtime still performs automatic language detection and does not accept a
request-level language hint. The API-only port does not expose the WebUI or
documentation; local management documentation remains available on port 34896.

## Capacity and bandwidth

The current Qwen SDK streaming path explicitly supports a single stream and no
batching, so the default `STT_MAX_ACTIVE_STREAMS=1` is deliberate. A second
simultaneous stream receives `server_busy` instead of silently competing for a
non-concurrent model path. Many idle clients may remain connected elsewhere in
the system, but this release guarantees one active speaker per GPU service.

Streaming input is mono 16 kHz PCM16: exactly 32,000 bytes/s (256 kbit/s), or
about 1.92 MB/minute per speaking client, plus small WebSocket/TLS overhead.
Text output is negligible by comparison. Ten active streams would be about
2.56 Mbit/s of audio uplink, but the current inference path reaches its
single-stream limit long before network bandwidth becomes relevant. Qwen also
reprocesses accumulated utterance audio to revise recent text, so longer
continuous utterances cost progressively more GPU time. Higher concurrency
requires a separately measured scheduler/batching implementation rather than
only increasing the configuration value.

## Authentication and proxying

Managed keys are generated from the loopback-only WebUI and enabling managed keys keeps
authentication required even if every key is later revoked. HTTP accepts
`Authorization: Bearer` or `X-API-Key`; browser WebSockets use the
`access_token` query parameter. CLI verification reads a managed key from
`XGC2_STT_API_KEY`. Before LAN use, configure a managed key and an HTTPS reverse
proxy that preserves WebSocket upgrades and long-lived sessions.

No private registry, LAN address, operator username, home path, organization
domain, or credential is compiled into the client, server, or public images.
Loopback and documentation-only example hosts are the only defaults. Private
deployment values belong in the Git-ignored local `.env` and client config.

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
and both partial and final native WebSocket transcripts. The verifier sends one
copy of the sample at real-time speed while receiving events concurrently. It
reports first-partial latency, updates received before commit, and finalization
latency; the model's configured 480 ms delay is not presented as end-to-end
first-text latency.

## Publication

The release workflow publishes versioned `base`, `qwen`, and `voxtral` tags to
GHCR; `latest` points to Voxtral. If all four `XGC_CN_*` repository secrets are
configured, the same tags are mirrored to the China registry. No workflow
deploys the service to a server.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for model/runtime license
and pinned-source information.
