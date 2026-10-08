#!/usr/bin/env python3
"""Explicit offline schema 1 import to a distinct schema 2 destination."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from xgc2_stt.runtime_settings import RuntimeSettingsStore, RuntimeTuning


def migrate(source: Path, destination: Path) -> None:
    if source.resolve() == destination.resolve() or destination.exists():
        raise ValueError("migration requires a distinct absent destination; retain the original")
    with source.open("rb") as file:
        raw = file.read(16_385)
    if len(raw) > 16_384:
        raise ValueError("source exceeds settings quota")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "values"} or payload["schema_version"] != 1:
        raise ValueError("exact schema 1 source required")
    tuning = RuntimeTuning.model_validate(payload["values"])
    # Reserve the destination before any write, so an existing file is never replaced.
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        store = RuntimeSettingsStore(str(destination))
        store.update(tuning, 0)
        check = RuntimeSettingsStore(str(destination))
        assert check.load() == tuning.model_dump(exclude_none=True) and check.revision == 1
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    migrate(arguments.source, arguments.destination)
