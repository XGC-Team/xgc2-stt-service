from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


def _timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ApiKeyStore:
    def __init__(self, path: str, legacy_secret: str | None = None):
        self.path = Path(path)
        self.legacy_secret = legacy_secret
        self._records: dict[str, dict[str, Any]] = {}
        self._managed_authentication = False
        self._lock = threading.RLock()
        self._flush_lock = threading.Lock()
        self._dirty = False
        self._flush_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        await asyncio.to_thread(self._load)
        self._flush_task = asyncio.create_task(self._flush_loop(), name="api-key-store-flush")

    async def close(self) -> None:
        if self._flush_task is not None:
            self._flush_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._flush_task
        await asyncio.to_thread(self.flush)

    @property
    def requires_authentication(self) -> bool:
        with self._lock:
            return bool(self.legacy_secret) or self._managed_authentication

    def authenticate(self, candidate: str | None) -> str | None:
        if not self.requires_authentication:
            return "trusted-network"
        if not candidate:
            return None
        if self.legacy_secret and hmac.compare_digest(self.legacy_secret, candidate):
            return "legacy"
        candidate_digest = _digest(candidate)
        with self._lock:
            for key_id, record in self._records.items():
                if record.get("enabled", True) and hmac.compare_digest(str(record["digest"]), candidate_digest):
                    return key_id
        return None

    def create(self, name: str) -> tuple[dict[str, Any], str]:
        normalized = name.strip()
        if not normalized or len(normalized) > 64:
            raise ValueError("API key name must contain 1 to 64 characters")
        secret = f"xgc2_sk_{secrets.token_urlsafe(32)}"
        key_id = secrets.token_hex(12)
        record: dict[str, Any] = {
            "id": key_id,
            "name": normalized,
            "prefix": secret[:16],
            "digest": _digest(secret),
            "created_at": _timestamp(),
            "last_used_at": None,
            "request_count": 0,
            "stream_sessions": 0,
            "audio_bytes": 0,
            "audio_seconds": 0.0,
            "active_sessions": 0,
            "enabled": True,
        }
        with self._lock:
            self._records[key_id] = record
            self._managed_authentication = True
            self._dirty = True
        self.flush()
        return self._public(record), secret

    def rotate(self, key_id: str) -> tuple[dict[str, Any], str]:
        secret = f"xgc2_sk_{secrets.token_urlsafe(32)}"
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                raise KeyError(key_id)
            record["prefix"] = secret[:16]
            record["digest"] = _digest(secret)
            record["enabled"] = True
            record["rotated_at"] = _timestamp()
            self._dirty = True
            public = self._public(record)
        self.flush()
        return public, secret

    def revoke(self, key_id: str) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                raise KeyError(key_id)
            record["enabled"] = False
            record["active_sessions"] = 0
            record["revoked_at"] = _timestamp()
            self._dirty = True
            public = self._public(record)
        self.flush()
        return public

    def list_public(self) -> list[dict[str, Any]]:
        with self._lock:
            records = [self._public(record) for record in self._records.values()]
        return sorted(records, key=lambda record: str(record["created_at"]), reverse=True)

    def record_request(self, key_id: str, *, stream: bool = False) -> None:
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                return
            record["request_count"] = int(record.get("request_count", 0)) + 1
            if stream:
                record["stream_sessions"] = int(record.get("stream_sessions", 0)) + 1
                record["active_sessions"] = int(record.get("active_sessions", 0)) + 1
            record["last_used_at"] = _timestamp()
            self._dirty = True

    def record_audio(self, key_id: str, byte_count: int, *, seconds: float | None = None) -> None:
        if byte_count <= 0:
            return
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                return
            record["audio_bytes"] = int(record.get("audio_bytes", 0)) + byte_count
            record["audio_seconds"] = round(
                float(record.get("audio_seconds", 0)) + (byte_count / 32_000 if seconds is None else seconds),
                3,
            )
            record["last_used_at"] = _timestamp()
            self._dirty = True

    def stream_closed(self, key_id: str) -> None:
        with self._lock:
            record = self._records.get(key_id)
            if record is None:
                return
            record["active_sessions"] = max(0, int(record.get("active_sessions", 0)) - 1)
            self._dirty = True

    async def _flush_loop(self) -> None:
        while True:
            await asyncio.sleep(5)
            await asyncio.to_thread(self.flush)

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (json.JSONDecodeError, OSError):
            with self._lock:
                self._managed_authentication = True
            return
        if not isinstance(payload, dict):
            with self._lock:
                self._managed_authentication = True
            return
        records = payload.get("keys", [])
        if not isinstance(records, list):
            records = []
        with self._lock:
            self._records = {
                str(record["id"]): record
                for record in records
                if isinstance(record, dict) and record.get("id") and record.get("digest")
            }
            self._managed_authentication = bool(payload.get("authentication_enabled", self._records))
            for record in self._records.values():
                if "audio_seconds" not in record:
                    record["audio_seconds"] = round(int(record.get("audio_bytes", 0)) / 32_000, 3)
                    self._dirty = True
                if record.get("active_sessions"):
                    record["active_sessions"] = 0
                    self._dirty = True

    def flush(self) -> None:
        with self._flush_lock:
            with self._lock:
                if not self._dirty:
                    return
                payload = {
                    "schema_version": 1,
                    "authentication_enabled": self._managed_authentication,
                    "keys": list(self._records.values()),
                }
                self._dirty = False
            try:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with NamedTemporaryFile("w", encoding="utf-8", dir=self.path.parent, delete=False) as temporary:
                    json.dump(payload, temporary, ensure_ascii=False, indent=2)
                    temporary.write("\n")
                    temporary_path = Path(temporary.name)
                os.chmod(temporary_path, 0o600)
                temporary_path.replace(self.path)
            except OSError:
                with self._lock:
                    self._dirty = True
                raise

    @staticmethod
    def _public(record: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in record.items()
            if key != "digest"
        }
