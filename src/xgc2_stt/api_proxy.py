from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

import httpx
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

_REQUEST_HEADERS = {"accept", "authorization", "content-type", "origin", "x-api-key"}
_RESPONSE_HEADERS = {
    "access-control-allow-credentials",
    "access-control-allow-headers",
    "access-control-allow-methods",
    "access-control-allow-origin",
    "access-control-expose-headers",
    "cache-control",
    "content-type",
    "retry-after",
}


def create_api_proxy(
    upstream_http_url: str | None = None,
    upstream_websocket_url: str | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    http_url = (upstream_http_url or os.getenv("STT_UPSTREAM_HTTP_URL") or "http://stt:8000").rstrip("/")
    websocket_url = (
        upstream_websocket_url or os.getenv("STT_UPSTREAM_WEBSOCKET_URL") or "ws://stt:8000"
    ).rstrip("/")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            base_url=http_url,
            transport=transport,
            timeout=360,
            trust_env=False,
        ) as client:
            app.state.upstream = client
            yield

    app = FastAPI(
        title="XGC2 STT API Gateway",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    async def forward_http(request: Request) -> Response:
        headers = {name: value for name, value in request.headers.items() if name.lower() in _REQUEST_HEADERS}
        try:
            async with request.app.state.upstream.stream(
                request.method,
                request.url.path,
                params=request.query_params,
                headers=headers,
                content=request.stream(),
            ) as upstream:
                body = await upstream.aread()
                response_headers = {
                    name: value for name, value in upstream.headers.items() if name.lower() in _RESPONSE_HEADERS
                }
                return Response(body, status_code=upstream.status_code, headers=response_headers)
        except httpx.HTTPError:
            return JSONResponse(
                {"error": "upstream_unavailable", "message": "STT service is unavailable"},
                status_code=502,
            )

    for path in ("/healthz", "/readyz"):
        app.add_api_route(path, forward_http, methods=["GET", "HEAD"], include_in_schema=False)
    app.add_api_route(
        "/v1/{path:path}",
        forward_http,
        methods=["GET", "POST", "OPTIONS"],
        include_in_schema=False,
    )

    async def forward_websocket(websocket: WebSocket) -> None:
        query = websocket.url.query
        target = f"{websocket_url}{websocket.url.path}{'?' + query if query else ''}"
        headers = {
            name: value
            for name, value in websocket.headers.items()
            if name.lower() in {"authorization", "origin", "x-api-key"}
        }
        try:
            upstream = await connect(
                target,
                additional_headers=headers,
                open_timeout=10,
                close_timeout=5,
                max_size=4 * 1024 * 1024,
                proxy=None,
            )
        except InvalidStatus as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            await websocket.accept()
            if status_code in {401, 403}:
                await websocket.send_json(
                    {"type": "error", "code": "invalid_api_key", "message": "API Key is invalid or missing"}
                )
                await websocket.close(code=4401, reason="invalid API key")
                return
            await websocket.send_json(
                {"type": "error", "code": "upstream_unavailable", "message": "STT service is unavailable"}
            )
            await websocket.close(code=1013, reason="upstream unavailable")
            return
        except Exception:
            await websocket.accept()
            await websocket.send_json(
                {"type": "error", "code": "upstream_unavailable", "message": "STT service is unavailable"}
            )
            await websocket.close(code=1013, reason="upstream unavailable")
            return

        await websocket.accept()

        async def client_to_upstream() -> None:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    await upstream.send(message["bytes"])
                elif message.get("text") is not None:
                    await upstream.send(message["text"])

        async def upstream_to_client() -> None:
            try:
                async for message in upstream:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)
            except ConnectionClosed:
                return

        tasks = {
            asyncio.create_task(client_to_upstream(), name="api-proxy-client"),
            asyncio.create_task(upstream_to_client(), name="api-proxy-upstream"),
        }
        try:
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in pending:
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            await upstream.close()
            with suppress(RuntimeError):
                await websocket.close(code=1000)

    app.websocket("/v1/audio/transcriptions/stream")(forward_websocket)
    return app


app = create_api_proxy()
