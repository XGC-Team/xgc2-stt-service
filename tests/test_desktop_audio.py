from __future__ import annotations

import threading
from typing import Any

import pytest

import xgc2_stt.desktop_audio as desktop_audio


class FakeAudioBackend:
    class CallbackAbort(Exception):
        pass

    def __init__(self, factory):
        self.RawInputStream = factory


class FakeRawInputStream:
    def __init__(self, **kwargs: Any):
        self.options = kwargs
        self.started = False
        self.stopped = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def emit(self, pcm: bytes) -> None:
        self.options["callback"](pcm, len(pcm) // 2, None, None)

    def read(self, frames: int) -> tuple[bytes, bool]:
        return (b"\x01\x00" * min(frames, 1), False)


def test_audio_capture_streams_mono_pcm16_at_16khz(monkeypatch: pytest.MonkeyPatch) -> None:
    streams: list[FakeRawInputStream] = []

    def open_stream(**kwargs: Any) -> FakeRawInputStream:
        stream = FakeRawInputStream(**kwargs)
        streams.append(stream)
        return stream

    received: list[bytes] = []
    capture = desktop_audio.AudioCapture(
        received.append,
        backend=FakeAudioBackend(open_stream),
    )

    capture.start()

    stream = streams[0]
    assert stream.options["samplerate"] == 16000
    assert stream.options["channels"] == 1
    assert stream.options["dtype"] == "int16"
    assert stream.options["blocksize"] == 0
    assert stream.started is True

    pcm = b"\x01\x00\xff\x7f"
    stream.emit(pcm)
    assert received == [pcm]

    capture.stop()
    assert stream.stopped is True
    assert stream.closed is True
    assert capture.stream is None

    stream.emit(b"\x02\x00")
    assert received == [pcm]
    capture.close()


def test_audio_capture_closes_stream_when_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRawInputStream(FakeRawInputStream):
        def start(self) -> None:
            raise OSError("device busy")

    stream = FailingRawInputStream()
    capture = desktop_audio.AudioCapture(
        lambda _pcm: None,
        backend=FakeAudioBackend(lambda **kwargs: _configure(stream, kwargs)),
    )

    with pytest.raises(RuntimeError, match="无法启动麦克风:.*device busy"):
        capture.start()

    assert stream.closed is True
    assert capture.stream is None


def test_audio_capture_stop_waits_for_inflight_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeRawInputStream()
    callback_entered = threading.Event()
    release_callback = threading.Event()

    def consume(_pcm: bytes) -> None:
        callback_entered.set()
        assert release_callback.wait(timeout=2)

    capture = desktop_audio.AudioCapture(
        consume,
        backend=FakeAudioBackend(lambda **kwargs: _configure(stream, kwargs)),
    )
    capture.start()
    callback_thread = threading.Thread(target=stream.emit, args=(b"\x01\x00",))
    callback_thread.start()
    assert callback_entered.wait(timeout=1)

    stopped = threading.Event()
    stop_thread = threading.Thread(target=lambda: (capture.stop(), stopped.set()))
    stop_thread.start()
    assert not stopped.wait(timeout=0.05)

    release_callback.set()
    callback_thread.join(timeout=1)
    stop_thread.join(timeout=1)
    assert stopped.is_set()
    assert stream.closed is True


def test_audio_capture_reports_callback_failure_exactly_once() -> None:
    stream = FakeRawInputStream()
    failures: list[str] = []
    capture = desktop_audio.AudioCapture(
        lambda _pcm: (_ for _ in ()).throw(RuntimeError("consumer failed")),
        failures.append,
        backend=FakeAudioBackend(lambda **kwargs: _configure(stream, kwargs)),
    )
    capture.start()

    with pytest.raises(FakeAudioBackend.CallbackAbort):
        stream.emit(b"\x01\x00")
    assert failures == ["consumer failed"]

    stream.emit(b"\x02\x00")
    assert failures == ["consumer failed"]
    capture.close()


def test_audio_capture_falls_back_to_second_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailingRawInputStream(FakeRawInputStream):
        def start(self) -> None:
            raise OSError("no default device")

    opened: list[str] = []

    class FakePaStream:
        def __init__(self) -> None:
            self.started = False

        def start_stream(self) -> None:
            self.started = True
            opened.append("pyaudio")

        def stop_stream(self) -> None:
            return None

        def close(self) -> None:
            return None

    class FakePyAudio:
        def open(self, **kwargs: Any) -> FakePaStream:
            assert kwargs["rate"] == 16000
            return FakePaStream()

        def terminate(self) -> None:
            return None

        def get_default_input_device_info(self) -> dict[str, Any]:
            raise OSError("no default")

        def get_device_count(self) -> int:
            return 0

        def get_device_info_by_index(self, index: int) -> dict[str, Any]:
            raise OSError(index)

    class FakePyAudioBackend:
        paInt16 = 8
        paContinue = 0
        paAbort = 1

        def PyAudio(self) -> FakePyAudio:
            return FakePyAudio()

        def get_device_count(self) -> int:
            return 0

    failing = FakeAudioBackend(lambda **kwargs: FailingRawInputStream())
    monkeypatch.setattr(
        desktop_audio,
        "load_audio_backends",
        lambda: [("sounddevice", failing), ("pyaudio", FakePyAudioBackend())],
    )
    capture = desktop_audio.AudioCapture(lambda _pcm: None)
    capture.start()
    assert opened == ["pyaudio"]
    capture.close()


def test_audio_capture_skips_cffi_callback_when_libffi_mismatches() -> None:
    class ClosureFailStream(FakeRawInputStream):
        def start(self) -> None:
            if self.options.get("callback") is not None:
                raise OSError(
                    "ffi_prep_closure(): bad user_data (it seems that the version "
                    "of the libffi library seen at runtime is different from the "
                    "'ffi.h' seen at compile-time)"
                )
            self.started = True

    stream = ClosureFailStream()
    capture = desktop_audio.AudioCapture(
        lambda _pcm: None,
        backend=FakeAudioBackend(lambda **kwargs: _configure(stream, kwargs)),
    )
    capture.start()
    assert stream.started is True
    assert "callback" not in stream.options
    assert capture._reader_thread is not None
    capture.close()
    assert capture._reader_thread is None


def test_audio_capture_resamples_native_rate_to_16khz() -> None:
    stream = FakeRawInputStream()

    class RateLimitedFactory:
        def __call__(self, **kwargs: Any) -> FakeRawInputStream:
            if kwargs["samplerate"] == 16000:
                raise OSError("Invalid sample rate")
            return _configure(stream, kwargs)

    received: list[bytes] = []
    capture = desktop_audio.AudioCapture(
        received.append,
        backend=FakeAudioBackend(RateLimitedFactory()),
    )
    capture.start()
    assert stream.options["samplerate"] == 48000

    # 6 samples at 48 kHz -> 2 samples at 16 kHz
    pcm_48k = bytes([1, 0, 2, 0, 3, 0, 4, 0, 5, 0, 6, 0])
    stream.emit(pcm_48k)
    assert received
    assert len(received[0]) == 4
    capture.close()


def _configure(stream: FakeRawInputStream, options: dict[str, Any]) -> FakeRawInputStream:
    stream.options = options
    return stream
