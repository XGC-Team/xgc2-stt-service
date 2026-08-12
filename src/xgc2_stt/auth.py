from __future__ import annotations

from fastapi import Request, WebSocket


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def http_api_token(request: Request) -> str | None:
    return _bearer(request.headers.get("authorization")) or request.headers.get("x-api-key")


def websocket_api_token(websocket: WebSocket) -> str | None:
    return (
        _bearer(websocket.headers.get("authorization"))
        or websocket.headers.get("x-api-key")
        or websocket.query_params.get("access_token")
    )
