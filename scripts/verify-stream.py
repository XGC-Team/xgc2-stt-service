from __future__ import annotations

import json
import os
import sys
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
    api_key = os.environ.get("STT_API_KEY", "")
    if api_key:
        query += f"&access_token={quote(api_key, safe='')}"
    uri = urlunparse(("wss" if base.scheme == "https" else "ws", base.netloc, "/v1/stream", "", query, ""))

    partials: list[str] = []
    final = ""
    with connect(uri, open_timeout=10, close_timeout=10) as socket:
        started = json.loads(socket.recv(timeout=10))
        if started.get("type") != "session.started":
            raise RuntimeError(f"unexpected first event: {started}")
        with pcm_path.open("rb") as pcm_file:
            while chunk := pcm_file.read(3200):
                socket.send(chunk)
                time.sleep(0.02)
        socket.send(json.dumps({"type": "commit"}))
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            event = json.loads(socket.recv(timeout=120))
            if event.get("type") == "transcript.partial" and event.get("text"):
                partials.append(str(event["text"]))
            if event.get("type") == "transcript.final":
                final = str(event.get("text", ""))
                break

    if not partials:
        raise RuntimeError("stream returned no partial transcript")
    if not any("\u4e00" <= character <= "\u9fff" for character in final):
        raise RuntimeError(f"unexpected final transcript: {final!r}")
    print(json.dumps({"partials": len(partials), "final": final}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
