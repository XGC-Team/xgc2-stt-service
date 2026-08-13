# Third-party notices

The XGC2 STT release images redistribute the following components and model
weights:

| Component | Pinned source | License |
| --- | --- | --- |
| vLLM | `vllm/vllm-openai:v0.27.1-cu129` (`sha256:07913e94...`) | Apache-2.0 |
| Qwen3-ASR-1.7B | `Qwen/Qwen3-ASR-1.7B@7278e1e70fe206f11671096ffdd38061171dd6e5` | Apache-2.0 |
| Voxtral Mini 4B Realtime 2602 | `mistralai/Voxtral-Mini-4B-Realtime-2602@2769294da9567371363522aac9bbcfdd19447add` | Apache-2.0 |
| XGC2 UI React | release package `v0.3.0` | See the package release |

The upstream model cards and notices are retained inside each embedded model
directory. Apache-2.0 components remain subject to their upstream copyright,
patent, trademark, attribution, and redistribution terms. The repository's MIT
license applies only to original XGC2 STT service code and does not replace
third-party licenses.

The `xgc2-stt-client` Debian package additionally bundles a CPython runtime
and desktop dependencies. CPython comes from python-build-standalone release
`20251217`, source commit
`85fdc74d0153799b6807702865a8a29df3ced47a`. Its selected architecture
archive is verified by SHA-256 during every package build.

| Component | Pinned version | License |
| --- | --- | --- |
| python-build-standalone build tooling | 20251217 / `85fdc74d…` | MPL-2.0 |
| CPython | 3.12.12 | Python-2.0 and CNRI-Python |
| Berkeley DB | 6.0.19 | Sleepycat |
| bzip2 | 1.0.8 | bzip2-1.0.6 |
| Expat | 2.6.3 | MIT |
| libedit | 20240808-3.1 | BSD-3-Clause |
| libffi | 3.4.6 | MIT |
| libX11 | 1.6.12 | MIT and X11 |
| libXau | 1.0.11 | MIT |
| libxcb | 1.17.0 | MIT |
| mpdecimal | 4.0.0 | BSD-2-Clause |
| ncurses | 6.5 | X11 |
| OpenSSL | 3.5.4 | Apache-2.0 |
| SQLite | 3.50.4.0 | Public domain |
| Tcl / Tk | 8.6.14 | TCL |
| Tix license material | 8.4.3.6 | TCL |
| libuuid | 1.0.3 | BSD-3-Clause |
| xz / liblzma | 5.8.1 | 0BSD |
| zlib | 1.3.1 | Zlib |
| PySide6 Essentials / Qt for Python / Shiboken6 | 6.7.3 | LGPL-3.0-only or GPL-3.0-only; commercial terms are also available upstream |
| ICU | 73.2 | Unicode-3.0 and bundled third-party terms |
| sounddevice | 0.5.5 | MIT |
| CFFI | 2.1.1 | MIT |
| PortAudio (system package) | distribution version | MIT |
| pynput | 1.8.2 | LGPL-3.0-or-later |
| python-xlib | 0.33 | LGPL-2.1-or-later |
| evdev | 1.9.3 | BSD-3-Clause |
| six | 1.17.0 | MIT |
| websockets | 17.0.1 | BSD-3-Clause |
| PyInstaller bootloader | 6.16.0 | GPL-2.0-or-later with the PyInstaller bootloader exception |

Some Python extension dependencies are statically linked by
python-build-standalone. Qt, Shiboken and ICU stay as replaceable shared
libraries; PortAudio is supplied by the operating system. The client does not
ship PySide6 Addons, Qt Multimedia, FFmpeg, GStreamer, Qt Virtual Keyboard or
Qt WebEngine. The Debian package archives the full upstream Python
license bundle, its component/source/digest map, and immutable build
provenance under
`/usr/share/doc/xgc2-stt-client/python-build-standalone/`. Python wheel
metadata and license files remain in the application directory where provided
by upstream. Debian systems also provide the GNU license texts under
`/usr/share/common-licenses`.

The package also archives the Qt Base and PySide/Shiboken license sets and
attribution manifests, ICU license and third-party notice, exact Linux wheel
digests, and immutable source snapshot URLs under
`/usr/share/doc/xgc2-stt-client/qt-runtime/`.

Berkeley DB, Tcl, Tk, Tix and Tcl Thread appear in the upstream standalone
component ledger, but the desktop package does not use their `_dbm` or
`_tkinter` extension modules. The build excludes those modules and fails if
their native libraries appear in the final application tree. Their entries and
license texts remain in this complete upstream ledger for auditability; they
do not describe shipped client functionality.

- https://github.com/vllm-project/vllm
- https://huggingface.co/Qwen/Qwen3-ASR-1.7B
- https://huggingface.co/mistralai/Voxtral-Mini-4B-Realtime-2602
- https://www.python.org/downloads/release/python-31212/
- https://github.com/astral-sh/python-build-standalone/tree/85fdc74d0153799b6807702865a8a29df3ced47a
- https://code.qt.io/cgit/pyside/pyside-setup.git/
- https://github.com/moses-palmer/pynput
- https://github.com/python-xlib/python-xlib
- https://github.com/gvalkov/python-evdev
- https://github.com/benjaminp/six
- https://github.com/python-websockets/websockets
- https://github.com/pyinstaller/pyinstaller
- https://github.com/spatialaudio/python-sounddevice
- https://github.com/python-cffi/cffi
- https://github.com/PortAudio/portaudio
- https://github.com/unicode-org/icu/tree/release-73-2
