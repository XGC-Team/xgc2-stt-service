from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from xgc2_stt.desktop_support import DesktopSettings


@pytest.mark.parametrize("steering", ["stop", "superseded", "shutdown", "connected"])
def test_delayed_connection_callback_cannot_reopen_capture(
    monkeypatch: pytest.MonkeyPatch, steering: str,
) -> None:
    gi = ModuleType("gi")
    gi.require_version = lambda *_args: None
    repository = ModuleType("gi.repository")
    repository.Gdk = repository.Gio = repository.GLib = SimpleNamespace()
    repository.Gtk = SimpleNamespace(Dialog=object, Window=object, Application=object)
    xlib = ModuleType("Xlib")
    xlib.XK = xlib.X = xlib.display = xlib.error = SimpleNamespace()
    monkeypatch.setitem(sys.modules, "gi", gi)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    monkeypatch.setitem(sys.modules, "Xlib", xlib)
    path = Path(__file__).parents[1] / "src/xgc2_stt/desktop.py"
    spec = importlib.util.spec_from_file_location("xgc2_stt._controller_fixture", path)
    assert spec is not None and spec.loader is not None
    desktop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(desktop)
    started: list[object] = []

    class Capture:
        def __init__(self, *_args):
            pass

        def start(self):
            started.append(self)

    class Worker:
        def __init__(self, *_args, **callbacks):
            self.callbacks = callbacks
            self.committed = False

        def start(self):
            pass

        def commit(self):
            self.committed = True

        def feed(self, _pcm):
            pass

        def fail(self, _message):
            pass

    desktop.AudioCapture, desktop.StreamingWorker = Capture, Worker
    controller = desktop.ClientController.__new__(desktop.ClientController)
    controller.settings = DesktopSettings(endpoint="https://stt.example")
    controller.state, controller._shutting_down = "idle", False
    controller.worker = controller.capture = None
    controller._streams = object()
    controller._sync_tray = lambda: None
    controller.overlay = SimpleNamespace(set_state=lambda *_args: None, show_bottom_center=lambda: None)
    controller.injector = SimpleNamespace(begin=lambda *_args: None)
    controller.start_recording()
    worker = controller.worker
    if steering == "stop":
        controller.stop_recording()
        assert worker.committed and controller.state == "finalizing"
    elif steering == "superseded":
        controller.worker = object()
    elif steering == "shutdown":
        controller._shutting_down = True
    worker.callbacks["on_connected"]()
    assert len(started) == (1 if steering == "connected" else 0)
    if steering == "stop":
        assert controller.state == "finalizing"
