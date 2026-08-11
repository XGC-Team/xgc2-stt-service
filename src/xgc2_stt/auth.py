from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, WebSocket, status

from .config import Settings


def _matches(expected: str, candidate: str | None) -> bool:
    return bool(candidate) and hmac.compare_digest(expected, candidate)


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_http_api_key(request: Request, settings: Settings) -> None:
    configured = settings.api_key
    if configured is None:
        return
    expected = configured.get_secret_value()
    candidate = _bearer(request.headers.get("authorization")) or request.headers.get("x-api-key")
    if not _matches(expected, candidate):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")


def websocket_is_authorized(websocket: WebSocket, settings: Settings) -> bool:
    configured = settings.api_key
    if configured is None:
        return True
    expected = configured.get_secret_value()
    candidate = (
        _bearer(websocket.headers.get("authorization"))
        or websocket.headers.get("x-api-key")
        or websocket.query_params.get("access_token")
    )
    return _matches(expected, candidate)
