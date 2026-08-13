from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STT_",
        case_sensitive=False,
        extra="ignore",
    )

    host: str = "0.0.0.0"
    port: int = Field(default=8000, ge=1, le=65535)
    internal_host: str = "127.0.0.1"
    internal_port: int = Field(default=8001, ge=1, le=65535)

    engine_variant: str = "voxtral"
    model_id: str = "mistralai/Voxtral-Mini-4B-Realtime-2602"
    model_name: str = "mistralai/Voxtral-Mini-4B-Realtime-2602"
    compute_type: str = "bfloat16"
    transcription_delay_ms: int = Field(default=480, ge=80, le=10_000)
    gpu_memory_utilization: float = Field(default=0.80, gt=0.1, le=0.98)
    max_model_len: int = Field(default=32_768, ge=2048, le=262_144)
    max_num_seqs: int = Field(default=8, ge=1, le=128)
    startup_timeout_seconds: int = Field(default=3600, ge=60, le=14_400)
    manage_engine: bool = True
    vllm_enforce_eager: bool = False
    vllm_log_level: str = "info"
    qwen_chunk_size_seconds: float = Field(default=1.0, ge=0.25, le=5.0)
    qwen_unfixed_chunk_num: int = Field(default=4, ge=1, le=16)
    qwen_unfixed_token_num: int = Field(default=5, ge=1, le=64)
    max_active_streams: int = Field(default=1, ge=1, le=128)
    silence_commit_ms: int = Field(default=2000, ge=500, le=30_000)
    api_key_store_path: str = "/var/lib/xgc2-stt/api-keys.json"
    runtime_settings_path: str = "/var/lib/xgc2-stt/runtime-settings.json"
    gpu_metrics_enabled: bool = True
    gpu_metrics_interval_seconds: float = Field(default=2.0, ge=0.5, le=60.0)

    cors_origins: str = "*"
    max_upload_bytes: int = Field(default=52_428_800, ge=1_048_576)
    web_dist: str = "/opt/xgc2-stt/web"
    log_level: str = "info"

    @field_validator("engine_variant")
    @classmethod
    def validate_engine_variant(cls, value: str) -> str:
        if value not in {"voxtral", "qwen"}:
            raise ValueError("engine_variant must be voxtral or qwen")
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        value = self.cors_origins.strip()
        if not value or value == "*":
            return ["*"]
        return [origin.strip() for origin in value.split(",") if origin.strip()]

    @property
    def internal_http_url(self) -> str:
        return f"http://{self.internal_host}:{self.internal_port}"

    @property
    def internal_websocket_url(self) -> str:
        return f"ws://{self.internal_host}:{self.internal_port}/v1/realtime"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
