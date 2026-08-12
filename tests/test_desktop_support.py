from __future__ import annotations

import json
import stat
from pathlib import Path

from xgc2_stt.desktop_support import (
    DesktopSettings,
    load_desktop_settings,
    replacement_plan,
    save_desktop_settings,
    should_auto_enter,
    streaming_url,
)


def test_streaming_url_targets_api_and_encodes_credentials() -> None:
    settings = DesktopSettings(endpoint="https://stt.example.test/base", api_key="a key/+", trim_leading_silence=False)
    assert streaming_url(settings) == (
        "wss://stt.example.test/v1/audio/transcriptions/stream?"
        "sample_rate=16000&output_script=simplified&trim_leading_silence=0&access_token=a+key%2F%2B"
    )


def test_replacement_plan_rewrites_only_the_changed_tail() -> None:
    assert replacement_plan("你为什么把我的中文", "你为什么把我的中文转录成英文") == (0, "转录成英文")
    assert replacement_plan("识别成为英文", "识别成了中文") == (3, "了中文")


def test_desktop_settings_are_private_and_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "config" / "client.json"
    expected = DesktopSettings(endpoint="http://127.0.0.1:34897", api_key="secret", start_at_login=True)
    save_desktop_settings(expected, target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert json.loads(target.read_text(encoding="utf-8"))["api_key"] == "secret"
    assert load_desktop_settings(target) == expected


def test_auto_enter_is_off_by_default() -> None:
    settings = DesktopSettings()

    assert settings.auto_enter is False
    assert settings.hotkey == "<ctrl>+<shift>+r"


def test_auto_enter_only_submits_silence_final() -> None:
    assert should_auto_enter(True, "silence") is True
    assert should_auto_enter(True, "commit") is False
    assert should_auto_enter(False, "silence") is False


def test_legacy_invalid_space_hotkey_is_migrated(tmp_path: Path) -> None:
    target = tmp_path / "client.json"
    target.write_text('{"hotkey":"<ctrl>+<alt>+space"}', encoding="utf-8")

    assert load_desktop_settings(target).hotkey == "<ctrl>+<shift>+r"
