from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

from websockets.sync.client import connect


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: verify-stream.py <http-base-url> <pcm16le-file>")
    base = urlparse(sys.argv[1])
    pcm_path = Path(sys.argv[2])
    query = "sample_rate=16000"
    api_key = os.environ.get("XGC2_STT_API_KEY", "")
    if api_key:
        query += f"&access_token={quote(api_key, safe='')}"
    uri = urlunparse(
        (
            "wss" if base.scheme == "https" else "ws",
            base.netloc,
            "/v1/audio/transcriptions/stream",
            "",
            query,
            "",
        )
    )

    partials: list[tuple[float, str]] = []
    finals: list[dict[str, object]] = []
    final = ""
    final_at = 0.0
    failure: list[BaseException] = []
    finished = threading.Event()
    with connect(uri, open_timeout=10, close_timeout=10) as socket:
        started = json.loads(socket.recv(timeout=10))
        if started.get("type") != "session.started":
            raise RuntimeError(f"unexpected first event: {started}")

        audio_started_at = time.monotonic()

        def receive_events() -> None:
            nonlocal final, final_at
            try:
                while True:
                    event = json.loads(socket.recv(timeout=120))
                    received_at = time.monotonic()
                    if event.get("type") == "transcript.partial" and event.get("text"):
                        partials.append((received_at, str(event["text"])))
                    if event.get("type") == "transcript.final":
                        finals.append(event)
                        final = str(event.get("text", ""))
                        final_at = received_at
                        if event.get("session_complete", True):
                            return
                    if event.get("type") == "error":
                        raise RuntimeError(f"stream returned an error: {event}")
            except BaseException as exc:
                failure.append(exc)
            finally:
                finished.set()

        receiver = threading.Thread(target=receive_events, name="stt-verifier-receiver", daemon=True)
        receiver.start()
        sent_audio_seconds = 0.0
        with pcm_path.open("rb") as pcm_file:
            while chunk := pcm_file.read(1280):
                socket.send(chunk)
                sent_audio_seconds += len(chunk) / (16_000 * 2)
                target = audio_started_at + sent_audio_seconds
                time.sleep(max(0.0, target - time.monotonic()))
        commit_sent_at = time.monotonic()
        socket.send(json.dumps({"type": "commit"}))
        if not finished.wait(timeout=120):
            raise TimeoutError("stream did not return a final transcript within 120 seconds")
        receiver.join(timeout=1)

    if failure:
        raise failure[0]
    if not partials:
        raise RuntimeError("stream returned no partial transcript")
    if not any("\u4e00" <= character <= "\u9fff" for character in final):
        raise RuntimeError(f"unexpected final transcript: {final!r}")
    partials_before_commit = sum(received_at <= commit_sent_at for received_at, _ in partials)
    if partials_before_commit == 0:
        raise RuntimeError("stream returned no partial transcript while audio was still being captured")
    partial_revisions = 0
    max_rewrite_characters = 0
    for (_, previous), (_, current) in zip(partials, partials[1:], strict=False):
        common_prefix = 0
        for before, after in zip(previous, current, strict=False):
            if before != after:
                break
            common_prefix += 1
        if common_prefix < len(previous):
            partial_revisions += 1
            max_rewrite_characters = max(max_rewrite_characters, len(previous) - common_prefix)
    result = {
        "audio_seconds": round(sent_audio_seconds, 3),
        "first_partial_seconds": round(partials[0][0] - audio_started_at, 3),
        "partials": len(partials),
        "partials_before_commit": partials_before_commit,
        "partial_revisions": partial_revisions,
        "max_rewrite_characters": max_rewrite_characters,
        "partial_trace": [text for _, text in partials],
        "segment_finals": len(finals),
        "final_after_commit_seconds": round(final_at - commit_sent_at, 3),
        "final": final,
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
