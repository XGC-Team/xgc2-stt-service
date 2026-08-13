from __future__ import annotations

import json
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
