from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


@dataclass(frozen=True)
class DesktopSettings:
    endpoint: str = "http://127.0.0.1:34897"
    api_key: str = ""
    hotkey: str = "<ctrl>+<alt>+space"
    output_script: str = "simplified"
    trim_leading_silence: bool = True
    paste_shortcut: str = "terminal"
    auto_enter: bool = False
    start_at_login: bool = False


def default_config_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "xgc2-stt" / "client.json"


def load_desktop_settings(path: Path | None = None) -> DesktopSettings:
    target = path or default_config_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return DesktopSettings()
    if not isinstance(raw, dict):
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
    query.update(
        {
            "sample_rate": "16000",
            "output_script": settings.output_script,
            "trim_leading_silence": "1" if settings.trim_leading_silence else "0",
        }
    )
    if settings.api_key:
        query["access_token"] = settings.api_key
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


def replacement_plan(previous: str, current: str) -> tuple[int, str]:
    common = 0
    for before, after in zip(previous, current, strict=False):
        if before != after:
            break
        common += 1
    return len(previous) - common, current[common:]


def should_auto_enter(enabled: bool, final_reason: str) -> bool:
    """Only a server-confirmed silence boundary may submit the focused input."""
    return enabled and final_reason == "silence"


def autostart_file(command: list[str] | None = None) -> str:
    executable = command or [sys.executable, "-m", "xgc2_stt.desktop"]
    exec_line = " ".join(shlex.quote(part) for part in executable)
    return "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=XGC2 STT Client",
            f"Exec={exec_line}",
            "Terminal=false",
            "X-GNOME-Autostart-enabled=true",
            "",
        ]
    )


def set_autostart(enabled: bool, command: list[str] | None = None) -> Path:
    target = Path.home() / ".config" / "autostart" / "xgc2-stt-client.desktop"
    if not enabled:
        target.unlink(missing_ok=True)
        return target
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as temporary:
        temporary.write(autostart_file(command))
        temporary_path = Path(temporary.name)
    os.chmod(temporary_path, 0o700)
    temporary_path.replace(target)
    return target
