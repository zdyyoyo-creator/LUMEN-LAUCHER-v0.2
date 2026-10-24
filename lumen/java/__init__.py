"""java 包：探测本机 Java + 自动获取 Mojang 运行时。"""

from .detect import (  # noqa: F401
    JavaRuntime,
    cached_scan,
    describe,
    invalidate_cache,
    managed_java_candidates,
    pick_java,
    probe_java,
    scan_java_runtimes,
)
from .runtime import (  # noqa: F401
    RuntimePackage,
    find_package,
    install_runtime,
    list_managed_runtimes,
    runtime_disk_usage,
)

__all__ = [
    "JavaRuntime",
    "scan_java_runtimes",
    "pick_java",
    "probe_java",
    "cached_scan",
    "invalidate_cache",
    "describe",
    "managed_java_candidates",
    "install_runtime",
    "find_package",
    "RuntimePackage",
    "list_managed_runtimes",
    "runtime_disk_usage",
]
