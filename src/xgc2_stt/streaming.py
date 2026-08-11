from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import suppress
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from .engine import EngineNotReady, SpeechEngine


async def handle_stream(websocket: WebSocket, engine: SpeechEngine) -> None:
    await websocket.accept()
    session_id = str(uuid.uuid4())
    sequence = 0

    def next_event(event_type: str, **payload: Any) -> dict[str, Any]:
        nonlocal sequence
        sequence += 1
        return {
            "type": event_type,
            "session_id": session_id,
            "sequence": sequence,
            "at": time.time(),
            **payload,
        }

    sample_rate_value = websocket.query_params.get("sample_rate", "16000")
    try:
        sample_rate = int(sample_rate_value)
    except ValueError:
        sample_rate = 0
    if sample_rate != 16000:
        await websocket.send_json(
            next_event("error", code="invalid_sample_rate", message="native realtime input must be PCM16 at 16000 Hz")
        )
        await websocket.close(code=4400)
        return

    try:
        upstream = await engine.open_session()
    except EngineNotReady as exc:
        await websocket.send_json(next_event("error", code="model_not_ready", message=str(exc)))
        await websocket.close(code=1013, reason="model not ready")
        return
    except Exception as exc:
        await websocket.send_json(
            next_event("error", code="upstream_connection_failed", message=f"vLLM connection failed: {exc}")
        )
        await websocket.close(code=1011, reason="upstream connection failed")
        return

    status = engine.status()
    await websocket.send_json(
        next_event(
            "session.started",
            format="pcm_s16le",
            channels=1,
            sample_rate=sample_rate,
            model=status.get("model"),
            variant=status.get("variant"),
            language="auto",
            transcription_delay_ms=status.get("transcription_delay_ms"),
        )
    )

    committed = False

    async def receive_upstream() -> None:
        partial = ""
        while True:
            event = await upstream.receive()
            event_type = event.get("type")
            if event_type == "transcription.delta":
                partial += str(event.get("delta", ""))
                await websocket.send_json(next_event("transcript.partial", text=partial))
            elif event_type == "transcription.done":
                await websocket.send_json(
                    next_event(
                        "transcript.final",
                        text=str(event.get("text", partial)).strip(),
                        usage=event.get("usage"),
                    )
                )
                return
            elif event_type == "error":
                await websocket.send_json(
                    next_event(
                        "error",
                        code=str(event.get("code") or "upstream_error"),
                        message=str(event.get("error")),
                    )
                )
                return

    async def receive_browser() -> None:
        nonlocal committed
        while True:
            try:
                message = await websocket.receive()
            except WebSocketDisconnect:
                return
            if message.get("type") == "websocket.disconnect":
                return
            chunk = message.get("bytes")
            if chunk is not None:
                if committed:
                    continue
                if len(chunk) % 2:
                    await websocket.send_json(
                        next_event(
                            "error",
                            code="invalid_pcm",
                            message="PCM chunks must contain complete int16 samples",
                        )
                    )
                    continue
                await upstream.send_pcm(chunk)
                continue
            raw = message.get("text")
            if raw is None:
                continue
            try:
                command = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    next_event("error", code="invalid_json", message="control messages must be JSON")
                )
                continue
            command_type = command.get("type")
            if command_type == "commit":
                if not committed:
                    committed = True
                    await upstream.commit()
            elif command_type in {"reset", "close"}:
                return
            else:
                await websocket.send_json(
                    next_event("error", code="unknown_command", message="supported commands: commit, reset, close")
                )

    upstream_task = asyncio.create_task(receive_upstream(), name=f"stt-upstream-{session_id}")
    browser_task = asyncio.create_task(receive_browser(), name=f"stt-browser-{session_id}")
    try:
        done, pending = await asyncio.wait(
            {upstream_task, browser_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in pending:
            with suppress(asyncio.CancelledError):
                await task
        for task in done:
            await task
    except WebSocketDisconnect:
        pass
    finally:
        await upstream.close()
        with suppress(RuntimeError):
            await websocket.close(code=1000)
