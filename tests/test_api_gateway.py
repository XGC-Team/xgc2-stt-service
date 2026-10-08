from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from native_client import TestClient
from test_api import FakeEngine, settings
from xgc2_xrpc import Runtime

from xgc2_stt.main import API_APP_KEY, create_app


def test_api_only_host_has_shared_auth_model_and_isolated_routes(tmp_path: Path) -> None:
    engine = FakeEngine()
    app = create_app(settings(tmp_path), engine)
    with TestClient(app) as web, TestClient(app[API_APP_KEY], runtime=web.runtime) as api:
        for path in ("/", "/api/status", "/api/keys", "/api/settings", "/docs", "/v1/stream"):
            assert api.get(path).status_code == 404
        key = web.post("/api/keys", json={"name": "gateway"}).json()["secret"]
        assert api.get("/v1/models").status_code == 401
        response = api.get("/v1/models", headers={"Authorization": f"Bearer {key}", "Origin": "https://stt.test"})
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == "*"
        assert api.get("/healthz").json() == {"status": "ok"}
        assert api.get("/readyz").json()["engine"]["state"] == "ready"
        assert engine.started and not engine.closed
        upload = api.post(
            "/v1/audio/transcriptions",
            files={"file": ("sample.wav", b"RIFF-test", "audio/wav")},
            headers={"X-API-Key": key},
        )
        assert upload.json()["text"] == "uploaded speech"
        assert engine.upload == b"RIFF-test"
        with api.websocket_connect(f"/v1/audio/transcriptions/stream?access_token={key}") as socket:
            assert socket.receive_json()["type"] == "session.started"
            assert web.get("/api/status").json()["stream"]["active"] == 1
            socket.send_bytes((1000).to_bytes(2, "little", signed=True) * 4000)
            assert socket.receive_json()["type"] == "transcript.partial"
            socket.send_text('{"type":"commit"}')
            assert socket.receive_json()["session_complete"] is True
        assert web.get("/api/keys").json()["keys"][0]["stream_sessions"] == 1
    assert engine.closed


def test_two_hosts_share_runtime_request_budget(tmp_path: Path) -> None:
    runtime = Runtime.from_environment(
        {}, defaults={"HOST_MAX_IN_FLIGHT": 1, "HOST_MAX_CONNECTIONS": 4, "CALL_TIMEOUT_MS": 5000}
    )
    app = create_app(settings(tmp_path), FakeEngine())
    try:
        with TestClient(app, runtime=runtime) as web, TestClient(app[API_APP_KEY], runtime=runtime) as api:
            assert web.host.limits.in_flight == api.host.limits.in_flight == runtime.max_calls == 1
            with api.websocket_connect("/v1/audio/transcriptions/stream") as socket:
                assert socket.receive_json()["type"] == "session.started"
                assert runtime.calls == 1
                assert web.get("/healthz").status_code == 429
            assert web.get("/healthz").status_code == 200
    finally:
        runtime.close()


def test_two_hosts_share_runtime_connection_budget(tmp_path: Path) -> None:
    runtime = Runtime.from_environment({}, defaults={"HOST_MAX_CONNECTIONS": 2})
    app = create_app(settings(tmp_path), FakeEngine())
    try:
        with TestClient(app, runtime=runtime) as web, TestClient(app[API_APP_KEY], runtime=runtime) as api:
            assert web.get("/healthz").status_code == api.get("/healthz").status_code == 200
            assert runtime.connections == 2
            with (
                httpx.Client(timeout=1, trust_env=False) as third,
                pytest.raises((httpx.RemoteProtocolError, httpx.ReadError)),
            ):
                third.get(web.origin + "/healthz")
            assert runtime.connections == 2
    finally:
        runtime.close()
