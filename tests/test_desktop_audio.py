from __future__ import annotations

import threading
from typing import Any

import pytest

import xgc2_stt.desktop as desktop


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


def test_audio_capture_streams_mono_pcm16_at_16khz(monkeypatch: pytest.MonkeyPatch) -> None:
    streams: list[FakeRawInputStream] = []

    def open_stream(**kwargs: Any) -> FakeRawInputStream:
        stream = FakeRawInputStream(**kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setattr(desktop.sounddevice, "RawInputStream", open_stream)
    received: list[bytes] = []
    capture = desktop.AudioCapture(received.append)

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
    monkeypatch.setattr(desktop.sounddevice, "RawInputStream", lambda **kwargs: _configure(stream, kwargs))
    capture = desktop.AudioCapture(lambda _pcm: None)

    with pytest.raises(RuntimeError, match="无法启动麦克风: device busy"):
        capture.start()

    assert stream.closed is True
    assert capture.stream is None


def test_audio_capture_stop_waits_for_inflight_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = FakeRawInputStream()
    monkeypatch.setattr(desktop.sounddevice, "RawInputStream", lambda **kwargs: _configure(stream, kwargs))
    callback_entered = threading.Event()
    release_callback = threading.Event()

    def consume(_pcm: bytes) -> None:
        callback_entered.set()
        assert release_callback.wait(timeout=2)

    capture = desktop.AudioCapture(consume)
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


def _configure(stream: FakeRawInputStream, options: dict[str, Any]) -> FakeRawInputStream:
    stream.options = options
    return stream
