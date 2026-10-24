"""Minecraft / Java 版本号解析与匹配规则。

这里刻意不依赖任何第三方库：版本比较、Java 需求推断、Java 输出解析都是纯函数，
便于单元测试（见 tests/selftest.py）。
"""

from __future__ import annotations

import platform
import re
from typing import Iterable

_RELEASE_RE = re.compile(
    r"^(?P<maj>\d+)\.(?P<min>\d+)(?:\.(?P<pat>\d+))?(?:-(?P<pre>[A-Za-z]+)(?P<prenum>\d*))?$"
)
_SNAPSHOT_RE = re.compile(r"^(?P<year>\d{2})w(?P<week>\d{2})(?P<rev>[a-z])$")
_OLD_RE = re.compile(
    r"^(?P<kind>[ab])(?P<maj>\d+)\.(?P<min>\d+)(?:\.(?P<pat>\d+))?[a-z]?$", re.IGNORECASE
)
_JAVA_VER_RE = re.compile(r'version\s+"(?P<ver>[^"]+)"', re.IGNORECASE)

#: 预发布阶段排序权重（release > rc > pre）
_PRERELEASE_ORDER = {"": 5, "release": 5, "rc": 4, "pre": 3, "snapshot": 2, "alpha": 1, "beta": 0}

#: (1,20,5) 等边界值，用于 Java 需求判断
_JAVA_21_BOUNDARY = (1, 20, 5, 0, 0)
_JAVA_17_BOUNDARY = (1, 17, 0, 0, 0)


def parse_release(version: str) -> tuple[int, int, int, int, int] | None:
    """把 ``1.21.4`` / ``1.21.4-pre2`` 解析为可比较元组。"""
    match = _RELEASE_RE.match(str(version).strip())
    if not match:
        return None
    major = int(match.group("maj"))
    minor = int(match.group("min"))
    patch = int(match.group("pat") or 0)
    pre = (match.group("pre") or "").lower()
    prenum = int(match.group("prenum") or 0)
    return (major, minor, patch, _PRERELEASE_ORDER.get(pre, 3), prenum)


def release_key(version: str) -> tuple[int, int, int, int, int] | None:
    return parse_release(version)


def is_snapshot(version: str) -> bool:
    return bool(_SNAPSHOT_RE.match(str(version).strip()))


def is_old(version: str) -> bool:
    """Old Alpha / Old Beta。"""
    return bool(_OLD_RE.match(str(version).strip()))


def is_release(version: str) -> bool:
    return parse_release(version) is not None and not is_snapshot(version) and not is_old(version)


def classify(version: str) -> str:
    """返回 release / snapshot / old / unknown，用于 UI 标签。"""
    if is_old(version):
        return "old"
    if is_snapshot(version):
        return "snapshot"
    if is_release(version):
        return "release"
    return "unknown"


def compare_release(a: str, b: str) -> int:
    """比较两个正式版号；无法解析时按字符串比较，保证永不抛异常。"""
    ka, kb = parse_release(a), parse_release(b)
    if ka and kb:
        return (ka > kb) - (ka < kb)
    return (a > b) - (a < b)


def at_least(version: str, target: str) -> bool:
    """version >= target（仅正式版语义）。"""
    key = parse_release(version)
    target_key = parse_release(target)
    if not key or not target_key:
        return False
    return key >= target_key


# --------------------------------------------------------------------------- #
# Java 需求推断
# --------------------------------------------------------------------------- #
def required_java_major(mc_version: str, *, detail_major: int | None = None, loader: str | None = None) -> int:
    """按 Minecraft 版本推断所需 Java 主版本。

    优先使用 version.json 里的 javaVersion.majorVersion（最权威）；
    否则按经验规则：<=1.16.5 → 8，1.17–1.20.4 → 17，>=1.20.5 → 21。
    """
    if detail_major:
        return int(detail_major)
    version = str(mc_version).strip()
    if is_snapshot(version):
        match = _SNAPSHOT_RE.match(version)
        year = int(match.group("year")) if match else 24
        if year >= 24:
            return 21
        if year >= 21:
            return 17
        return 8
    if is_old(version):
        return 8
    key = parse_release(version)
    if not key:
        return 21
    if key >= _JAVA_21_BOUNDARY:
        return 21
    if key >= _JAVA_17_BOUNDARY:
        return 17
    return 8


def java_major_from_string(version: str) -> int | None:
    """从 "1.8.0_402" / "17.0.8" / "21" 提取主版本。"""
    text = str(version).strip()
    if not text:
        return None
    if text.startswith("1."):
        parts = text.split(".")
        if len(parts) >= 2 and parts[1].isdigit():
            return int(parts[1])
        return None
    match = re.match(r"^(\d+)", text)
    return int(match.group(1)) if match else None


def parse_java_version_output(text: str) -> tuple[str | None, int | None]:
    """解析 ``java -version`` 输出（默认走 stderr）。

    兼容：
        java version "1.8.0_402"
        openjdk version "17.0.8" 2023-07-18
        openjdk version "21.0.1" 2023-10-17 LTS
    """
    if not text:
        return None, None
    match = _JAVA_VER_RE.search(text)
    if match:
        raw = match.group("ver").strip()
        return raw, java_major_from_string(raw)
    # 兜底：某些发行版只打印 "OpenJDK 64-Bit Server VM 21+35-..."
    match = re.search(
        r"(?:java|jdk|jre)[^\d]{0,20}(\d+(?:\.\d+){0,3}(?:_\d+)?)", text, re.IGNORECASE
    )
    if match:
        raw = match.group(1)
        return raw, java_major_from_string(raw)
    return None, None


def java_arch_tag() -> str:
    """Mojang 运行时清单使用的架构标签。"""
    machine = platform.machine().lower()
    if machine in ("amd64", "x86_64", "x64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    if machine in ("x86", "i386", "i686"):
        return "x86"
    return machine or "x64"


def os_tag() -> str:
    """归一化的平台标签：windows / osx / linux。"""
    system = platform.system().lower()
    if system.startswith("win"):
        return "windows"
    if system == "darwin":
        return "osx"
    return "linux"


def runtime_platform_key() -> str:
    """Mojang java-runtime all.json 的顶层 key，例如 windows-x64、mac-os-arm64。"""
    os_name = platform.system().lower()
    arch = java_arch_tag()
    if os_name.startswith("win"):
        return f"windows-{arch}"
    if os_name == "darwin":
        return f"mac-os-{arch}"
    return f"linux-{arch}" if arch != "x64" else "linux"


def maven_os_name(os_name: str | None = None) -> str:
    """libraries rules 中的 os.name 取值。"""
    tag = os_name or os_tag()
    return {"windows": "windows", "osx": "osx", "linux": "linux"}.get(tag, tag)


def human_java_requirement(major: int) -> str:
    return f"Java {major}"


def loader_label(loader_type: str | None, loader_version: str | None) -> str:
    if not loader_type:
        return "原版"
    pretty = {
        "fabric": "Fabric",
        "quilt": "Quilt",
        "forge": "Forge",
        "neoforge": "NeoForge",
    }.get(loader_type, loader_type.title())
    return f"{pretty} {loader_version}" if loader_version else pretty


def any_match(values: Iterable[str], version: str) -> bool:
    return any(value == version for value in values)
