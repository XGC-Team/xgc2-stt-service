from __future__ import annotations

import asyncio

from xgc2_stt.gpu_metrics import GpuMonitor


def test_gpu_monitor_caches_samples_without_spawning_commands() -> None:
    def sample() -> dict[str, object]:
        return {
            "available": True,
            "name": "Example GPU",
            "utilization_percent": 42,
            "memory_percent": 50.0,
            "temperature_c": 61,
            "power_watts": 180.0,
        }

    async def exercise() -> None:
        monitor = GpuMonitor(interval_seconds=60, sample_provider=sample)
        await monitor.start()
        snapshot = monitor.snapshot()
        assert snapshot["available"] is True
        assert snapshot["utilization_percent"] == 42
        assert snapshot["history"][0]["memory_percent"] == 50.0
        await monitor.close()

    asyncio.run(exercise())
