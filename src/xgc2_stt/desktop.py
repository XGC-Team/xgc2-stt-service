from __future__ import annotations

import json
import queue
import sys
import threading
from array import array
from contextlib import suppress
from html import escape
from typing import Any

import sounddevice
from pynput.keyboard import GlobalHotKeys, HotKey, Key, KeyCode
from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QCloseEvent, QCursor, QMouseEvent, QTextCursor
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
from Xlib import XK, X, display, error

from . import __version__
from .desktop_support import (
    DesktopIpcListener,
    DesktopSettings,
    InsertionOutcome,
    apply_qt_platform,
    format_desktop_version,
    insert_finalized_text,
    is_wayland_session,
    load_desktop_settings,
    packaged_client_command,
    parse_desktop_cli,
    save_desktop_settings,
    send_running_instance,
    set_autostart,
    should_auto_enter,
    streaming_headers,
    streaming_url,
)

_COMMIT = object()
_CANCEL = object()


def _parse_hotkey(specification: str) -> tuple[int, frozenset[str]]:
    modifier_names = {
        "alt": "alt",
        "alt_l": "alt",
        "alt_r": "alt",
        "cmd": "super",
        "cmd_l": "super",
        "cmd_r": "super",
        "ctrl": "ctrl",
        "ctrl_l": "ctrl",
        "ctrl_r": "ctrl",
        "shift": "shift",
        "shift_l": "shift",
        "shift_r": "shift",
    }
    modifiers: set[str] = set()
    keysyms: list[int] = []
    for key in HotKey.parse(specification):
        modifier_name = key.name if isinstance(key, Key) else None
        if modifier_name in modifier_names:
            modifiers.add(modifier_names[modifier_name])
            continue
        if isinstance(key, KeyCode):
            keysym = key.vk if key.vk is not None else XK.string_to_keysym(key.char or "")
        else:
            value = key.value
            keysym = getattr(value, "vk", value)
        if not isinstance(keysym, int) or keysym <= 0:
            raise ValueError("快捷键包含 X11 无法识别的按键")
        keysyms.append(keysym)
    if len(keysyms) != 1:
        raise ValueError("快捷键必须包含且只能包含一个非修饰键")
    return keysyms[0], frozenset(modifiers)


def _modifier_mask_for_keysyms(x_display: Any, keysym_names: tuple[str, ...]) -> int:
    keycodes = {
        x_display.keysym_to_keycode(XK.string_to_keysym(name))
        for name in keysym_names
    } - {0}
    for index, mapped_keycodes in enumerate(x_display.get_modifier_mapping()):
        if keycodes.intersection(mapped_keycodes):
            return 1 << index
    return 0


def _x11_modifier_masks(x_display: Any, modifier_names: frozenset[str]) -> tuple[int, tuple[int, ...]]:
    available = {
        "ctrl": X.ControlMask,
        "shift": X.ShiftMask,
        "alt": _modifier_mask_for_keysyms(x_display, ("Alt_L", "Alt_R")),
        "super": _modifier_mask_for_keysyms(x_display, ("Super_L", "Super_R")),
    }
    missing = modifier_names - {name for name, mask in available.items() if mask}
    if missing:
        raise ValueError(f"当前 X11 键盘映射缺少修饰键: {', '.join(sorted(missing))}")
    modifiers = 0
    for name in modifier_names:
        modifiers |= available[name]

    lock_masks = {
        _modifier_mask_for_keysyms(x_display, ("Caps_Lock",)),
        _modifier_mask_for_keysyms(x_display, ("Num_Lock",)),
        _modifier_mask_for_keysyms(x_display, ("Scroll_Lock",)),
    } - {0}
    ignored = {0}
    for mask in lock_masks:
        ignored.update({existing | mask for existing in tuple(ignored)})
    return modifiers, tuple(sorted(ignored))


class ExclusiveX11HotKey:
    """A passive X11 key grab that doesn't leak the shortcut into the focused app."""

    def __init__(self, specification: str, callback: Any):
        self.keysym, self.modifier_names = _parse_hotkey(specification)
        self.callback = callback
        self._display: Any = None
        self._root: Any = None
        self._keycode = 0
        self._modifiers = 0
        self._ignored_modifier_masks: tuple[int, ...] = ()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            raise RuntimeError("快捷键监听器已在运行")
        self._stop.clear()
        self._ready.clear()
        self._startup_error = None
        self._thread = threading.Thread(target=self._run, name="xgc2-stt-hotkey", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3):
            self.stop()
            raise RuntimeError("快捷键监听器启动超时")
        if self._startup_error is not None:
            startup_error = self._startup_error
            self._thread.join(timeout=1)
            self._thread = None
            raise RuntimeError(str(startup_error)) from startup_error

    def _run(self) -> None:
        try:
            self._display = display.Display()
            self._root = self._display.screen().root
            self._keycode = self._display.keysym_to_keycode(self.keysym)
            if not self._keycode:
                raise ValueError("快捷键在当前 X11 键盘映射中不可用")
            self._modifiers, self._ignored_modifier_masks = _x11_modifier_masks(
                self._display, self.modifier_names
            )
            grab_errors: list[error.CatchError] = []
            for ignored in self._ignored_modifier_masks:
                catcher = error.CatchError()
                self._root.grab_key(
                    self._keycode,
                    self._modifiers | ignored,
                    False,
                    X.GrabModeAsync,
                    X.GrabModeAsync,
                    onerror=catcher,
                )
                grab_errors.append(catcher)
            self._display.sync()
            if any(catcher.get_error() is not None for catcher in grab_errors):
                raise RuntimeError("快捷键已被其他应用占用")
            self._ready.set()
            self._listen()
        except BaseException as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            self._release()

    def _listen(self) -> None:
        pressed = False
        pending_event: Any = None
        while not self._stop.wait(0.02):
            if pending_event is not None:
                event = pending_event
                pending_event = None
            elif self._display is not None and self._display.pending_events():
                event = self._display.next_event()
            else:
                continue
            # X11 can interleave MappingNotify and other housekeeping events
            # with grabbed key events. Those events do not expose ``detail``;
            # inspect the type before reading key-specific fields so one
            # keyboard-map notification cannot terminate the listener.
            if event.type not in (X.KeyPress, X.KeyRelease):
                continue
            if event.detail != self._keycode:
                continue
            if event.type == X.KeyPress:
                if not pressed:
                    pressed = True
                    self.callback()
                continue
            if event.type != X.KeyRelease:
                continue
            if self._display is not None and self._display.pending_events():
                next_event = self._display.next_event()
                if (
                    next_event.type == X.KeyPress
                    and next_event.detail == self._keycode
                    and next_event.time == event.time
                ):
                    continue
                pending_event = next_event
            pressed = False

    def _release(self) -> None:
        if self._display is None:
            return
        if self._root is not None and self._keycode:
            for ignored in self._ignored_modifier_masks:
                with suppress(Exception):
                    self._root.ungrab_key(self._keycode, self._modifiers | ignored)
            with suppress(Exception):
                self._display.sync()
        with suppress(Exception):
            self._display.close()
        self._display = None
        self._root = None
        self._keycode = 0

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout=2)
        if thread.is_alive():
            raise RuntimeError("快捷键监听器未能停止")
        self._thread = None


def create_hotkey_listener(specification: str, callback: Any) -> ExclusiveX11HotKey | GlobalHotKeys:
    _parse_hotkey(specification)
    if is_wayland_session():
        return GlobalHotKeys({specification: callback})
    return ExclusiveX11HotKey(specification, callback)


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
                additional_headers=streaming_headers(self.settings),
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
        self.stream: sounddevice.RawInputStream | None = None
        self._lifecycle_lock = threading.Lock()
        self._callback_lock = threading.Lock()
        self._accepting_audio = threading.Event()
        self._failure_reported = threading.Event()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.stream is not None:
                raise RuntimeError("麦克风已经启动")
            stream: sounddevice.RawInputStream | None = None
            try:
                stream = sounddevice.RawInputStream(
                    samplerate=16000,
                    blocksize=0,
                    channels=1,
                    dtype="int16",
                    callback=self._read,
                )
                self.stream = stream
                self._failure_reported.clear()
                self._accepting_audio.set()
                stream.start()
            except Exception as exc:
                self._accepting_audio.clear()
                self.stream = None
                if stream is not None:
                    with suppress(Exception):
                        stream.close()
                raise RuntimeError(f"无法启动麦克风: {exc}") from exc

    def _read(self, indata: Any, _frames: int, _time: Any, _status: Any) -> None:
        try:
            with self._callback_lock:
                if not self._accepting_audio.is_set():
                    return
                pcm = bytes(indata)
                if sys.byteorder != "little":
                    samples = array("h")
                    samples.frombytes(pcm)
                    samples.byteswap()
                    pcm = samples.tobytes()
                if pcm:
                    self.on_pcm(pcm)
        except Exception as exc:
            self._accepting_audio.clear()
            if not self._failure_reported.is_set():
                self._failure_reported.set()
                self.failed.emit(str(exc))
            raise sounddevice.CallbackAbort from exc

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._accepting_audio.clear()
            stream = self.stream
            self.stream = None
            if stream is not None:
                with suppress(Exception):
                    stream.stop()
                with suppress(Exception):
                    stream.close()
            # A callback that passed the active check before stop() must finish
            # before this method returns. A queued callback sees the cleared
            # event and cannot send audio into the next recognition session.
            with self._callback_lock:
                pass

    def close(self) -> None:
        self.stop()


class FocusTracker:
    def __init__(self):
        self._display: Any = None
        if is_wayland_session():
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
        self.focus = FocusTracker()
        self.target_focus: int | None = None
        self.hypothesis = ""
        self.original_clipboard = ""
        self.last_clipboard = ""
        self.last_outcome = InsertionOutcome(copied=False, pasted=False, method="empty")
        self.shortcut = "terminal"

    def begin(self, shortcut: str) -> None:
        self.target_focus = self.focus.current()
        self.hypothesis = ""
        self.shortcut = shortcut
        self.original_clipboard = self.application.clipboard().text()
        self.last_clipboard = ""
        self.last_outcome = InsertionOutcome(copied=False, pasted=False, method="empty")

    def stage(self, hypothesis: str) -> bool:
        current_focus = self.focus.current()
        if self.target_focus is not None and current_focus != self.target_focus:
            return False
        self.hypothesis = hypothesis
        return True

    def _paste(self, text: str, *, send_enter: bool = False) -> InsertionOutcome:
        def set_clipboard(value: str) -> None:
            self.application.clipboard().setText(value)

        outcome = insert_finalized_text(
            text,
            paste_shortcut=self.shortcut,
            send_enter=send_enter,
            set_clipboard=set_clipboard,
        )
        if not outcome.copied:
            raise RuntimeError(outcome.detail or "无法写入剪贴板")
        if outcome.pasted or outcome.method == "clipboard":
            self.last_clipboard = text
        return outcome

    def end(self) -> None:
        clipboard = self.application.clipboard()
        if self.last_clipboard and clipboard.text() == self.last_clipboard:
            clipboard.setText(self.original_clipboard)
        self.target_focus = None
        self.hypothesis = ""
        self.last_clipboard = ""
        self.last_outcome = InsertionOutcome(copied=False, pasted=False, method="empty")

    def commit_segment(self, *, auto_enter: bool = False) -> bool:
        has_text = bool(self.hypothesis)
        current_focus = self.focus.current()
        focus_matches = self.target_focus is None or current_focus == self.target_focus
        if has_text and focus_matches:
            self.last_outcome = self._paste(self.hypothesis, send_enter=auto_enter)
        else:
            self.last_outcome = InsertionOutcome(copied=False, pasted=False, method="empty")
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
        self.endpoint.setPlaceholderText("输入自有 STT API URL")
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
        self.hotkey.setPlaceholderText("<f9>")
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
        hint = QLabel(
            "客户端从命令行启动后驻留状态栏。登录自启动仅在勾选后写入 XDG autostart。"
            " Wayland 下若全局快捷键不可用，可将系统快捷键绑定到 xgc2-stt-client --toggle-capture；"
            " 无法注入按键时会把定稿文本留在剪贴板。"
        )
        hint.setWordWrap(True)
        hint.setObjectName("fieldHint")
        input_layout.addWidget(hint)

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
            QLabel#fieldHint {
                color: #8b97a8;
                font-size: 11px;
                line-height: 1.4;
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
        self._drag_offset: QPoint | None = None
        self.dot = QLabel("●")
        self.label = QLabel("待机")
        self.dot.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setUndoRedoEnabled(False)
        self.preview.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.preview.document().setDocumentMargin(0)
        self.drag_handle = QWidget()
        self.drag_handle.setObjectName("dragHandle")
        self.drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
        self.drag_handle.installEventFilter(self)
        header = QHBoxLayout(self.drag_handle)
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        header.addWidget(self.dot)
        header.addWidget(self.label, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 7, 8, 9)
        layout.setSpacing(5)
        layout.addWidget(self.drag_handle)
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

    def show_bottom_center(self) -> None:
        self.setFixedSize(560, 136)
        self.preview.show()
        screen = QApplication.screenAt(QCursor.pos()) or QApplication.primaryScreen()
        if screen is None:
            self.show()
            return
        area = screen.availableGeometry()
        x = area.center().x() - self.width() // 2
        y = area.bottom() - self.height() - 48
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

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self.drag_handle or not isinstance(event, QMouseEvent):
            return super().eventFilter(watched, event)
        if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self.drag_handle.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return True
        if event.type() == QEvent.Type.MouseMove and self._drag_offset is not None:
            global_position = event.globalPosition().toPoint()
            screen = QApplication.screenAt(global_position) or QApplication.primaryScreen()
            target = global_position - self._drag_offset
            if screen is not None:
                area = screen.availableGeometry()
                target.setX(max(area.left() + 8, min(target.x(), area.right() - self.width() - 8)))
                target.setY(max(area.top() + 8, min(target.y(), area.bottom() - self.height() - 8)))
            self.move(target)
            event.accept()
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self.drag_handle.setCursor(Qt.CursorShape.OpenHandCursor)
            event.accept()
            return True
        return super().eventFilter(watched, event)

    def closeEvent(self, event: QCloseEvent) -> None:
        event.ignore()
        self.hide()


class ClientController(QObject):
    toggle_requested = Signal()
    ipc_requested = Signal(str)

    def __init__(self, application: QApplication, *, start_capture: bool = False, open_settings: bool = False):
        super().__init__()
        self.application = application
        self.settings = load_desktop_settings()
        self.state = "idle"
        self.worker: StreamingWorker | None = None
        self.capture: AudioCapture | None = None
        self.injector = TextInjector(application)
        self.overlay = Overlay()
        self.toggle_requested.connect(self.toggle)
        self.ipc_requested.connect(self._handle_ipc)
        self.hotkeys: ExclusiveX11HotKey | GlobalHotKeys | None = None
        self._hotkey_binding: tuple[int, frozenset[str]] | None = None
        self._shutting_down = False
        self._ipc = DesktopIpcListener(self.ipc_requested.emit)
        self._ipc.start()
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
        if open_settings or not self.settings.endpoint:
            QTimer.singleShot(0, self.open_settings)
        elif start_capture:
            QTimer.singleShot(0, self.toggle)

    @Slot(str)
    def _handle_ipc(self, command: str) -> None:
        if command in {"toggle", "toggle-capture"}:
            self.toggle()
        elif command == "settings":
            self.open_settings()
        elif command == "activate":
            self.tray.showMessage("XGC2 STT", "客户端已在状态栏运行", QSystemTrayIcon.MessageIcon.Information, 2500)

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
            self.hotkeys = None
            self._hotkey_binding = None
        try:
            self.hotkeys = create_hotkey_listener(self.settings.hotkey, self.toggle_requested.emit)
            self.hotkeys.start()
            self._hotkey_binding = _parse_hotkey(self.settings.hotkey)
        except Exception as exc:
            self.hotkeys = None
            self._hotkey_binding = None
            if is_wayland_session():
                message = (
                    f"全局快捷键不可用: {exc}。请使用状态栏菜单，"
                    "或把系统快捷键绑定到 xgc2-stt-client --toggle-capture。"
                )
                QTimer.singleShot(
                    0,
                    lambda: self.tray.showMessage(
                        "XGC2 STT", message, QSystemTrayIcon.MessageIcon.Information, 6000
                    ),
                )
                return
            message = f"快捷键不可用: {exc}"
            QTimer.singleShot(0, lambda: self._show_error(message))

    def _replace_hotkey(self, specification: str) -> None:
        binding = _parse_hotkey(specification)
        if self.hotkeys is not None and binding == self._hotkey_binding:
            return
        if is_wayland_session():
            previous = self.hotkeys
            try:
                candidate = create_hotkey_listener(specification, self.toggle_requested.emit)
                candidate.start()
            except Exception:
                if previous is not None:
                    with suppress(Exception):
                        previous.stop()
                self.hotkeys = None
                self._hotkey_binding = None
                return
            self.hotkeys = candidate
            self._hotkey_binding = binding
            if previous is not None:
                with suppress(Exception):
                    previous.stop()
            return
        candidate = create_hotkey_listener(specification, self.toggle_requested.emit)
        candidate.start()
        previous = self.hotkeys
        previous_binding = self._hotkey_binding
        self.hotkeys = candidate
        self._hotkey_binding = binding
        try:
            if previous is not None:
                previous.stop()
        except Exception:
            candidate.stop()
            self.hotkeys = previous
            self._hotkey_binding = previous_binding
            raise

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
        self.overlay.show_bottom_center()
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
            self.capture.failed.connect(self._stream_failed)
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
        try:
            focus_matches = self.injector.commit_segment(auto_enter=auto_enter)
        except RuntimeError as exc:
            self.overlay.set_state("文本注入失败", "#df8589")
            self._show_error(str(exc))
            return
        if not focus_matches:
            self.overlay.set_state("焦点已变", "#d7ae66")
        elif self.injector.last_outcome.method == "clipboard":
            self.overlay.set_state("已复制，请按 Ctrl+V", "#d7ae66")
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
            self._replace_hotkey(candidate.hotkey)
        except Exception as exc:
            QMessageBox.critical(self.overlay, "设置无效", str(exc))
            return
        previous = self.settings
        self.settings = candidate
        try:
            save_desktop_settings(candidate)
            set_autostart(candidate.start_at_login, packaged_client_command())
        except Exception as exc:
            self.settings = previous
            with suppress(Exception):
                self._replace_hotkey(previous.hotkey)
            QMessageBox.critical(self.overlay, "设置保存失败", str(exc))

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
        if getattr(self, "_ipc", None) is not None:
            self._ipc.stop()
        self.injector.end()
        self.tray.hide()
        self.application.quit()


def main(argv: list[str] | None = None) -> int:
    args = parse_desktop_cli(sys.argv[1:] if argv is None else argv)
    if args.version:
        sys.stdout.write(f"{format_desktop_version()}\n")
        return 0
    if args.toggle_capture and send_running_instance("toggle"):
        return 0
    if args.settings and send_running_instance("settings"):
        return 0
    if not args.toggle_capture and not args.settings and send_running_instance("activate"):
        return 0
    apply_qt_platform()
    application = QApplication([sys.argv[0]])
    application.setApplicationName("XGC2 STT Client")
    application.setApplicationVersion(__version__)
    application.setQuitOnLastWindowClosed(False)
    controller = ClientController(
        application,
        start_capture=args.toggle_capture,
        open_settings=args.settings,
    )
    application.aboutToQuit.connect(controller.shutdown)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
