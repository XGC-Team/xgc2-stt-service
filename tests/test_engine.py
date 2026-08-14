from __future__ import annotations

import asyncio
import os
import signal
import sys
import wave
from contextlib import suppress
from pathlib import Path
from typing import Any

import xgc2_stt.engine as engine_module
from xgc2_stt.config import Settings
from xgc2_stt.engine import (
    QwenSdkRealtimeSession,
    VllmEngine,
    _decode_audio_pcm16,
    build_qwen_command,
    qwen_preview_parts,
)


def test_qwen_command_selects_official_revision_capable_streaming_server() -> None:
    command = build_qwen_command(Settings())
    assert command[:3] == ["qwen-asr-demo-streaming", "--asr-model-path", "Qwen/Qwen3-ASR-1.7B"]
    assert command[command.index("--unfixed-chunk-num") + 1] == "4"
    assert command[command.index("--unfixed-token-num") + 1] == "5"
    assert command[command.index("--chunk-size-sec") + 1] == "1.0"


def test_managed_engine_close_terminates_orphaned_worker_process_group(tmp_path: Path, monkeypatch: Any) -> None:
    if os.name != "posix":
        return

    worker_pid_path = tmp_path / "worker.pid"
    launcher = (
        "import pathlib, subprocess, sys; "
        "worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(worker.pid))"
    )

    async def no_cuda_probe() -> None:
        return None

    monkeypatch.setattr(engine_module, "_cuda_device_count", no_cuda_probe)
    monkeypatch.setattr(
        engine_module,
        "build_engine_command",
        lambda settings: [sys.executable, "-c", launcher, str(worker_pid_path)],
    )

    async def exercise() -> None:
        engine = VllmEngine(Settings())
        worker_pid: int | None = None
        await engine.start()
        process = engine._process
        assert process is not None
        try:
            assert os.getpgid(process.pid) == process.pid
            await asyncio.wait_for(process.wait(), timeout=5)
            for _ in range(100):
                if worker_pid_path.exists():
                    break
                await asyncio.sleep(0.02)
            worker_pid = int(worker_pid_path.read_text())
            assert os.getpgid(worker_pid) == process.pid

            # The launcher is gone, but close must still terminate its live
            # EngineCore-like worker by using the remembered process group.
            assert process.returncode == 0
            os.kill(worker_pid, 0)
            await engine.close()
            try:
                os.kill(worker_pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError("orphaned worker survived engine.close()")
        finally:
            await engine.close()
            if worker_pid is not None:
                with suppress(ProcessLookupError):
                    os.kill(worker_pid, signal.SIGKILL)

    asyncio.run(exercise())


def test_audio_upload_is_decoded_to_mono_pcm16(tmp_path: Path) -> None:
    audio = tmp_path / "sample.wav"
    expected = (1000).to_bytes(2, "little", signed=True) * 1600
    with wave.open(str(audio), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(expected)
    assert _decode_audio_pcm16(str(audio)) == expected


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
