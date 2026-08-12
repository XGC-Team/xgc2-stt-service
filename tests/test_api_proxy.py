from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from xgc2_stt.api_proxy import create_api_proxy


def test_api_proxy_exposes_only_api_routes_and_forwards_credentials() -> None:
    requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v1/models":
            return httpx.Response(
                200,
                json={"object": "list", "data": []},
                headers={"Access-Control-Allow-Origin": "https://stt.example.test"},
            )
        return httpx.Response(404, json={"detail": "not found"})

    app = create_api_proxy(
        "http://stt.test:8000",
        "ws://stt.test:8000",
        transport=httpx.MockTransport(upstream),
    )
    with TestClient(app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/api/status").status_code == 404
        assert client.get("/docs").status_code == 404
        assert requests == []
        assert client.get("/healthz").json() == {"status": "ok"}
        response = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer secret", "Origin": "https://stt.example.test"},
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "https://stt.example.test"

    assert requests[-1].headers["authorization"] == "Bearer secret"
    assert requests[-1].headers["origin"] == "https://stt.example.test"
