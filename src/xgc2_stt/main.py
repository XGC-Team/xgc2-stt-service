from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic import ValidationError
from xgc2_xrpc import HOST_KEY, AppRouter, Host, Limits, Runtime

from . import __version__
from .api_keys import ApiKeyStore
from .auth import http_api_token
from .config import Settings, get_settings
from .engine import EngineNotReady, SpeechEngine, VllmEngine
from .gpu_metrics import GpuMonitor
from .runtime_settings import ENGINE_RESTART_FIELDS, HOT_FIELDS, RuntimeSettingsStore, RuntimeTuning, settings_metadata
from .streaming import NativeStreamSocket, handle_stream

API_APP_KEY = web.AppKey("stt.api", web.Application)


class ActiveRequestGate:
    def __init__(self, capacity: int):
        self.capacity, self.active = capacity, 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self.active >= self.capacity:
                return False
            self.active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self.active -= 1

    async def set_capacity(self, capacity: int) -> None:
        async with self._lock:
            self.capacity = capacity


class DomainFailure(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status


def fail(status: int, detail: str) -> None:
    raise DomainFailure(status, detail)


@web.middleware
async def errors(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except DomainFailure as error:
        response = web.json_response({"detail": str(error)}, status=error.status)
        response.force_close()
        return response
    except web.HTTPException as error:
        response = web.json_response({"detail": error.reason}, status=error.status)
        response.force_close()
        return response
    except (ValidationError, json.JSONDecodeError, UnicodeError, ValueError) as error:
        return web.json_response({"detail": str(error)}, status=400)


async def payload(request: web.Request) -> Any:
    if request.content_type != "application/json":
        fail(400, "application/json required")
    if request.content_length is not None and request.content_length > 16_384:
        fail(413, "configuration body exceeds limit")
    raw = bytearray()
    async for chunk in request.content.iter_chunked(4096):
        if len(raw) + len(chunk) > 16_384:
            fail(413, "configuration body exceeds limit")
        raw.extend(chunk)
    return json.loads(raw)


async def blocking(request: web.Request, function: Any, *args: Any) -> Any:
    host = request[HOST_KEY]
    return await host.runtime.blocking(host, function, *args)


def create_app(
    settings: Settings | None = None,
    engine: SpeechEngine | None = None,
    api_keys: ApiKeyStore | None = None,
    gpu_monitor: GpuMonitor | None = None,
) -> web.Application:
    base_config = settings or get_settings()
    runtime_store = RuntimeSettingsStore(base_config.runtime_settings_path)
    config = base_config.model_copy(update=runtime_store.load())
    speech_engine = engine or VllmEngine(config)
    key_store = api_keys or ApiKeyStore(config.api_key_store_path)
    gpu = gpu_monitor or GpuMonitor(
        enabled=config.gpu_metrics_enabled, interval_seconds=config.gpu_metrics_interval_seconds
    )
    gate = ActiveRequestGate(config.max_active_streams)
    started = time.time()

    @web.middleware
    async def cors(request: web.Request, handler: Any) -> web.StreamResponse:
        response = web.Response(status=204) if request.method == "OPTIONS" else await handler(request)
        origin = request.headers.get("Origin")
        if origin and (config.cors_origin_list == ["*"] or origin in config.cors_origin_list):
            response.headers["Access-Control-Allow-Origin"] = "*" if config.cors_origin_list == ["*"] else origin
            response.headers["Access-Control-Allow-Headers"] = "Authorization,Content-Type,X-API-Key"
            response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
            if config.cors_origin_list != ["*"]:
                response.headers["Access-Control-Allow-Credentials"] = "true"
        return response

    router = AppRouter(client_max_size=config.max_upload_bytes + 32_768, middlewares=(cors, errors))
    app = router.app

    async def lifecycle(_app: web.Application):
        await key_store.start()
        await gpu.start()
        try:
            await speech_engine.start()
            try:
                yield
            finally:
                await speech_engine.close()
        finally:
            await gpu.close()
            await key_store.close()

    app.cleanup_ctx.append(lifecycle)

    def authorize(request: web.Request) -> str:
        key_id = key_store.authenticate(http_api_token(request))
        if key_id is None:
            fail(401, "invalid API key")
        key_store.record_request(key_id)
        return key_id

    async def healthz(_request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def readyz(_request: web.Request) -> web.Response:
        state = speech_engine.status()
        ready = state.get("state") == "ready"
        return web.json_response(
            {"status": "ready" if ready else "not_ready", "engine": state}, status=200 if ready else 503
        )

    async def service_status(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "service": "xgc2-stt-service",
                "version": __version__,
                "uptime_seconds": round(time.time() - started, 3),
                "authentication": "api-key" if key_store.requires_authentication else "trusted-network",
                "stream": {
                    "protocol": "openai-realtime",
                    "format": "pcm_s16le",
                    "sample_rate": 16000,
                    "channels": 1,
                    "transcription_delay_ms": config.transcription_delay_ms,
                    "finalization": "silence-or-manual",
                    "silence_commit_ms": config.silence_commit_ms,
                    "active": gate.active,
                    "capacity": gate.capacity,
                },
                "engine": speech_engine.status(),
                "gpu": gpu.snapshot(),
                "xrpc": _request[HOST_KEY].runtime.effective_policy(),
            }
        )

    async def keys_list(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "authentication": "api-key" if key_store.requires_authentication else "trusted-network",
                "keys": key_store.list_public(),
            }
        )

    def tuning_response() -> dict[str, Any]:
        values = {key: getattr(config, key) for key in RuntimeTuning.model_fields}
        engine_ready = speech_engine.status().get("state") == "ready"
        applied = {key: value if key in HOT_FIELDS or engine_ready else None for key, value in values.items()}
        pending = {
            key: value
            for key, value in runtime_store.values.items()
            if key in ENGINE_RESTART_FIELDS and value != values[key]
        }
        return {
            "values": values,
            "desired": {**values, **runtime_store.values},
            "applied": applied,
            "persisted": dict(runtime_store.values),
            "revision": runtime_store.revision,
            "pending": pending,
            "restart_required": bool(pending),
            "fields": settings_metadata(),
        }

    async def tuning_get(_request: web.Request) -> web.Response:
        return web.json_response(tuning_response())

    async def tuning_update(request: web.Request) -> web.Response:
        body = await payload(request)
        if not isinstance(body, dict) or set(body) != {"expected_revision", "values"}:
            fail(400, "expected_revision and values required")
        tuning = RuntimeTuning.model_validate(body["values"])
        updates = tuning.model_dump(exclude_none=True)
        if updates.get("max_active_streams", 1) != 1:
            fail(400, "the current Qwen streaming backend supports one active stream")
        try:
            await blocking(request, runtime_store.update, tuning, body["expected_revision"])
        except RuntimeError:
            fail(409, "runtime settings revision conflict")
        for key, value in updates.items():
            if key in HOT_FIELDS:
                setattr(config, key, value)
        if "max_active_streams" in updates:
            await gate.set_capacity(int(updates["max_active_streams"]))
        return web.json_response(tuning_response())

    async def restart(_request: web.Request) -> web.Response:
        if gate.active:
            fail(409, "cannot restart the model while transcription is active")
        reconfigure = getattr(speech_engine, "reconfigure", None)
        if reconfigure is None:
            fail(501, "the configured engine cannot restart in process")
        next_config = config.model_copy(update=runtime_store.values)
        await reconfigure(next_config)
        for key in RuntimeTuning.model_fields:
            setattr(config, key, getattr(next_config, key))
        await gate.set_capacity(config.max_active_streams)
        return web.json_response({"engine": speech_engine.status(), **tuning_response()}, status=202)

    async def keys_create(request: web.Request) -> web.Response:
        body = await payload(request)
        if not isinstance(body, dict) or set(body) != {"name"} or not isinstance(body["name"], str):
            fail(400, "API key name required")
        record, secret = await blocking(request, key_store.create, body["name"])
        return web.json_response({"key": record, "secret": secret}, status=201)

    async def keys_rotate(request: web.Request) -> web.Response:
        try:
            record, secret = await blocking(request, key_store.rotate, request.match_info["key_id"])
            return web.json_response({"key": record, "secret": secret})
        except KeyError:
            fail(404, "API key not found")

    async def keys_revoke(request: web.Request) -> web.Response:
        try:
            return web.json_response({"key": await blocking(request, key_store.revoke, request.match_info["key_id"])})
        except KeyError:
            fail(404, "API key not found")

    async def models(request: web.Request) -> web.Response:
        authorize(request)
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {"id": config.model_name, "object": "model", "owned_by": "xgc2", "aliases": ["whisper-1", "stt-1"]}
                ],
            }
        )

    async def transcriptions(request: web.Request) -> web.Response:
        key_id = authorize(request)
        if not await gate.try_acquire():
            return web.json_response(
                {"detail": "all transcription slots are busy"}, status=429, headers={"Retry-After": "2"}
            )
        path: str | None = None
        fd: int | None = None
        try:
            reader = await request.multipart()
            fields: dict[str, str] = {}
            total, count = 0, 0
            while part := await reader.next():
                count += 1
                if count > 6 or part.name in fields or part.headers.get("Content-Encoding"):
                    fail(400, "invalid transcription multipart fields")
                if part.name == "file":
                    if path is not None:
                        fail(400, "one audio file required")
                    fd, path = tempfile.mkstemp(
                        prefix="xgc2-stt-upload-", suffix=Path(part.filename or "audio.bin").suffix[:16]
                    )
                    with os.fdopen(fd, "wb") as output:
                        fd = None
                        while chunk := await part.read_chunk(65_536):
                            total += len(chunk)
                            if total > config.max_upload_bytes:
                                fail(413, "audio upload exceeds configured limit")
                            await blocking(request, output.write, chunk)
                elif part.name in {"model", "language", "prompt", "response_format"}:
                    value = bytearray()
                    while chunk := await part.read_chunk(4096):
                        if len(value) + len(chunk) > 4096:
                            fail(413, "transcription field exceeds limit")
                        value.extend(chunk)
                    fields[part.name] = value.decode("utf-8")
                else:
                    fail(400, "unknown transcription field")
            if path is None or not total:
                fail(400, "audio file is empty")
            model = fields.get("model", "whisper-1")
            if model not in {"whisper-1", "stt-1", config.model_name, config.model_name.rsplit("/", 1)[-1]}:
                fail(400, "unknown transcription model")
            response_format = fields.get("response_format", "json")
            if response_format not in {"json", "text", "verbose_json"}:
                fail(400, "response_format must be json, text, or verbose_json")
            language = fields.get("language")
            try:
                result = await speech_engine.transcribe_file(
                    path, language=None if language in {None, "", "auto"} else language, prompt=fields.get("prompt")
                )
            except EngineNotReady as error:
                fail(503, str(error))
            key_store.record_audio(key_id, total, seconds=result.duration or 0)
            if response_format == "text":
                return web.Response(text=result.text)
            return web.json_response(
                result.verbose_dict() if response_format == "verbose_json" else {"text": result.text}
            )
        finally:
            if fd is not None:
                os.close(fd)
            if path is not None:
                Path(path).unlink(missing_ok=True)
            await gate.release()

    async def stream(request: web.Request) -> web.StreamResponse:
        socket = NativeStreamSocket(request)
        key_id = key_store.authenticate(http_api_token(request) or request.query.get("access_token"))
        if key_id is None:
            await socket.accept()
            await socket.close(code=4401, reason="invalid API key")
            return socket.native
        if not await gate.try_acquire():
            await socket.accept()
            await socket.send_json(
                {
                    "type": "error",
                    "code": "server_busy",
                    "message": "all transcription slots are busy",
                    "active": gate.active,
                    "capacity": gate.capacity,
                }
            )
            await socket.close(code=1013, reason="server busy")
            return socket.native
        key_store.record_request(key_id, stream=True)
        try:
            await handle_stream(
                socket,
                speech_engine,
                silence_commit_ms=config.silence_commit_ms,
                on_audio_bytes=lambda count: key_store.record_audio(key_id, count),
            )
            return socket.native
        finally:
            key_store.stream_closed(key_id)
            await gate.release()

    web_dist = Path(config.web_dist)

    async def worklet(_request: web.Request) -> web.StreamResponse:
        path = web_dist / "pcm-worklet.js"
        if not path.is_file():
            fail(404, "not found")
        return web.FileResponse(path, headers={"Content-Type": "text/javascript", "Cache-Control": "no-cache"})

    async def index(_request: web.Request) -> web.StreamResponse:
        path = web_dist / "index.html"
        return (
            web.FileResponse(path)
            if path.is_file()
            else web.Response(text="XGC2 STT service", content_type="text/plain")
        )

    api_routes = [
        web.get("/healthz", healthz),
        web.get("/readyz", readyz),
        web.get("/v1/models", models),
        web.post("/v1/audio/transcriptions", transcriptions),
        web.get("/v1/audio/transcriptions/stream", stream),
    ]
    api_router = AppRouter(client_max_size=config.max_upload_bytes + 32_768, middlewares=(cors, errors))
    api_router.add_routes(api_routes)
    app[API_APP_KEY] = api_router.app
    router.add_routes(
        [
            web.get("/healthz", healthz),
            web.get("/readyz", readyz),
            web.get("/api/status", service_status),
            web.get("/api/keys", keys_list),
            web.post("/api/keys", keys_create),
            web.post("/api/keys/{key_id}/rotate", keys_rotate),
            web.delete("/api/keys/{key_id}", keys_revoke),
            web.get("/api/settings", tuning_get),
            web.put("/api/settings", tuning_update),
            web.post("/api/engine/restart", restart),
            web.get("/v1/models", models),
            web.post("/v1/audio/transcriptions", transcriptions),
            web.get("/v1/audio/transcriptions/stream", stream),
            web.get("/pcm-worklet.js", worklet),
            web.get("/", index),
        ]
    )
    if (web_dist / "assets").is_dir():
        app.router.add_static("/assets", web_dist / "assets", follow_symlinks=False)
    return app


def main() -> int:
    settings = get_settings()
    runtime = Runtime.from_environment(
        blocking_workers=2,
        defaults={
            "HOST_MAX_IN_FLIGHT": 16,
            "HOST_MAX_CONNECTIONS": 32,
            "CLIENT_MAX_REFERENCES": 2,
            "MAX_REQUEST_BYTES": 52461568,
            "MAX_RESPONSE_BYTES": 1048576,
            "CALL_TIMEOUT_MS": 1800000,
            "SHUTDOWN_TIMEOUT_MS": 30000,
        },
        ceilings={
            "HOST_MAX_IN_FLIGHT": 16,
            "HOST_MAX_CONNECTIONS": 32,
            "CLIENT_MAX_REFERENCES": 2,
            "MAX_REQUEST_BYTES": 52461568,
            "MAX_RESPONSE_BYTES": 1048576,
        },
    )
    domain_app = create_app(settings)
    host = Host.from_app(
        domain_app,
        address=(settings.host, settings.port),
        runtime=runtime,
        limits=Limits(
            connections=32,
            in_flight=16,
            body_bytes=settings.max_upload_bytes + 32_768,
            response_bytes=1_048_576,
            call_timeout=1800,
            shutdown_timeout=30,
        ),
    )
    api_host = Host.from_app(
        domain_app[API_APP_KEY],
        address=(settings.api_host, settings.api_port),
        runtime=runtime,
        limits=Limits(
            connections=32,
            in_flight=16,
            body_bytes=settings.max_upload_bytes + 32_768,
            response_bytes=1_048_576,
            call_timeout=1800,
            shutdown_timeout=30,
        ),
    )
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _sig, _frame: runtime.loop.call_soon_threadsafe(stop.set))

    async def serve() -> None:
        try:
            await host.start_async()
            await api_host.start_async()
            await stop.wait()
        finally:
            await api_host.close_async()
            await host.close_async()

    try:
        runtime.run(serve())
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
