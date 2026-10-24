"""Java 运行时探测与管理。

扫描顺序：PATH → JAVA_HOME/JDK_HOME → Windows 注册表 → 常见安装目录 → 启动器托管 runtimes。
每个候选都真实执行一次 ``java -version`` 得到主版本，避免只看目录名误判。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from ..config import AppPaths
from ..utils.fsutil import human_size
from ..utils.log import get as get_logger
from ..utils.versions import parse_java_version_output

log = get_logger("java.detect")

if sys.platform == "win32":
    _JAVA_NAMES = ("java.exe", "javaw.exe")
else:
    _JAVA_NAMES = ("java",)


@dataclass
class JavaRuntime:
    path: Path
    version: str = ""
    major: int = 0
    vendor: str = ""
    arch: str = ""
    source: str = ""
    managed: bool = False

    @property
    def label(self) -> str:
        bits = [f"Java {self.major}" if self.major else "Java ?"]
        if self.vendor:
            bits.append(self.vendor)
        if self.arch:
            bits.append(self.arch)
        bits.append(self.source)
        return " · ".join(bits)

    @property
    def exists(self) -> bool:
        try:
            return self.path.exists()
        except OSError:
            return False

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "version": self.version,
            "major": self.major,
            "vendor": self.vendor,
            "arch": self.arch,
            "source": self.source,
            "managed": self.managed,
        }


def _creation_flags() -> int:
    if sys.platform == "win32":
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return 0


def _probe_version_text(java_path: Path) -> str:
    """执行 java -version 并合并 stderr/stdout。"""
    try:
        completed = subprocess.run(
            [str(java_path), "-version"],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=_creation_flags(),
            encoding="utf-8",
            errors="replace",
        )
        return (completed.stderr or "") + (completed.stdout or "")
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("探测 Java 失败（%s）：%s", java_path, exc)
        return ""


def _release_file_version(java_path: Path) -> tuple[str, int]:
    """兜底：读 JDK 的 release 文件拿 JAVA_VERSION。"""
    home = java_path.parent.parent if java_path.parent.name == "bin" else java_path.parent
    release = home / "release"
    if not release.exists():
        return "", 0
    try:
        text = release.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return "", 0
    match = re.search(r'JAVA_VERSION="?([^"\n]+)"?', text)
    if not match:
        return "", 0
    version = match.group(1).strip()
    _, major = parse_java_version_output(f'version "{version}"')
    return version, major or 0


def _vendor_of(text: str) -> str:
    lowered = text.lower()
    for key, label in (
        ("adoptium", "Adoptium"),
        ("temurin", "Temurin"),
        ("zulu", "Zulu"),
        ("corretto", "Corretto"),
        ("microsoft", "Microsoft"),
        ("graalvm", "GraalVM"),
        ("semeru", "Semeru"),
        ("liberica", "Liberica"),
        ("dragonwell", "Dragonwell"),
        ("openjdk", "OpenJDK"),
        ("oracle", "Oracle"),
    ):
        if key in lowered:
            return label
    return ""


def _arch_of(text: str) -> str:
    lowered = text.lower()
    if "64-bit" in lowered or "x86_64" in lowered or "amd64" in lowered:
        return "64-bit"
    if "32-bit" in lowered or "i386" in lowered or "x86" in lowered:
        return "32-bit"
    return ""


def probe_java(java_path: Path, *, source: str = "", managed: bool = False) -> JavaRuntime | None:
    java_path = Path(java_path)
    if not java_path.exists():
        return None
    text = _probe_version_text(java_path)
    version, major = parse_java_version_output(text)
    if not major:
        release_version, release_major = _release_file_version(java_path)
        version = version or release_version
        major = major or release_major
    if not major:
        log.debug("无法识别 Java 版本：%s", java_path)
        return None
    return JavaRuntime(
        path=java_path,
        version=version or "",
        major=major,
        vendor=_vendor_of(text),
        arch=_arch_of(text),
        source=source,
        managed=managed,
    )


# --------------------------------------------------------------------------- #
# 候选发现
# --------------------------------------------------------------------------- #
def _java_from_home(home: Path) -> Path | None:
    if not home:
        return None
    home = Path(home)
    for name in _JAVA_NAMES:
        candidate = home / "bin" / name
        if candidate.exists():
            return candidate
    # 传入的本身就是可执行文件
    if home.name in _JAVA_NAMES and home.exists():
        return home
    return None


def _registry_java_homes() -> list[Path]:
    if sys.platform != "win32":
        return []
    import winreg  # noqa: PLC0415 - 仅 Windows 需要

    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JDK"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\JRE"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Development Kit"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\JavaSoft\Java Runtime Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JavaSoft\JDK"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\JavaSoft\JRE"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\JavaSoft\JDK"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\JavaSoft\JRE"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Adoptium\JDK"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Eclipse Foundation\JDK"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\JDK"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Azul Systems\Zulu"),
    ]
    homes: list[Path] = []
    for root, subkey in roots:
        try:
            with winreg.OpenKey(root, subkey) as key:
                index = 0
                while True:
                    try:
                        child_name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, child_name) as child:
                            for value_name in ("JavaHome", "Path", "InstallationPath"):
                                try:
                                    value, _ = winreg.QueryValueEx(child, value_name)
                                except OSError:
                                    continue
                                if value:
                                    homes.append(Path(str(value)))
                    except OSError:
                        continue
                # 有些键直接放 JavaHome
                for value_name in ("JavaHome", "CurrentVersion"):
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except OSError:
                        continue
                    if value_name == "JavaHome" and value:
                        homes.append(Path(str(value)))
        except OSError:
            continue
    return homes


def _common_java_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ
    if sys.platform == "win32":
        bases = [
            Path(env.get("ProgramFiles", r"C:\Program Files")),
            Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")),
            Path(env.get("LOCALAPPDATA", "")) / "Programs",
            Path(env.get("USERPROFILE", "")) / ".jdks",
            Path(env.get("APPDATA", "")) / ".minecraft" / "runtime",
            Path(env.get("ProgramData", r"C:\ProgramData")) / "Oracle" / "Java",
            Path(r"C:\Program Files\Common Files\Oracle\Java\javapath"),
        ]
        patterns = [
            "Java/jdk*",
            "Java/jre*",
            "Eclipse Adoptium/jdk*",
            "Eclipse Adoptium/jre*",
            "Eclipse Foundation/jdk*",
            "Microsoft/jdk*",
            "BellSoft/LibericaJDK*",
            "Amazon Corretto/*",
            "Zulu/*",
            "Semeru/*",
            "GraalVM/*",
            "AdoptOpenJDK/*",
            "JetBrains/*/jbr",
        ]
        for base in bases:
            if not str(base):
                continue
            for pattern in patterns:
                try:
                    dirs.extend(sorted(base.glob(pattern)))
                except OSError:
                    continue
            # Mojang 官方启动器 runtime 结构：runtime/<component>/<platform>/<component>/
            try:
                for component in base.glob("runtime/*"):
                    for platform_dir in component.glob("*"):
                        for inner in platform_dir.glob("*"):
                            dirs.append(inner)
            except OSError:
                pass
    elif sys.platform == "darwin":
        dirs.extend(sorted(Path("/Library/Java/JavaVirtualMachines").glob("*/Contents/Home")))
        dirs.extend(sorted(Path("/opt/homebrew/opt").glob("openjdk*")))
        dirs.extend(sorted(Path("/usr/local/opt").glob("openjdk*")))
        dirs.extend(sorted(Path.home().glob(".sdkman/candidates/java/*")))
        dirs.extend(sorted(Path.home().glob("Library/Java/JavaVirtualMachines/*/Contents/Home")))
    else:
        dirs.extend(sorted(Path("/usr/lib/jvm").glob("*")))
        dirs.extend(sorted(Path("/usr/java").glob("*")))
        dirs.extend(sorted(Path("/opt/java").glob("*")))
        dirs.extend(sorted(Path.home().glob(".sdkman/candidates/java/*")))
        dirs.extend(sorted(Path.home().glob(".jdks/*")))
    return dirs


def managed_java_candidates(paths: AppPaths) -> list[Path]:
    result: list[Path] = []
    root = paths.runtimes_dir
    if not root.is_dir():
        return result
    for home in root.iterdir():
        if not home.is_dir():
            continue
        found = _java_from_home(home)
        if found is None:
            # 有的 runtime 多套一层目录（Mojang 清单解压后的结构）
            for nested in home.glob("**/bin"):
                for name in _JAVA_NAMES:
                    candidate = nested / name
                    if candidate.exists():
                        result.append(candidate)
                        break
        else:
            result.append(found)
    return result


def scan_java_runtimes(paths: AppPaths | None = None, *, include_managed: bool = True) -> list[JavaRuntime]:
    """扫描全机可用的 Java，去重后按版本从高到低返回。"""
    candidates: list[tuple[Path, str, bool]] = []

    which = shutil.which("java")
    if which:
        candidates.append((Path(which), "PATH", False))

    for env_name in ("JAVA_HOME", "JDK_HOME", "GRAALVM_HOME"):
        value = os.environ.get(env_name)
        if value:
            found = _java_from_home(Path(value))
            if found:
                candidates.append((found, env_name, False))

    for home in _registry_java_homes():
        found = _java_from_home(home)
        if found:
            candidates.append((found, "注册表", False))

    for directory in _common_java_dirs():
        found = _java_from_home(directory)
        if found:
            candidates.append((found, "常见目录", False))

    if include_managed and paths is not None:
        for found in managed_java_candidates(paths):
            candidates.append((found, "启动器托管", True))

    seen: set[str] = set()
    runtimes: list[JavaRuntime] = []
    for path, source, managed in candidates:
        try:
            key = str(path.resolve()).lower()
        except OSError:
            key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        runtime = probe_java(path, source=source, managed=managed)
        if runtime is not None:
            runtimes.append(runtime)
    runtimes.sort(key=lambda item: (item.major, item.managed), reverse=True)
    log.info(
        "扫描到 %d 个 Java：%s",
        len(runtimes),
        "、".join(f"{r.major}({r.vendor or r.source})" for r in runtimes) or "无",
    )
    return runtimes


def pick_java(runtimes: list[JavaRuntime], required_major: int) -> JavaRuntime | None:
    """按需求挑最合适的 Java：精确匹配 > 更高版本里最小的 > 最接近的更低版本。"""
    if not runtimes:
        return None
    exact = [item for item in runtimes if item.major == required_major]
    if exact:
        exact.sort(key=lambda item: (item.managed, item.version), reverse=True)
        return exact[0]
    higher = [item for item in runtimes if item.major > required_major]
    if higher:
        return min(higher, key=lambda item: item.major)
    return max(runtimes, key=lambda item: item.major)


def describe(runtime: JavaRuntime | None) -> str:
    if runtime is None:
        return "未找到 Java"
    return f"Java {runtime.major}（{runtime.vendor or '未知发行版'}）{runtime.path}"


_cache_lock = threading.RLock()
_cache: list[JavaRuntime] = []


def cached_scan(paths: AppPaths) -> list[JavaRuntime]:
    with _cache_lock:
        if _cache:
            return list(_cache)
    result = scan_java_runtimes(paths)
    with _cache_lock:
        _cache[:] = result
    return result


def invalidate_cache() -> None:
    with _cache_lock:
        _cache.clear()


def runtime_size(runtime: JavaRuntime) -> str:
    try:
        if runtime.path.parent.name == "bin":
            home = runtime.path.parent.parent
            total = sum(f.stat().st_size for f in home.rglob("*") if f.is_file())
            return human_size(total)
    except OSError:
        pass
    return "?"
