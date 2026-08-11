from __future__ import annotations

import asyncio
import base64
import json
import wave
from pathlib import Path
from typing import Any

import xgc2_stt.engine as engine_module
from xgc2_stt.config import Settings
from xgc2_stt.engine import VllmRealtimeSession, _decode_audio_pcm16, build_vllm_command


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
    assert "--hf-overrides" not in command


def test_qwen_command_selects_vllm_realtime_architecture() -> None:
    settings = Settings(
        engine_variant="qwen",
        model_id="Qwen/Qwen3-ASR-1.7B",
        transcription_delay_ms=5000,
    )
    command = build_vllm_command(settings)
    overrides = json.loads(command[command.index("--hf-overrides") + 1])
    assert overrides == {"architectures": ["Qwen3ASRRealtimeGeneration"]}
    assert "--tokenizer-mode" not in command


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
