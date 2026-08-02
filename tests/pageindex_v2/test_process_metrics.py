"""OS process metric sampling for PageIndex benchmark workers."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest
import app.index.v2.process_metrics as process_metrics_module

from app.index.v2.process_metrics import ProcessMonitor


class FakeBackend:
    name = "fake"

    def __init__(self, samples: list[dict[str, int]] | None = None) -> None:
        self.samples = list(samples or [])
        self.position = 0
        self.close_calls = 0

    def sample(self) -> dict[str, int] | None:
        if self.position >= len(self.samples):
            return None
        value = self.samples[self.position]
        self.position += 1
        return value

    def close(self) -> None:
        self.close_calls += 1


def test_monitor_reports_unknown_values_as_none() -> None:
    metrics = ProcessMonitor(FakeBackend(), sample_interval_ms=10).finish()

    assert metrics.status == "unavailable"
    assert metrics.peak_working_set_bytes is None
    assert metrics.peak_private_bytes_observed is None
    assert metrics.io_read_transfer_bytes is None
    assert metrics.io_write_transfer_bytes is None

def test_attach_backend_initialization_failure_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingWindowsBackend:
        def __init__(self, _pid: int) -> None:
            raise OSError("OpenProcess denied")

    monkeypatch.setattr(process_metrics_module.os, "name", "nt")
    monkeypatch.setattr(
        process_metrics_module,
        "_WindowsProcessBackend",
        FailingWindowsBackend,
    )

    monitor = ProcessMonitor.attach(123, sample_interval_ms=10)
    try:
        monitor.sample()
        metrics = monitor.finish()
    finally:
        monitor.close()

    assert metrics.backend == "windows-psapi"
    assert metrics.status == "unavailable"
    assert metrics.samples == 0
    assert metrics.peak_working_set_bytes is None
    assert metrics.peak_private_bytes_observed is None
    assert any(
        warning.startswith("backend_initialization_failed:OSError:")
        for warning in metrics.warnings
    )


def test_monitor_keeps_os_peak_and_observed_private_peak() -> None:
    backend = FakeBackend(
        [
            {"peak_working_set": 40, "private": 20, "read": 10, "write": 5},
            {"peak_working_set": 90, "private": 70, "read": 30, "write": 25},
            {"peak_working_set": 90, "private": 50, "read": 40, "write": 35},
        ]
    )
    monitor = ProcessMonitor(backend, sample_interval_ms=10)
    for _ in range(3):
        monitor.sample()

    metrics = monitor.finish()

    assert metrics.status == "measured"
    assert metrics.samples == 3
    assert metrics.peak_working_set_bytes == 90
    assert metrics.peak_private_bytes_observed == 70
    assert metrics.io_read_transfer_bytes == 40
    assert metrics.io_write_transfer_bytes == 35


def test_monitor_close_is_idempotent() -> None:
    backend = FakeBackend([{"peak_working_set": 1}])
    monitor = ProcessMonitor(backend, sample_interval_ms=10)

    monitor.close()
    monitor.close()

    assert backend.close_calls == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows PSAPI smoke test")
def test_windows_monitor_reports_a_real_child_working_set() -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; payload = bytearray(16 * 1024 * 1024); time.sleep(0.2)",
        ]
    )
    monitor = ProcessMonitor.attach(process.pid, sample_interval_ms=10)
    try:
        while process.poll() is None:
            monitor.sample()
            time.sleep(0.01)
        monitor.sample()
        metrics = monitor.finish()
    finally:
        process.wait()
        monitor.close()

    assert metrics.status == "measured"
    assert metrics.samples > 0
    assert metrics.peak_working_set_bytes is not None
    assert metrics.peak_working_set_bytes > 0
    assert metrics.peak_private_bytes_observed is not None
    assert metrics.peak_private_bytes_observed > 0
