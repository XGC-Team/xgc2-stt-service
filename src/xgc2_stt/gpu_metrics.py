from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from typing import Any


class GpuMonitor:
    def __init__(
        self,
        *,
        enabled: bool = True,
        interval_seconds: float = 2.0,
        sample_provider: Callable[[], dict[str, Any]] | None = None,
    ):
        self.enabled = enabled
        self.interval_seconds = interval_seconds
        self.sample_provider = sample_provider
        self._task: asyncio.Task[None] | None = None
        self._latest: dict[str, Any] = {"available": False}
        self._history: deque[dict[str, Any]] = deque(maxlen=120)
        self._shutdown: Callable[[], None] | None = None

    async def start(self) -> None:
        if not self.enabled:
            return
        if self.sample_provider is None:
            try:
                import pynvml

                pynvml.nvmlInit()
                handle = pynvml.nvmlDeviceGetHandleByIndex(0)

                def sample() -> dict[str, Any]:
                    memory = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    utilization = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    power_limit = pynvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000
                    name = pynvml.nvmlDeviceGetName(handle)
                    return {
                        "available": True,
                        "name": name.decode("utf-8") if isinstance(name, bytes) else name,
                        "utilization_percent": utilization.gpu,
                        "memory_used_bytes": memory.used,
                        "memory_total_bytes": memory.total,
                        "memory_percent": round(memory.used / memory.total * 100, 1),
                        "temperature_c": pynvml.nvmlDeviceGetTemperature(handle, pynvml.NVML_TEMPERATURE_GPU),
                        "power_watts": round(pynvml.nvmlDeviceGetPowerUsage(handle) / 1000, 1),
                        "power_limit_watts": round(power_limit, 1),
                    }

                self.sample_provider = sample
                self._shutdown = pynvml.nvmlShutdown
            except Exception as exc:
                self._latest = {"available": False, "error": f"{type(exc).__name__}: {exc}"}
                return
        await self._sample_once()
        self._task = asyncio.create_task(self._run(), name="gpu-metrics")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
        if self._shutdown is not None:
            await asyncio.to_thread(self._shutdown)

    def snapshot(self) -> dict[str, Any]:
        return {**self._latest, "history": list(self._history)}

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self._sample_once()

    async def _sample_once(self) -> None:
        if self.sample_provider is None:
            return
        try:
            sample = await asyncio.to_thread(self.sample_provider)
            sample = {**sample, "sampled_at": time.time()}
            self._latest = sample
            self._history.append(
                {
                    key: sample.get(key)
                    for key in ("sampled_at", "utilization_percent", "memory_percent", "temperature_c", "power_watts")
                }
            )
        except Exception as exc:
            self._latest = {"available": False, "error": f"{type(exc).__name__}: {exc}", "sampled_at": time.time()}
