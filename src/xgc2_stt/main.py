from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .api_keys import ApiKeyStore
from .auth import http_api_token, websocket_api_token
from .config import Settings, get_settings
from .engine import EngineNotReady, SpeechEngine, VllmEngine
from .gpu_metrics import GpuMonitor
from .runtime_settings import (
    ENGINE_RESTART_FIELDS,
    HOT_FIELDS,
    RuntimeSettingsStore,
    RuntimeTuning,
    settings_metadata,
)
from .streaming import handle_stream


class ActiveRequestGate:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self.active = 0
        self._lock = asyncio.Lock()

    async def try_acquire(self) -> bool:
        async with self._lock:
            if self.active >= self.capacity:
                return False
            self.active += 1
            return True

    async def release(self) -> None:
        async with self._lock:
            self.active = max(0, self.active - 1)

    async def set_capacity(self, capacity: int) -> None:
        async with self._lock:
            self.capacity = capacity


class ApiKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)


def create_app(
    settings: Settings | None = None,
    engine: SpeechEngine | None = None,
    api_keys: ApiKeyStore | None = None,
    gpu_monitor: GpuMonitor | None = None,
) -> FastAPI:
    base_config = settings or get_settings()
    runtime_store = RuntimeSettingsStore(base_config.runtime_settings_path)
    config = base_config.model_copy(update=runtime_store.load())
    speech_engine = engine or VllmEngine(config)
    key_store = api_keys or ApiKeyStore(config.api_key_store_path)
    gpu = gpu_monitor or GpuMonitor(
        enabled=config.gpu_metrics_enabled,
        interval_seconds=config.gpu_metrics_interval_seconds,
    )
    started_at = time.time()
    request_gate = ActiveRequestGate(config.max_active_streams)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
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

    app = FastAPI(
        title="XGC2 STT Service",
        version=__version__,
        description="GPU speech-to-text service for the trusted XGC2 network.",
        lifespan=lifespan,
    )
    app.state.settings = config
    app.state.engine = speech_engine
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origin_list,
        allow_credentials=config.cors_origin_list != ["*"],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-API-Key"],
    )

    def authorized(request: Request) -> str:
        key_id = key_store.authenticate(http_api_token(request))
        if key_id is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        key_store.record_request(key_id)
        request.state.api_key_id = key_id
        return key_id

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        status = speech_engine.status()
        ready = status.get("state") == "ready"
        code = 200 if ready else 503
        return JSONResponse({"status": "ready" if ready else "not_ready", "engine": status}, status_code=code)

    @app.get("/api/status")
    async def service_status() -> dict[str, Any]:
        return {
            "service": "xgc2-stt-service",
            "version": __version__,
            "uptime_seconds": round(time.time() - started_at, 3),
            "authentication": "api-key" if key_store.requires_authentication else "trusted-network",
            "stream": {
                "protocol": "openai-realtime",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "transcription_delay_ms": config.transcription_delay_ms,
                "finalization": "silence-or-manual",
                "silence_commit_ms": config.silence_commit_ms,
                "active": request_gate.active,
                "capacity": request_gate.capacity,
            },
            "engine": speech_engine.status(),
            "gpu": gpu.snapshot(),
        }

    @app.get("/api/keys")
    async def api_keys_list() -> dict[str, Any]:
        return {
            "authentication": "api-key" if key_store.requires_authentication else "trusted-network",
            "keys": key_store.list_public(),
        }

    def runtime_settings_response() -> dict[str, Any]:
        values = {key: getattr(config, key) for key in RuntimeTuning.model_fields}
        pending = {
            key: value
            for key, value in runtime_store.values.items()
            if key in ENGINE_RESTART_FIELDS and value != values[key]
        }
        return {
            "values": values,
            "pending": pending,
            "restart_required": bool(pending),
            "fields": settings_metadata(),
        }

    @app.get("/api/settings")
    async def runtime_settings_get() -> dict[str, Any]:
        return runtime_settings_response()

    @app.put("/api/settings")
    async def runtime_settings_update(payload: RuntimeTuning) -> dict[str, Any]:
        updates = payload.model_dump(exclude_none=True)
        if config.engine_variant == "qwen" and updates.get("max_active_streams", 1) != 1:
            raise HTTPException(status_code=400, detail="the current Qwen streaming backend supports one active stream")
        runtime_store.update(payload)
        for key, value in updates.items():
            if key in HOT_FIELDS:
                setattr(config, key, value)
        if "max_active_streams" in updates:
            await request_gate.set_capacity(int(updates["max_active_streams"]))
        return runtime_settings_response()

    @app.post("/api/engine/restart", status_code=202)
    async def runtime_engine_restart() -> dict[str, Any]:
        if request_gate.active:
            raise HTTPException(status_code=409, detail="cannot restart the model while transcription is active")
        reconfigure = getattr(speech_engine, "reconfigure", None)
        if reconfigure is None:
            raise HTTPException(status_code=501, detail="the configured engine cannot restart in process")
        next_config = config.model_copy(update=runtime_store.values)
        await reconfigure(next_config)
        for key in RuntimeTuning.model_fields:
            setattr(config, key, getattr(next_config, key))
        await request_gate.set_capacity(config.max_active_streams)
        return {"engine": speech_engine.status(), **runtime_settings_response()}

    @app.post("/api/keys", status_code=201)
    async def api_keys_create(payload: ApiKeyCreate) -> dict[str, Any]:
        try:
            record, secret = key_store.create(payload.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"key": record, "secret": secret}

    @app.post("/api/keys/{key_id}/rotate")
    async def api_keys_rotate(key_id: str) -> dict[str, Any]:
        try:
            record, secret = key_store.rotate(key_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="API key not found") from exc
        return {"key": record, "secret": secret}

    @app.delete("/api/keys/{key_id}")
    async def api_keys_revoke(key_id: str) -> dict[str, Any]:
        try:
            return {"key": key_store.revoke(key_id)}
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="API key not found") from exc

    @app.get("/v1/models", dependencies=[Depends(authorized)])
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": config.model_name,
                    "object": "model",
                    "owned_by": "xgc2",
                    "aliases": ["whisper-1", "stt-1"],
                }
            ],
        }

    @app.post(
        "/v1/audio/transcriptions",
        dependencies=[Depends(authorized)],
        response_model=None,
    )
    async def transcriptions(
        request: Request,
        file: Annotated[UploadFile, File(...)],
        model: Annotated[str, Form()] = "whisper-1",
        language: Annotated[str | None, Form()] = None,
        prompt: Annotated[str | None, Form()] = None,
        response_format: Annotated[str, Form()] = "json",
    ) -> JSONResponse | PlainTextResponse:
        allowed_models = {"whisper-1", "stt-1", config.model_name, config.model_name.rsplit("/", 1)[-1]}
        if model not in allowed_models:
            raise HTTPException(status_code=400, detail=f"model must be one of: {', '.join(sorted(allowed_models))}")
        if response_format not in {"json", "text", "verbose_json"}:
            raise HTTPException(status_code=400, detail="response_format must be json, text, or verbose_json")
        if not await request_gate.try_acquire():
            raise HTTPException(
                status_code=429,
                detail="all transcription slots are busy",
                headers={"Retry-After": "2"},
            )
        fd: int | None = None
        path: str | None = None
        try:
            normalized_language = None if language in {None, "", "auto"} else language
            suffix = Path(file.filename or "audio.bin").suffix[:16]
            fd, path = tempfile.mkstemp(prefix="xgc2-stt-upload-", suffix=suffix)
            total = 0
            with os.fdopen(fd, "wb") as output:
                fd = None
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > config.max_upload_bytes:
                        raise HTTPException(status_code=413, detail="audio upload exceeds configured limit")
                    output.write(chunk)
            if total == 0:
                raise HTTPException(status_code=400, detail="audio file is empty")
            try:
                result = await speech_engine.transcribe_file(
                    path,
                    language=normalized_language,
                    prompt=prompt,
                )
            except EngineNotReady as exc:
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            key_store.record_audio(str(request.state.api_key_id), total, seconds=result.duration or 0)
            if response_format == "text":
                return PlainTextResponse(result.text)
            if response_format == "verbose_json":
                return JSONResponse(result.verbose_dict())
            return JSONResponse({"text": result.text})
        finally:
            if fd is not None:
                with suppress(OSError):
                    os.close(fd)
            if path is not None:
                Path(path).unlink(missing_ok=True)
            await file.close()
            await request_gate.release()

    async def stream(websocket: WebSocket) -> None:
        key_id = key_store.authenticate(websocket_api_token(websocket))
        if key_id is None:
            await websocket.close(code=4401, reason="invalid API key")
            return
        if not await request_gate.try_acquire():
            await websocket.accept()
            await websocket.send_json(
                {
                    "type": "error",
                    "code": "server_busy",
                    "message": "all transcription slots are busy",
                    "active": request_gate.active,
                    "capacity": request_gate.capacity,
                }
            )
            await websocket.close(code=1013, reason="server busy")
            return
        key_store.record_request(key_id, stream=True)
        try:
            await handle_stream(
                websocket,
                speech_engine,
                silence_commit_ms=config.silence_commit_ms,
                on_audio_bytes=lambda byte_count: key_store.record_audio(key_id, byte_count),
            )
        finally:
            key_store.stream_closed(key_id)
            await request_gate.release()

    app.websocket("/v1/audio/transcriptions/stream")(stream)

    web_dist = Path(config.web_dist)
    assets = web_dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    @app.get("/pcm-worklet.js", include_in_schema=False, response_model=None)
    async def pcm_worklet() -> FileResponse:
        worklet = web_dist / "pcm-worklet.js"
        if not worklet.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(
            worklet,
            media_type="text/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/", include_in_schema=False, response_model=None)
    async def web_index() -> FileResponse | PlainTextResponse:
        index = web_dist / "index.html"
        if index.is_file():
            return FileResponse(index)
        return PlainTextResponse("XGC2 STT Service", status_code=200)

    @app.get("/{path:path}", include_in_schema=False)
    async def web_fallback(path: str) -> FileResponse:
        index = web_dist / "index.html"
        if not index.is_file() or path.startswith(("api/", "v1/")) or "." in Path(path).name:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(index)

    return app


app = create_app()
