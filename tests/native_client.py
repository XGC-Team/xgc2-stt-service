"""Synchronous test facade over real XRPC-owned HTTP/WebSocket listeners."""

from __future__ import annotations

import json
from typing import Any

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect
from xgc2_xrpc import Host, Limits, Runtime


class WebSocketDisconnect(ConnectionError):
    def __init__(self, code: int):
        super().__init__(f"websocket closed: {code}")
        self.code = code


class NativeSocket:
    def __init__(self, url: str, **options: Any):
        self.url, self.options = url, options

    def __enter__(self):
        self.context = connect(self.url, open_timeout=2, close_timeout=2, max_queue=4, proxy=None, **self.options)
        self.socket = self.context.__enter__()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.context.__exit__(*_args)

    def send_bytes(self, data: bytes) -> None:
        self.socket.send(data)

    def send_text(self, data: str) -> None:
        self.socket.send(data)

    def receive_json(self) -> Any:
        try:
            return json.loads(self.socket.recv(timeout=2))
        except ConnectionClosed as error:
            raise WebSocketDisconnect(error.rcvd.code if error.rcvd else 1006) from error


class TestClient:
    __test__ = False

    def __init__(self, app: Any, *, runtime: Runtime | None = None, **_options: Any):
        self.owns_runtime = runtime is None
        self.runtime = runtime or Runtime(blocking_workers=2)
        self.host = Host.from_app(
            app, address=("127.0.0.1", 0), runtime=self.runtime, limits=Limits(body_bytes=60_000_000, call_timeout=5)
        )

    def __enter__(self):
        self.host.start()
        self.origin = f"http://127.0.0.1:{self.host.bound_address[1]}"
        self.client = httpx.Client(base_url=self.origin, timeout=3, trust_env=False)
        return self

    def __exit__(self, *_args: Any) -> None:
        self.client.close()
        self.host.close()
        if self.owns_runtime:
            self.runtime.close()

    def get(self, path: str, **options: Any):
        return self.client.get(path, **options)

    def post(self, path: str, **options: Any):
        return self.client.post(path, **options)

    def put(self, path: str, **options: Any):
        return self.client.put(path, **options)

    def delete(self, path: str, **options: Any):
        return self.client.delete(path, **options)

    def websocket_connect(self, path: str, **options: Any) -> NativeSocket:
        return NativeSocket(self.origin.replace("http:", "ws:") + path, **options)
