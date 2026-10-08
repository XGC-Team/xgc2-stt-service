from __future__ import annotations

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from xgc2_stt.api_keys import ApiKeyStore
from xgc2_stt.runtime_settings import RuntimeSettingsStore, RuntimeTuning


def test_concurrent_settings_cas_has_one_winner_and_recovers(tmp_path: Path) -> None:
    store = RuntimeSettingsStore(str(tmp_path / "settings.json"))

    def save(value: int) -> str:
        try:
            store.update(RuntimeTuning(silence_commit_ms=value), 0)
            return "saved"
        except RuntimeError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(save, (2000, 2500))) == ["conflict", "saved"]
    recovered = RuntimeSettingsStore(str(store.path))
    assert recovered.load() == store.values
    assert recovered.revision == store.revision == 1
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_failed_settings_save_keeps_revision_and_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = RuntimeSettingsStore(str(tmp_path / "settings.json"))
    store.update(RuntimeTuning(silence_commit_ms=2000), 0)
    before = store.path.read_bytes()

    def fail_replace(_self: Path, _target: Path) -> None:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        store.update(RuntimeTuning(silence_commit_ms=2500), 1)
    assert store.revision == 1 and store.values == {"silence_commit_ms": 2000}
    assert store.path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [store.path]


def test_legacy_settings_require_explicit_distinct_import(tmp_path: Path) -> None:
    source = tmp_path / "original.json"
    before = b'{"schema_version":1,"values":{"silence_commit_ms":2500}}'
    source.write_bytes(before)
    with pytest.raises(ValueError, match="explicit"):
        RuntimeSettingsStore(str(source)).load()
    spec = importlib.util.spec_from_file_location(
        "settings_migration",
        Path(__file__).resolve().parents[1] / "scripts/migrate-runtime-settings.py",
    )
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    destination = tmp_path / "imported.json"
    migration.migrate(source, destination)
    assert source.read_bytes() == before
    assert json.loads(destination.read_bytes())["revision"] == 1
    with pytest.raises(ValueError, match="absent"):
        migration.migrate(source, destination)


def test_key_quota_and_oversized_store_fail_closed(tmp_path: Path) -> None:
    store = ApiKeyStore(str(tmp_path / "keys.json"))
    store.MAX_KEYS = 1
    store.create("one")
    with pytest.raises(ValueError, match="quota"):
        store.create("two")
    assert len(store.list_public()) == 1
    store.path.write_bytes(b" " * (store.MAX_BYTES + 1))
    reloaded = ApiKeyStore(str(store.path))
    reloaded._load()
    assert reloaded.requires_authentication and reloaded.authenticate(None) is None
