"""Audio capture isolated from optional GUI and display backends."""

from __future__ import annotations

import sys
import threading
from array import array
from collections.abc import Callable
from contextlib import suppress
from importlib import import_module
from typing import Any


def load_audio_backend() -> Any:
    """Load PortAudio only when a desktop session starts recording."""

    errors: list[str] = []
    for name in ("sounddevice", "pyaudio"):
        try:
            return import_module(name)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError("无法加载麦克风后端: " + "; ".join(errors))


def _is_sounddevice(backend: Any) -> bool:
    return hasattr(backend, "RawInputStream")


class AudioCapture:
    def __init__(
        self,
        on_pcm: Callable[[bytes], None],
        on_failed: Callable[[str], None] | None = None,
        *,
        backend: Any | None = None,
    ):
        self.on_pcm = on_pcm
        self.on_failed = on_failed
        self._backend = backend
        self.stream: Any | None = None
        self._pyaudio: Any | None = None
        self._lifecycle_lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._accepting_audio = threading.Event()
        self._failure_reported = threading.Event()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.stream is not None:
                raise RuntimeError("麦克风已经启动")
            backend = self._backend or load_audio_backend()
            self._backend = backend
            stream: Any | None = None
            try:
                if _is_sounddevice(backend):
                    stream = backend.RawInputStream(
                        samplerate=16000,
                        blocksize=0,
                        channels=1,
                        dtype="int16",
                        callback=self._read,
                    )
                    self.stream = stream
                    self._failure_reported.clear()
                    self._accepting_audio.set()
                    stream.start()
                    return
                self._pyaudio = backend.PyAudio()
                stream = self._pyaudio.open(
                    format=backend.paInt16,
                    channels=1,
                    rate=16000,
                    input=True,
                    frames_per_buffer=1024,
                    stream_callback=self._read_pyaudio,
                )
                self.stream = stream
                self._failure_reported.clear()
                self._accepting_audio.set()
                stream.start_stream()
            except Exception as exc:
                self._accepting_audio.clear()
                self.stream = None
                if stream is not None:
                    with suppress(Exception):
                        if _is_sounddevice(backend):
                            stream.close()
                        else:
                            stream.close()
                if self._pyaudio is not None:
                    with suppress(Exception):
                        self._pyaudio.terminate()
                    self._pyaudio = None
                raise RuntimeError(f"无法启动麦克风: {exc}") from exc

    def _emit_pcm(self, pcm: bytes) -> None:
        if sys.byteorder != "little":
            samples = array("h")
            samples.frombytes(pcm)
            samples.byteswap()
            pcm = samples.tobytes()
        if pcm:
            self.on_pcm(pcm)

    def _read(self, indata: Any, _frames: int, _time: Any, _status: Any) -> None:
        try:
            with self._callback_lock:
                if not self._accepting_audio.is_set():
                    return
                self._emit_pcm(bytes(indata))
        except Exception as exc:
            self._accepting_audio.clear()
            if not self._failure_reported.is_set():
                self._failure_reported.set()
                if self.on_failed is not None:
                    self.on_failed(str(exc))
            backend = self._backend
            if backend is None:
                raise RuntimeError("audio backend is unavailable") from exc
            raise backend.CallbackAbort from exc

    def _read_pyaudio(self, indata: Any, _frames: int, _time: Any, _status: Any) -> tuple[None, int]:
        backend = self._backend
        continue_flag = getattr(backend, "paContinue", 0)
        abort_flag = getattr(backend, "paAbort", 1)
        try:
            with self._callback_lock:
                if not self._accepting_audio.is_set():
                    return (None, continue_flag)
                self._emit_pcm(bytes(indata))
            return (None, continue_flag)
        except Exception as exc:
            self._accepting_audio.clear()
            if not self._failure_reported.is_set():
                self._failure_reported.set()
                if self.on_failed is not None:
                    self.on_failed(str(exc))
            return (None, abort_flag)

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._accepting_audio.clear()
            stream = self.stream
            self.stream = None
            if stream is not None:
                with suppress(Exception):
                    if hasattr(stream, "stop"):
                        stream.stop()
                    elif hasattr(stream, "stop_stream"):
                        stream.stop_stream()
                with suppress(Exception):
                    stream.close()
            if self._pyaudio is not None:
                with suppress(Exception):
                    self._pyaudio.terminate()
                self._pyaudio = None
            with self._callback_lock:
                pass

    def close(self) -> None:
        self.stop()
