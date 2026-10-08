from __future__ import annotations

import asyncio
import shutil
import ssl
import subprocess
import threading
from contextlib import suppress
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web
from native_client import TestClient
from test_api import FakeEngine, FakeRealtimeSession, settings
from xgc2_xrpc import BoundedWebSocketResponse, Runtime

from xgc2_stt import streaming
from xgc2_stt.desktop_stream import DesktopStreams, StreamingWorker
from xgc2_stt.desktop_support import DesktopSettings
from xgc2_stt.main import create_app


class Events:
    def __init__(self):
        self.connected = threading.Event()
        self.partial = threading.Event()
        self.completed = threading.Event()
        self.failed = threading.Event()
        self.hypotheses: list[tuple[str, str, str]] = []
        self.segments: list[str] = []
        self.failures: list[str] = []
        self.states: list[str] = []

    def hypothesis(self, *values: str) -> None:
        self.hypotheses.append(values)
        self.partial.set()

    def failure(self, message: str) -> None:
        self.failures.append(message)
        self.failed.set()

    def worker(self, owner: DesktopStreams, endpoint: str, api_key: str = "") -> StreamingWorker:
        return StreamingWorker(
            DesktopSettings(endpoint=endpoint, api_key=api_key), owner=owner,
            dispatch=lambda function, *args: function(*args),
            on_connected=self.connected.set, on_hypothesis=self.hypothesis,
            on_state=self.states.append, on_segment=self.segments.append,
            on_failed=self.failure, on_completed=self.completed.set,
        )


def test_desktop_native_sdk_stream_auth_partial_commit_and_sequential_recordings(tmp_path: Path) -> None:
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as web:
        key = web.post("/api/keys", json={"name": "desktop"}).json()["secret"]
        owner = DesktopStreams()
        try:
            events = Events()
            worker = events.worker(owner, web.origin, key)
            worker.start()
            assert events.connected.wait(1)
            with pytest.raises(RuntimeError, match="清理"):
                Events().worker(owner, web.origin, key).start()
            threads = {thread.ident for thread in threading.enumerate()}
            pcm = (1000).to_bytes(2, "little", signed=True) * 1000
            worker.feed(pcm)
            assert events.partial.wait(1)
            worker.commit()
            assert events.completed.wait(1)
            assert not events.failures
            assert events.hypotheses[-1] == ("streamed speech", "streamed speech", "")
            assert events.segments == ["commit"]
            assert bytes(engine.sessions[0].pcm) == pcm and engine.sessions[0].closed
            second = Events()
            second_worker = second.worker(owner, web.origin, key)
            second_worker.start()
            assert second.connected.wait(1)
            assert {thread.ident for thread in threading.enumerate()} == threads
            second_worker.cancel()
            assert second.completed.wait(1)
        finally:
            owner.close()


def test_desktop_invalid_credentials_never_start_microphone_domain(tmp_path: Path) -> None:
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as web:
        web.post("/api/keys", json={"name": "required"})
        owner = DesktopStreams()
        events = Events()
        try:
            events.worker(owner, web.origin, "wrong").start()
            assert events.failed.wait(1)
            assert not events.connected.is_set() and not events.completed.is_set()
            assert engine.sessions == []
        finally:
            owner.close()


def test_desktop_audio_queue_is_bounded_under_actual_native_send_backpressure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = asyncio.Event()
    original = aiohttp.ClientWebSocketResponse.send_bytes

    async def blocked_send(socket, data, *args, **kwargs):
        entered.set()
        await release.wait()
        return await original(socket, data, *args, **kwargs)

    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as web:
        owner = DesktopStreams()
        events = Events()
        worker = events.worker(owner, web.origin)
        monkeypatch.setattr(aiohttp.ClientWebSocketResponse, "send_bytes", blocked_send)
        try:
            worker.start()
            assert events.connected.wait(1)
            worker.feed(b"\x00\x01" * 1000)
            assert entered.wait(1)
            for _ in range(worker.MAX_PENDING_CHUNKS):
                worker.feed(b"\x00\x01" * 1000)
            with pytest.raises(RuntimeError, match="队列已满"):
                worker.feed(b"\x00\x01")
            with pytest.raises(ValueError):
                worker.feed(b"odd")
            with pytest.raises(ValueError):
                worker.feed(b"x" * 160_002)
            worker.fail("backpressure stopped capture")
            assert events.failed.wait(1)
            assert events.failures == ["backpressure stopped capture"]
            assert not events.completed.is_set()
        finally:
            owner.runtime.loop.call_soon_threadsafe(release.set)
            owner.close()


def test_desktop_completion_and_admission_wait_for_actual_native_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = asyncio.Event()
    original = aiohttp.ClientWebSocketResponse.close

    async def blocked_close(socket, *args, **kwargs):
        entered.set()
        while not release.is_set():
            with suppress(asyncio.CancelledError):
                await release.wait()
        return await original(socket, *args, **kwargs)

    runtime = Runtime.from_environment(
        {}, defaults={"HOST_MAX_IN_FLIGHT": 1, "CLIENT_MAX_REFERENCES": 1,
                      "CALL_TIMEOUT_MS": 200, "SHUTDOWN_TIMEOUT_MS": 100}
    )
    engine = FakeEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as web:
        owner = DesktopStreams(runtime=runtime)
        events = Events()
        worker = events.worker(owner, web.origin)
        monkeypatch.setattr(aiohttp.ClientWebSocketResponse, "close", blocked_close)
        try:
            worker.start()
            assert events.connected.wait(1)
            worker.commit()
            assert entered.wait(1)
            assert worker._terminated.wait(1)
            assert not events.completed.is_set() and not events.failed.is_set()
            assert events.states[-1] == "连接清理未完成；资源仍被保留"
            with pytest.raises(RuntimeError, match="清理"):
                Events().worker(owner, web.origin).start()
            with pytest.raises(RuntimeError, match="cleanup"):
                owner.close(timeout=.03)
        finally:
            owner.runtime.loop.call_soon_threadsafe(release.set)
            owner.close(timeout=1)
        assert runtime.closed


def test_desktop_wss_requires_trusted_certificate_and_matching_hostname(tmp_path: Path) -> None:
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("isolated TLS fixture requires openssl")
    cert, key = tmp_path / "fixture.crt", tmp_path / "fixture.key"
    subprocess.run(
        [openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
         "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True, timeout=10,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    server_runtime = Runtime()
    seen: list[tuple[str, str | None]] = []

    async def native_fixture(request):
        seen.append((request.raw_path, request.headers.get("Authorization")))
        socket = BoundedWebSocketResponse(
            max_msg_size=160_000, max_receive_bytes=160_000, max_send_bytes=160_000,
            receive_timeout=1, timeout=1,
        )
        await socket.prepare(request)
        await socket.send_json({"type": "session.started"})
        async for message in socket:
            if message.type == aiohttp.WSMsgType.TEXT:
                await socket.send_json({"type": "transcript.final", "text": "verified TLS", "session_complete": True})
                break
        await socket.close()
        return socket

    async def start_fixture():
        app = web.Application()
        app.router.add_get("/v1/audio/transcriptions/stream", native_fixture)
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls).start()
        return runner

    runner = server_runtime.run(start_fixture())
    port = runner.addresses[0][1]
    owner = DesktopStreams()
    try:
        untrusted = Events()
        untrusted.worker(owner, f"https://localhost:{port}").start()
        assert untrusted.failed.wait(1) and not untrusted.connected.is_set()
        owner.runtime.tls_context.load_verify_locations(cert)
        mismatch = Events()
        mismatch.worker(owner, f"https://127.0.0.1:{port}").start()
        assert mismatch.failed.wait(1) and not mismatch.connected.is_set()
        trusted = Events()
        worker = trusted.worker(owner, f"https://localhost:{port}", "tls-explicit")
        worker.start()
        assert trusted.connected.wait(1)
        worker.commit()
        assert trusted.completed.wait(1) and not trusted.failures
        assert trusted.hypotheses[-1] == ("verified TLS", "verified TLS", "")
        assert len(seen) == 1
        assert "access_token" not in seen[0][0] and seen[0][1] == "Bearer tls-explicit"
    finally:
        owner.close()
        server_runtime.run(runner.cleanup())
        server_runtime.close()


def test_native_heartbeat_remains_alive_during_actual_slow_finish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pong_count: list[bytes] = []
    original_pong = aiohttp.ClientWebSocketResponse.pong
    original_response = streaming.BoundedWebSocketResponse

    async def pong(socket, data=b""):
        pong_count.append(data)
        return await original_pong(socket, data)

    def heartbeat_response(**options):
        return original_response(**{**options, "heartbeat": .04})

    class SlowSession(FakeRealtimeSession):
        async def commit(self):
            await asyncio.sleep(.25)
            await super().commit()

    class SlowEngine(FakeEngine):
        async def open_session(self):
            session = SlowSession()
            self.sessions.append(session)
            return session

    monkeypatch.setattr(aiohttp.ClientWebSocketResponse, "pong", pong)
    monkeypatch.setattr(streaming, "BoundedWebSocketResponse", heartbeat_response)
    engine = SlowEngine()
    with TestClient(create_app(settings(tmp_path), engine)) as web:
        owner = DesktopStreams()
        events = Events()
        worker = events.worker(owner, web.origin)
        try:
            worker.start()
            assert events.connected.wait(1)
            worker.feed((1000).to_bytes(2, "little", signed=True) * 1000)
            assert events.partial.wait(1)
            worker.commit()
            assert events.completed.wait(1), events.failures
            assert len(pong_count) >= 2
            assert not events.failures and engine.sessions[0].closed
            assert events.hypotheses[-1] == ("streamed speech", "streamed speech", "")
        finally:
            owner.close()
