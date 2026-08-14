# Bundled Qt runtime provenance

The desktop package uses the dynamically linked libraries in the official
`PySide6_Essentials 6.7.3` wheels. It does not install `PySide6_Addons`, Qt
Multimedia, FFmpeg, GStreamer, Qt Virtual Keyboard, or Qt WebEngine.

The exact Linux wheel digests recorded by the repository lock are:

| Artifact | Architecture | SHA-256 |
| --- | --- | --- |
| `PySide6_Essentials-6.7.3` | amd64 | `cda6fd26aead48f32e57f044d18aa75dc39265b49d7957f515ce7ac3989e7029` |
| `PySide6_Essentials-6.7.3` | arm64 | `acdde06b74f26e7d26b4ae1461081b32a6cb17fcaa2a580050b5e0f0f12236c9` |
| `shiboken6-6.7.3` | amd64 | `f0852e5781de78be5b13c140ec4c7fb9734e2aaf2986eb2d6a224363e03efccc` |
| `shiboken6-6.7.3` | arm64 | `f0dd635178e64a45be2f84c9f33dd79ac30328da87f834f21a0baf69ae210e6e` |

The corresponding unmodified upstream source snapshots are:

| Source | Tag | Archive | SHA-256 |
| --- | --- | --- | --- |
| Qt Base | `v6.7.3` | `https://codeload.github.com/qt/qtbase/tar.gz/refs/tags/v6.7.3` | `65771d1618cab08ec5e9bbfdc265b5d2ce2ccf0373143d7d9d139647a7196aec` |
| Qt for Python / Shiboken | `v6.7.3` | `https://codeload.github.com/pyside/pyside-setup/tar.gz/refs/tags/v6.7.3` | `d640be2fe6d21cb1879da8a13c91093d7bc591257a0a4591f051847caed4ed07` |
| ICU | `release-73-2` | `https://codeload.github.com/unicode-org/icu/tar.gz/refs/tags/release-73-2` | `c15f704e83c221c0680640a995d9db641f5b82098fb4b258a94b7d0561493c88` |

The `licenses/` directory beside this file is a mechanical extraction of the
Qt Base `LICENSES/` tree and attribution manifests, the PySide/Shiboken license
texts and attribution manifests, and ICU's `LICENSE` plus `license.html` from
those immutable source snapshots. The build copies this material unchanged to
`/usr/share/doc/xgc2-stt-client/qt-runtime/`.

The packaged client also ships the Wayland platform plugins,
`Qt6WaylandClient`, `Qt6OpenGL` (required by `libqwayland-egl.so`), and
`Qt6WlShellIntegration` from the same `PySide6_Essentials 6.7.3` wheel so
the tray UI can run on X11 and Wayland. Those libraries remain LGPL-3.0-only /
GPL-3.0-only as part of Qt for Python; compositor/server plugins are not
included.

Qt and Shiboken are shipped as replaceable shared libraries. The package does
not add an integrity mechanism that prevents an operator from relinking or
replacing them with a compatible modified build.
