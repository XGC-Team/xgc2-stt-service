from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

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


def test_api_key_guards_http_and_websocket(tmp_path: Path) -> None:
    app = create_app(settings(tmp_path, api_key="secret"), FakeEngine())
    with TestClient(app) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/api/status", headers={"Authorization": "Bearer secret"}).status_code == 200
        with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect("/v1/stream"):
            pass
        assert error.value.code == 4401


def test_stream_emits_partial_and_final_events(tmp_path: Path) -> None:
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as client:
        with client.websocket_connect(
            "/v1/audio/transcriptions/stream?sample_rate=16000&language=zh&partial_interval_ms=250"
        ) as socket:
            started = socket.receive_json()
            assert started["type"] == "session.started"
            assert started["format"] == "pcm_s16le"
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
