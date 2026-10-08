from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, ConfigDict, Field


class RuntimeTuning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    silence_commit_ms: int | None = Field(default=None, ge=500, le=30_000)
    max_active_streams: int | None = Field(default=None, ge=1, le=128)
    gpu_memory_utilization: float | None = Field(default=None, gt=0.1, le=0.98)
    max_model_len: int | None = Field(default=None, ge=2048, le=262_144)
    max_num_seqs: int | None = Field(default=None, ge=1, le=128)
    vllm_enforce_eager: bool | None = None
    qwen_chunk_size_seconds: float | None = Field(default=None, ge=0.25, le=5.0)
    qwen_unfixed_chunk_num: int | None = Field(default=None, ge=1, le=16)
    qwen_unfixed_token_num: int | None = Field(default=None, ge=1, le=64)


HOT_FIELDS = {"silence_commit_ms", "max_active_streams"}
ENGINE_RESTART_FIELDS = set(RuntimeTuning.model_fields) - HOT_FIELDS


class RuntimeSettingsStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.values: dict[str, int | float | bool] = {}
        self.revision = 0
        self._lock = threading.Lock()

    def load(self) -> dict[str, int | float | bool]:
        try:
            with self.path.open("rb") as source:
                raw_bytes = source.read(16_385)
            if len(raw_bytes) > 16_384:
                raise ValueError("runtime settings exceed 16 KiB quota")
            raw = json.loads(raw_bytes)
            if (
                not isinstance(raw, dict)
                or set(raw) != {"schema_version", "revision", "values"}
                or raw["schema_version"] != 2
                or type(raw["revision"]) is not int
                or raw["revision"] < 1
            ):
                raise ValueError("runtime settings require explicit schema 2 migration")
            tuning = RuntimeTuning.model_validate(raw["values"])
        except FileNotFoundError:
            return {}
        self.revision = raw["revision"]
        self.values = tuning.model_dump(exclude_none=True)
        return dict(self.values)

    def update(self, tuning: RuntimeTuning, expected_revision: int) -> dict[str, int | float | bool]:
        with self._lock:
            return self._update(tuning, expected_revision)

    def _update(self, tuning: RuntimeTuning, expected_revision: int) -> dict[str, int | float | bool]:
        if type(expected_revision) is not int or expected_revision != self.revision:
            raise RuntimeError("runtime settings revision conflict")
        desired = {**self.values, **tuning.model_dump(exclude_none=True)}
        validated = RuntimeTuning.model_validate(desired).model_dump(exclude_none=True)
        self._flush(validated, self.revision + 1)
        self.values = validated
        self.revision += 1
        return dict(self.values)

    def _flush(self, values: dict[str, int | float | bool], revision: int) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = json.dumps({"schema_version": 2, "revision": revision, "values": values}, allow_nan=False).encode()
        if len(data) > 16_384:
            raise ValueError("runtime settings exceed 16 KiB quota")
        temporary_path: Path | None = None
        try:
            with NamedTemporaryFile("wb", dir=self.path.parent, delete=False) as temporary:
                temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            temporary_path.replace(self.path)
            parent = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def settings_metadata() -> list[dict[str, str]]:
    return [
        {"key": "silence_commit_ms", "label": "静音定稿", "unit": "ms", "apply": "hot", "variant": "all"},
        {"key": "max_active_streams", "label": "活跃流上限", "unit": "", "apply": "hot", "variant": "all"},
        {
            "key": "gpu_memory_utilization",
            "label": "GPU 显存比例",
            "unit": "",
            "apply": "engine-restart",
            "variant": "all",
        },
        {
            "key": "qwen_chunk_size_seconds",
            "label": "Qwen 分块",
            "unit": "s",
            "apply": "engine-restart",
            "variant": "qwen",
        },
        {
            "key": "qwen_unfixed_chunk_num",
            "label": "Qwen 回改分块",
            "unit": "",
            "apply": "engine-restart",
            "variant": "qwen",
        },
        {
            "key": "qwen_unfixed_token_num",
            "label": "Qwen 回改 Token",
            "unit": "",
            "apply": "engine-restart",
            "variant": "qwen",
        },
    ]
