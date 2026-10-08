from __future__ import annotations

from aiohttp.web import Request


def _bearer(value: str | None) -> str | None:
    if not value:
        return None
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def http_api_token(request: Request) -> str | None:
    return _bearer(request.headers.get("authorization")) or request.headers.get("x-api-key")
