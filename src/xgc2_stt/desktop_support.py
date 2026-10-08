from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

DEFAULT_HOTKEY = "<f9>"
CLIENT_BINARY = "xgc2-stt-client"
CLIENT_VERSION = "0.2.1"
IPC_SOCKET_NAME = "desktop.sock"

_MODIFIER_ALIASES = {
    "alt": "alt",
    "alt_l": "alt",
    "alt_r": "alt",
    "cmd": "super",
    "cmd_l": "super",
    "cmd_r": "super",
    "control": "ctrl",
    "ctrl": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "shift": "shift",
    "shift_l": "shift",
    "shift_r": "shift",
    "super": "super",
    "win": "super",
    "meta": "super",
}
_KEY_ALIASES = {
    "backspace": "BackSpace",
    "enter": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "return": "Return",
    "space": "space",
    "spacebar": "space",
    "tab": "Tab",
}


@dataclass(frozen=True)
class DesktopSettings:
    schema_version: int = 1
    endpoint: str = ""
    api_key: str = ""
    hotkey: str = DEFAULT_HOTKEY
    output_script: str = "simplified"
    trim_leading_silence: bool = True
    silence_commit_ms: int = 2000
    paste_shortcut: str = "terminal"
    auto_enter: bool = False
    start_at_login: bool = False


@dataclass(frozen=True)
class InsertionOutcome:
    copied: bool
    pasted: bool
    method: str
    detail: str = ""


def default_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "xgc2-stt" / "client.json"


def load_desktop_settings(path: Path | None = None) -> DesktopSettings:
    target = path or default_config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return DesktopSettings()
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        return DesktopSettings()
    allowed = {field.name for field in fields(DesktopSettings)}
    values = {key: value for key, value in raw.items() if key in allowed}
    try:
        return DesktopSettings(**values)
    except TypeError:
        return DesktopSettings()


def save_desktop_settings(settings: DesktopSettings, path: Path | None = None) -> Path:
    target = path or default_config_path()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    with NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as temporary:
        json.dump(asdict(settings), temporary, ensure_ascii=False, indent=2)
        temporary.write("\n")
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o600)
    temporary_path.replace(target)
    return target


def streaming_url(settings: DesktopSettings) -> str:
    endpoint = urlparse(settings.endpoint.strip())
    if endpoint.scheme not in {"http", "https", "ws", "wss"} or not endpoint.netloc:
        raise ValueError("服务器地址必须是 http(s) 或 ws(s) URL")
    scheme = {"http": "ws", "https": "wss"}.get(endpoint.scheme, endpoint.scheme)
    query = dict(parse_qsl(endpoint.query, keep_blank_values=True))
    # Older deployments accepted browser credentials in this query field.
    # A native client can send a real Authorization header, so never preserve
    # a credential-bearing value from a pasted endpoint URL.
    query.pop("access_token", None)
    query.update(
        {
            "sample_rate": "16000",
            "output_script": settings.output_script,
            "trim_leading_silence": "1" if settings.trim_leading_silence else "0",
            "silence_commit_ms": str(settings.silence_commit_ms),
        }
    )
    return urlunparse(
        (
            scheme,
            endpoint.netloc,
            "/v1/audio/transcriptions/stream",
            "",
            urlencode(query),
            "",
        )
    )


def streaming_headers(settings: DesktopSettings) -> dict[str, str]:
    """Keep credentials out of URLs and reverse-proxy access logs."""
    if not settings.api_key:
        return {}
    return {"Authorization": f"Bearer {settings.api_key}"}


def parse_hotkey(specification: str) -> tuple[str, frozenset[str]]:
    """Parse `<f9>` / `<ctrl>+<shift>+r` into an X11 keysym name and modifiers."""
    raw = specification.strip()
    if not raw:
        raise ValueError("快捷键不能为空")
    modifiers: set[str] = set()
    keys: list[str] = []
    for part in raw.split("+"):
        token = part.strip().lower()
        if len(token) >= 3 and token.startswith("<") and token.endswith(">"):
            token = token[1:-1]
        if not token:
            raise ValueError("快捷键格式无效")
        if token in _MODIFIER_ALIASES:
            modifiers.add(_MODIFIER_ALIASES[token])
            continue
        keys.append(token)
    if len(keys) != 1:
        raise ValueError("快捷键必须包含且只能包含一个非修饰键")
    key = keys[0]
    if key.startswith("f") and key[1:].isdigit():
        keysym_name = f"F{int(key[1:])}"
    elif key in _KEY_ALIASES:
        keysym_name = _KEY_ALIASES[key]
    elif len(key) == 1:
        keysym_name = key
    else:
        keysym_name = key
    return keysym_name, frozenset(modifiers)


def replacement_plan(previous: str, current: str) -> tuple[int, str]:
    common = 0
    limit = min(len(previous), len(current))
    while common < limit and previous[common] == current[common]:
        common += 1
    return len(previous) - common, current[common:]


def should_auto_enter(enabled: bool, final_reason: str) -> bool:
    """Only a server-confirmed silence boundary may submit the focused input."""
    return enabled and final_reason == "silence"


def desktop_session_type() -> str:
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session in {"wayland", "x11", "mir", "tty"}:
        return session
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def is_wayland_session() -> bool:
    return desktop_session_type() == "wayland"


def packaged_client_command() -> list[str]:
    binary = shutil.which(CLIENT_BINARY)
    if binary:
        return [binary]
    return [sys.executable, "-m", "xgc2_stt.desktop_cli"]


def autostart_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "autostart" / "xgc2-stt-client.desktop"


def autostart_file(command: list[str] | None = None) -> str:
    executable = command or packaged_client_command()
    exec_line = " ".join(shlex.quote(part) for part in executable)
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=XGC2 STT Client",
            "Comment=Tray client for a self-hosted streaming STT API",
            f"Exec={exec_line}",
            "Terminal=false",
            "StartupNotify=false",
            "X-GNOME-Autostart-enabled=true",
            "",
        ]
    )


def set_autostart(enabled: bool, command: list[str] | None = None) -> Path:
    target = autostart_path()
    if not enabled:
        target.unlink(missing_ok=True)
        return target
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as temporary:
        temporary.write(autostart_file(command))
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o644)
    temporary_path.replace(target)
    return target


def parse_desktop_cli(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=CLIENT_BINARY,
        description=(
            "XGC2 STT desktop client. Running the command starts a system-tray "
            "session. Supply your own STT service URL and API key in Settings."
        ),
        epilog=(
            "Autostart is off unless enabled in Settings. On Wayland, bind a "
            "compositor shortcut to 'xgc2-stt-client --toggle-capture' if a "
            "global grab is unavailable."
        ),
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the client version and exit",
    )
    parser.add_argument(
        "--toggle-capture",
        action="store_true",
        help="Toggle microphone capture on the running tray process",
    )
    parser.add_argument(
        "--settings",
        action="store_true",
        help="Open Settings on the running tray process",
    )
    return parser.parse_args(argv)


def format_desktop_version() -> str:
    return f"{CLIENT_BINARY} {CLIENT_VERSION}"


def ipc_socket_path() -> Path:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    return runtime / "xgc2-stt" / IPC_SOCKET_NAME


def send_running_instance(command: str, timeout: float = 1.0) -> bool:
    from xgc2_xrpc import Client, Fault, Limits, Runtime, TransportError

    if command not in {"ping", "toggle", "settings", "activate"}:
        return False
    path = ipc_socket_path()
    if not path.exists():
        return False
    try:
        with Runtime.from_environment(
            blocking_workers=1,
            defaults={
                "HOST_MAX_IN_FLIGHT": 2,
                "HOST_MAX_CONNECTIONS": 2,
                "CLIENT_MAX_REFERENCES": 2,
                "CLIENT_MAX_CONNECTIONS": 2,
                "MAX_REQUEST_BYTES": 1024,
                "MAX_RESPONSE_BYTES": 1024,
            },
            ceilings={
                "HOST_MAX_IN_FLIGHT": 2,
                "HOST_MAX_CONNECTIONS": 2,
                "CLIENT_MAX_REFERENCES": 2,
                "CLIENT_MAX_CONNECTIONS": 2,
                "MAX_REQUEST_BYTES": 1024,
                "MAX_RESPONSE_BYTES": 1024,
            },
        ) as runtime:
            with Client(str(path), runtime=runtime, limits=Limits(body_bytes=1024, response_bytes=1024)) as discovery:
                instance = discovery.json("/v1/instance", method="GET", timeout=timeout)["instance_id"]
            if command == "ping":
                return True
            with Client(
                str(path), runtime=runtime, instance_id=instance, limits=Limits(body_bytes=1024, response_bytes=1024)
            ) as client:
                return client.json("/v1/command", {"command": command}, timeout=timeout).get("accepted") is True
    except (OSError, RuntimeError, ValueError, KeyError, Fault, TransportError):
        return False


class DesktopIpcListener:
    """XRPC endpoint owned until all command handlers have quiesced."""

    def __init__(self, handler: Callable[[str], None]):
        self._handler = handler
        self._runtime: Any = None
        self._host: Any = None
        self.path = ipc_socket_path()
        self.instance_id = secrets.token_hex(16)

    def start(self) -> None:
        from xgc2_xrpc import Host, Limits, Runtime

        if self._runtime is not None:
            raise RuntimeError("desktop IPC already started")
        self.path.parent.mkdir(mode=0o700, exist_ok=True)
        runtime = Runtime.from_environment(
            blocking_workers=1,
            defaults={
                "HOST_MAX_IN_FLIGHT": 1,
                "HOST_MAX_CONNECTIONS": 4,
                "CLIENT_MAX_REFERENCES": 1,
                "MAX_REQUEST_BYTES": 1024,
                "MAX_RESPONSE_BYTES": 1024,
                "CALL_TIMEOUT_MS": 1000,
                "SHUTDOWN_TIMEOUT_MS": 2000,
            },
            ceilings={
                "HOST_MAX_IN_FLIGHT": 1,
                "HOST_MAX_CONNECTIONS": 4,
                "CLIENT_MAX_REFERENCES": 1,
                "MAX_REQUEST_BYTES": 1024,
                "MAX_RESPONSE_BYTES": 1024,
            },
        )
        host = Host(
            str(self.path),
            {
                ("GET", "/v1/instance"): self._instance,
                ("POST", "/v1/command"): self._command,
            },
            runtime=runtime,
            limits=Limits(connections=4, in_flight=1, body_bytes=1024, response_bytes=1024, call_timeout=1),
            instance_id=self.instance_id,
            discovery_routes=("/v1/instance",),
            reclaim_unreachable=True,
            allowed_uids={os.getuid()},
        )
        try:
            host.start()
        except BaseException:
            runtime.close()
            raise
        self._runtime, self._host = runtime, host

    async def _instance(self, _context: Any, _payload: Any) -> dict[str, str]:
        return {"instance_id": self.instance_id}

    def _command(self, _context: Any, payload: Any) -> dict[str, bool]:
        from xgc2_xrpc import Fault

        if (
            not isinstance(payload, dict)
            or set(payload) != {"command"}
            or not isinstance(payload["command"], str)
            or payload["command"]
            not in {
                "toggle",
                "settings",
                "activate",
            }
        ):
            raise Fault("invalid_argument", "unknown desktop command")
        self._handler(payload["command"])
        # The GUI dispatcher accepted the command; this is not capture completion.
        return {"accepted": True}

    def stop(self) -> None:
        if self._host is not None:
            self._host.close()
            self._host = None
        if self._runtime is not None:
            self._runtime.close()
            self._runtime = None


def paste_xdotool_chord(paste_shortcut: str) -> str:
    return "ctrl+shift+v" if paste_shortcut == "terminal" else "ctrl+v"


def _wtype_paste_args(binary: str, paste_shortcut: str) -> list[str]:
    if paste_shortcut == "terminal":
        return [binary, "-M", "ctrl", "-M", "shift", "v", "-m", "shift", "-m", "ctrl"]
    return [binary, "-M", "ctrl", "v", "-m", "ctrl"]


def _run_ok(
    command: list[str],
    *,
    run: Callable[..., Any],
    stdin: bytes | None = None,
) -> bool:
    options: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "timeout": 2,
        "check": False,
    }
    if stdin is None:
        options["stdin"] = subprocess.DEVNULL
    else:
        options["input"] = stdin
    try:
        completed = run(command, **options)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False
    return getattr(completed, "returncode", 1) == 0


def insert_finalized_text(
    text: str,
    *,
    paste_shortcut: str = "terminal",
    session_type: str | None = None,
    send_enter: bool = False,
    set_clipboard: Callable[[str], None] | None = None,
    which: Callable[[str], str | None] | None = None,
    run: Callable[..., Any] | None = None,
) -> InsertionOutcome:
    """Copy finalized text, then paste into the focused field when possible.

    X11 uses xclip/xdotool when present. Wayland prefers the compositor
    clipboard (Qt and/or wl-copy) plus wtype/ydotool when those helpers exist.
    If a paste keystroke cannot be synthesized, the clipboard still holds the
    text so the user can press Ctrl+V (IME-safe fallback).
    """
    if not text:
        return InsertionOutcome(copied=False, pasted=False, method="empty")
    which = which or shutil.which
    run = run or subprocess.run
    session = (session_type or desktop_session_type()).lower()
    copied = False
    if set_clipboard is not None:
        set_clipboard(text)
        copied = True

    copy_commands: list[tuple[list[str], bytes | None]] = []
    wl_copy = which("wl-copy")
    if wl_copy and session == "wayland":
        copy_commands.append(([wl_copy], text.encode("utf-8")))
    xclip = which("xclip")
    if xclip:
        copy_commands.append(([xclip, "-selection", "clipboard", "-in"], text.encode("utf-8")))
    xsel = which("xsel")
    if xsel:
        copy_commands.append(([xsel, "--clipboard", "--input"], text.encode("utf-8")))
    for command, payload in copy_commands:
        if _run_ok(command, run=run, stdin=payload):
            copied = True
            break

    if not copied:
        return InsertionOutcome(
            copied=False,
            pasted=False,
            method="failed",
            detail="clipboard unavailable",
        )

    chord = paste_xdotool_chord(paste_shortcut)
    paste_attempts: list[tuple[str, list[str]]] = []
    xdotool = which("xdotool")
    wtype = which("wtype")
    ydotool = which("ydotool")
    if session == "wayland":
        if wtype:
            paste_attempts.append(("wtype", _wtype_paste_args(wtype, paste_shortcut)))
        if ydotool:
            paste_attempts.append(("ydotool", [ydotool, "key", chord]))
        if xdotool:
            paste_attempts.append(("xdotool", [xdotool, "key", "--clearmodifiers", chord]))
    else:
        if xdotool:
            paste_attempts.append(("xdotool", [xdotool, "key", "--clearmodifiers", chord]))
        if wtype:
            paste_attempts.append(("wtype", _wtype_paste_args(wtype, paste_shortcut)))

    pasted = False
    method = "clipboard"
    for name, command in paste_attempts:
        if _run_ok(command, run=run):
            pasted = True
            method = name
            break

    if pasted and send_enter:
        enter_attempts: list[list[str]] = []
        if xdotool:
            enter_attempts.append([xdotool, "key", "--clearmodifiers", "Return"])
        if wtype:
            enter_attempts.append([wtype, "-k", "Return"])
        if ydotool:
            enter_attempts.append([ydotool, "key", "Return"])
        for command in enter_attempts:
            if _run_ok(command, run=run):
                break

    return InsertionOutcome(copied=True, pasted=pasted, method=method)
