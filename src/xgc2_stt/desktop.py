from __future__ import annotations

import json
import queue
import sys
import threading
from collections.abc import Callable
from contextlib import suppress
from html import escape
from typing import Any

import gi
from Xlib import XK, X, display, error

from .desktop_audio import AudioCapture
from .desktop_support import (
    CLIENT_BINARY,
    DesktopIpcListener,
    DesktopSettings,
    InsertionOutcome,
    insert_finalized_text,
    is_wayland_session,
    load_desktop_settings,
    packaged_client_command,
    parse_hotkey,
    save_desktop_settings,
    set_autostart,
    should_auto_enter,
    streaming_headers,
    streaming_url,
)

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk

_COMMIT = object()
_CANCEL = object()


def _load_appindicator() -> Any:
    try:
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3 as AppIndicator3

        return AppIndicator3
    except (ValueError, ImportError):
        gi.require_version("AppIndicator3", "0.1")
        from gi.repository import AppIndicator3

        return AppIndicator3


def _notify(title: str, message: str, icon: str = "dialog-information") -> None:
    try:
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify

        if not Notify.is_initted():
            Notify.init("XGC2 STT")
        Notify.Notification.new(title, message, icon).show()
    except Exception:
        return


def _ui(func: Callable[..., Any], *args: Any) -> None:
    def runner() -> bool:
        func(*args)
        return False

    GLib.idle_add(runner)


def _modifier_mask_for_keysyms(x_display: Any, keysym_names: tuple[str, ...]) -> int:
    keycodes = {x_display.keysym_to_keycode(XK.string_to_keysym(name)) for name in keysym_names} - {0}
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
        self.keysym_name, self.modifier_names = parse_hotkey(specification)
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
            keysym = XK.string_to_keysym(self.keysym_name)
            if not keysym:
                raise ValueError("快捷键包含 X11 无法识别的按键")
            self._keycode = self._display.keysym_to_keycode(keysym)
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


def create_hotkey_listener(specification: str, callback: Any) -> ExclusiveX11HotKey:
    parse_hotkey(specification)
    if is_wayland_session():
        raise RuntimeError(
            "Wayland 下全局抓键不可用。请使用状态栏菜单，或把系统快捷键绑定到 xgc2-stt-client --toggle-capture。"
        )
    return ExclusiveX11HotKey(specification, callback)


class StreamingWorker:
    def __init__(
        self,
        settings: DesktopSettings,
        *,
        on_connected: Callable[[], None],
        on_hypothesis: Callable[[str, str, str], None],
        on_state: Callable[[str], None],
        on_segment: Callable[[str], None],
        on_failed: Callable[[str], None],
        on_completed: Callable[[], None],
    ):
        self.settings = settings
        self.on_connected = on_connected
        self.on_hypothesis = on_hypothesis
        self.on_state = on_state
        self.on_segment = on_segment
        self.on_failed = on_failed
        self.on_completed = on_completed
        self._outgoing: queue.Queue[Any] = queue.Queue()
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
        from websocket import ABNF, WebSocketTimeoutException, create_connection

        final_received = False
        socket = None
        try:
            headers = [f"{key}: {value}" for key, value in streaming_headers(self.settings).items()]
            connect_options = {"timeout": 10}
            if headers:
                connect_options["header"] = headers
            socket = create_connection(streaming_url(self.settings), **connect_options)
            socket.settimeout(15)
            first = json.loads(socket.recv())
            if first.get("type") != "session.started":
                raise RuntimeError(first.get("message") or "服务未创建识别会话")
            _ui(self.on_connected)
            socket.settimeout(0.01)
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
                    _ui(self.on_state, "收尾")
                elif isinstance(outgoing, bytes):
                    socket.send(outgoing, opcode=ABNF.OPCODE_BINARY)
                try:
                    raw = socket.recv()
                except (WebSocketTimeoutException, TimeoutError):
                    continue
                event = json.loads(raw)
                event_type = event.get("type")
                if event_type == "transcript.partial":
                    text = str(event.get("text") or "")
                    stable = str(event.get("stable_text") or "")
                    unstable = str(event.get("unstable_text") or "")
                    if not stable and not unstable:
                        unstable = text
                    _ui(self.on_hypothesis, text, stable, unstable)
                elif event_type == "transcript.final":
                    final_text = str(event.get("text") or "")
                    if final_text:
                        _ui(self.on_hypothesis, final_text, final_text, "")
                    _ui(self.on_segment, str(event.get("reason") or "commit"))
                    if event.get("session_complete", True):
                        final_received = True
                        return
                    _ui(self.on_state, "录音中")
                elif event_type == "error":
                    raise RuntimeError(str(event.get("message") or event.get("code") or "识别失败"))
        except Exception as exc:
            _ui(self.on_failed, str(exc))
        finally:
            if socket is not None:
                with suppress(Exception):
                    socket.close()
            if final_received:
                _ui(self.on_completed)


class FocusTracker:
    def __init__(self):
        self._display: Any = None
        if is_wayland_session():
            return
        with suppress(Exception):
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
    def __init__(self):
        self.focus = FocusTracker()
        self.target_focus: int | None = None
        self.hypothesis = ""
        self.original_clipboard = ""
        self.last_clipboard = ""
        self.last_outcome = InsertionOutcome(copied=False, pasted=False, method="empty")
        self.shortcut = "terminal"

    def _clipboard(self) -> Gtk.Clipboard:
        return Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    def begin(self, shortcut: str) -> None:
        self.target_focus = self.focus.current()
        self.hypothesis = ""
        self.shortcut = shortcut
        self.original_clipboard = self._clipboard().wait_for_text() or ""
        self.last_clipboard = ""
        self.last_outcome = InsertionOutcome(copied=False, pasted=False, method="empty")

    def stage(self, hypothesis: str) -> bool:
        current_focus = self.focus.current()
        if self.target_focus is not None and current_focus != self.target_focus:
            return False
        self.hypothesis = hypothesis
        return True

    def _paste(self, text: str, send_enter: bool = False) -> InsertionOutcome:
        def set_clipboard(value: str) -> None:
            self._clipboard().set_text(value, -1)
            self._clipboard().store()

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
        clipboard = self._clipboard()
        current = clipboard.wait_for_text() or ""
        if self.last_clipboard and current == self.last_clipboard:
            clipboard.set_text(self.original_clipboard, -1)
            clipboard.store()
        self.target_focus = None
        self.hypothesis = ""
        self.last_clipboard = ""
        self.last_outcome = InsertionOutcome(copied=False, pasted=False, method="empty")

    def commit_segment(self, auto_enter: bool = False) -> bool:
        has_text = bool(self.hypothesis)
        current_focus = self.focus.current()
        focus_matches = self.target_focus is None or current_focus == self.target_focus
        if has_text and focus_matches:
            self.last_outcome = self._paste(self.hypothesis, send_enter=auto_enter)
        else:
            self.last_outcome = InsertionOutcome(copied=False, pasted=False, method="empty")
        self.hypothesis = ""
        return focus_matches


def _apply_css(widget: Gtk.Widget, css: str) -> None:
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode("utf-8"))
    widget.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class SettingsDialog(Gtk.Dialog):
    def __init__(self, settings: DesktopSettings, parent: Gtk.Window | None = None):
        Gtk.Dialog.__init__(self, title="XGC2 STT · 设置", transient_for=parent, modal=True)
        self.set_default_size(560, 520)
        self.add_button("取消", Gtk.ResponseType.CANCEL)
        save = self.add_button("保存设置", Gtk.ResponseType.OK)
        save.get_style_context().add_class("suggested-action")
        self.endpoint = Gtk.Entry()
        self.endpoint.set_text(settings.endpoint)
        self.endpoint.set_placeholder_text("输入自有 STT API URL")
        self.api_key = Gtk.Entry()
        self.api_key.set_text(settings.api_key)
        self.api_key.set_visibility(False)
        self.api_key.set_placeholder_text("输入服务器分配的 API Key")
        self.api_key_visibility = Gtk.ToggleButton(label="显示")
        self.api_key_visibility.connect("toggled", self._toggle_api_key)
        self.hotkey = Gtk.Entry()
        self.hotkey.set_text(settings.hotkey)
        self.hotkey.set_placeholder_text("<f9>")
        self.silence_seconds = Gtk.SpinButton.new_with_range(0.5, 30.0, 0.5)
        self.silence_seconds.set_digits(1)
        self.silence_seconds.set_value(settings.silence_commit_ms / 1000)
        self.output_script = Gtk.ComboBoxText()
        self.output_script.append("simplified", "简体中文")
        self.output_script.append("original", "模型原样")
        self.output_script.set_active_id(settings.output_script or "simplified")
        self.trim_silence = Gtk.CheckButton(label="跳过开头静音")
        self.trim_silence.set_active(settings.trim_leading_silence)
        self.paste_shortcut = Gtk.ComboBoxText()
        self.paste_shortcut.append("terminal", "终端 Ctrl+Shift+V")
        self.paste_shortcut.append("desktop", "桌面 Ctrl+V")
        self.paste_shortcut.set_active_id(settings.paste_shortcut or "terminal")
        self.auto_enter = Gtk.CheckButton(label="停顿定稿后自动回车")
        self.auto_enter.set_active(settings.auto_enter)
        self.start_at_login = Gtk.CheckButton(label="登录系统时自动启动")
        self.start_at_login.set_active(settings.start_at_login)

        content = self.get_content_area()
        content.set_margin_top(12)
        content.set_margin_bottom(8)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_spacing(12)
        content.add(self._labeled("服务器 URL", self.endpoint))
        key_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        key_row.pack_start(self.api_key, True, True, 0)
        key_row.pack_start(self.api_key_visibility, False, False, 0)
        content.add(self._labeled("访问密钥", key_row))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.pack_start(self._labeled("输出文字", self.output_script), True, True, 0)
        row.pack_start(self._labeled("停顿定稿（秒）", self.silence_seconds), True, True, 0)
        row.pack_start(self._labeled("全局快捷键", self.hotkey), True, True, 0)
        content.add(row)
        content.add(self.trim_silence)
        content.add(self._labeled("粘贴方式", self.paste_shortcut))
        content.add(self.auto_enter)
        content.add(self.start_at_login)
        hint = Gtk.Label(
            label=(
                "客户端从命令行启动后驻留状态栏。登录自启动仅在勾选后写入 XDG autostart。"
                " Wayland 下若全局快捷键不可用，可将系统快捷键绑定到 xgc2-stt-client --toggle-capture；"
                " 无法注入按键时会把定稿文本留在剪贴板。"
            )
        )
        hint.set_line_wrap(True)
        hint.set_xalign(0)
        hint.get_style_context().add_class("dim-label")
        content.add(hint)
        self.show_all()

    def _toggle_api_key(self, button: Gtk.ToggleButton) -> None:
        visible = button.get_active()
        self.api_key.set_visibility(visible)
        button.set_label("隐藏" if visible else "显示")

    @staticmethod
    def _labeled(caption: str, widget: Gtk.Widget) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        label = Gtk.Label(label=caption, xalign=0)
        box.pack_start(label, False, False, 0)
        box.pack_start(widget, True, True, 0)
        return box

    def values(self) -> DesktopSettings:
        return DesktopSettings(
            endpoint=self.endpoint.get_text().strip(),
            api_key=self.api_key.get_text().strip(),
            hotkey=self.hotkey.get_text().strip(),
            output_script=self.output_script.get_active_id() or "simplified",
            trim_leading_silence=self.trim_silence.get_active(),
            silence_commit_ms=round(self.silence_seconds.get_value() * 1000),
            paste_shortcut=self.paste_shortcut.get_active_id() or "terminal",
            auto_enter=self.auto_enter.get_active(),
            start_at_login=self.start_at_login.get_active(),
        )


class Overlay(Gtk.Window):
    def __init__(self):
        Gtk.Window.__init__(self, type=Gtk.WindowType.POPUP)
        self.set_decorated(False)
        self.set_keep_above(True)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_accept_focus(False)
        self.set_resizable(False)
        self._drag_x = 0
        self._drag_y = 0
        self._dragging = False
        self.dot = Gtk.Label(label="●")
        self.label = Gtk.Label(label="待机")
        self.preview = Gtk.Label()
        self.preview.set_line_wrap(True)
        self.preview.set_xalign(0)
        self.preview.set_max_width_chars(52)
        header = Gtk.EventBox()
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_box.pack_start(self.dot, False, False, 0)
        header_box.pack_start(self.label, True, True, 0)
        header.add(header_box)
        header.connect("button-press-event", self._on_press)
        header.connect("button-release-event", self._on_release)
        header.connect("motion-notify-event", self._on_motion)
        header.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(8)
        root.set_margin_bottom(8)
        root.set_margin_start(12)
        root.set_margin_end(12)
        root.pack_start(header, False, False, 0)
        root.pack_start(self.preview, True, True, 0)
        self.add(root)
        _apply_css(
            self,
            """
            window {
              background-color: #181e27;
              color: #cdd6e2;
              border: 1px solid #303a46;
              border-radius: 6px;
            }
            label { color: #eef2f7; }
            """,
        )
        self.set_state("待机", "#79b88c")
        self.set_idle_size()
        self.connect("delete-event", lambda *_args: True)

    def set_idle_size(self) -> None:
        self.preview.set_text("")
        self.preview.hide()
        self.resize(154, 38)

    def show_bottom_center(self) -> None:
        self.preview.show()
        self.resize(560, 136)
        display = Gdk.Display.get_default()
        if display is None:
            self.show_all()
            return
        seat = display.get_default_seat()
        _screen, pointer_x, pointer_y = seat.get_pointer().get_position()
        monitor = display.get_monitor_at_point(pointer_x, pointer_y) or display.get_primary_monitor()
        area = monitor.get_geometry()
        x = area.x + (area.width - 560) // 2
        y = area.y + area.height - 136 - 48
        self.move(x, y)
        self.show_all()

    def set_preview(self, stable: str, unstable: str, text: str) -> None:
        if stable + unstable != text:
            stable = text[: max(0, len(text) - len(unstable))]
        stable_markup = escape(stable).replace("\n", "&#10;")
        unstable_markup = escape(unstable).replace("\n", "&#10;")
        if unstable_markup:
            unstable_markup = (
                f'<span foreground="#ffe2a8" background="#664817" weight="bold">{unstable_markup}</span>'
            )
        self.preview.set_markup(stable_markup + unstable_markup)

    def set_state(self, label: str, color: str) -> None:
        self.label.set_text(label)
        self.dot.set_markup(f'<span foreground="{color}">●</span>')

    def _on_press(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button != 1:
            return False
        self._dragging = True
        win_x, win_y = self.get_position()
        self._drag_x = int(event.x_root) - win_x
        self._drag_y = int(event.y_root) - win_y
        return True

    def _on_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button == 1:
            self._dragging = False
        return True

    def _on_motion(self, _widget: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        if not self._dragging:
            return False
        self.move(int(event.x_root) - self._drag_x, int(event.y_root) - self._drag_y)
        return True


class ClientController:
    def __init__(self, application: Gtk.Application, *, start_capture: bool = False, open_settings: bool = False):
        self.application = application
        self.settings = load_desktop_settings()
        self.state = "idle"
        self.worker: StreamingWorker | None = None
        self.capture: AudioCapture | None = None
        self.injector = TextInjector()
        self.overlay = Overlay()
        self.hotkeys: ExclusiveX11HotKey | None = None
        self._hotkey_binding: tuple[str, frozenset[str]] | None = None
        self._shutting_down = False
        self._ipc = DesktopIpcListener(lambda command: _ui(self._handle_ipc, command))
        self._ipc.start()
        self._start_hotkey()

        AppIndicator3 = _load_appindicator()
        self.indicator = AppIndicator3.Indicator.new(
            "xgc2-stt-client",
            "audio-input-microphone",
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.menu = Gtk.Menu()
        self.capture_item = Gtk.MenuItem(label="开始录音")
        self.capture_item.connect("activate", lambda *_args: self.toggle())
        self.settings_item = Gtk.MenuItem(label="设置")
        self.settings_item.connect("activate", lambda *_args: self.open_settings())
        quit_item = Gtk.MenuItem(label="退出")
        quit_item.connect("activate", lambda *_args: self.shutdown())
        self.menu.append(self.capture_item)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.menu.append(self.settings_item)
        self.menu.append(quit_item)
        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.overlay.hide()
        self._sync_tray()
        if open_settings or not self.settings.endpoint:
            GLib.idle_add(lambda: (self.open_settings(), False)[1])
        elif start_capture:
            GLib.idle_add(lambda: (self.toggle(), False)[1])

    def _handle_ipc(self, command: str) -> None:
        if command in {"toggle", "toggle-capture"}:
            self.toggle()
        elif command == "settings":
            self.open_settings()
        elif command == "activate":
            _notify("XGC2 STT", "客户端已在状态栏运行")

    def _sync_tray(self) -> None:
        label, tooltip, enabled = {
            "idle": ("开始录音", "XGC2 STT · 待机", True),
            "connecting": ("停止录音", "XGC2 STT · 连接中", True),
            "recording": ("停止录音", "XGC2 STT · 录音中", True),
            "finalizing": ("正在收尾", "XGC2 STT · 收尾", False),
        }[self.state]
        self.capture_item.set_label(label)
        self.capture_item.set_sensitive(enabled)
        self.settings_item.set_sensitive(self.state == "idle")
        self.indicator.set_title(tooltip)

    def _start_hotkey(self) -> None:
        if self.hotkeys is not None:
            self.hotkeys.stop()
            self.hotkeys = None
            self._hotkey_binding = None
        try:
            self.hotkeys = create_hotkey_listener(self.settings.hotkey, lambda: _ui(self.toggle))
            self.hotkeys.start()
            self._hotkey_binding = parse_hotkey(self.settings.hotkey)
        except Exception as exc:
            self.hotkeys = None
            self._hotkey_binding = None
            message = str(exc)
            if is_wayland_session():
                GLib.idle_add(lambda: (_notify("XGC2 STT", message), False)[1])
                return
            GLib.idle_add(lambda: (self._show_error(message), False)[1])

    def _replace_hotkey(self, specification: str) -> None:
        binding = parse_hotkey(specification)
        if self.hotkeys is not None and binding == self._hotkey_binding:
            return
        candidate = create_hotkey_listener(specification, lambda: _ui(self.toggle))
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
        worker = StreamingWorker(
            self.settings,
            on_connected=self._start_audio,
            on_hypothesis=self._inject_hypothesis,
            on_state=lambda state: self.overlay.set_state(state, "#d7ae66"),
            on_segment=self._commit_segment,
            on_failed=self._stream_failed,
            on_completed=self._stream_completed,
        )
        self.worker = worker
        worker.start()

    def _start_audio(self) -> None:
        if self.worker is None:
            return
        try:
            self.capture = AudioCapture(self.worker.feed, lambda message: _ui(self._stream_failed, message))
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
        if self.worker is not None:
            self.worker.commit()
        else:
            self._finish_session()

    def _inject_hypothesis(self, text: str, stable: str, unstable: str) -> None:
        self.overlay.set_preview(stable, unstable, text)
        if text and not self.injector.stage(text):
            self.overlay.set_state("焦点已变", "#d7ae66")

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

    def _stream_completed(self) -> None:
        self._finish_session()

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

    def open_settings(self) -> None:
        if self.state != "idle":
            return
        dialog = SettingsDialog(self.settings)
        try:
            if dialog.run() != Gtk.ResponseType.OK:
                return
            candidate = dialog.values()
            try:
                streaming_url(candidate)
                parse_hotkey(candidate.hotkey)
            except Exception as exc:
                self._show_error_dialog("设置无效", str(exc))
                return
            try:
                self._replace_hotkey(candidate.hotkey)
            except Exception as exc:
                if not is_wayland_session():
                    self._show_error_dialog("设置无效", str(exc))
                    return
                self.hotkeys = None
                self._hotkey_binding = None
            previous = self.settings
            self.settings = candidate
            try:
                save_desktop_settings(candidate)
                set_autostart(candidate.start_at_login, packaged_client_command())
            except Exception as exc:
                self.settings = previous
                with suppress(Exception):
                    self._replace_hotkey(previous.hotkey)
                self._show_error_dialog("设置保存失败", str(exc))
        finally:
            dialog.destroy()

    def _show_error_dialog(self, title: str, message: str) -> None:
        alert = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=title,
        )
        alert.format_secondary_text(message)
        alert.run()
        alert.destroy()

    def _show_error(self, message: str) -> None:
        self.overlay.set_state("错误", "#df8589")
        self.indicator.set_title("XGC2 STT · 错误")
        _notify("XGC2 STT", message, "dialog-error")
        GLib.timeout_add(2500, self._restore_idle_status)

    def _restore_idle_status(self) -> bool:
        if self.state == "idle":
            self.overlay.set_state("待机", "#79b88c")
            self._sync_tray()
        return False

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        if self.capture is not None:
            self.capture.stop()
        if self.worker is not None:
            self.worker.cancel()
        if self.hotkeys is not None:
            with suppress(Exception):
                self.hotkeys.stop()
        if getattr(self, "_ipc", None) is not None:
            self._ipc.stop()
        self.injector.end()
        self.overlay.hide()
        self.application.quit()


class SttApplication(Gtk.Application):
    def __init__(self, *, start_capture: bool = False, open_settings: bool = False):
        Gtk.Application.__init__(
            self,
            application_id="io.xgc2.stt-client",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._start_capture = start_capture
        self._open_settings = open_settings
        self.controller: ClientController | None = None

    def do_activate(self) -> None:  # noqa: N802
        if self.controller is None:
            self.hold()
            self.controller = ClientController(
                self,
                start_capture=self._start_capture,
                open_settings=self._open_settings,
            )


def run_desktop(*, start_capture: bool = False, open_settings: bool = False) -> int:
    GLib.set_prgname(CLIENT_BINARY)
    application = SttApplication(start_capture=start_capture, open_settings=open_settings)

    def _shutdown(*_args: object) -> None:
        if application.controller is not None:
            application.controller.shutdown()

    application.connect("shutdown", _shutdown)
    return application.run([sys.argv[0]])
