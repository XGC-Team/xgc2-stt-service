# Third-party notices

The XGC2 STT release images redistribute the following components and model
weights:

| Component | Pinned source | License |
| --- | --- | --- |
| vLLM | `vllm/vllm-openai:v0.14.0` (`sha256:1d6866b8...`) | Apache-2.0 |
| Qwen3-ASR-1.7B | `Qwen/Qwen3-ASR-1.7B@7278e1e70fe206f11671096ffdd38061171dd6e5` | Apache-2.0 |
| XGC2 UI React | release package `v0.3.0` | See the package release |

The upstream model cards and notices are retained inside each embedded model
directory. Apache-2.0 components remain subject to their upstream copyright,
patent, trademark, attribution, and redistribution terms. The repository's MIT
license applies only to original XGC2 STT service code and does not replace
third-party licenses.

The `xgc2-stt-client` Debian package does not bundle a Python interpreter or Qt.
It depends on distribution packages and keeps original XGC2 client code under
the repository MIT license.

| Component | Source | License |
| --- | --- | --- |
| CPython | Ubuntu `python3` | Python-2.0 and CNRI-Python |
| GTK 3 / PyGObject | Ubuntu `python3-gi`, `gir1.2-gtk-3.0` | LGPL-2.1-or-later |
| Ayatana AppIndicator or AppIndicator3 | Ubuntu `gir1.2-ayatanaappindicator3-0.1` or `gir1.2-appindicator3-0.1` | GPL-3.0-or-later / LGPL-2.1-or-later |
| websocket-client | Ubuntu `python3-websocket` | Apache-2.0 |
| PyAudio / PortAudio | Ubuntu `python3-pyaudio` | MIT |
| python-xlib | Ubuntu `python3-xlib` | LGPL-2.1-or-later |

Debian systems also provide the GNU license texts under
`/usr/share/common-licenses`.

- https://github.com/vllm-project/vllm
- https://huggingface.co/Qwen/Qwen3-ASR-1.7B
- https://gitlab.gnome.org/GNOME/gtk
- https://github.com/AyatanaIndicators/libayatana-appindicator
- https://github.com/websocket-client/websocket-client
- https://github.com/python-xlib/python-xlib
- https://github.com/PortAudio/portaudio
