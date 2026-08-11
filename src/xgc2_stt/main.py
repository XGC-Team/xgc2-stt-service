from __future__ import annotations

import os
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import require_http_api_key, websocket_is_authorized
from .config import Settings, get_settings
from .engine import EngineNotReady, SpeechEngine, VllmEngine
from .streaming import handle_stream


def create_app(settings: Settings | None = None, engine: SpeechEngine | None = None) -> FastAPI:
    config = settings or get_settings()
    speech_engine = engine or VllmEngine(config)
    started_at = time.time()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await speech_engine.start()
        try:
            yield
        finally:
            await speech_engine.close()

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

    def authorized(request: Request) -> None:
        require_http_api_key(request, config)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        status = speech_engine.status()
        ready = status.get("state") == "ready"
        code = 200 if ready else 503
        return JSONResponse({"status": "ready" if ready else "not_ready", "engine": status}, status_code=code)

    @app.get("/api/status", dependencies=[Depends(authorized)])
    async def service_status() -> dict[str, Any]:
        return {
            "service": "xgc2-stt-service",
            "version": __version__,
            "uptime_seconds": round(time.time() - started_at, 3),
            "authentication": "api-key" if config.api_key is not None else "trusted-network",
            "stream": {
                "protocol": "openai-realtime",
                "format": "pcm_s16le",
                "sample_rate": 16000,
                "channels": 1,
                "transcription_delay_ms": config.transcription_delay_ms,
                "finalization": "manual",
            },
            "engine": speech_engine.status(),
        }

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
        normalized_language = None if language in {None, "", "auto"} else language
        suffix = Path(file.filename or "audio.bin").suffix[:16]
        fd, path = tempfile.mkstemp(prefix="xgc2-stt-upload-", suffix=suffix)
        total = 0
        try:
            with os.fdopen(fd, "wb") as output:
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
        finally:
            Path(path).unlink(missing_ok=True)
            await file.close()
        if response_format == "text":
            return PlainTextResponse(result.text)
        if response_format == "verbose_json":
            return JSONResponse(result.verbose_dict())
        return JSONResponse({"text": result.text})

    async def stream(websocket: WebSocket) -> None:
        if not websocket_is_authorized(websocket, config):
            await websocket.close(code=4401, reason="invalid API key")
            return
        await handle_stream(websocket, speech_engine)

    app.websocket("/v1/audio/transcriptions/stream")(stream)
    app.websocket("/v1/stream")(stream)

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
