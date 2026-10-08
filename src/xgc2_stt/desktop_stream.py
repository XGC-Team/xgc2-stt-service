"""Desktop recording domain over borrowed, SDK-owned native WebSockets."""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from aiohttp import WSMsgType
from xgc2_xrpc import Limits, Runtime
from xgc2_xrpc.websocket import WebSocketClient
from xgc2_xrpc.wire import strict_json_loads

from .desktop_support import DesktopSettings, streaming_headers, streaming_url


class DesktopStreams:
    """One public-network Runtime and one actual recording at a time.

    This Runtime is distinct from the 1-KiB local control Runtime. Neither
    permits per-recording transport threads or queues of pending recordings.
    """

    def __init__(self, *, runtime: Runtime | None = None):
        self.runtime = runtime or Runtime.from_environment(
            blocking_workers=1,
            defaults={
                "HOST_MAX_IN_FLIGHT": 1,
                "CLIENT_MAX_CONNECTIONS": 1, "CLIENT_MAX_REFERENCES": 1,
                "MAX_REQUEST_BYTES": 52_461_568, "MAX_RESPONSE_BYTES": 1_048_576,
                "CALL_TIMEOUT_MS": 1_800_000, "SHUTDOWN_TIMEOUT_MS": 2000,
            },
            ceilings={
                "HOST_MAX_IN_FLIGHT": 1,
                "CLIENT_MAX_CONNECTIONS": 1, "CLIENT_MAX_REFERENCES": 1,
                "MAX_REQUEST_BYTES": 52_461_568, "MAX_RESPONSE_BYTES": 1_048_576,
                "CALL_TIMEOUT_MS": 1_800_000,
            },
        )
        self._lock = threading.Lock()
        self._active: StreamingWorker | None = None
        self._closed = False

    def _start(self, worker: StreamingWorker) -> None:
        with self._lock:
            if self._closed or self._active is not None:
                raise RuntimeError("识别连接仍在使用或清理中")
            self._active = worker
        try:
            worker._future = self.runtime.submit(worker._run(), on_done=worker._done)
        except BaseException:
            self._release(worker)
            raise

    def _release(self, worker: StreamingWorker) -> None:
        with self._lock:
            if self._active is worker:
                self._active = None

    def close(self, timeout: float = 2) -> None:
        with self._lock:
            self._closed = True
            worker = self._active
        if worker is not None:
            worker.cancel()
            if not worker._terminated.wait(timeout):
                raise RuntimeError("识别连接尚未完成清理；资源仍由桌面宿主保留")
            if worker._retained and worker._client is not None:
                self.runtime.run(worker._client.close_async(timeout=timeout), timeout + .1)
                worker._retained = False
                self._release(worker)
        self.runtime.close(timeout=timeout)


class StreamingWorker:
    MAX_PENDING_CHUNKS = 8
    MAX_PENDING_BYTES = 160_000

    def __init__(
        self,
        settings: DesktopSettings,
        *,
        owner: DesktopStreams,
        dispatch: Callable[..., None],
        on_connected: Callable[[], None],
        on_hypothesis: Callable[[str, str, str], None],
        on_state: Callable[[str], None],
        on_segment: Callable[[str], None],
        on_failed: Callable[[str], None],
        on_completed: Callable[[], None],
    ):
        self.settings, self.owner, self.dispatch = settings, owner, dispatch
        self.on_connected, self.on_hypothesis, self.on_state = on_connected, on_hypothesis, on_state
        self.on_segment, self.on_failed, self.on_completed = on_segment, on_failed, on_completed
        self._lock = threading.Lock()
        self._pending: deque[bytes] = deque()
        self._pending_bytes = 0
        self._commit = self._cancel = self._wake_scheduled = False
        self._changed = asyncio.Event()
        self._future: Any = None
        self._client: WebSocketClient | None = None
        self._failure: str | None = None
        self._retained = False
        self._terminated = threading.Event()

    def start(self) -> None:
        self.owner._start(self)

    def _notify(self) -> None:
        with self._lock:
            if self._wake_scheduled:
                return
            self._wake_scheduled = True

        def wake():
            with self._lock:
                self._wake_scheduled = False
            self._changed.set()
        self.owner.runtime.loop.call_soon_threadsafe(wake)

    def feed(self, pcm: bytes) -> None:
        if type(pcm) is not bytes or len(pcm) > 160_000 or len(pcm) % 2:
            raise ValueError("PCM16分块必须完整且不超过160000字节")
        if not pcm:
            return
        with self._lock:
            if self._cancel or self._commit or self._terminated.is_set():
                return
            if self._future is None:
                raise RuntimeError("识别连接尚未启动")
            if len(self._pending) >= self.MAX_PENDING_CHUNKS or self._pending_bytes + len(pcm) > self.MAX_PENDING_BYTES:
                raise RuntimeError("识别发送队列已满；请停止录音后重试")
            self._pending.append(pcm)
            self._pending_bytes += len(pcm)
        self._notify()

    def commit(self) -> None:
        with self._lock:
            self._commit = True
        self._notify()

    def fail(self, message: str) -> None:
        with self._lock:
            self._failure = message
        self.cancel()

    def cancel(self) -> None:
        with self._lock:
            self._cancel = True
            self._pending.clear()
            self._pending_bytes = 0
            future = self._future
        if future is not None:
            future.cancel()

    def _done(self) -> None:
        # Runtime on_done follows the actual asyncio task, not its externally
        # cancellable Future. A failed native close keeps the owner's slot.
        with self._lock:
            self._pending.clear()
            self._pending_bytes = 0
        if self._retained:
            self.dispatch(self.on_state, "连接清理未完成；资源仍被保留")
            self._terminated.set()
            return
        self.owner._release(self)
        if self._failure is not None:
            self.dispatch(self.on_failed, self._failure)
        else:
            self.dispatch(self.on_completed)
        self._terminated.set()

    async def _send(self, socket) -> None:
        while True:
            self._changed.clear()
            with self._lock:
                chunk = self._pending.popleft() if self._pending else None
                if chunk is not None:
                    self._pending_bytes -= len(chunk)
                commit = self._commit and chunk is None
            if chunk is not None:
                await socket.send_bytes(chunk)
            elif commit:
                await socket.send_str('{"type":"commit"}')
                self.dispatch(self.on_state, "收尾")
                return
            else:
                await self._changed.wait()

    async def _event(self, socket) -> dict[str, Any]:
        message = await socket.receive()
        if message.type != WSMsgType.TEXT:
            raise RuntimeError("识别连接已关闭或返回非文本事件")
        value = strict_json_loads(message.data)
        if not isinstance(value, dict):
            raise RuntimeError("识别事件必须是JSON对象")
        return value

    async def _receive(self, socket) -> None:
        while True:
            event = await self._event(socket)
            event_type = event.get("type")
            if event_type == "transcript.partial":
                text = str(event.get("text") or "")
                stable, unstable = str(event.get("stable_text") or ""), str(event.get("unstable_text") or "")
                self.dispatch(self.on_hypothesis, text, stable, unstable if stable or unstable else text)
            elif event_type == "transcript.final":
                text = str(event.get("text") or "")
                if text:
                    self.dispatch(self.on_hypothesis, text, text, "")
                self.dispatch(self.on_segment, str(event.get("reason") or "commit"))
                if event.get("session_complete", True):
                    return
                self.dispatch(self.on_state, "录音中")
            elif event_type == "error":
                raise RuntimeError(str(event.get("message") or event.get("code") or "识别失败"))

    async def _consume(self, socket) -> None:
        first = await self._event(socket)
        if first.get("type") != "session.started":
            raise RuntimeError(first.get("message") or "服务未创建识别会话")
        self.dispatch(self.on_connected)
        sender = asyncio.create_task(self._send(socket))
        receiver = asyncio.create_task(self._receive(socket))
        try:
            done, _ = await asyncio.wait((sender, receiver), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if not receiver.done():
                await receiver
        finally:
            for task in (sender, receiver):
                task.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)

    async def _run(self) -> None:
        try:
            endpoint = urlsplit(streaming_url(self.settings))
            self._client = WebSocketClient(
                f"{endpoint.scheme}://{endpoint.netloc}", runtime=self.owner.runtime,
                limits=Limits(connections=1, body_bytes=52_461_568, response_bytes=1_048_576,
                              call_timeout=1800, shutdown_timeout=2),
            )
            await self._client.consume_async(
                endpoint.path + ("?" + endpoint.query if endpoint.query else ""), self._consume,
                headers=tuple(streaming_headers(self.settings).items()), timeout=1800,
                max_msg_bytes=160_000, total_send_bytes=52_461_568, total_receive_bytes=1_048_576,
            )
        except asyncio.CancelledError:
            pass
        except Exception as error:
            self._failure = str(error)
        finally:
            if self._client is not None:
                try:
                    await self._client.close_async()
                except BaseException:
                    self._retained = True
