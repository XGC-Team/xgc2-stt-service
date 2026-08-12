from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import BaseModel, Field


class RuntimeTuning(BaseModel):
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

    def load(self) -> dict[str, int | float | bool]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            payload = raw.get("values", raw) if isinstance(raw, dict) else {}
            tuning = RuntimeTuning.model_validate(payload)
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            self.values = {}
            return {}
        self.values = tuning.model_dump(exclude_none=True)
        return dict(self.values)

    def update(self, tuning: RuntimeTuning) -> dict[str, int | float | bool]:
        self.values.update(tuning.model_dump(exclude_none=True))
        validated = RuntimeTuning.model_validate(self.values)
        self.values = validated.model_dump(exclude_none=True)
        self._flush()
        return dict(self.values)

    def _flush(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as temporary:
            json.dump({"schema_version": 1, "values": self.values}, temporary, ensure_ascii=False, indent=2)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(self.path)


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
            "key": "max_model_len",
            "label": "最大模型长度",
            "unit": "",
            "apply": "engine-restart",
            "variant": "voxtral",
        },
        {
            "key": "max_num_seqs",
            "label": "最大序列数",
            "unit": "",
            "apply": "engine-restart",
            "variant": "voxtral",
        },
        {
            "key": "vllm_enforce_eager",
            "label": "Eager 模式",
            "unit": "",
            "apply": "engine-restart",
            "variant": "voxtral",
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
