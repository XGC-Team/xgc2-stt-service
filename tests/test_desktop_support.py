from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from xgc2_stt.desktop_support import (
    DesktopSettings,
    load_desktop_settings,
    replacement_plan,
    save_desktop_settings,
    should_auto_enter,
    streaming_headers,
    streaming_url,
)


def test_streaming_url_targets_api_and_keeps_credentials_out_of_the_url() -> None:
    settings = DesktopSettings(
        endpoint="https://stt.example.test/base?access_token=legacy-secret&tenant=example",
        api_key="a key/+",
        trim_leading_silence=False,
    )
    assert streaming_url(settings) == (
        "wss://stt.example.test/v1/audio/transcriptions/stream?"
        "tenant=example&sample_rate=16000&output_script=simplified&trim_leading_silence=0&silence_commit_ms=2000"
    )
    assert streaming_headers(settings) == {"Authorization": "Bearer a key/+"}
    assert streaming_headers(DesktopSettings(endpoint="https://stt.example.test")) == {}


def test_replacement_plan_rewrites_only_the_changed_tail() -> None:
    assert replacement_plan("你为什么把我的中文", "你为什么把我的中文转录成英文") == (0, "转录成英文")
    assert replacement_plan("识别成为英文", "识别成了中文") == (3, "了中文")


def test_desktop_settings_are_private_and_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "config" / "client.json"
    expected = DesktopSettings(endpoint="https://stt.example.com", api_key="secret", start_at_login=True)
    save_desktop_settings(expected, target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["api_key"] == "secret"
    assert load_desktop_settings(target) == expected


def test_auto_enter_is_off_by_default() -> None:
    settings = DesktopSettings()

    assert settings.auto_enter is False
    assert settings.endpoint == ""
    assert settings.hotkey == "<f9>"
    assert settings.silence_commit_ms == 2000


def test_auto_enter_only_submits_silence_final() -> None:
    assert should_auto_enter(True, "silence") is True
    assert should_auto_enter(True, "commit") is False
    assert should_auto_enter(False, "silence") is False


def test_pre_schema_desktop_settings_are_rejected_without_migration(tmp_path: Path) -> None:
    target = tmp_path / "client.json"
    target.write_text('{"hotkey":"<ctrl>+<alt>+space"}', encoding="utf-8")

    assert load_desktop_settings(target) == DesktopSettings()


@pytest.mark.parametrize("hotkey", ["<ctrl>+<shift>+r", "<ctrl>+<shift>+s"])
def test_explicit_schema_hotkey_is_not_rewritten(tmp_path: Path, hotkey: str) -> None:
    target = tmp_path / "client.json"
    target.write_text(
        json.dumps({"schema_version": 1, "hotkey": hotkey}),
        encoding="utf-8",
    )

    assert load_desktop_settings(target).hotkey == hotkey


def test_autostart_is_written_and_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    from xgc2_stt.desktop_support import autostart_path, set_autostart

    target = set_autostart(True, ["xgc2-stt-client"])
    assert target == autostart_path()
    text = target.read_text(encoding="utf-8")
    assert "Exec=xgc2-stt-client" in text
    assert "X-GNOME-Autostart-enabled=true" in text
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    set_autostart(False)
    assert not target.exists()


def test_cli_version_and_toggle_flags() -> None:
    from xgc2_stt.desktop_support import format_desktop_version, parse_desktop_cli

    args = parse_desktop_cli(["--version"])
    assert args.version is True
    assert format_desktop_version().startswith("xgc2-stt-client ")
    toggle = parse_desktop_cli(["--toggle-capture"])
    assert toggle.toggle_capture is True
    settings = parse_desktop_cli(["--settings"])
    assert settings.settings is True


def test_version_cli_does_not_import_tray_backends(capsys: pytest.CaptureFixture[str]) -> None:
    import sys

    sys.modules.pop("xgc2_stt.desktop", None)
    from xgc2_stt.desktop_support import run_desktop_cli

    assert run_desktop_cli(["--version"]) == 0
    assert capsys.readouterr().out.startswith("xgc2-stt-client ")
    assert "xgc2_stt.desktop" not in sys.modules


def test_cli_help_exits_zero() -> None:
    from xgc2_stt.desktop_support import parse_desktop_cli

    with pytest.raises(SystemExit) as caught:
        parse_desktop_cli(["--help"])
    assert caught.value.code == 0


def test_session_type_prefers_xdg(monkeypatch: pytest.MonkeyPatch) -> None:
    from xgc2_stt.desktop_support import apply_qt_platform, desktop_session_type, is_wayland_session

    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    assert desktop_session_type() == "wayland"
    assert is_wayland_session() is True
    apply_qt_platform()
    assert os.environ["QT_QPA_PLATFORM"] == "wayland;xcb"


def test_insert_uses_xdotool_on_x11() -> None:
    from xgc2_stt.desktop_support import insert_finalized_text

    calls: list[list[str]] = []

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"xclip", "xdotool"} else None

    def run(command: list[str], **_kwargs: object) -> object:
        calls.append(command)

        class Result:
            returncode = 0

        return Result()

    copied: list[str] = []
    outcome = insert_finalized_text(
        "你好",
        paste_shortcut="desktop",
        session_type="x11",
        set_clipboard=copied.append,
        which=which,
        run=run,
    )
    assert copied == ["你好"]
    assert outcome.pasted is True
    assert outcome.method == "xdotool"
    assert any(command[:2] == ["/usr/bin/xdotool", "key"] for command in calls)


def test_insert_wayland_falls_back_to_clipboard() -> None:
    from xgc2_stt.desktop_support import insert_finalized_text

    def which(name: str) -> str | None:
        return "/usr/bin/wl-copy" if name == "wl-copy" else None

    def run(command: list[str], **_kwargs: object) -> object:
        class Result:
            returncode = 0

        return Result()

    outcome = insert_finalized_text(
        "定稿",
        paste_shortcut="desktop",
        session_type="wayland",
        set_clipboard=lambda _text: None,
        which=which,
        run=run,
    )
    assert outcome.copied is True
    assert outcome.pasted is False
    assert outcome.method == "clipboard"


def test_insert_wayland_prefers_wtype() -> None:
    from xgc2_stt.desktop_support import insert_finalized_text

    calls: list[list[str]] = []

    def which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in {"wl-copy", "wtype"} else None

    def run(command: list[str], **_kwargs: object) -> object:
        calls.append(command)

        class Result:
            returncode = 0

        return Result()

    outcome = insert_finalized_text(
        "hello",
        paste_shortcut="desktop",
        session_type="wayland",
        set_clipboard=lambda _text: None,
        which=which,
        run=run,
    )
    assert outcome.method == "wtype"
    assert outcome.pasted is True
    assert any(command[0] == "/usr/bin/wtype" for command in calls)


def test_ipc_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from xgc2_stt.desktop_support import DesktopIpcListener, send_running_instance

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    received: list[str] = []
    ready = threading.Event()

    def handler(command: str) -> None:
        received.append(command)
        ready.set()

    listener = DesktopIpcListener(handler)
    listener.start()
    try:
        assert send_running_instance("toggle") is True
        assert ready.wait(timeout=2)
        assert received == ["toggle"]
        assert send_running_instance("ping") is True
    finally:
        listener.stop()
    assert send_running_instance("toggle") is False
