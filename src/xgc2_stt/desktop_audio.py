"""Audio capture isolated from optional GUI and display backends."""

from __future__ import annotations

import audioop
import sys
import threading
from array import array
from collections.abc import Callable
from contextlib import suppress
from importlib import import_module
from typing import Any

TARGET_RATE = 16000
CANDIDATE_RATES = (16000, 48000, 44100)
BACKEND_NAMES = ("pyaudio", "sounddevice")
BLOCK_FRAMES = 1024


def load_audio_backend() -> Any:
    """Load PortAudio only when a desktop session starts recording."""

    loaded = load_audio_backends()
    return loaded[0][1]


def load_audio_backends() -> list[tuple[str, Any]]:
    errors: list[str] = []
    loaded: list[tuple[str, Any]] = []
    for name in BACKEND_NAMES:
        try:
            loaded.append((name, import_module(name)))
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    if not loaded:
        raise RuntimeError("无法加载麦克风后端: " + "; ".join(errors))
    return loaded


def _is_sounddevice(backend: Any) -> bool:
    return hasattr(backend, "RawInputStream")


def _is_cffi_closure_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "ffi_prep_closure" in text or "bad user_data" in text


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
        self._native_rate = TARGET_RATE
        self._ratecv_state: Any = None
        self._reader_stop = threading.Event()
        self._reader_thread: threading.Thread | None = None

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.stream is not None:
                raise RuntimeError("麦克风已经启动")
            if self._backend is not None:
                try:
                    self._start_backend(self._backend)
                except Exception as exc:
                    raise RuntimeError(f"无法启动麦克风: {exc}") from exc
                return
            errors: list[str] = []
            for name, backend in load_audio_backends():
                try:
                    self._start_backend(backend)
                    return
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                    self._abort_open()
            raise RuntimeError("无法启动麦克风: " + "; ".join(errors))

    def _start_backend(self, backend: Any) -> None:
        self._backend = backend
        errors: list[str] = []
        if _is_sounddevice(backend):
            for device in _sounddevice_input_devices(backend):
                for rate in CANDIDATE_RATES:
                    for blocksize in (0, BLOCK_FRAMES):
                        for use_callback in (True, False):
                            try:
                                self._open_sounddevice(
                                    backend,
                                    device=device,
                                    rate=rate,
                                    blocksize=blocksize,
                                    use_callback=use_callback,
                                )
                                return
                            except Exception as exc:
                                errors.append(
                                    f"device={device} rate={rate} block={blocksize} "
                                    f"cb={use_callback}: {exc}"
                                )
                                self._abort_open()
            raise RuntimeError("; ".join(errors) or "sounddevice 没有可用输入设备")
        for device in _pyaudio_input_devices(backend):
            for rate in CANDIDATE_RATES:
                for use_callback in (True, False):
                    try:
                        self._open_pyaudio(
                            backend, device=device, rate=rate, use_callback=use_callback
                        )
                        return
                    except Exception as exc:
                        errors.append(f"device={device} rate={rate} cb={use_callback}: {exc}")
                        self._abort_open()
        raise RuntimeError("; ".join(errors) or "pyaudio 没有可用输入设备")

    def _open_sounddevice(
        self,
        backend: Any,
        *,
        device: Any,
        rate: int,
        blocksize: int,
        use_callback: bool,
    ) -> None:
        frames = blocksize or BLOCK_FRAMES
        kwargs: dict[str, Any] = {
            "samplerate": rate,
            "blocksize": blocksize,
            "channels": 1,
            "dtype": "int16",
        }
        if use_callback:
            kwargs["callback"] = self._read
        if device is not None:
            kwargs["device"] = device
        stream = backend.RawInputStream(**kwargs)
        self.stream = stream
        self._native_rate = rate
        self._ratecv_state = None
        self._failure_reported.clear()
        self._accepting_audio.set()
        try:
            stream.start()
            if not use_callback:
                self._start_reader(lambda: _sounddevice_read(stream, frames))
        except Exception:
            self._accepting_audio.clear()
            self._stop_reader()
            self.stream = None
            with suppress(Exception):
                stream.close()
            raise

    def _open_pyaudio(
        self, backend: Any, *, device: Any, rate: int, use_callback: bool
    ) -> None:
        pa = backend.PyAudio()
        self._pyaudio = pa
        kwargs: dict[str, Any] = {
            "format": backend.paInt16,
            "channels": 1,
            "rate": rate,
            "input": True,
            "frames_per_buffer": BLOCK_FRAMES,
        }
        if use_callback:
            kwargs["stream_callback"] = self._read_pyaudio
        if device is not None:
            kwargs["input_device_index"] = device
        stream = pa.open(**kwargs)
        self.stream = stream
        self._native_rate = rate
        self._ratecv_state = None
        self._failure_reported.clear()
        self._accepting_audio.set()
        try:
            stream.start_stream()
            if not use_callback:
                self._start_reader(lambda: _pyaudio_read(stream, BLOCK_FRAMES))
        except Exception:
            self._accepting_audio.clear()
            self._stop_reader()
            self.stream = None
            with suppress(Exception):
                stream.close()
            with suppress(Exception):
                pa.terminate()
            self._pyaudio = None
            raise

    def _start_reader(self, read_pcm: Callable[[], bytes]) -> None:
        self._reader_stop.clear()

        def loop() -> None:
            while not self._reader_stop.is_set() and self._accepting_audio.is_set():
                try:
                    pcm = read_pcm()
                except Exception as exc:
                    self._accepting_audio.clear()
                    if not self._failure_reported.is_set():
                        self._failure_reported.set()
                        if self.on_failed is not None:
                            self.on_failed(str(exc))
                    return
                if not pcm:
                    continue
                try:
                    with self._callback_lock:
                        if self._accepting_audio.is_set():
                            self._emit_pcm(pcm)
                except Exception as exc:
                    self._accepting_audio.clear()
                    if not self._failure_reported.is_set():
                        self._failure_reported.set()
                        if self.on_failed is not None:
                            self.on_failed(str(exc))
                    return

        self._reader_thread = threading.Thread(target=loop, name="xgc2-stt-mic", daemon=True)
        self._reader_thread.start()

    def _stop_reader(self) -> None:
        self._reader_stop.set()
        thread = self._reader_thread
        self._reader_thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _abort_open(self) -> None:
        self._accepting_audio.clear()
        self._stop_reader()
        stream = self.stream
        self.stream = None
        if stream is not None:
            with suppress(Exception):
                stream.close()
        if self._pyaudio is not None:
            with suppress(Exception):
                self._pyaudio.terminate()
            self._pyaudio = None
        self._native_rate = TARGET_RATE
        self._ratecv_state = None

    def _emit_pcm(self, pcm: bytes) -> None:
        if sys.byteorder != "little":
            samples = array("h")
            samples.frombytes(pcm)
            samples.byteswap()
            pcm = samples.tobytes()
        if self._native_rate != TARGET_RATE:
            pcm, self._ratecv_state = audioop.ratecv(
                pcm, 2, 1, self._native_rate, TARGET_RATE, self._ratecv_state
            )
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
            self._stop_reader()
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
            self._native_rate = TARGET_RATE
            self._ratecv_state = None
            with self._callback_lock:
                pass

    def close(self) -> None:
        self.stop()


def _sounddevice_read(stream: Any, frames: int) -> bytes:
    result = stream.read(frames)
    data = result[0] if isinstance(result, tuple) else result
    return bytes(data)


def _pyaudio_read(stream: Any, frames: int) -> bytes:
    try:
        return stream.read(frames, exception_on_overflow=False)
    except TypeError:
        return stream.read(frames)


def _sounddevice_input_devices(backend: Any) -> list[Any]:
    candidates: list[Any] = [None]
    try:
        listing = list(backend.query_devices())
    except Exception:
        return candidates
    try:
        default = backend.default.device[0]
    except Exception:
        default = None
    if default not in (None, -1) and default not in candidates:
        candidates.append(default)
    for index, info in enumerate(listing):
        if isinstance(info, dict):
            channels = info.get("max_input_channels", 0)
        else:
            channels = getattr(info, "max_input_channels", 0)
        if channels and index not in candidates:
            candidates.append(index)
    return candidates


def _pyaudio_input_devices(backend: Any) -> list[Any]:
    pa = None
    try:
        pa = backend.PyAudio()
        candidates: list[Any] = [None]
        try:
            default = int(pa.get_default_input_device_info()["index"])
            candidates.append(default)
        except Exception:
            pass
        for index in range(int(pa.get_device_count())):
            try:
                info = pa.get_device_info_by_index(index)
            except Exception:
                continue
            if int(info.get("maxInputChannels") or 0) > 0 and index not in candidates:
                candidates.append(index)
        return candidates
    except Exception:
        return [None]
    finally:
        if pa is not None:
            with suppress(Exception):
                pa.terminate()
