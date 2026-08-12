from __future__ import annotations

import asyncio
import base64
import json
import wave
from pathlib import Path
from typing import Any

import xgc2_stt.engine as engine_module
from xgc2_stt.config import Settings
from xgc2_stt.engine import (
    QwenSdkRealtimeSession,
    VllmRealtimeSession,
    _decode_audio_pcm16,
    build_qwen_command,
    build_vllm_command,
    qwen_preview_parts,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.messages = [json.dumps({"type": "session.created", "id": "session-1"})]
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def recv(self) -> str:
        return self.messages.pop(0)

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def close(self) -> None:
        self.closed = True


def test_voxtral_command_uses_native_mistral_realtime_runtime() -> None:
    command = build_vllm_command(Settings())
    assert command[:3] == ["vllm", "serve", "mistralai/Voxtral-Mini-4B-Realtime-2602"]
    assert command[command.index("--tokenizer-mode") + 1] == "mistral"
    assert command[command.index("--config-format") + 1] == "mistral"
    compilation = json.loads(command[command.index("--compilation-config") + 1])
    assert compilation == {"cudagraph_mode": "PIECEWISE"}
    overrides = json.loads(command[command.index("--hf-overrides") + 1])
    assert overrides == {"architectures": ["VoxtralRealtimeGeneration"]}


def test_qwen_command_selects_official_revision_capable_streaming_server() -> None:
    settings = Settings(
        engine_variant="qwen",
        model_id="Qwen/Qwen3-ASR-1.7B",
        transcription_delay_ms=5000,
    )
    command = build_qwen_command(settings)
    assert command[:3] == ["qwen-asr-demo-streaming", "--asr-model-path", "Qwen/Qwen3-ASR-1.7B"]
    assert command[command.index("--unfixed-chunk-num") + 1] == "4"
    assert command[command.index("--unfixed-token-num") + 1] == "5"
    assert command[command.index("--chunk-size-sec") + 1] == "1.0"


def test_audio_upload_is_decoded_to_mono_pcm16(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    expected = (1000).to_bytes(2, "little", signed=True) * 1600
    with wave.open(str(audio), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(expected)
    assert _decode_audio_pcm16(str(audio)) == expected


def test_vllm_realtime_session_uses_official_protocol(monkeypatch: Any) -> None:
    socket = FakeWebSocket()

    async def fake_connect(*args: Any, **kwargs: Any) -> FakeWebSocket:
        return socket

    monkeypatch.setattr(engine_module, "connect", fake_connect)

    async def exercise() -> None:
        session = await VllmRealtimeSession.open(Settings())
        await session.send_pcm(b"\x01\x00")
        await session.commit()
        await session.close()

    asyncio.run(exercise())
    assert socket.sent == [
        {"type": "session.update", "model": "mistralai/Voxtral-Mini-4B-Realtime-2602"},
        {"type": "input_audio_buffer.commit"},
        {"type": "input_audio_buffer.append", "audio": base64.b64encode(b"\x01\x00").decode("ascii")},
        {"type": "input_audio_buffer.commit", "final": True},
    ]
    assert socket.closed


def test_qwen_session_emits_replaceable_partial_and_final() -> None:
    texts = iter(["这是旧的", "这是修正后的结果"])

    def handler(request: Any) -> Any:
        if request.url.path == "/api/chunk":
            assert request.headers["content-type"] == "application/octet-stream"
            assert len(request.content) == 16000 * 4
            return engine_module.httpx.Response(200, json={"language": "Chinese", "text": next(texts)})
        if request.url.path == "/api/finish":
            return engine_module.httpx.Response(200, json={"language": "Chinese", "text": "这是修正后的结果"})
        raise AssertionError(request.url.path)

    async def exercise() -> None:
        transport = engine_module.httpx.MockTransport(handler)
        client = engine_module.httpx.AsyncClient(base_url="http://qwen.test", transport=transport)
        session = QwenSdkRealtimeSession(client, "session-1", 16000 * 2)
        await session.send_pcm((1000).to_bytes(2, "little", signed=True) * 16000)
        first = await session.receive()
        await session.send_pcm((1000).to_bytes(2, "little", signed=True) * 16000)
        revised = await session.receive()
        await session.commit()
        final = await session.receive()
        await session.close()
        assert first["text"] == "这是旧的"
        assert revised["text"] == "这是修正后的结果"
        assert final["type"] == "transcription.done"

    asyncio.run(exercise())


def test_qwen_preview_marks_only_the_recent_unfixed_window() -> None:
    assert qwen_preview_parts(
        "前面的内容已经稳定后面会修改",
        chunk_count=4,
        unfixed_chunk_num=4,
        unfixed_token_num=5,
    ) == ("", "前面的内容已经稳定后面会修改")
    assert qwen_preview_parts(
        "前面的内容已经稳定后面会修改",
        chunk_count=5,
        unfixed_chunk_num=4,
        unfixed_token_num=5,
    ) == ("前面的内容已经稳定", "后面会修改")
    assert qwen_preview_parts(
        "支持 network API 混合输入",
        chunk_count=5,
        unfixed_chunk_num=4,
        unfixed_token_num=3,
    ) == ("支持 network API 混", "合输入")
