# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

from PyInstaller.utils.hooks import get_package_paths

repo_root = Path(SPECPATH).parents[1]
pyside_root = Path(get_package_paths("PySide6")[1])
required_plugins = {
    "platforms/libqxcb.so",
    "platforminputcontexts/libcomposeplatforminputcontextplugin.so",
    "platforminputcontexts/libibusplatforminputcontextplugin.so",
}
plugin_binaries = [
    (str(pyside_root / "Qt" / "plugins" / relative), f"PySide6/Qt/plugins/{Path(relative).parent}")
    for relative in sorted(required_plugins)
]
analysis = Analysis(
    [str(repo_root / ".xgc2" / "desktop" / "launcher.py")],
    pathex=[str(repo_root / "src")],
    binaries=plugin_binaries,
    datas=[],
    hiddenimports=["pynput.keyboard._xorg", "pynput.mouse._xorg"],
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
        "mistral_common",
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

# PyInstaller's Qt hooks intentionally collect plugins for many deployment
# targets. This package is explicitly X11-only, so keep a small auditable
# allowlist and drop Wayland, embedded, VNC, image-codec and platform-theme
# plugins together with the now-unreferenced Qt modules they pulled in.
allowed_qt_libraries = {
    "libQt6Core.so.6",
    "libQt6DBus.so.6",
    "libQt6Gui.so.6",
    "libQt6Widgets.so.6",
    "libQt6XcbQpa.so.6",
    "libicudata.so.73",
    "libicui18n.so.73",
    "libicuuc.so.73",
}
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
