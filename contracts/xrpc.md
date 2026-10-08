# STT XRPC consumption and storage declaration

Isolation baseline: `30e43ad5db1812e4b54a827ce719d0ecf4c153e7`. This change is source-only. No service, GPU container, desktop session, APT repository, or user data was restarted or migrated.

| Boundary | Owner and process | Current source | Removed entry |
| --- | --- | --- | --- |
| Model/WebUI/API | `xgc2_stt.main`, Python 3.12; owns aiohttp Application through `Host.from_app` | HTTP/WS external edge, explicit domain auth, bounded native multipart, one transcription gate; `Runtime.from_environment`; policy visible in `/api/status` | FastAPI/uvicorn main listener and ASGI lifespan |
| Qwen inference | `xgc2_stt.model_worker`, child process group owned by `VllmEngine` | Private HTTP.v1 UDS, fixed one inference worker, four admitted calls, instance binding, one generation-scoped model session; base64 PCM16 chunks <= 160,000 decoded bytes and bounded JSON body | `qwen-asr-demo-streaming` Flask listener, `STT_INTERNAL_HOST/PORT`, `/api/start/chunk/finish`, per-session private HTTPX client |
| Desktop control | Tray process, SDK Runtime/Host; CLI SDK Client | Private UID-authorized HTTP.v1 UDS under `$XDG_RUNTIME_DIR/xgc2-stt/desktop.sock`; command acknowledgement means GUI dispatch accepted | Newline command/`ok`, manual bind/accept/reclaim/unlink, `/tmp` fallback endpoint |
| API gateway | Second SDK Host in `xgc2_stt.main`, same process and Runtime as management | API-only aiohttp router on 8002; exposes health/readiness/models/upload/WS; shares actual model, key store and transcription gate; no management/static routes | `xgc2_stt.api_proxy`, private HTTPX/WebSocket relay and separate `api` container entry |

Model execution directly uses the official `qwen-asr==0.0.6` `Qwen3ASRModel.LLM`, `init_streaming_state`, `streaming_transcribe`, and `finish_streaming_transcribe` APIs. The reference is the [official streaming API](https://github.com/QwenLM/Qwen3-ASR/blob/main/qwen_asr/inference/qwen3_asr.py); the pinned wheel was inspected. Tests inject a native model fixture and do not load weights or claim GPU inference acceptance. Model options (`dtype`, memory utilization, sequence/model length, eager execution) are passed to the actual model factory.

All production composition roots snapshot `XGC2_XRPC_*` once through `Runtime.from_environment`. Product resource ceilings reject larger operator settings instead of silently ignoring them. The main HTTP handler has a 30-minute default finite lifetime because streaming/cold inference is a domain operation. Model sessions expire after 30 idle seconds, default utterances are limited to 300 seconds, revision events coalesce into a two-item queue, and audio rollover rejects overload instead of silently dropping samples. Client deadlines do not promise inference rollback; executor slots and endpoint ownership survive cancellation until actual work ends.

## Persistent and temporary writes

| Writer / trigger | Location and content | Enforced bound / recovery owner |
| --- | --- | --- |
| `ApiKeyStore`, create/rotate/revoke and five-second usage flush | `STT_API_KEY_STORE_PATH`, default `/var/lib/xgc2-stt/api-keys.json`; key digests and usage, no raw secret | 128 records, 256 KiB input/output; 0600 atomic replace plus file and parent fsync; corrupt/oversized input fails auth closed; startup clears stale active counts |
| `RuntimeSettingsStore`, explicit operator save | `STT_RUNTIME_SETTINGS_PATH`, default `/var/lib/xgc2-stt/runtime-settings.json`; schema 2, revision and tuning | 16 KiB; atomic durable replace, revision CAS serialized over the full commit; old or corrupt schemas reject at startup without writes. An absent owner-selected path remains unpersisted at revision 0 until an explicit save initializes schema 2 at revision 1; there is no old-schema converter or fallback |
| Desktop settings, explicit Settings save | `$XDG_CONFIG_HOME/xgc2-stt/client.json` (default user XDG config allocation); schema 1, endpoint/key/UI preferences | 16 KiB input/output, typed and bounded fields, 0600 atomic replacement plus file/parent fsync; failed writes remove temporary files and leave pre-replacement state intact. Managed-root allocator convergence for this pre-existing external configuration allocation remains a deployment review item |
| Desktop autostart, explicit Settings opt-in | `$XDG_CONFIG_HOME/autostart/xgc2-stt-client.desktop` | One fixed desktop entry; desktop removes only its own entry when opted out; no automatic opt-in |
| Upload request, multipart decode | `tempfile` host/container `/tmp`, one temporary audio file | <= 50 MiB file, one transcription gate; success/error/cancellation `finally` removes file. Container `/tmp` is 4 GiB tmpfs; process-crash leftovers disappear on tmpfs recreation. Host crash retention/allocator policy remains a deployment review item |
| SDK Unix lease and lock | `/run/xgc2-stt/model.sock{,.xrpc.lock}` and desktop private runtime directory | 0700 owner directory, 0600 endpoint/lock, SDK inode-aware lease; runtime only, no business state |
| HF/vLLM/Triton/Qwen dependency caches | `HF_HOME`, `XDG_CACHE_HOME`, `VLLM_CACHE_ROOT`, `TRITON_CACHE_DIR` under the existing `/var/lib/xgc2-stt` volume | Reconstructible model/runtime cache. Actual volume quota, retention and managed-root allocation are not established by these unit tests and remain deployment gates |
| Product and worker logs | stdout/stderr, Docker logging owner or desktop session log owner | No product log file authority; installer/host rotation and retention must be declared at deployment |

The container volume is the existing state allocation; this source change does not relocate it or claim the common managed-directory allocator has been deployed. No SQLite authority exists in this product. Config responses separate `desired`, `applied` (model fields are null until the engine is ready), `persisted`, and `revision`; saves require `{expected_revision, values}` and stale saves return 409. SDK policy is transport configuration and is separate from model tuning.

## Deployment inputs and remaining gates

- Runtime image must provide `xgc2-xrpc==0.1.0`, `aiohttp==3.14.4`, its HTTPX/httpcore dependencies, and the existing `qwen-asr[vllm]==0.0.6` runtime. Docker build imports and checks the SDK instead of relying on `pip --no-deps` to supply it.
- The model worker requires an owner-2000 0700 `/run/xgc2-stt` tmpfs. The main image command is `python3 -m xgc2_stt.main`. No old model TCP port remains.
- Desktop Debian metadata requires Python >= 3.10 and `python3-xgc2-xrpc (>= 0.1.0)`. Focal/Jammy/Noble matrices are preserved. Focal's current system Python 3.8 is an explicit unresolved release/runtime gate; the build rejects it. Root/SDK owner must supply an approved runtime/package plan, without a local PPA or an EOL aiohttp workaround.
- Management/API Hosts each have 32-connection and 16-request ceilings; the shared SDK Runtime also bounds their combined active connections/requests to 32/16. Actual dual-Host tests prove shared request admission, route separation, auth, upload, WebSocket and key usage. Both external ports 34896/34897 map to 8000/8002 in the single `stt` container. Startup/termination now couples both API surfaces to the model process. The retired API container must be removed during the Root deployment window; no running deployment is altered here.
- SDK wheel/deb publication and a refreshed dependency lock, image ABI, real GPU inference/load/unload, two-end restart, managed volume/cache allocation, desktop UI smoke and actual APT deployment are not established here. All deployments remain paused.

## Reproduction

Isolated dependencies: Python 3.12.3, aiohttp 3.14.4, HTTPX 0.28.1, httpcore 1.0.9; source SDK at `devops/products/common/xrpc/python`. Run `PYTHONPATH=src:<isolated-product-test-deps>:<SDK-native-deps>:<xrpc-python> python3 -m pytest`. `ruff check src tests` is the source gate. Real TCP/UDS and native WebSocket test fixtures are used for transport consumption; inference is a controlled native-model fixture.

The cancellation/close tests first reproduced an SDK `concurrent.futures.Future` / `asyncio.wait` type mismatch in Host shutdown, then passed after the sole SDK owner repaired it. Product negative controls cover stale instances/sessions, collisions preserving another owner and regular files, active-work lease retention, overlap rejection, malformed/oversized PCM and state, concurrent CAS, failed disk replacement, old/corrupt schema rejection without writes, and explicit new schema-2 initialization.
