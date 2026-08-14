# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import get_package_paths

repo_root = Path(SPECPATH).parents[1]
pyside_root = Path(get_package_paths("PySide6")[1])
# PySide6 6.7.3 already packages for Ubuntu 20.04 (manylinux_2_28). Keep the
# same Qt runtime for 22.04/24.04 and ship both xcb and Wayland plugins so the
# tray client can run on either session type without an Electron/Tauri stack.
required_plugins = {
    "platforms/libqxcb.so",
    "platforms/libqwayland-generic.so",
    "platforms/libqwayland-egl.so",
    "platforminputcontexts/libcomposeplatforminputcontextplugin.so",
    "platforminputcontexts/libibusplatforminputcontextplugin.so",
    "wayland-shell-integration/libxdg-shell.so",
    "wayland-shell-integration/libwl-shell-plugin.so",
    "wayland-graphics-integration-client/libqt-plugin-wayland-egl.so",
    "wayland-decoration-client/libbradient.so",
}
# Wayland EGL plugins DT_NEEDED libQt6OpenGL.so.6; libwl-shell-plugin.so
# DT_NEEDED libQt6WlShellIntegration.so.6. Collect them explicitly so the
# tray runtime is complete even if PyInstaller does not follow plugin
# dependencies into the Qt allowlist below.
required_qt_shared_libraries = {
    "libQt6OpenGL.so.6",
    "libQt6WlShellIntegration.so.6",
}
plugin_binaries = [
    (str(pyside_root / "Qt" / "plugins" / relative), f"PySide6/Qt/plugins/{Path(relative).parent}")
    for relative in sorted(required_plugins)
]
qt_lib_binaries = []
for name in sorted(required_qt_shared_libraries):
    source = pyside_root / "Qt" / "lib" / name
    if not source.is_file():
        raise SystemExit(f"Pinned PySide6 wheel is missing {source}")
    qt_lib_binaries.append((str(source), "PySide6/Qt/lib"))
analysis = Analysis(
    [str(repo_root / ".xgc2" / "desktop" / "launcher.py")],
    pathex=[str(repo_root / "src")],
    binaries=plugin_binaries + qt_lib_binaries,
    datas=[],
    hiddenimports=[
        "sounddevice",
        "pynput.keyboard._xorg",
        "pynput.mouse._xorg",
        "pynput.keyboard._uinput",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "_dbm",
        "_tkinter",
        "av",
        "dbm",
        "fastapi",
        "httpx",
        "numpy",
        "opencc",
        "PySide6.QtNetwork",
        "pydantic",
        "pynvml",
        "tkinter",
        "uvicorn",
    ],
    noarchive=False,
    optimize=0,
)
# The application ships its locked CPython and wheel libraries, but system ABI
# libraries belong to the Deb dependency graph. PyInstaller always preserves
# the Python runtime and lib-dynload extensions when this method is used.
analysis.exclude_system_libraries()

# Keep an auditable Qt allowlist. Include Wayland client libraries so the tray
# can use the native compositor, plus Qt OpenGL for the Wayland EGL plugin.
# Drop compositor/server, VNC, eglfs and multimedia plugins that this client
# does not use.
allowed_qt_libraries = {
    "libQt6Core.so.6",
    "libQt6DBus.so.6",
    "libQt6Gui.so.6",
    "libQt6Widgets.so.6",
    "libQt6XcbQpa.so.6",
    "libQt6WaylandClient.so.6",
    "libQt6WaylandEglClientHwIntegration.so.6",
    "libicudata.so.73",
    "libicui18n.so.73",
    "libicuuc.so.73",
} | required_qt_shared_libraries
filtered_binaries = []
for entry in analysis.binaries:
    destination = entry[0].replace("\\", "/")
    if destination.startswith("PySide6/Qt/plugins/"):
        relative = destination.removeprefix("PySide6/Qt/plugins/")
        if relative not in required_plugins:
            continue
    if destination.startswith("PySide6/Qt/lib/") and Path(destination).name not in allowed_qt_libraries:
        continue
    filtered_binaries.append(entry)
analysis.binaries = filtered_binaries
pyz = PYZ(analysis.pure)
executable = EXE(
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="xgc2-stt-client",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
collection = COLLECT(
    executable,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    name="xgc2-stt-client",
)
