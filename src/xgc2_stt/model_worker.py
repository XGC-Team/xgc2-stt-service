"""Qwen native inference behind one bounded XRPC host, without its demo server."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import os
import secrets
import signal
import time
from contextlib import suppress
from typing import Any

import numpy as np
from xgc2_xrpc import Fault, Host, Limits, Runtime

from .config import Settings


def load_model(settings: Settings) -> Any:
    from qwen_asr import Qwen3ASRModel

    return Qwen3ASRModel.LLM(
        model=settings.model_id,
        gpu_memory_utilization=settings.gpu_memory_utilization,
        max_model_len=settings.max_model_len,
        max_num_seqs=settings.max_num_seqs,
        enforce_eager=settings.vllm_enforce_eager,
        dtype=settings.compute_type,
    )


class ModelDomain:
    def __init__(self, settings: Settings, model: Any = None):
        self.settings = settings
        self.model = model
        self.state = "ready" if model is not None else "loading"
        self.error: str | None = None
        self.session_id: str | None = None
        self.session: Any = None
        self.audio_bytes = 0
        self.last_seen = 0.0

    def load(self) -> None:
        try:
            self.model = load_model(self.settings)
            self.state = "ready"
        except Exception as error:
            self.state = "error"
            self.error = f"{type(error).__name__}: {error}"

    async def status(self, _context: Any, _value: Any) -> dict[str, Any]:
        return {"state": self.state, "error": self.error}

    def expire(self) -> None:
        if self.session is not None and time.monotonic() - self.last_seen > self.settings.model_session_idle_seconds:
            self.session_id, self.session = None, None
            self.audio_bytes = 0

    def start(self, _context: Any, value: Any) -> dict[str, str]:
        if value != {}:
            raise Fault("invalid_argument", "session start takes an empty object")
        self.expire()
        if self.state != "ready":
            raise Fault("unavailable", self.error or "model is loading")
        if self.session is not None:
            raise Fault("resource_exhausted", "model supports one active session")
        self.session = self.model.init_streaming_state(
            unfixed_chunk_num=self.settings.qwen_unfixed_chunk_num,
            unfixed_token_num=self.settings.qwen_unfixed_token_num,
            chunk_size_sec=self.settings.qwen_chunk_size_seconds,
        )
        self.session_id = secrets.token_hex(16)
        self.audio_bytes = 0
        self.last_seen = time.monotonic()
        return {"session_id": self.session_id}

    def _session(self, value: Any, fields: set[str]) -> Any:
        self.expire()
        if not isinstance(value, dict) or set(value) != fields or not isinstance(value.get("session_id"), str):
            raise Fault("invalid_argument", "invalid model session request")
        if self.session is None or value["session_id"] != self.session_id:
            raise Fault("conflict", "model session is stale")
        self.last_seen = time.monotonic()
        return self.session

    def chunk(self, _context: Any, value: Any) -> dict[str, Any]:
        state = self._session(value, {"session_id", "pcm16"})
        if not isinstance(value["pcm16"], str) or len(value["pcm16"]) > 4 * ((160_000 + 2) // 3):
            raise Fault("resource_exhausted", "PCM chunk exceeds five seconds")
        try:
            pcm = base64.b64decode(value["pcm16"], validate=True)
        except (ValueError, binascii.Error) as error:
            raise Fault("invalid_argument", "invalid PCM base64") from error
        if not pcm or len(pcm) % 2 or len(pcm) > 160_000:
            raise Fault("invalid_argument", "nonempty even-length PCM16 required")
        if self.audio_bytes + len(pcm) > self.settings.model_session_max_seconds * 32_000:
            raise Fault("resource_exhausted", "model utterance duration exceeds limit; commit it first")
        self.audio_bytes += len(pcm)
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
        samples *= 1.0 / 32768.0
        self.model.streaming_transcribe(samples, state)
        return {"text": state.text, "language": state.language}

    def finish(self, _context: Any, value: Any) -> dict[str, Any]:
        state = self._session(value, {"session_id"})
        try:
            self.model.finish_streaming_transcribe(state)
            return {"text": state.text, "language": state.language}
        finally:
            self.session_id, self.session = None, None
            self.audio_bytes = 0

    def abort(self, _context: Any, value: Any) -> dict[str, bool]:
        self._session(value, {"session_id"})
        self.session_id, self.session = None, None
        self.audio_bytes = 0
        return {"closed": True}

    def routes(self) -> dict[Any, Any]:
        return {
            ("GET", "/v1/status"): self.status,
            ("POST", "/v1/session/start"): self.start,
            ("POST", "/v1/session/chunk"): self.chunk,
            ("POST", "/v1/session/finish"): self.finish,
            ("POST", "/v1/session/abort"): self.abort,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--instance", required=True)
    args = parser.parse_args()
    settings = Settings()
    domain = ModelDomain(settings)
    runtime = Runtime.from_environment(
        blocking_workers=1,
        defaults={
            "HOST_MAX_IN_FLIGHT": 4,
            "HOST_MAX_CONNECTIONS": 4,
            "CLIENT_MAX_REFERENCES": 1,
            "MAX_REQUEST_BYTES": 220000,
            "MAX_RESPONSE_BYTES": 65536,
            "CALL_TIMEOUT_MS": 300000,
            "SHUTDOWN_TIMEOUT_MS": 30000,
        },
        ceilings={
            "HOST_MAX_IN_FLIGHT": 4,
            "HOST_MAX_CONNECTIONS": 4,
            "CLIENT_MAX_REFERENCES": 1,
            "MAX_REQUEST_BYTES": 220000,
            "MAX_RESPONSE_BYTES": 65536,
        },
    )
    host = Host(
        args.socket,
        domain.routes(),
        runtime=runtime,
        limits=Limits(
            connections=4, in_flight=4, body_bytes=220_000, response_bytes=65_536, call_timeout=300, shutdown_timeout=30
        ),
        instance_id=args.instance,
        allowed_uids={os.getuid()},
        reclaim_unreachable=True,
    )
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda _sig, _frame: runtime.loop.call_soon_threadsafe(stop.set))

    async def serve() -> None:
        await host.start_async()
        loading = asyncio.create_task(runtime.blocking(host, domain.load), name="qwen-native-load")

        async def expire() -> None:
            while not stop.is_set():
                await asyncio.sleep(1)
                with suppress(Fault):
                    await runtime.blocking(host, domain.expire)

        expiry = asyncio.create_task(expire(), name="qwen-session-expiry")
        try:
            await stop.wait()
        finally:
            expiry.cancel()
            await asyncio.gather(expiry, return_exceptions=True)
            await host.close_async()
            await loading

    try:
        runtime.run(serve())
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
