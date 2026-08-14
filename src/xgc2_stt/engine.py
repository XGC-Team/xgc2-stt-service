from __future__ import annotations

import asyncio
import os
import re
import signal
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
import numpy as np

from .config import Settings


@dataclass(frozen=True)
class TranscriptSegment:
    id: int
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    text: str
    language: str | None
    language_probability: float | None
    duration: float | None
    duration_after_vad: float | None
    inference_seconds: float
    segments: tuple[TranscriptSegment, ...]

    def verbose_dict(self) -> dict[str, Any]:
        return {
            "task": "transcribe",
            "language": self.language,
            "duration": self.duration,
            "duration_after_vad": self.duration_after_vad,
            "text": self.text,
            "inference_seconds": self.inference_seconds,
            "segments": [asdict(segment) for segment in self.segments],
        }


class RealtimeSession(Protocol):
    async def send_pcm(self, pcm: bytes) -> None: ...

    async def commit(self) -> None: ...

    async def receive(self) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class SpeechEngine(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    def status(self) -> dict[str, Any]: ...

    async def open_session(self) -> RealtimeSession: ...

    async def transcribe_file(
        self,
        path: str,
        *,
        language: str | None,
        prompt: str | None,
    ) -> Transcript: ...


class EngineNotReady(RuntimeError):
    pass


class QwenSdkRealtimeSession:
    """Adapter for Qwen's official revision-capable streaming state API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        session_id: str,
        chunk_bytes: int,
        unfixed_chunk_num: int = 4,
        unfixed_token_num: int = 5,
    ):
        self.client = client
        self.session_id = session_id
        self.chunk_bytes = chunk_bytes
        self.buffer = bytearray()
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.last_text = ""
        self.language = ""
        self.finished = False
        self.chunk_count = 0
        self.unfixed_chunk_num = unfixed_chunk_num
        self.unfixed_token_num = unfixed_token_num

    @classmethod
    async def open(cls, settings: Settings) -> QwenSdkRealtimeSession:
        client = httpx.AsyncClient(base_url=settings.internal_http_url, timeout=300)
        try:
            response = await client.post("/api/start")
            response.raise_for_status()
            session_id = str(response.json()["session_id"])
            chunk_bytes = round(settings.qwen_chunk_size_seconds * 16000 * 2)
            return cls(
                client,
                session_id,
                chunk_bytes,
                settings.qwen_unfixed_chunk_num,
                settings.qwen_unfixed_token_num,
            )
        except Exception:
            await client.aclose()
            raise

    async def send_pcm(self, pcm: bytes) -> None:
        if self.finished or not pcm:
            return
        self.buffer.extend(pcm)
        while len(self.buffer) >= self.chunk_bytes:
            chunk = bytes(self.buffer[: self.chunk_bytes])
            del self.buffer[: self.chunk_bytes]
            await self._send_chunk(chunk)

    async def _send_chunk(self, pcm: bytes) -> None:
        self.chunk_count += 1
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        samples *= 1.0 / 32768.0
        response = await self.client.post(
            "/api/chunk",
            params={"session_id": self.session_id},
            content=samples.tobytes(),
            headers={"Content-Type": "application/octet-stream"},
        )
        response.raise_for_status()
        payload = response.json()
        self.language = str(payload.get("language") or self.language)
        text = str(payload.get("text") or "")
        if text != self.last_text:
            self.last_text = text
            stable_text, unstable_text = qwen_preview_parts(
                text,
                chunk_count=self.chunk_count,
                unfixed_chunk_num=self.unfixed_chunk_num,
                unfixed_token_num=self.unfixed_token_num,
            )
            await self.events.put(
                {
                    "type": "transcription.partial",
                    "text": text,
                    "stable_text": stable_text,
                    "unstable_text": unstable_text,
                    "stability": "qwen-unfixed-window",
                    "language": self.language or None,
                }
            )

    async def commit(self) -> None:
        if self.finished:
            return
        if self.buffer:
            chunk = bytes(self.buffer)
            self.buffer.clear()
            await self._send_chunk(chunk)
        response = await self.client.post("/api/finish", params={"session_id": self.session_id})
        response.raise_for_status()
        payload = response.json()
        self.finished = True
        self.language = str(payload.get("language") or self.language)
        self.last_text = str(payload.get("text") or self.last_text)
        await self.events.put(
            {
                "type": "transcription.done",
                "text": self.last_text,
                "language": self.language or None,
            }
        )

    async def receive(self) -> dict[str, Any]:
        return await self.events.get()

    async def close(self) -> None:
        if not self.finished:
            with suppress(Exception):
                await self.client.post("/api/finish", params={"session_id": self.session_id})
        self.finished = True
        await self.client.aclose()


def qwen_preview_parts(
    text: str,
    *,
    chunk_count: int,
    unfixed_chunk_num: int,
    unfixed_token_num: int,
) -> tuple[str, str]:
    """Expose Qwen's revisable suffix without claiming exact tokenizer boundaries."""
    if chunk_count <= unfixed_chunk_num or not text:
        return "", text
    units = list(re.finditer(r"[\u3400-\u9fff]|[A-Za-z0-9_]+|[^\s]", text))
    if len(units) <= unfixed_token_num:
        return "", text
    split_at = units[-unfixed_token_num].start()
    return text[:split_at], text[split_at:]


def build_qwen_command(settings: Settings) -> list[str]:
    return [
        "qwen-asr-demo-streaming",
        "--asr-model-path",
        settings.model_id,
        "--gpu-memory-utilization",
        str(settings.gpu_memory_utilization),
        "--host",
        settings.internal_host,
        "--port",
        str(settings.internal_port),
        "--unfixed-chunk-num",
        str(settings.qwen_unfixed_chunk_num),
        "--unfixed-token-num",
        str(settings.qwen_unfixed_token_num),
        "--chunk-size-sec",
        str(settings.qwen_chunk_size_seconds),
    ]


def build_engine_command(settings: Settings) -> list[str]:
    return build_qwen_command(settings)


class VllmEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._process: asyncio.subprocess.Process | None = None
        self._process_group_id: int | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._state = "idle"
        self._error: str | None = None
        self._loaded_at: float | None = None
        self._load_seconds: float | None = None
        self._started_monotonic: float | None = None
        self._cuda_devices: int | None = None

    async def start(self) -> None:
        if self._process is not None:
            return
        if not self.settings.manage_engine:
            self._state = "idle"
            return
        self._state = "loading"
        self._error = None
        self._started_monotonic = time.monotonic()
        self._cuda_devices = await _cuda_device_count()
        environment = os.environ.copy()
        environment.update(
            {
                "VLLM_LOGGING_LEVEL": self.settings.vllm_log_level.upper(),
                "VLLM_NO_USAGE_STATS": "1",
                "HF_XET_HIGH_PERFORMANCE": "1",
            }
        )
        try:
            _enable_child_subreaper()
            self._process = await asyncio.create_subprocess_exec(
                *build_engine_command(self.settings),
                env=environment,
                start_new_session=True,
            )
            # start_new_session makes the engine launcher the leader of a new
            # process group. Keep the id independently: qwen's launcher can
            # exit before its VLLM::EngineCore child, at which point getpgid()
            # can no longer recover the group from the launcher pid.
            self._process_group_id = self._process.pid
        except Exception as exc:
            self._state = "error"
            self._error = f"{type(exc).__name__}: {exc}"
            raise
        self._monitor_task = asyncio.create_task(self._monitor(), name="vllm-readiness")

    async def _monitor(self) -> None:
        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        async with httpx.AsyncClient(timeout=3) as client:
            while True:
                process = self._process
                if process is None:
                    return
                if process.returncode is not None:
                    self._state = "error"
                    self._error = f"vLLM exited with code {process.returncode}"
                    return
                try:
                    response = await client.get(f"{self.settings.internal_http_url}/")
                    if response.status_code == 200:
                        self._state = "ready"
                        self._loaded_at = time.time()
                        if self._started_monotonic is not None:
                            self._load_seconds = round(time.monotonic() - self._started_monotonic, 3)
                        break
                except httpx.HTTPError:
                    pass
                if time.monotonic() >= deadline:
                    self._state = "error"
                    self._error = f"{self.settings.engine_variant} model startup timed out"
                    return
                await asyncio.sleep(2)
        process = self._process
        if process is not None:
            return_code = await process.wait()
            if self._process is process:
                self._state = "error"
                self._error = f"vLLM exited with code {return_code}"

    async def close(self) -> None:
        task = self._monitor_task
        self._monitor_task = None
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        process = self._process
        process_group_id = self._process_group_id
        self._process = None
        self._process_group_id = None
        if process is None and process_group_id is None:
            return
        if process_group_id is not None:
            await _terminate_process_group(process, process_group_id)
        elif process is not None:
            await _terminate_process(process)

    async def reconfigure(self, settings: Settings) -> None:
        await self.close()
        self.settings = settings
        self._state = "idle"
        self._error = None
        self._loaded_at = None
        self._load_seconds = None
        await self.start()

    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "error": self._error,
            "backend": "qwen-asr-streaming",
            "variant": self.settings.engine_variant,
            "model": self.settings.model_name,
            "model_path": self.settings.model_id,
            "device": "cuda",
            "device_index": 0,
            "compute_type": self.settings.compute_type,
            "cuda_devices": self._cuda_devices,
            "loaded_at": self._loaded_at,
            "load_seconds": self._load_seconds,
            "transcription_delay_ms": self.settings.transcription_delay_ms,
            "process_id": self._process.pid if self._process is not None else None,
            "managed": self.settings.manage_engine,
        }

    async def open_session(self) -> RealtimeSession:
        if self._state != "ready":
            raise EngineNotReady(self._error or f"model is {self._state}")
        return await QwenSdkRealtimeSession.open(self.settings)

    async def transcribe_file(
        self,
        path: str,
        *,
        language: str | None,
        prompt: str | None,
    ) -> Transcript:
        pcm = await asyncio.to_thread(_decode_audio_pcm16, path)
        started = time.perf_counter()
        session = await self.open_session()
        try:
            for offset in range(0, len(pcm), 64 * 1024):
                await session.send_pcm(pcm[offset : offset + 64 * 1024])
            await session.commit()
            text = ""
            while True:
                event = await asyncio.wait_for(session.receive(), timeout=300)
                if event.get("type") == "transcription.delta":
                    text += str(event.get("delta", ""))
                elif event.get("type") == "transcription.partial":
                    text = str(event.get("text", text))
                elif event.get("type") == "transcription.done":
                    text = str(event.get("text", text))
                    break
                elif event.get("type") == "error":
                    raise RuntimeError(str(event.get("error", "vLLM transcription failed")))
        finally:
            await session.close()
        duration = len(pcm) / (16000 * 2)
        return Transcript(
            text=text.strip(),
            language=language,
            language_probability=None,
            duration=duration,
            duration_after_vad=None,
            inference_seconds=round(time.perf_counter() - started, 3),
            segments=(),
        )


async def _cuda_device_count() -> int | None:
    try:
        process = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=index",
            "--format=csv,noheader",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
        if process.returncode != 0:
            return None
        return len([line for line in output.decode().splitlines() if line.strip()])
    except (OSError, TimeoutError):
        return None


def _enable_child_subreaper() -> None:
    """Adopt grandchildren when a launcher exits before its workers.

    Docker PID namespaces often make ``killpg`` return ESRCH after the
    session leader dies, even though orphaned workers are still running.
    """
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
        libc.prctl(36, 1, 0, 0, 0)  # PR_SET_CHILD_SUBREAPER
    except (AttributeError, OSError):
        return


def _pids_in_group(process_group_id: int) -> list[int]:
    """Return live PIDs that still belong to *process_group_id*."""
    found: list[int] = []
    proc_dir = Path("/proc")
    try:
        entries = list(proc_dir.iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in {1, os.getpid()}:
            continue
        try:
            if os.getpgid(pid) == process_group_id:
                found.append(pid)
        except (ProcessLookupError, PermissionError, OSError):
            continue
    return found


def _signal_process_group(process_group_id: int, sig: int) -> None:
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(process_group_id, sig)
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(-process_group_id, sig)
    for pid in _pids_in_group(process_group_id):
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


async def _terminate_process_group(
    process: asyncio.subprocess.Process | None,
    process_group_id: int,
    *,
    timeout: float = 30,
) -> None:
    """Stop an engine launcher and every worker it spawned in its session."""
    _signal_process_group(process_group_id, signal.SIGTERM)

    deadline = asyncio.get_running_loop().time() + timeout
    if process is not None:
        await _wait_for_process_until(process, deadline)
    if await _wait_for_process_group_until(process_group_id, deadline):
        return

    _signal_process_group(process_group_id, signal.SIGKILL)
    if process is not None:
        await _wait_for_process_until(process, asyncio.get_running_loop().time() + 5)
    await _wait_for_process_group_until(process_group_id, asyncio.get_running_loop().time() + 5)


async def _terminate_process(process: asyncio.subprocess.Process, *, timeout: float = 30) -> None:
    """Fallback for a process that predates process-group management."""
    if process.returncode is not None:
        await process.wait()
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except TimeoutError:
        process.kill()
        await process.wait()


async def _wait_for_process_until(process: asyncio.subprocess.Process, deadline: float) -> bool:
    if process.returncode is not None:
        await process.wait()
        return True
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return False
    try:
        await asyncio.wait_for(process.wait(), timeout=remaining)
    except TimeoutError:
        return False
    return True


async def _wait_for_process_group_until(process_group_id: int, deadline: float) -> bool:
    proc_available = Path("/proc").is_dir()
    while True:
        if proc_available:
            members = [pid for pid in _pids_in_group(process_group_id) if pid not in {1, os.getpid()}]
            if not members:
                return True
        else:
            try:
                os.killpg(process_group_id, 0)
            except ProcessLookupError:
                return True
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(0.1)


def _decode_audio_pcm16(path: str) -> bytes:
    import av

    output = bytearray()
    with av.open(path) as container:
        if not container.streams.audio:
            raise ValueError("uploaded file does not contain an audio stream")
        stream = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
        for frame in container.decode(stream):
            for resampled in resampler.resample(frame):
                output.extend(resampled.to_ndarray().astype("<i2", copy=False).tobytes())
        for resampled in resampler.resample(None):
            output.extend(resampled.to_ndarray().astype("<i2", copy=False).tobytes())
    if not output:
        raise ValueError("uploaded audio has no decodable samples")
    return bytes(output)
