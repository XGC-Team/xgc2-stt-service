# Bundled Python runtime provenance

The `xgc2-stt-client` package embeds CPython from the following immutable
python-build-standalone release:

- CPython: `3.12.12`
- release: `20251217`
- source commit: `85fdc74d0153799b6807702865a8a29df3ced47a`
- source archive SHA-256:
  `6f1c7e017161716065c2c30c4dc6ea8564d8aaba9bddb878fd9b1da9697dc212`
- amd64 install archive SHA-256:
  `9f5474351378aeca746ee8a2ff3b187edec71d791ef92827eca14ab5b0e15441`
- arm64 install archive SHA-256:
  `6b9dfd582900666c42baef1ad495fa68948964a8bff0dc3bccd0393febc2de7b`

The package build verifies the selected architecture archive against this
digest before creating the Python environment. It also verifies and extracts
the source snapshot for the same release commit. That snapshot's
`pythonbuild/downloads.py` is shipped beside this document and is the
authoritative component-version, source-URL, source-digest and license map for
the embedded runtime. Its complete `LICENSE*.txt` bundle and
`python-licenses.rst` are shipped in the same directory.

The source snapshot is available at:

`https://github.com/astral-sh/python-build-standalone/tree/85fdc74d0153799b6807702865a8a29df3ced47a`

The architecture archives are release assets at:

`https://github.com/astral-sh/python-build-standalone/releases/tag/20251217`
