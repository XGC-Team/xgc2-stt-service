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
    build_engine_command,
    qwen_preview_parts,
)


def test_model_worker_uses_only_xrpc_endpoint() -> None:
    settings = Settings()
    command = build_engine_command(settings, "instance-1")
    assert command == [
        sys.executable,
        "-m",
        "xgc2_stt.model_worker",
        "--socket",
        settings.model_socket_path,
        "--instance",
        "instance-1",
    ]


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
        lambda settings, instance: [sys.executable, "-c", launcher, str(worker_pid_path)],
    )

    async def exercise() -> None:
        engine = VllmEngine(Settings(model_socket_path=str(tmp_path / "model.sock")))
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
                try:
                    worker_pgid = os.getpgid(worker_pid)
                except ProcessLookupError:
                    worker_pgid = None
                listing = os.popen("ps -eo pid,pgid,ppid,args").read()
                raise AssertionError(
                    "orphaned worker survived engine.close() "
                    f"pid={worker_pid} worker_pgid={worker_pgid} "
                    f"launcher={process.pid} self={os.getpid()} "
                    f"scanned={engine_module._pids_in_group(process.pid)!r}\n{listing}"
                )
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


def test_qwen_session_emits_replaceable_partial_and_final(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from xgc2_xrpc import Client, Host, Runtime

    from xgc2_stt.model_worker import ModelDomain

    class Model:
        def init_streaming_state(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(text="", language="Chinese")

        def streaming_transcribe(self, samples: Any, state: Any) -> None:
            assert len(samples) == 16000
            state.text = "这是旧的" if not state.text else "这是修正后的结果"

        def finish_streaming_transcribe(self, state: Any) -> None:
            state.text = "这是修正后的结果"

    config = Settings()
    domain = ModelDomain(config, Model())
    with (
        Runtime(blocking_workers=1) as runtime,
        Host(str(tmp_path / "model.sock"), domain.routes(), runtime=runtime, instance_id="model-1"),
        Client(str(tmp_path / "model.sock"), runtime=runtime, instance_id="model-1") as client,
    ):

        async def exercise() -> None:
            session = await QwenSdkRealtimeSession.open(config, client)
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
            assert domain.session is None

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
