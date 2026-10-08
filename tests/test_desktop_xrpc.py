from __future__ import annotations

import threading
from pathlib import Path

import pytest
from xgc2_xrpc import Client, Fault, Runtime, TransportError

from xgc2_stt.desktop_support import DesktopIpcListener, ipc_socket_path, send_running_instance


def test_endpoint_collision_preserves_first_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    listener = DesktopIpcListener(lambda _: None)
    listener.start()
    before = listener.path.stat().st_ino
    try:
        with pytest.raises(FileExistsError):
            DesktopIpcListener(lambda _: None).start()
        assert listener.path.stat().st_ino == before
        assert send_running_instance("ping")
    finally:
        listener.stop()
    assert not listener.path.exists()


def test_endpoint_never_removes_regular_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = ipc_socket_path()
    path.parent.mkdir(mode=0o700)
    path.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError):
        DesktopIpcListener(lambda _: None).start()
    assert path.read_text(encoding="utf-8") == "keep"


def test_commands_reject_wrong_instance_and_unknown_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    received: list[str] = []
    listener = DesktopIpcListener(received.append)
    listener.start()
    try:
        with Runtime() as runtime:
            with Client(str(listener.path), runtime=runtime, instance_id="stale") as client:
                with pytest.raises(TransportError, match="instance") as error:
                    client.json("/v1/command", {"command": "toggle"})
                assert error.value.disposition == "outcome_unknown"
            with Client(str(listener.path), runtime=runtime, instance_id=listener.instance_id) as client:
                for payload in ({"command": "stop"}, {"command": []}, {"command": "toggle", "extra": 1}):
                    with pytest.raises(Fault) as error:
                        client.json("/v1/command", payload)
                    assert error.value.code == "invalid_argument"
        assert received == []
    finally:
        listener.stop()


def test_timed_out_command_keeps_lease_until_work_ends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    entered, release = threading.Event(), threading.Event()

    def command(_: str) -> None:
        entered.set()
        release.wait(5)

    listener = DesktopIpcListener(command)
    listener.start()
    try:
        assert not send_running_instance("toggle", timeout=0.05)
        assert entered.wait(1)
        with pytest.raises(RuntimeError, match="quiesced"):
            listener.stop()
        assert listener.path.exists()
        with pytest.raises(FileExistsError):
            DesktopIpcListener(lambda _: None).start()
    finally:
        release.set()
        listener.stop()
    assert not listener.path.exists()
