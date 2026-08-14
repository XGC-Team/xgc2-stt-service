from __future__ import annotations

import os
import subprocess
import sys
import types


def _headless_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("DISPLAY", None)
    environment.pop("WAYLAND_DISPLAY", None)
    environment["PYTHONPATH"] = "src"
    return environment


def test_version_is_headless_and_does_not_import_desktop_backends() -> None:
    script = """
import sys
from xgc2_stt.desktop_cli import main
raise SystemExit(main([\"--version\"]) if \"xgc2_stt.desktop\" not in sys.modules else 91)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=_headless_environment(),
    )
    assert result.returncode == 0
    assert result.stdout.startswith("xgc2-stt-client ")
    assert result.stderr == ""


def test_help_is_headless_and_does_not_import_desktop_backends() -> None:
    script = """
import sys
from xgc2_stt.desktop_cli import main
try:
    main([\"--help\"])
except SystemExit as exc:
    if exc.code != 0:
        raise
raise SystemExit(0 if \"xgc2_stt.desktop\" not in sys.modules else 91)
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=_headless_environment(),
    )
    assert result.returncode == 0
    assert "--toggle-capture" in result.stdout


def test_gui_backend_is_lazy_imported_only_for_desktop_start(monkeypatch) -> None:
    from xgc2_stt import desktop_cli as cli

    monkeypatch.setattr(cli, "send_running_instance", lambda _command: False)
    calls: list[dict[str, bool]] = []
    fake_desktop = types.ModuleType("xgc2_stt.desktop")
    fake_desktop.run_desktop = lambda **options: calls.append(options) or 17
    monkeypatch.setitem(sys.modules, "xgc2_stt.desktop", fake_desktop)

    assert cli.main([]) == 17
    assert calls == [{"start_capture": False, "open_settings": False}]


def test_ipc_control_commands_do_not_import_gui(monkeypatch) -> None:
    from xgc2_stt import desktop_cli as cli

    monkeypatch.delitem(sys.modules, "xgc2_stt.desktop", raising=False)
    for arguments, expected in (
        ([], "activate"),
        (["--toggle-capture"], "toggle"),
        (["--settings"], "settings"),
    ):
        calls: list[str] = []

        def send(command: str, *, sink: list[str] = calls) -> bool:
            sink.append(command)
            return True

        monkeypatch.setattr(cli, "send_running_instance", send)
        assert cli.main(arguments) == 0
        assert calls == [expected]
        assert "xgc2_stt.desktop" not in sys.modules
