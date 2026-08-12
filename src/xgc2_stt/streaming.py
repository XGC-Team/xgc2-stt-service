from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from array import array
from collections.abc import Callable
from contextlib import suppress
from functools import lru_cache
from sys import byteorder
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from opencc import OpenCC

from .engine import EngineNotReady, SpeechEngine


@lru_cache(maxsize=1)
def _traditional_to_simplified() -> OpenCC:
    return OpenCC("tw2sp")


def normalize_transcript(text: str, output_script: str) -> str:
    if output_script == "original":
        return text
    return _traditional_to_simplified().convert(text)


def parse_boolean_query(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


class LeadingSilenceGate:
    """Drops initial silence while retaining a short pre-roll before first speech."""

    def __init__(self, *, threshold: int = 250, pre_roll_bytes: int = 5120):
        self.threshold = threshold
        self.pre_roll_bytes = pre_roll_bytes - (pre_roll_bytes % 2)
        self._pending = bytearray()
        self._started = False

    @staticmethod
    def rms(pcm: bytes) -> float:
        if not pcm:
            return 0.0
        samples = array("h")
        samples.frombytes(pcm)
        if byteorder != "little":
            samples.byteswap()
        if not samples:
            return 0.0
        return math.sqrt(sum(sample * sample for sample in samples) / len(samples))

    def push(self, pcm: bytes) -> bytes:
        if self._started:
            return pcm
        if self.rms(pcm) >= self.threshold:
            self._started = True
            result = bytes(self._pending) + pcm
            self._pending.clear()
            return result
        self._pending.extend(pcm)
        if len(self._pending) > self.pre_roll_bytes:
            del self._pending[: len(self._pending) - self.pre_roll_bytes]
        return b""


async def handle_stream(
    websocket: WebSocket,
    engine: SpeechEngine,
    *,
    silence_commit_ms: int = 3000,
    silence_threshold: int = 250,
    on_audio_bytes: Callable[[int], None] | None = None,
) -> None:
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

    output_script = websocket.query_params.get("output_script", "simplified")
    if output_script not in {"simplified", "original"}:
        await websocket.send_json(
            next_event(
                "error",
                code="invalid_output_script",
                message="output_script must be simplified or original",
            )
        )
        await websocket.close(code=4400)
        return
    trim_leading_silence = parse_boolean_query(websocket.query_params.get("trim_leading_silence", "1"))
    if trim_leading_silence is None:
        await websocket.send_json(
            next_event(
                "error",
                code="invalid_trim_leading_silence",
                message="trim_leading_silence must be a boolean",
            )
        )
        await websocket.close(code=4400)
        return

    status = engine.status()
    if status.get("state") != "ready":
        await websocket.send_json(
            next_event("error", code="model_not_ready", message=str(status.get("error") or "model is not ready"))
        )
        await websocket.close(code=1013, reason="model not ready")
        return
    await websocket.send_json(
        next_event(
            "session.started",
            format="pcm_s16le",
            channels=1,
            sample_rate=sample_rate,
            model=status.get("model"),
            variant=status.get("variant"),
            language="auto",
            output_script=output_script,
            trim_leading_silence=trim_leading_silence,
            transcription_delay_ms=status.get("transcription_delay_ms"),
            silence_commit_ms=silence_commit_ms,
        )
    )

    upstream: Any = None
    upstream_task: asyncio.Task[dict[str, Any]] | None = None
    browser_task: asyncio.Task[dict[str, Any]] | None = asyncio.create_task(
        websocket.receive(), name=f"stt-browser-{session_id}"
    )
    client_committed = False
    upstream_committing = False
    speech_started = False
    silence_seconds = 0.0
    segment_index = 0
    partial = ""
    pending_rollover: list[bytes] = []
    leading_silence_gate = LeadingSilenceGate() if trim_leading_silence else None

    async def open_upstream() -> bool:
        nonlocal upstream, upstream_task
        try:
            upstream = await engine.open_session()
        except EngineNotReady as exc:
            await websocket.send_json(next_event("error", code="model_not_ready", message=str(exc)))
            return False
        except Exception as exc:
            await websocket.send_json(
                next_event("error", code="upstream_connection_failed", message=f"model connection failed: {exc}")
            )
            return False
        upstream_task = asyncio.create_task(upstream.receive(), name=f"stt-upstream-{session_id}")
        return True

    async def process_pcm(chunk: bytes) -> bool:
        nonlocal speech_started, silence_seconds, upstream_committing
        if len(chunk) % 2:
            await websocket.send_json(
                next_event("error", code="invalid_pcm", message="PCM chunks must contain complete int16 samples")
            )
            return True
        if upstream_committing:
            pending_rollover.append(chunk)
            while sum(map(len, pending_rollover)) > 160_000:
                pending_rollover.pop(0)
            return True

        is_speech = LeadingSilenceGate.rms(chunk) >= silence_threshold
        if not speech_started:
            outgoing = leading_silence_gate.push(chunk) if leading_silence_gate is not None else chunk
            if not outgoing or not is_speech:
                return True
            if upstream is None and not await open_upstream():
                return False
            speech_started = True
            silence_seconds = 0.0
            await upstream.send_pcm(outgoing)
            return True

        if upstream is None and not await open_upstream():
            return False
        await upstream.send_pcm(chunk)
        if is_speech:
            silence_seconds = 0.0
            return True
        silence_seconds += len(chunk) / (16_000 * 2)
        if silence_seconds * 1000 >= silence_commit_ms:
            upstream_committing = True
            await upstream.commit()
        return True

    should_close = False
    try:
        while not should_close:
            tasks = {task for task in (browser_task, upstream_task) if task is not None}
            if not tasks:
                break
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

            if browser_task in done:
                try:
                    message = browser_task.result()
                except WebSocketDisconnect:
                    break
                browser_task = None
                if message.get("type") == "websocket.disconnect":
                    break
                chunk = message.get("bytes")
                if chunk is not None and not client_committed:
                    if on_audio_bytes is not None:
                        on_audio_bytes(len(chunk))
                    if not await process_pcm(chunk):
                        break
                else:
                    raw = message.get("text")
                    if raw is not None:
                        try:
                            command = json.loads(raw)
                        except json.JSONDecodeError:
                            await websocket.send_json(
                                next_event("error", code="invalid_json", message="control messages must be JSON")
                            )
                        else:
                            command_type = command.get("type")
                            if command_type == "commit" and not client_committed:
                                client_committed = True
                                if upstream is None:
                                    await websocket.send_json(
                                        next_event(
                                            "transcript.final",
                                            text="",
                                            segment_index=segment_index,
                                            reason="commit",
                                            session_complete=True,
                                        )
                                    )
                                    break
                                if not upstream_committing:
                                    upstream_committing = True
                                    await upstream.commit()
                            elif command_type in {"reset", "close"}:
                                break
                            elif command_type != "commit":
                                await websocket.send_json(
                                    next_event(
                                        "error",
                                        code="unknown_command",
                                        message="supported commands: commit, reset, close",
                                    )
                                )
                if not client_committed:
                    browser_task = asyncio.create_task(websocket.receive(), name=f"stt-browser-{session_id}")

            if upstream_task in done:
                try:
                    event = upstream_task.result()
                except Exception as exc:
                    await websocket.send_json(
                        next_event("error", code="upstream_failed", message=f"model stream failed: {exc}")
                    )
                    break
                upstream_task = None
                event_type = event.get("type")
                if event_type == "transcription.delta":
                    partial += str(event.get("delta", ""))
                    await websocket.send_json(
                        next_event(
                            "transcript.partial",
                            text=normalize_transcript(partial, output_script),
                            segment_index=segment_index,
                        )
                    )
                elif event_type == "transcription.partial":
                    partial = str(event.get("text", ""))
                    await websocket.send_json(
                        next_event(
                            "transcript.partial",
                            text=normalize_transcript(partial, output_script),
                            stable_text=normalize_transcript(str(event.get("stable_text", "")), output_script),
                            unstable_text=normalize_transcript(str(event.get("unstable_text", partial)), output_script),
                            stability=event.get("stability"),
                            language=event.get("language"),
                            segment_index=segment_index,
                        )
                    )
                elif event_type == "transcription.done":
                    await websocket.send_json(
                        next_event(
                            "transcript.final",
                            text=normalize_transcript(str(event.get("text", partial)).strip(), output_script),
                            language=event.get("language"),
                            usage=event.get("usage"),
                            segment_index=segment_index,
                            reason="commit" if client_committed else "silence",
                            session_complete=client_committed,
                        )
                    )
                    segment_index += 1
                    await upstream.close()
                    upstream = None
                    if client_committed:
                        break
                    upstream_committing = False
                    speech_started = False
                    silence_seconds = 0.0
                    partial = ""
                    leading_silence_gate = LeadingSilenceGate() if trim_leading_silence else None
                    buffered = pending_rollover[:]
                    pending_rollover.clear()
                    for buffered_chunk in buffered:
                        if not await process_pcm(buffered_chunk):
                            should_close = True
                            break
                elif event_type == "error":
                    await websocket.send_json(
                        next_event(
                            "error",
                            code=str(event.get("code") or "upstream_error"),
                            message=str(event.get("error")),
                        )
                    )
                    break
                if upstream is not None and upstream_task is None:
                    upstream_task = asyncio.create_task(upstream.receive(), name=f"stt-upstream-{session_id}")
    except WebSocketDisconnect:
        pass
    finally:
        for task in (browser_task, upstream_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        if upstream is not None:
            await upstream.close()
        with suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=1000)
