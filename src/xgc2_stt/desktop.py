from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading
from contextlib import suppress
from typing import Any

from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import GlobalHotKeys, Key
from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from websockets.sync.client import connect

from .desktop_support import (
    DesktopSettings,
    load_desktop_settings,
    replacement_plan,
    save_desktop_settings,
    set_autostart,
    should_auto_enter,
    streaming_url,
)

_COMMIT = object()
_CANCEL = object()


class StreamSignals(QObject):
    connected = Signal()
    hypothesis = Signal(str)
    state = Signal(str)
    segment_completed = Signal(str)
    failed = Signal(str)
    completed = Signal()


class StreamingWorker:
    def __init__(self, settings: DesktopSettings):
        self.settings = settings
        self.signals = StreamSignals()
        self._outgoing: queue.Queue[bytes | object] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name="xgc2-stt-stream", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def feed(self, pcm: bytes) -> None:
        if pcm:
            self._outgoing.put(pcm)

    def commit(self) -> None:
        self._outgoing.put(_COMMIT)

    def cancel(self) -> None:
        self._outgoing.put(_CANCEL)

    def _run(self) -> None:
        final_received = False
        try:
            with connect(
                streaming_url(self.settings),
                open_timeout=10,
                close_timeout=5,
                max_size=4 * 1024 * 1024,
            ) as socket:
                first = json.loads(socket.recv(timeout=15))
                if first.get("type") != "session.started":
                    raise RuntimeError(first.get("message") or "服务未创建识别会话")
                self.signals.connected.emit()
                while True:
                    try:
                        outgoing = self._outgoing.get(timeout=0.01)
                    except queue.Empty:
                        outgoing = None
                    if outgoing is _CANCEL:
                        socket.send(json.dumps({"type": "close"}))
                        return
                    if outgoing is _COMMIT:
                        socket.send(json.dumps({"type": "commit"}))
                        self.signals.state.emit("收尾")
                    elif isinstance(outgoing, bytes):
                        socket.send(outgoing)
                    try:
                        raw = socket.recv(timeout=0.01)
                    except TimeoutError:
                        continue
                    event = json.loads(raw)
                    event_type = event.get("type")
                    if event_type == "transcript.partial":
                        self.signals.hypothesis.emit(str(event.get("text") or ""))
                    elif event_type == "transcript.final":
                        final_text = str(event.get("text") or "")
                        if final_text:
                            self.signals.hypothesis.emit(final_text)
                        self.signals.segment_completed.emit(str(event.get("reason") or "commit"))
                        if event.get("session_complete", True):
                            final_received = True
                            return
                        self.signals.state.emit("录音中")
                    elif event_type == "error":
                        raise RuntimeError(str(event.get("message") or event.get("code") or "识别失败"))
        except Exception as exc:
            self.signals.failed.emit(str(exc))
        finally:
            if final_received:
                self.signals.completed.emit()


class AudioCapture(QObject):
    failed = Signal(str)

    def __init__(self, on_pcm: Any, parent: QObject | None = None):
        super().__init__(parent)
        self.on_pcm = on_pcm
        self.source: QAudioSource | None = None
        self.device: Any = None

    def start(self) -> None:
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise RuntimeError("没有可用的麦克风")
        audio_format = QAudioFormat()
        audio_format.setSampleRate(16000)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(audio_format):
            raise RuntimeError("当前音频设备不支持 16 kHz 单声道 PCM16")
        self.source = QAudioSource(device, audio_format, self)
        self.source.setBufferSize(16000)
        self.device = self.source.start()
        if self.device is None:
            self.source = None
            raise RuntimeError("无法启动麦克风")
        self.device.readyRead.connect(self._read)

    @Slot()
    def _read(self) -> None:
        if self.device is None:
            return
        data = bytes(self.device.readAll())
        if data:
            self.on_pcm(data)

    def stop(self) -> None:
        if self.source is not None:
            self.source.stop()
            self.source.deleteLater()
        self.source = None
        self.device = None


class FocusTracker:
    def __init__(self):
        self._display: Any = None
        if os.environ.get("XDG_SESSION_TYPE", "x11").lower() == "wayland":
            return
        with suppress(Exception):
            from Xlib import display

            self._display = display.Display()

    def current(self) -> int | None:
        if self._display is None:
            return None
        with suppress(Exception):
            self._display.sync()
            focus = self._display.get_input_focus().focus
            return int(getattr(focus, "id", focus))
        return None


class TextInjector:
    def __init__(self, application: QApplication):
        self.application = application
        self.keyboard = KeyboardController()
        self.focus = FocusTracker()
        self.target_focus: int | None = None
        self.hypothesis = ""
        self.original_clipboard = ""
        self.last_clipboard = ""
        self.shortcut = "terminal"

    def begin(self, shortcut: str) -> None:
        self.target_focus = self.focus.current()
        self.hypothesis = ""
        self.shortcut = shortcut
        self.original_clipboard = self.application.clipboard().text()
        self.last_clipboard = ""

    def sync(self, hypothesis: str) -> bool:
        current_focus = self.focus.current()
        if self.target_focus is not None and current_focus != self.target_focus:
            return False
        backspaces, suffix = replacement_plan(self.hypothesis, hypothesis)
        for _ in range(backspaces):
            self.keyboard.press(Key.backspace)
            self.keyboard.release(Key.backspace)
        if suffix:
            self._paste(suffix)
        self.hypothesis = hypothesis
        return True

    def _paste(self, text: str) -> None:
        clipboard = self.application.clipboard()
        clipboard.setText(text)
        self.last_clipboard = text
        keys = [Key.ctrl, Key.shift] if self.shortcut == "terminal" else [Key.ctrl]
        for key in keys:
            self.keyboard.press(key)
        self.keyboard.press("v")
        self.keyboard.release("v")
        for key in reversed(keys):
            self.keyboard.release(key)

    def end(self) -> None:
        clipboard = self.application.clipboard()
        if self.last_clipboard and clipboard.text() == self.last_clipboard:
            clipboard.setText(self.original_clipboard)
        self.target_focus = None
        self.hypothesis = ""
        self.last_clipboard = ""

    def commit_segment(self, *, auto_enter: bool = False) -> bool:
        has_text = bool(self.hypothesis)
        current_focus = self.focus.current()
        focus_matches = self.target_focus is None or current_focus == self.target_focus
        if auto_enter and has_text and focus_matches:
            self.keyboard.press(Key.enter)
            self.keyboard.release(Key.enter)
        self.hypothesis = ""
        return focus_matches


class SettingsDialog(QDialog):
    def __init__(self, settings: DesktopSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("XGC2 STT 设置")
        self.setModal(True)
        self.endpoint = QLineEdit(settings.endpoint)
        self.api_key = QLineEdit(settings.api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.hotkey = QLineEdit(settings.hotkey)
        self.output_script = QComboBox()
        self.output_script.addItem("简体中文", "simplified")
        self.output_script.addItem("模型原样", "original")
        self.output_script.setCurrentIndex(max(0, self.output_script.findData(settings.output_script)))
        self.trim_silence = QCheckBox()
        self.trim_silence.setChecked(settings.trim_leading_silence)
        self.paste_shortcut = QComboBox()
        self.paste_shortcut.addItem("终端 Ctrl+Shift+V", "terminal")
        self.paste_shortcut.addItem("桌面 Ctrl+V", "desktop")
        self.paste_shortcut.setCurrentIndex(max(0, self.paste_shortcut.findData(settings.paste_shortcut)))
        self.auto_enter = QCheckBox()
        self.auto_enter.setChecked(settings.auto_enter)
        self.start_at_login = QCheckBox()
        self.start_at_login.setChecked(settings.start_at_login)

        form = QFormLayout()
        form.addRow("服务器", self.endpoint)
        form.addRow("API Key", self.api_key)
        form.addRow("快捷键", self.hotkey)
        form.addRow("中文输出", self.output_script)
        form.addRow("开头静音", self.trim_silence)
        form.addRow("粘贴方式", self.paste_shortcut)
        form.addRow("自动回车", self.auto_enter)
        form.addRow("开机自启", self.start_at_login)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存")
        save.clicked.connect(self.accept)
        actions = QHBoxLayout()
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(actions)

    def values(self) -> DesktopSettings:
        return DesktopSettings(
            endpoint=self.endpoint.text().strip(),
            api_key=self.api_key.text().strip(),
            hotkey=self.hotkey.text().strip(),
            output_script=str(self.output_script.currentData()),
            trim_leading_silence=self.trim_silence.isChecked(),
            paste_shortcut=str(self.paste_shortcut.currentData()),
            auto_enter=self.auto_enter.isChecked(),
            start_at_login=self.start_at_login.isChecked(),
        )


class Overlay(QWidget):
    settings_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(154, 38)
        self.dot = QLabel("●")
        self.label = QLabel("待机")
        self.gear = QToolButton()
        self.gear.setText("⚙")
        self.gear.setToolTip("设置")
        self.gear.clicked.connect(self.settings_requested)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 6, 4)
        layout.setSpacing(8)
        layout.addWidget(self.dot)
        layout.addWidget(self.label, 1)
        layout.addWidget(self.gear)
        self.setStyleSheet(
            "QWidget { background:#181e27; color:#cdd6e2; border:1px solid #303a46; border-radius:6px; }"
            "QLabel { border:0; } QToolButton { border:0; background:transparent; padding:4px; }"
            "QToolButton:hover { background:#293542; }"
        )
        self.set_state("待机", "#79b88c")

    def show_top_right(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.right() - self.width() - 16, area.top() + 16)
        self.show()

    def set_state(self, label: str, color: str) -> None:
        self.label.setText(label)
        self.dot.setStyleSheet(f"color:{color}; border:0;")

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()


class ClientController(QObject):
    toggle_requested = Signal()

    def __init__(self, application: QApplication):
        super().__init__()
        self.application = application
        self.settings = load_desktop_settings()
        self.state = "idle"
        self.worker: StreamingWorker | None = None
        self.capture: AudioCapture | None = None
        self.injector = TextInjector(application)
        self.overlay = Overlay()
        self.overlay.settings_requested.connect(self.open_settings)
        self.toggle_requested.connect(self.toggle)
        self.hotkeys: GlobalHotKeys | None = None
        self._shutting_down = False
        self._start_hotkey()

        icon = application.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        self.tray = QSystemTrayIcon(icon, application)
        menu = QMenu()
        settings_action = QAction("设置", menu)
        settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("退出", menu)
        quit_action.triggered.connect(self.shutdown)
        menu.addAction(settings_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self.toggle() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()
        self.overlay.show_top_right()

    def _start_hotkey(self) -> None:
        if self.hotkeys is not None:
            self.hotkeys.stop()
        try:
            self.hotkeys = GlobalHotKeys({self.settings.hotkey: self.toggle_requested.emit})
            self.hotkeys.start()
        except Exception as exc:
            self.hotkeys = None
            message = f"快捷键不可用: {exc}"
            QTimer.singleShot(0, lambda: self._show_error(message))

    @Slot()
    def toggle(self) -> None:
        if self.state == "idle":
            self.start_recording()
        elif self.state in {"connecting", "recording"}:
            self.stop_recording()

    def start_recording(self) -> None:
        try:
            streaming_url(self.settings)
        except ValueError as exc:
            self._show_error(str(exc))
            return
        self.state = "connecting"
        self.overlay.set_state("连接中", "#d7ae66")
        self.overlay.gear.setEnabled(False)
        self.injector.begin(self.settings.paste_shortcut)
        worker = StreamingWorker(self.settings)
        self.worker = worker
        worker.signals.connected.connect(self._start_audio)
        worker.signals.hypothesis.connect(self._inject_hypothesis)
        worker.signals.segment_completed.connect(self._commit_segment)
        worker.signals.state.connect(lambda state: self.overlay.set_state(state, "#d7ae66"))
        worker.signals.failed.connect(self._stream_failed)
        worker.signals.completed.connect(self._stream_completed)
        worker.start()

    @Slot()
    def _start_audio(self) -> None:
        if self.worker is None:
            return
        try:
            self.capture = AudioCapture(self.worker.feed, self)
            self.capture.start()
        except Exception as exc:
            self.worker.cancel()
            self._stream_failed(str(exc))
            return
        self.state = "recording"
        self.overlay.set_state("录音中", "#df8589")

    def stop_recording(self) -> None:
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        self.state = "finalizing"
        self.overlay.set_state("收尾", "#d7ae66")
        self.worker.commit() if self.worker is not None else self._finish_session()

    @Slot(str)
    def _inject_hypothesis(self, text: str) -> None:
        if text and not self.injector.sync(text):
            self.overlay.set_state("焦点已变", "#d7ae66")

    @Slot(str)
    def _commit_segment(self, reason: str) -> None:
        auto_enter = should_auto_enter(self.settings.auto_enter, reason)
        if not self.injector.commit_segment(auto_enter=auto_enter):
            self.overlay.set_state("焦点已变", "#d7ae66")

    @Slot()
    def _stream_completed(self) -> None:
        self._finish_session()

    @Slot(str)
    def _stream_failed(self, message: str) -> None:
        self._finish_session()
        self._show_error(message)

    def _finish_session(self) -> None:
        if self.capture is not None:
            self.capture.stop()
        self.capture = None
        self.worker = None
        self.injector.end()
        self.state = "idle"
        self.overlay.gear.setEnabled(True)
        self.overlay.set_state("待机", "#79b88c")

    @Slot()
    def open_settings(self) -> None:
        if self.state != "idle":
            return
        dialog = SettingsDialog(self.settings, self.overlay)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        candidate = dialog.values()
        try:
            streaming_url(candidate)
            GlobalHotKeys({candidate.hotkey: lambda: None})
        except Exception as exc:
            QMessageBox.critical(self.overlay, "设置无效", str(exc))
            return
        self.settings = candidate
        save_desktop_settings(candidate)
        command = [shutil.which("xgc2-stt-client") or sys.executable]
        if command[0] == sys.executable:
            command.extend(["-m", "xgc2_stt.desktop"])
        set_autostart(candidate.start_at_login, command)
        self._start_hotkey()

    def _show_error(self, message: str) -> None:
        self.overlay.set_state("错误", "#df8589")
        self.tray.showMessage("XGC2 STT", message, QSystemTrayIcon.MessageIcon.Critical, 5000)
        QTimer.singleShot(2500, lambda: self.overlay.set_state("待机", "#79b88c") if self.state == "idle" else None)

    @Slot()
    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self.capture is not None:
            self.capture.stop()
        if self.worker is not None:
            self.worker.cancel()
        if self.hotkeys is not None:
            self.hotkeys.stop()
        self.injector.end()
        self.tray.hide()
        self.application.quit()


def main() -> int:
    application = QApplication(sys.argv)
    application.setApplicationName("XGC2 STT Client")
    application.setQuitOnLastWindowClosed(False)
    controller = ClientController(application)
    application.aboutToQuit.connect(controller.shutdown)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
