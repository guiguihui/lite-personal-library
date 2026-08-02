"""Standard-library OS metrics for one short-lived PageIndex worker."""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping, Protocol


class _MetricBackend(Protocol):
    name: str
    warnings: tuple[str, ...]

    def sample(self) -> Mapping[str, int | None] | None:
        ...

    def close(self) -> None:
        ...


@dataclass(frozen=True, slots=True)
class OsProcessMetrics:
    """Aggregated metrics for exactly one operating-system process."""

    backend: str
    status: str
    scope: str
    sample_interval_ms: int
    samples: int
    peak_working_set_bytes: int | None
    peak_private_bytes_observed: int | None
    peak_pagefile_usage_bytes: int | None
    io_read_operations: int | None
    io_write_operations: int | None
    io_read_transfer_bytes: int | None
    io_write_transfer_bytes: int | None
    warnings: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _metric_value(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _maximum(current: int | None, value: object) -> int | None:
    measured = _metric_value(value)
    if measured is None:
        return current
    return measured if current is None else max(current, measured)


class ProcessMonitor:
    """Sample and aggregate one process without retaining process output."""

    def __init__(
        self,
        backend: _MetricBackend,
        *,
        sample_interval_ms: int = 10,
    ) -> None:
        if (
            isinstance(sample_interval_ms, bool)
            or not isinstance(sample_interval_ms, int)
            or sample_interval_ms < 1
        ):
            raise ValueError("sample_interval_ms must be an integer >= 1")
        self._backend = backend
        self.sample_interval_ms = sample_interval_ms
        self._samples = 0
        self._peak_working_set: int | None = None
        self._peak_private: int | None = None
        self._peak_pagefile: int | None = None
        self._read_operations: int | None = None
        self._write_operations: int | None = None
        self._read_transfer: int | None = None
        self._write_transfer: int | None = None
        self._warnings = list(getattr(backend, "warnings", ()))
        self._closed = False

    @classmethod
    def attach(
        cls,
        pid: int,
        sample_interval_ms: int = 10,
    ) -> "ProcessMonitor":
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise ValueError("pid must be an integer >= 1")
        if os.name == "nt":
            backend_name = "windows-psapi"
            backend_factory = _WindowsProcessBackend
        elif sys.platform.startswith("linux"):
            backend_name = "linux-procfs"
            backend_factory = _LinuxProcessBackend
        else:
            backend: _MetricBackend = _UnsupportedBackend(sys.platform)
            return cls(backend, sample_interval_ms=sample_interval_ms)
        try:
            backend = backend_factory(pid)
        except OSError as exc:
            backend = _UnavailableBackend(backend_name, exc)
        return cls(backend, sample_interval_ms=sample_interval_ms)

    def sample(self) -> None:
        if self._closed:
            raise RuntimeError("process monitor is closed")
        try:
            sample = self._backend.sample()
        except OSError as exc:
            warning = f"sample_failed:{type(exc).__name__}:{exc}"
            if warning not in self._warnings:
                self._warnings.append(warning)
            return
        if sample is None:
            return
        self._samples += 1
        self._peak_working_set = _maximum(
            self._peak_working_set,
            sample.get("peak_working_set"),
        )
        self._peak_private = _maximum(
            self._peak_private,
            sample.get("private"),
        )
        self._peak_pagefile = _maximum(
            self._peak_pagefile,
            sample.get("peak_pagefile"),
        )
        self._read_operations = _maximum(
            self._read_operations,
            sample.get("read_operations"),
        )
        self._write_operations = _maximum(
            self._write_operations,
            sample.get("write_operations"),
        )
        self._read_transfer = _maximum(
            self._read_transfer,
            sample.get("read"),
        )
        self._write_transfer = _maximum(
            self._write_transfer,
            sample.get("write"),
        )

    def finish(self) -> OsProcessMetrics:
        measured = any(
            value is not None
            for value in (
                self._peak_working_set,
                self._peak_private,
                self._peak_pagefile,
                self._read_operations,
                self._write_operations,
                self._read_transfer,
                self._write_transfer,
            )
        )
        status = "measured" if measured else "unavailable"
        return OsProcessMetrics(
            backend=str(getattr(self._backend, "name", "unknown")),
            status=status,
            scope="worker_process",
            sample_interval_ms=self.sample_interval_ms,
            samples=self._samples,
            peak_working_set_bytes=self._peak_working_set,
            peak_private_bytes_observed=self._peak_private,
            peak_pagefile_usage_bytes=self._peak_pagefile,
            io_read_operations=self._read_operations,
            io_write_operations=self._write_operations,
            io_read_transfer_bytes=self._read_transfer,
            io_write_transfer_bytes=self._write_transfer,
            warnings=tuple(self._warnings),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._backend.close()


class _UnavailableBackend:
    def __init__(self, name: str, error: OSError) -> None:
        self.name = name
        message = str(error).replace("\r", " ").replace("\n", " ")
        self.warnings = (
            f"backend_initialization_failed:{type(error).__name__}:{message}",
        )

    def sample(self) -> None:
        return None

    def close(self) -> None:
        return None


class _UnsupportedBackend:
    def __init__(self, platform: str) -> None:
        self.name = "unsupported"
        self.warnings = (f"unsupported_platform:{platform}",)

    def sample(self) -> None:
        return None

    def close(self) -> None:
        return None


class _LinuxProcessBackend:
    name = "linux-procfs"
    warnings = ("private_bytes_not_available_from_proc_status",)

    def __init__(self, pid: int) -> None:
        self._root = Path("/proc") / str(pid)

    @staticmethod
    def _kilobytes(value: str) -> int:
        fields = value.split()
        if not fields:
            raise ValueError("empty proc status metric")
        return int(fields[0]) * 1024

    def sample(self) -> Mapping[str, int | None]:
        status_values: dict[str, str] = {}
        for line in (self._root / "status").read_text(
            encoding="ascii",
            errors="replace",
        ).splitlines():
            name, separator, value = line.partition(":")
            if separator:
                status_values[name] = value.strip()

        io_values: dict[str, int] = {}
        io_path = self._root / "io"
        if io_path.is_file():
            for line in io_path.read_text(
                encoding="ascii",
                errors="replace",
            ).splitlines():
                name, separator, value = line.partition(":")
                if separator:
                    io_values[name] = int(value.strip())

        vm_hwm = status_values.get("VmHWM")
        vm_rss = status_values.get("VmRSS")
        return {
            "peak_working_set": (
                self._kilobytes(vm_hwm)
                if vm_hwm is not None
                else self._kilobytes(vm_rss)
                if vm_rss is not None
                else None
            ),
            "private": None,
            "peak_pagefile": None,
            "read_operations": io_values.get("syscr"),
            "write_operations": io_values.get("syscw"),
            "read": io_values.get("read_bytes"),
            "write": io_values.get("write_bytes"),
        }

    def close(self) -> None:
        return None


if os.name == "nt":
    from ctypes import wintypes

    class _ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]


    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]


    class _WindowsProcessBackend:
        name = "windows-psapi"
        warnings: tuple[str, ...] = ()

        _PROCESS_VM_READ = 0x0010
        _PROCESS_QUERY_INFORMATION = 0x0400

        def __init__(self, pid: int) -> None:
            self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            self._psapi = ctypes.WinDLL("psapi", use_last_error=True)

            self._kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            self._kernel32.OpenProcess.restype = wintypes.HANDLE
            self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            self._kernel32.CloseHandle.restype = wintypes.BOOL
            self._kernel32.GetProcessIoCounters.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_IoCounters),
            ]
            self._kernel32.GetProcessIoCounters.restype = wintypes.BOOL
            self._psapi.GetProcessMemoryInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_ProcessMemoryCountersEx),
                wintypes.DWORD,
            ]
            self._psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

            access = self._PROCESS_QUERY_INFORMATION | self._PROCESS_VM_READ
            self._handle = self._kernel32.OpenProcess(access, False, pid)
            if not self._handle:
                self._raise_last_error("OpenProcess")

        @staticmethod
        def _raise_last_error(operation: str) -> None:
            code = ctypes.get_last_error()
            raise OSError(code, f"{operation}: {ctypes.FormatError(code)}")

        def sample(self) -> Mapping[str, int]:
            memory = _ProcessMemoryCountersEx()
            memory.cb = ctypes.sizeof(_ProcessMemoryCountersEx)
            if not self._psapi.GetProcessMemoryInfo(
                self._handle,
                ctypes.byref(memory),
                memory.cb,
            ):
                self._raise_last_error("GetProcessMemoryInfo")

            io = _IoCounters()
            if not self._kernel32.GetProcessIoCounters(
                self._handle,
                ctypes.byref(io),
            ):
                self._raise_last_error("GetProcessIoCounters")

            return {
                "peak_working_set": int(memory.PeakWorkingSetSize),
                "private": int(memory.PrivateUsage),
                "peak_pagefile": int(memory.PeakPagefileUsage),
                "read_operations": int(io.ReadOperationCount),
                "write_operations": int(io.WriteOperationCount),
                "read": int(io.ReadTransferCount),
                "write": int(io.WriteTransferCount),
            }

        def close(self) -> None:
            handle = self._handle
            if handle:
                self._handle = None
                self._kernel32.CloseHandle(handle)

else:
    class _WindowsProcessBackend:
        def __init__(self, pid: int) -> None:
            raise OSError(f"Windows process metrics unavailable for PID {pid}")


__all__ = ["OsProcessMetrics", "ProcessMonitor"]
