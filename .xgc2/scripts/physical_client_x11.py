#!/usr/bin/env python3
"""Exercise a frozen desktop client against real X11 and microphone hardware."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve

AUDIO_BYTES_MINIMUM = 16_000
HOTKEY = "F10"
MAPS_DENYLIST = (
    "ffmpeg",
    "gstreamer",
    "qtmultimedia",
    "qt6multimedia",
    "virtualkeyboard",
    "webengine",
    "libavcodec",
    "libavdevice",
    "libavfilter",
    "libavformat",
    "libavutil",
    "libswresample",
    "libswscale",
)


class GateFailure(RuntimeError):
    """A physical release-gate requirement was not met."""


@dataclass
class SessionState:
    connected: bool = False
    binary_frames: int = 0
    audio_bytes: int = 0
    committed: bool = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a packaged XGC2 STT client on a physical X11 workstation.")
    parser.add_argument("binary", metavar="BINARY", type=Path)
    return parser.parse_args()


def require_machine(binary: Path) -> tuple[Path, str]:
    candidate = binary.expanduser().resolve()
    if not candidate.is_file():
        raise GateFailure(f"client binary does not exist: {candidate}")
    if not os.access(candidate, os.X_OK):
        raise GateFailure(f"client binary is not executable: {candidate}")
    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "x11":
        raise GateFailure("XDG_SESSION_TYPE must be x11")
    if not os.environ.get("DISPLAY"):
        raise GateFailure("DISPLAY is not set")

    xdotool = shutil.which("xdotool")
    if xdotool is None:
        raise GateFailure("xdotool is required")
    display_probe = subprocess.run(
        [xdotool, "getmouselocation", "--shell"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
    )
    if display_probe.returncode != 0:
        detail = display_probe.stderr.strip() or f"exit {display_probe.returncode}"
        raise GateFailure(f"xdotool cannot access the X11 display: {detail}")

    try:
        import sounddevice

        device = sounddevice.query_devices(kind="input")
        sounddevice.check_input_settings(device=None, channels=1, dtype="int16", samplerate=16_000)
    except Exception as exc:
        raise GateFailure(f"default microphone is unavailable at 16 kHz mono int16: {exc}") from exc
    if int(device["max_input_channels"]) < 1:
        raise GateFailure("default microphone exposes no input channel")
    return candidate, xdotool


def write_config(config_home: Path, port: int) -> None:
    target = config_home / "xgc2-stt" / "client.json"
    target.parent.mkdir(mode=0o700, parents=True)
    target.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "endpoint": f"http://127.0.0.1:{port}",
                "api_key": "",
                "hotkey": "<f10>",
                "output_script": "simplified",
                "trim_leading_silence": True,
                "silence_commit_ms": 2000,
                "paste_shortcut": "terminal",
                "auto_enter": False,
                "start_at_login": False,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)


def press_hotkey(xdotool: str) -> None:
    result = subprocess.run(
        [xdotool, "key", "--clearmodifiers", HOTKEY],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit {result.returncode}"
        raise GateFailure(f"could not send {HOTKEY}: {detail}")


def inspect_maps(pid: int) -> None:
    try:
        maps = Path(f"/proc/{pid}/maps").read_text(encoding="utf-8", errors="replace").lower()
    except OSError as exc:
        raise GateFailure(f"could not inspect client process maps: {exc}") from exc
    forbidden = sorted(needle for needle in MAPS_DENYLIST if needle in maps)
    if forbidden:
        raise GateFailure(f"forbidden runtime libraries are mapped: {', '.join(forbidden)}")
    if "libportaudio" not in maps:
        raise GateFailure("client captured audio without mapping libportaudio")


async def terminate_process(process: asyncio.subprocess.Process) -> tuple[str, str]:
    if process.returncode is None:
        process.send_signal(signal.SIGTERM)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
    except TimeoutError:
        process.kill()
        stdout, stderr = await process.communicate()
    return stdout.decode(errors="replace"), stderr.decode(errors="replace")


async def run_gate(binary: Path, xdotool: str) -> SessionState:
    state = SessionState()
    audio_ready = asyncio.Event()
    commit_ready = asyncio.Event()

    async def handler(socket: Any) -> None:
        state.connected = True
        await socket.send(json.dumps({"type": "session.started"}))
        async for message in socket:
            if isinstance(message, bytes):
                state.binary_frames += 1
                state.audio_bytes += len(message)
                if state.audio_bytes >= AUDIO_BYTES_MINIMUM:
                    audio_ready.set()
                continue
            event = json.loads(message)
            if event.get("type") != "commit":
                continue
            state.committed = True
            await socket.send(
                json.dumps(
                    {
                        "type": "transcript.final",
                        "text": "",
                        "reason": "commit",
                        "session_complete": True,
                    }
                )
            )
            commit_ready.set()
            return

    async with serve(handler, "127.0.0.1", 0) as server:
        if not server.sockets:
            raise GateFailure("local WebSocket server did not expose a listener")
        port = int(server.sockets[0].getsockname()[1])
        with tempfile.TemporaryDirectory(prefix="xgc2-stt-physical-gate-") as temporary:
            config_home = Path(temporary)
            write_config(config_home, port)
            environment = os.environ.copy()
            environment.update(
                {
                    "XDG_CONFIG_HOME": str(config_home),
                    "XDG_SESSION_TYPE": "x11",
                    "QT_QPA_PLATFORM": "xcb",
                }
            )
            process = await asyncio.create_subprocess_exec(
                str(binary),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            stdout = ""
            stderr = ""
            try:
                await asyncio.sleep(2)
                if process.returncode is not None:
                    stdout, stderr = await process.communicate()
                    raise GateFailure(
                        f"client exited before {HOTKEY} (exit {process.returncode}): "
                        f"{stderr.decode(errors='replace').strip() or stdout.decode(errors='replace').strip()}"
                    )
                press_hotkey(xdotool)
                try:
                    await asyncio.wait_for(audio_ready.wait(), timeout=10)
                except TimeoutError as exc:
                    raise GateFailure(f"client delivered only {state.audio_bytes} audio bytes in 10 seconds") from exc
                inspect_maps(process.pid)
                press_hotkey(xdotool)
                try:
                    await asyncio.wait_for(commit_ready.wait(), timeout=8)
                except TimeoutError as exc:
                    raise GateFailure("client did not commit within 8 seconds after the second F10") from exc
            finally:
                if process.returncode is None:
                    stdout, stderr = await terminate_process(process)
            if process.returncode is not None and process.returncode not in {0, -signal.SIGTERM}:
                detail = stderr.strip() or stdout.strip()
                raise GateFailure(f"client exited unexpectedly ({process.returncode}): {detail}")

    if not state.connected:
        raise GateFailure("client never connected to the local WebSocket server")
    if state.audio_bytes < AUDIO_BYTES_MINIMUM:
        raise GateFailure(f"client delivered only {state.audio_bytes} audio bytes")
    if not state.committed:
        raise GateFailure("client did not send commit")
    return state


def main() -> int:
    try:
        binary, xdotool = require_machine(parse_args().binary)
        state = asyncio.run(run_gate(binary, xdotool))
    except (GateFailure, OSError, subprocess.SubprocessError) as exc:
        print(f"FAIL physical-client-x11: {exc}", file=sys.stderr)
        return 1
    print(
        "PASS physical-client-x11 "
        f"frames={state.binary_frames} bytes={state.audio_bytes} commit=yes maps=clean portaudio=yes"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
