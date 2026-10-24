"""物理内存探测（无第三方依赖），用于默认内存推荐值。"""

from __future__ import annotations

import os
import platform
import subprocess
import sys

_BYTES_PER_GB = 1024**3


def physical_memory_gb() -> float:
    """返回物理内存总量（GB），探测失败时返回 0.0。"""
    try:
        if sys.platform == "win32":
            return _windows_memory_gb()
        if sys.platform == "darwin":
            return _darwin_memory_gb()
        return _linux_memory_gb()
    except Exception:  # noqa: BLE001 - 探测失败不影响主流程
        return 0.0


def _windows_memory_gb() -> float:
    import ctypes

    class MemoryStatusEx(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong),
            ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong),
            ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong),
            ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong),
            ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatusEx()
    status.dwLength = ctypes.sizeof(MemoryStatusEx)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):  # type: ignore[attr-defined]
        return 0.0
    return status.ullTotalPhys / _BYTES_PER_GB


def _darwin_memory_gb() -> float:
    out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
    return int(out.stdout.strip()) / _BYTES_PER_GB


def _linux_memory_gb() -> float:
    with open("/proc/meminfo", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if line.startswith("MemTotal:"):
                kb = int(line.split()[1])
                return kb * 1024 / _BYTES_PER_GB
    return 0.0


def recommend_memory_gb(total_gb: float | None = None) -> int:
    """按物理内存推荐默认分配：总量一半，夹在 4–8GB 之间（并留 2GB 给系统）。"""
    total = total_gb if total_gb is not None else physical_memory_gb()
    if total <= 0:
        return 4
    half = int(total // 2)
    # 至少给系统留 2GB
    usable = max(2, int(total) - 2)
    return max(2, min(8, min(half or 4, usable)))


def machine_summary() -> dict[str, str]:
    return {
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "python": platform.python_version(),
        "cpus": str(os.cpu_count() or 1),
        "memory_gb": f"{physical_memory_gb():.1f}",
    }
