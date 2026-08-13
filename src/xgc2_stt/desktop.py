from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from contextlib import suppress
from html import escape
from typing import Any

from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import GlobalHotKeys, Key
from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QCursor, QTextCursor
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStyle,
    QSystemTrayIcon,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from websockets.sync.client import connect

from .desktop_support import (
    DesktopSettings,
    load_desktop_settings,
    save_desktop_settings,
    set_autostart,
    should_auto_enter,
    streaming_url,
)

_COMMIT = object()
_CANCEL = object()


class StreamSignals(QObject):
    connected = Signal()
    hypothesis = Signal(str, str, str)
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
                        text = str(event.get("text") or "")
                        stable = str(event.get("stable_text") or "")
                        unstable = str(event.get("unstable_text") or "")
                        if not stable and not unstable:
                            unstable = text
                        self.signals.hypothesis.emit(text, stable, unstable)
                    elif event_type == "transcript.final":
                        final_text = str(event.get("text") or "")
                        if final_text:
                            self.signals.hypothesis.emit(final_text, final_text, "")
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

    def pointer(self) -> tuple[int, int] | None:
        if self._display is None:
            return None
        with suppress(Exception):
            self._display.sync()
            root = self._display.screen().root
            pointer = root.query_pointer()
            return int(pointer.root_x), int(pointer.root_y)
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

    def stage(self, hypothesis: str) -> bool:
        current_focus = self.focus.current()
        if self.target_focus is not None and current_focus != self.target_focus:
            return False
        self.hypothesis = hypothesis
        return True

    def _paste(self, text: str) -> None:
        xclip = shutil.which("xclip")
        xdotool = shutil.which("xdotool")
        chord = "ctrl+shift+v" if self.shortcut == "terminal" else "ctrl+v"
        if xclip is not None and xdotool is not None:
            try:
                copied = subprocess.run(
                    [xclip, "-selection", "clipboard", "-in"],
                    input=text.encode("utf-8"),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
                if copied.returncode != 0:
                    raise OSError("xclip failed to acquire the clipboard")
                result = subprocess.run(
                    [xdotool, "key", "--clearmodifiers", chord],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2,
                    check=False,
                )
                if result.returncode == 0:
                    self.last_clipboard = text
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass

        clipboard = self.application.clipboard()
        clipboard.setText(text)
        self.application.processEvents()
        self.last_clipboard = text
        if self.shortcut == "terminal":
            # The installer requires xclip/xdotool. This fallback is retained
            # for manually packaged clients and lets Qt service the clipboard
            # request once this slot returns to the event loop.
            with self.keyboard.pressed(Key.ctrl), self.keyboard.pressed(Key.shift):
                self.keyboard.tap("v")
            return
        with self.keyboard.pressed(Key.ctrl):
            self.keyboard.tap("v")

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
        if has_text and focus_matches:
            self._paste(self.hypothesis)
        if auto_enter and has_text and focus_matches:
            self.keyboard.press(Key.enter)
            self.keyboard.release(Key.enter)
        self.hypothesis = ""
        return focus_matches


class SettingsDialog(QDialog):
    def __init__(self, settings: DesktopSettings, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("XGC2 STT · 设置")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setMinimumSize(480, 440)
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        available_height = screen.availableGeometry().height() if screen is not None else 720
        self.resize(560, min(680, max(480, int(available_height * 0.9))))
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center().x() - self.width() // 2, area.center().y() - self.height() // 2)

        self.endpoint = QLineEdit(settings.endpoint)
        self.endpoint.setPlaceholderText("http://127.0.0.1:34897")
        self.api_key = QLineEdit(settings.api_key)
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("输入服务器分配的 API Key")
        self.api_key_visibility = QToolButton()
        self.api_key_visibility.setText("显示")
        self.api_key_visibility.setCheckable(True)
        self.api_key_visibility.setObjectName("revealButton")
        self.api_key_visibility.setFixedWidth(58)
        self.api_key_visibility.toggled.connect(self._toggle_api_key)
        self.hotkey = QLineEdit(settings.hotkey)
        self.hotkey.setPlaceholderText("<f8>")
        self.silence_seconds = QDoubleSpinBox()
        self.silence_seconds.setRange(0.5, 30.0)
        self.silence_seconds.setSingleStep(0.5)
        self.silence_seconds.setDecimals(1)
        self.silence_seconds.setSuffix(" 秒")
        self.silence_seconds.setValue(settings.silence_commit_ms / 1000)
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

        connection, connection_layout = self._section("连接")
        connection_layout.addWidget(self._field("服务器 URL", self.endpoint))
        api_key_row = QHBoxLayout()
        api_key_row.setContentsMargins(0, 0, 0, 0)
        api_key_row.setSpacing(8)
        api_key_row.addWidget(self.api_key, 1)
        api_key_row.addWidget(self.api_key_visibility)
        connection_layout.addWidget(self._field("访问密钥", api_key_row))

        recognition, recognition_layout = self._section("识别")
        recognition_grid = QGridLayout()
        recognition_grid.setContentsMargins(0, 0, 0, 0)
        recognition_grid.setHorizontalSpacing(12)
        recognition_grid.setVerticalSpacing(0)
        recognition_grid.setColumnStretch(0, 1)
        recognition_grid.setColumnStretch(1, 1)
        recognition_grid.setColumnStretch(2, 1)
        recognition_grid.addWidget(self._field("输出文字", self.output_script), 0, 0)
        recognition_grid.addWidget(self._field("停顿定稿", self.silence_seconds), 0, 1)
        recognition_grid.addWidget(self._field("全局快捷键", self.hotkey), 0, 2)
        recognition_layout.addLayout(recognition_grid)
        self.trim_silence.setText("跳过开头静音")
        recognition_layout.addWidget(self.trim_silence)

        input_section, input_layout = self._section("输入")
        input_layout.addWidget(self._field("粘贴方式", self.paste_shortcut))
        self.auto_enter.setText("停顿定稿后自动回车")
        self.start_at_login.setText("登录系统时自动启动")
        input_layout.addWidget(self.auto_enter)
        input_layout.addWidget(self.start_at_login)

        content = QWidget()
        content.setObjectName("settingsContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(18, 18, 18, 12)
        content_layout.setSpacing(12)
        content_layout.addWidget(connection)
        content_layout.addWidget(recognition)
        content_layout.addWidget(input_section)
        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(content)

        cancel = QPushButton("取消")
        cancel.setObjectName("secondaryButton")
        cancel.clicked.connect(self.reject)
        save = QPushButton("保存设置")
        save.setObjectName("primaryButton")
        save.setDefault(True)
        save.clicked.connect(self.accept)
        actions = QHBoxLayout()
        actions.setContentsMargins(18, 12, 18, 12)
        actions.setSpacing(8)
        actions.addStretch(1)
        actions.addWidget(cancel)
        actions.addWidget(save)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(scroll, 1)
        layout.addLayout(actions)
        self.setStyleSheet(self._stylesheet())

    @staticmethod
    def _section(title: str) -> tuple[QFrame, QVBoxLayout]:
        section = QFrame()
        section.setObjectName("settingsSection")
        layout = QVBoxLayout(section)
        layout.setContentsMargins(16, 14, 16, 16)
        layout.setSpacing(12)
        heading = QLabel(title)
        heading.setObjectName("sectionHeading")
        layout.addWidget(heading)
        return section, layout

    @staticmethod
    def _field(label: str, control: QWidget | QHBoxLayout) -> QWidget:
        field = QWidget()
        layout = QVBoxLayout(field)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        caption = QLabel(label)
        caption.setObjectName("fieldLabel")
        layout.addWidget(caption)
        if isinstance(control, QWidget):
            layout.addWidget(control)
        else:
            layout.addLayout(control)
        return field

    @Slot(bool)
    def _toggle_api_key(self, visible: bool) -> None:
        self.api_key.setEchoMode(QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password)
        self.api_key_visibility.setText("隐藏" if visible else "显示")

    @staticmethod
    def _stylesheet() -> str:
        return """
            QDialog { background: #111720; color: #e9eef5; }
            QWidget#settingsContent, QScrollArea#settingsScroll,
            QScrollArea#settingsScroll > QWidget > QWidget { background: #111720; }
            QFrame#settingsSection {
                background: #19212c;
                border: 1px solid #2d3948;
                border-radius: 9px;
            }
            QLabel#sectionHeading {
                color: #f4f7fb;
                font-size: 14px;
                font-weight: 600;
            }
            QLabel#fieldLabel {
                color: #9eaabd;
                font-size: 11px;
                font-weight: 500;
            }
            QLineEdit, QComboBox, QDoubleSpinBox {
                min-height: 38px;
                padding: 0 11px;
                color: #edf2f8;
                background: #10161e;
                border: 1px solid #354354;
                border-radius: 6px;
                selection-background-color: #315fdc;
            }
            QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus { border: 1px solid #6f91f3; }
            QComboBox { padding-right: 28px; }
            QComboBox::drop-down { width: 26px; border: 0; }
            QDoubleSpinBox::up-button, QDoubleSpinBox::down-button { width: 0; border: 0; }
            QComboBox QAbstractItemView {
                color: #edf2f8;
                background: #19212c;
                border: 1px solid #354354;
                selection-background-color: #294b9f;
                outline: 0;
            }
            QCheckBox {
                min-height: 24px;
                color: #d9e0e9;
                spacing: 9px;
            }
            QCheckBox::indicator {
                width: 17px;
                height: 17px;
            }
            QPushButton, QToolButton#revealButton {
                min-height: 36px;
                padding: 0 15px;
                color: #dce4ee;
                background: #202a37;
                border: 1px solid #374558;
                border-radius: 6px;
            }
            QPushButton:hover, QToolButton#revealButton:hover {
                background: #283548;
                border-color: #4c5d73;
            }
            QPushButton#primaryButton {
                color: white;
                background: #315fdc;
                border-color: #527cf0;
                font-weight: 600;
            }
            QPushButton#primaryButton:hover { background: #294fae; }
            QPushButton:focus, QToolButton:focus { border-color: #8ba7f7; }
            QScrollBar:vertical {
                width: 8px;
                margin: 4px 1px;
                background: transparent;
            }
            QScrollBar::handle:vertical {
                min-height: 30px;
                background: #3c495b;
                border-radius: 3px;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        """

    def values(self) -> DesktopSettings:
        return DesktopSettings(
            endpoint=self.endpoint.text().strip(),
            api_key=self.api_key.text().strip(),
            hotkey=self.hotkey.text().strip(),
            output_script=str(self.output_script.currentData()),
            trim_leading_silence=self.trim_silence.isChecked(),
            silence_commit_ms=round(self.silence_seconds.value() * 1000),
            paste_shortcut=str(self.paste_shortcut.currentData()),
            auto_enter=self.auto_enter.isChecked(),
            start_at_login=self.start_at_login.isChecked(),
        )


class Overlay(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(560, 112)
        self.dot = QLabel("●")
        self.label = QLabel("待机")
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setUndoRedoEnabled(False)
        self.preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.preview.document().setDocumentMargin(0)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self.dot)
        header.addWidget(self.label, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 7, 8, 9)
        layout.setSpacing(5)
        layout.addLayout(header)
        layout.addWidget(self.preview, 1)
        self.setStyleSheet(
            "QWidget { background:#181e27; color:#cdd6e2; border:1px solid #303a46; border-radius:6px; }"
            "QLabel { border:0; }"
            "QTextEdit#transcriptPreview { font-size:15px; color:#eef2f7; background:transparent; "
            "border:0; padding:0; }"
            "QScrollBar:vertical { width:6px; margin:0; background:transparent; }"
            "QScrollBar::handle:vertical { min-height:24px; background:#465468; border-radius:3px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background:transparent; }"
        )
        self.preview.setObjectName("transcriptPreview")
        self.set_state("待机", "#79b88c")
        self.set_idle_size()

    def set_idle_size(self) -> None:
        self.preview.clear()
        self.preview.hide()
        self.setFixedSize(154, 38)

    def show_near_focus(self, anchor: tuple[int, int] | None) -> None:
        self.setFixedSize(560, 136)
        self.preview.show()
        if anchor is None:
            cursor = QCursor.pos()
            anchor = (cursor.x(), cursor.y())
        anchor_x, anchor_y = anchor
        screen = QApplication.screenAt(QPoint(anchor_x, anchor_y)) or QApplication.primaryScreen()
        if screen is None:
            self.show()
            return
        area = screen.availableGeometry()
        x = anchor_x - 36
        y_below = anchor_y + 24
        y_above = anchor_y - self.height() - 24
        y = y_below if y_below + self.height() <= area.bottom() - 8 else y_above
        x = max(area.left() + 8, min(x, area.right() - self.width() - 8))
        y = max(area.top() + 8, min(y, area.bottom() - self.height() - 8))
        self.move(x, y)
        self.show()

    def set_preview(self, stable: str, unstable: str, text: str) -> None:
        if stable + unstable != text:
            stable = text[: max(0, len(text) - len(unstable))]
        stable_markup = escape(stable).replace("\n", "<br>")
        unstable_markup = escape(unstable).replace("\n", "<br>")
        if unstable_markup:
            unstable_markup = (
                '<span style="color:#ffe2a8; background-color:#664817; font-weight:600;">'
                f"{unstable_markup}</span>"
            )
        self.preview.setHtml(stable_markup + unstable_markup)
        cursor = self.preview.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.preview.setTextCursor(cursor)
        self.preview.ensureCursorVisible()

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
        self.toggle_requested.connect(self.toggle)
        self.hotkeys: GlobalHotKeys | None = None
        self._shutting_down = False
        self._start_hotkey()

        icon = application.style().standardIcon(QStyle.StandardPixmap.SP_MediaVolume)
        self.tray = QSystemTrayIcon(icon, application)
        self.tray_menu = QMenu()
        self.capture_action = QAction("开始录音", self.tray_menu)
        self.capture_action.triggered.connect(self.toggle)
        self.settings_action = QAction("设置", self.tray_menu)
        self.settings_action.triggered.connect(self.open_settings)
        quit_action = QAction("退出", self.tray_menu)
        quit_action.triggered.connect(self.shutdown)
        self.tray_menu.addAction(self.capture_action)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction(self.settings_action)
        self.tray_menu.addAction(quit_action)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(
            lambda reason: self.toggle() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()
        self.overlay.hide()
        self._sync_tray()

    def _sync_tray(self) -> None:
        label, tooltip, enabled = {
            "idle": ("开始录音", "XGC2 STT · 待机", True),
            "connecting": ("停止录音", "XGC2 STT · 连接中", True),
            "recording": ("停止录音", "XGC2 STT · 录音中", True),
            "finalizing": ("正在收尾", "XGC2 STT · 收尾", False),
        }[self.state]
        self.capture_action.setText(label)
        self.capture_action.setEnabled(enabled)
        self.settings_action.setEnabled(self.state == "idle")
        self.tray.setToolTip(tooltip)

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
        self._sync_tray()
        self.overlay.set_state("连接中", "#d7ae66")
        self.injector.begin(self.settings.paste_shortcut)
        self.overlay.show_near_focus(self.injector.focus.pointer())
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
        self._sync_tray()
        self.overlay.set_state("录音中", "#df8589")

    def stop_recording(self) -> None:
        if self.capture is not None:
            self.capture.stop()
            self.capture = None
        self.state = "finalizing"
        self._sync_tray()
        self.overlay.set_state("收尾", "#d7ae66")
        self.worker.commit() if self.worker is not None else self._finish_session()

    @Slot(str, str, str)
    def _inject_hypothesis(self, text: str, stable: str, unstable: str) -> None:
        self.overlay.set_preview(stable, unstable, text)
        if text and not self.injector.stage(text):
            self.overlay.set_state("焦点已变", "#d7ae66")

    @Slot(str)
    def _commit_segment(self, reason: str) -> None:
        auto_enter = should_auto_enter(self.settings.auto_enter, reason)
        if not self.injector.commit_segment(auto_enter=auto_enter):
            self.overlay.set_state("焦点已变", "#d7ae66")
        self.overlay.set_preview("", "", "")

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
        self.overlay.set_state("待机", "#79b88c")
        self.overlay.set_idle_size()
        self.overlay.hide()
        self._sync_tray()

    @Slot()
    def open_settings(self) -> None:
        if self.state != "idle":
            return
        dialog = SettingsDialog(self.settings)
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
        self.tray.setToolTip("XGC2 STT · 错误")
        self.tray.showMessage("XGC2 STT", message, QSystemTrayIcon.MessageIcon.Critical, 5000)
        QTimer.singleShot(2500, self._restore_idle_status)

    @Slot()
    def _restore_idle_status(self) -> None:
        if self.state == "idle":
            self.overlay.set_state("待机", "#79b88c")
            self._sync_tray()

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
