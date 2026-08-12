from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from xgc2_stt.api_keys import ApiKeyStore
from xgc2_stt.config import Settings
from xgc2_stt.engine import Transcript, TranscriptSegment
from xgc2_stt.main import create_app


class FakeEngine:
    def __init__(self, state: str = "ready"):
        self.state = state
        self.started = False
        self.closed = False
        self.upload = b""
        self.sessions: list[FakeRealtimeSession] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "backend": "vllm-realtime",
            "variant": "voxtral",
            "model": "mistralai/Voxtral-Mini-4B-Realtime-2602",
            "device": "cuda",
            "compute_type": "bfloat16",
            "cuda_devices": 1,
            "transcription_delay_ms": 480,
        }

    async def open_session(self) -> FakeRealtimeSession:
        session = FakeRealtimeSession()
        self.sessions.append(session)
        return session

    async def transcribe_file(self, path: str, *, language: str | None, prompt: str | None) -> Transcript:
        self.upload = Path(path).read_bytes()
        return transcript("uploaded speech", language or "zh")


class ReconfigurableFakeEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.reconfigured: Settings | None = None

    async def reconfigure(self, updated: Settings) -> None:
        self.reconfigured = updated


class FakeRealtimeSession:
    def __init__(self) -> None:
        self.pcm = bytearray()
        self.closed = False
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def send_pcm(self, pcm: bytes) -> None:
        self.pcm.extend(pcm)
        await self.events.put({"type": "transcription.delta", "delta": "streamed speech"})

    async def commit(self) -> None:
        await self.events.put({"type": "transcription.done", "text": "streamed speech"})

    async def receive(self) -> dict[str, Any]:
        return await self.events.get()

    async def close(self) -> None:
        self.closed = True


def transcript(text: str, language: str) -> Transcript:
    return Transcript(
        text=text,
        language=language,
        language_probability=0.99,
        duration=1.0,
        duration_after_vad=0.8,
        inference_seconds=0.05,
        segments=(TranscriptSegment(id=0, start=0.0, end=0.8, text=text),),
    )


def settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "web_dist": str(tmp_path / "missing-web"),
        "api_key_store_path": str(tmp_path / "api-keys.json"),
        "runtime_settings_path": str(tmp_path / "runtime-settings.json"),
        "gpu_metrics_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def test_health_status_and_model_contract(tmp_path: Path) -> None:
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        ready = client.get("/readyz")
        assert ready.status_code == 200
        assert ready.json()["engine"]["state"] == "ready"
        status = client.get("/api/status").json()
        assert status["engine"]["cuda_devices"] == 1
        assert status["stream"]["format"] == "pcm_s16le"
        assert status["stream"]["protocol"] == "openai-realtime"
        assert status["stream"]["finalization"] == "silence-or-manual"
        assert status["stream"]["silence_commit_ms"] == 3000
        assert status["stream"]["active"] == 0
        assert status["stream"]["capacity"] == 1
        models = client.get("/v1/models").json()
        assert models["data"][0]["aliases"] == ["whisper-1", "stt-1"]
    assert engine.started and engine.closed


def test_missing_static_asset_is_not_spa_html(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path), FakeEngine())) as client:
        assert client.get("/favicon.ico").status_code == 404


def test_audio_worklet_is_served_as_javascript(tmp_path: Path) -> None:
    web_dist = tmp_path / "web"
    web_dist.mkdir()
    (web_dist / "pcm-worklet.js").write_text("registerProcessor('pcm', class {});", encoding="utf-8")
    with TestClient(create_app(settings(tmp_path, web_dist=str(web_dist)), FakeEngine())) as client:
        response = client.get("/pcm-worklet.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")
        assert response.headers["cache-control"] == "no-cache"
        assert "registerProcessor" in response.text


def test_not_ready_while_model_loads(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path), FakeEngine("loading"))) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"


def test_openai_compatible_upload(tmp_path: Path) -> None:
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("sample.wav", b"RIFF-test", "audio/wav")},
            data={"model": "whisper-1", "language": "zh", "response_format": "verbose_json"},
        )
        assert response.status_code == 200
        assert response.json()["text"] == "uploaded speech"
        assert response.json()["segments"][0]["start"] == 0.0
        assert engine.upload == b"RIFF-test"


def test_upload_limits_and_model_validation(tmp_path: Path) -> None:
    with TestClient(create_app(settings(tmp_path, max_upload_bytes=1_048_576), FakeEngine())) as client:
        empty = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("empty.wav", b"", "audio/wav")},
        )
        assert empty.status_code == 400
        invalid_model = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("sample.wav", b"data", "audio/wav")},
            data={"model": "unknown"},
        )
        assert invalid_model.status_code == 400


def test_upload_setup_failure_releases_capacity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_tempfile(*args: Any, **kwargs: Any) -> tuple[int, str]:
        raise OSError("temporary storage unavailable")

    monkeypatch.setattr("xgc2_stt.main.tempfile.mkstemp", fail_tempfile)
    app = create_app(settings(tmp_path, max_active_streams=1), FakeEngine())
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("sample.wav", b"data", "audio/wav")},
        )
        assert response.status_code == 500
        assert client.get("/api/status").json()["stream"]["active"] == 0


def test_api_key_guards_http_and_websocket(tmp_path: Path) -> None:
    key_store = ApiKeyStore(str(tmp_path / "api-keys.json"))
    _, secret = key_store.create("guard")
    app = create_app(settings(tmp_path), FakeEngine(), api_keys=key_store)
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 200
        assert client.get("/v1/models").status_code == 401
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect(
            "/v1/audio/transcriptions/stream"
        ):
            pass
        assert error.value.code == 4401


def test_managed_api_keys_are_returned_once_and_usage_is_counted(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), FakeEngine())
    with TestClient(app) as client:
        created = client.post("/api/keys", json={"name": "terminal"})
        assert created.status_code == 201
        key_id = created.json()["key"]["id"]
        secret = created.json()["secret"]
        assert secret.startswith("xgc2_sk_")
        listed = client.get("/api/keys").json()["keys"]
        assert listed[0]["id"] == key_id
        assert "digest" not in listed[0]
        assert "secret" not in listed[0]
        assert client.get("/v1/models").status_code == 401
        headers = {"Authorization": f"Bearer {secret}"}
        assert client.get("/v1/models", headers=headers).status_code == 200
        with client.websocket_connect(f"/v1/audio/transcriptions/stream?access_token={secret}") as socket:
            assert socket.receive_json()["type"] == "session.started"
            socket.send_bytes((1000).to_bytes(2, "little", signed=True) * 4000)
            assert socket.receive_json()["type"] == "transcript.partial"
            socket.send_text('{"type":"commit"}')
            assert socket.receive_json()["session_complete"] is True
        usage = client.get("/api/keys").json()["keys"][0]
        assert usage["request_count"] == 2
        assert usage["stream_sessions"] == 1
        assert usage["audio_bytes"] == 8000
        assert usage["audio_seconds"] == 0.25
        assert usage["active_sessions"] == 0

        rotated = client.post(f"/api/keys/{key_id}/rotate").json()["secret"]
        assert client.get("/v1/models", headers=headers).status_code == 401
        assert client.get("/v1/models", headers={"X-API-Key": rotated}).status_code == 200
        assert client.delete(f"/api/keys/{key_id}").status_code == 200
        assert client.get("/v1/models", headers={"X-API-Key": rotated}).status_code == 401


def test_corrupt_managed_key_store_fails_closed(tmp_path: Path) -> None:
    key_store = tmp_path / "api-keys.json"
    key_store.write_text("{not-json", encoding="utf-8")
    app = create_app(settings(tmp_path, api_key_store_path=str(key_store)), FakeEngine())

    with TestClient(app) as client:
        assert client.get("/api/keys").json()["authentication"] == "api-key"
        assert client.get("/v1/models").status_code == 401
        recovery = client.post("/api/keys", json={"name": "recovery"})
        secret = recovery.json()["secret"]
        assert client.get("/v1/models", headers={"Authorization": f"Bearer {secret}"}).status_code == 200


def test_pre_schema_managed_key_store_is_rejected_without_migration(tmp_path: Path) -> None:
    key_store = tmp_path / "api-keys.json"
    key_store.write_text('{"authentication_enabled":false,"keys":[]}', encoding="utf-8")
    app = create_app(settings(tmp_path, api_key_store_path=str(key_store)), FakeEngine())

    with TestClient(app) as client:
        assert client.get("/api/keys").json() == {"authentication": "api-key", "keys": []}
        assert client.get("/v1/models").status_code == 401


def test_runtime_settings_distinguish_hot_changes_from_model_restart(tmp_path: Path) -> None:
    engine = ReconfigurableFakeEngine()
    app = create_app(settings(tmp_path), engine)
    with TestClient(app) as client:
        current = client.get("/api/settings").json()
        assert current["values"]["silence_commit_ms"] == 3000
        hot = client.put("/api/settings", json={"silence_commit_ms": 2500, "max_active_streams": 2})
        assert hot.status_code == 200
        assert hot.json()["restart_required"] is False
        assert client.get("/api/status").json()["stream"]["capacity"] == 2

        pending = client.put("/api/settings", json={"gpu_memory_utilization": 0.7})
        assert pending.json()["restart_required"] is True
        assert pending.json()["pending"] == {"gpu_memory_utilization": 0.7}
        restarted = client.post("/api/engine/restart")
        assert restarted.status_code == 202
        assert restarted.json()["restart_required"] is False
        assert engine.reconfigured is not None
        assert engine.reconfigured.gpu_memory_utilization == 0.7


def test_stream_emits_partial_and_final_events(tmp_path: Path) -> None:
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as client:
        with client.websocket_connect(
            "/v1/audio/transcriptions/stream?sample_rate=16000&output_script=simplified&trim_leading_silence=1"
        ) as socket:
            started = socket.receive_json()
            assert started["type"] == "session.started"
            assert started["format"] == "pcm_s16le"
            assert started["output_script"] == "simplified"
            assert started["trim_leading_silence"] is True
            socket.send_bytes((1000).to_bytes(2, "little", signed=True) * 4000)
            partial = socket.receive_json()
            assert partial["type"] == "transcript.partial"
            assert partial["text"] == "streamed speech"
            socket.send_text('{"type":"commit"}')
            final = socket.receive_json()
            assert final["type"] == "transcript.final"
            assert final["sequence"] > partial["sequence"]
        assert bytes(engine.sessions[0].pcm) == (1000).to_bytes(2, "little", signed=True) * 4000
        assert engine.sessions[0].closed


def test_stream_preserves_initial_audio_when_silence_trimming_is_disabled(tmp_path: Path) -> None:
    engine = FakeEngine()
    silence = (0).to_bytes(2, "little", signed=True) * 4000
    speech = (1000).to_bytes(2, "little", signed=True) * 4000
    with (
        TestClient(create_app(settings(tmp_path), engine)) as client,
        client.websocket_connect(
            "/v1/audio/transcriptions/stream?trim_leading_silence=0"
        ) as socket,
    ):
        started = socket.receive_json()
        assert started["trim_leading_silence"] is False
        socket.send_bytes(silence)
        assert socket.receive_json()["type"] == "transcript.partial"
        socket.send_bytes(speech)
        assert socket.receive_json()["type"] == "transcript.partial"
        socket.send_text('{"type":"commit"}')
        assert socket.receive_json()["type"] == "transcript.final"

    assert bytes(engine.sessions[0].pcm) == silence + speech


def test_stream_rejects_invalid_recognition_settings(tmp_path: Path) -> None:
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as client:
        with client.websocket_connect("/v1/audio/transcriptions/stream?output_script=translated") as socket:
            error = socket.receive_json()
            assert error["type"] == "error"
            assert error["code"] == "invalid_output_script"
        with client.websocket_connect(
            "/v1/audio/transcriptions/stream?trim_leading_silence=maybe"
        ) as socket:
            error = socket.receive_json()
            assert error["code"] == "invalid_trim_leading_silence"
    assert engine.sessions == []


def test_retired_stream_alias_is_not_registered(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path), FakeEngine())
    assert all(getattr(route, "path", None) != "/v1/stream" for route in app.routes)


def test_stream_rejects_a_second_active_session(tmp_path: Path) -> None:
    engine = FakeEngine()
    with (
        TestClient(create_app(settings(tmp_path, max_active_streams=1), engine)) as client,
        client.websocket_connect("/v1/audio/transcriptions/stream") as first,
    ):
        assert first.receive_json()["type"] == "session.started"
        assert client.get("/api/status").json()["stream"]["active"] == 1
        with client.websocket_connect("/v1/audio/transcriptions/stream") as second:
            busy = second.receive_json()
            assert busy["type"] == "error"
            assert busy["code"] == "server_busy"
            assert busy["active"] == 1
            assert busy["capacity"] == 1


def test_silence_finalizes_a_segment_without_closing_the_stream(tmp_path: Path) -> None:
    engine = FakeEngine()
    with (
        TestClient(create_app(settings(tmp_path, silence_commit_ms=3000), engine)) as client,
        client.websocket_connect("/v1/audio/transcriptions/stream") as socket,
    ):
        assert socket.receive_json()["type"] == "session.started"
        socket.send_bytes((1000).to_bytes(2, "little", signed=True) * 8000)
        assert socket.receive_json()["type"] == "transcript.partial"
        socket.send_bytes((0).to_bytes(2, "little", signed=True) * (16000 * 3))
        while True:
            first_final = socket.receive_json()
            if first_final["type"] == "transcript.final":
                break
        assert first_final["reason"] == "silence"
        assert first_final["session_complete"] is False
        assert first_final["segment_index"] == 0

        socket.send_bytes((1200).to_bytes(2, "little", signed=True) * 8000)
        assert socket.receive_json()["type"] == "transcript.partial"
        socket.send_text('{"type":"commit"}')
        second_final = socket.receive_json()
        assert second_final["type"] == "transcript.final"
        assert second_final["reason"] == "commit"
        assert second_final["session_complete"] is True
        assert second_final["segment_index"] == 1
    assert len(engine.sessions) == 2
