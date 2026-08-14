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

    return import_module("sounddevice")


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
            except Exception as exc:
                self._accepting_audio.clear()
                self.stream = None
                if stream is not None:
                    with suppress(Exception):
                        stream.close()
                raise RuntimeError(f"无法启动麦克风: {exc}") from exc

    def _read(self, indata: Any, _frames: int, _time: Any, _status: Any) -> None:
        try:
            with self._callback_lock:
                if not self._accepting_audio.is_set():
                    return
                pcm = bytes(indata)
                if sys.byteorder != "little":
                    samples = array("h")
                    samples.frombytes(pcm)
                    samples.byteswap()
                    pcm = samples.tobytes()
                if pcm:
                    self.on_pcm(pcm)
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

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._accepting_audio.clear()
            stream = self.stream
            self.stream = None
            if stream is not None:
                with suppress(Exception):
                    stream.stop()
                with suppress(Exception):
                    stream.close()
            with self._callback_lock:
                pass

    def close(self) -> None:
        self.stop()
