"""Kaynak profili gerçek nvidia-smi çalıştırmadan, sahte sorgu çıktısıyla sınanır."""

import asyncio
import sys

import pytest

from bench.voice_resources import GpuSampler, process_peak_memory_bytes


def _query(memory: str | None, apps: str | None = ""):
    calls = []

    def run(arguments):
        calls.append(arguments)
        return memory if arguments[0].startswith("--query-gpu") else apps

    return run, calls


def test_sampler_keeps_peak_used_min_free_and_other_processes():
    outputs = iter(["1000, 5000, 6141\n", "3000, 3000, 6141\n", "2000, 4000, 6141\n"])
    apps = iter(["42\n", "42\n77\n", "42\n77\n88\n"])

    def run(arguments):
        return next(outputs) if arguments[0].startswith("--query-gpu") else next(apps)

    sampler = GpuSampler(run)
    for _ in range(3):
        sampler.sample(own_pid=42)
    report = sampler.report()
    assert report["gpu_samples"] == 3
    assert report["gpu_peak_memory_bytes"] == 3000 * 1024 * 1024
    assert report["free_vram_bytes"] == 3000 * 1024 * 1024
    assert report["total_vram_bytes"] == 6141 * 1024 * 1024
    assert report["contention"] == {"max_other_gpu_processes": 2}


@pytest.mark.parametrize("memory", [None, "", "N/A, N/A, N/A", "1, 2"])
def test_missing_or_unreadable_gpu_stays_null(memory):
    run, calls = _query(memory)
    sampler = GpuSampler(run)
    sampler.sample(own_pid=1)
    report = sampler.report()
    assert report["gpu_samples"] == 0
    assert report["gpu_peak_memory_bytes"] is None and report["free_vram_bytes"] is None
    assert report["contention"] is None
    assert len(calls) == 1  # Bellek okunamazsa süreç listesi de sorulmaz.


async def test_sampler_loop_stops_promptly():
    run, _ = _query("1, 2, 3")
    sampler = GpuSampler(run, interval=10)
    stop = asyncio.Event()
    task = asyncio.create_task(sampler.run(1, stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, 1)
    assert sampler.samples == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX getrusage yolu")
def test_process_peak_memory_is_positive_bytes():
    peak = process_peak_memory_bytes()
    assert isinstance(peak, int) and peak > 1024 * 1024
