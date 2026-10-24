"""Mojang Java 运行时自动获取。

流程：
1. 读 ``all.json``（各平台 component 清单，带 manifest 的 sha1/size/url）；
2. 按版本需求选 component（javaVersion.component 优先，其次按 Java 主版本映射）；
3. 拉取该 component 的 manifest.json，逐文件下载 raw 包到 ``runtimes/<component>/``；
4. 校验 SHA1、还原可执行位，最终定位 ``bin/java``。

Mojang 清单不可达时，回退到 Adoptium（Eclipse Temurin）官方 API 的 JRE zip 包。
"""

from __future__ import annotations

import json
import os
import platform
import stat
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..config import AppPaths
from ..core.downloader import DownloadProgress, DownloadTask, Downloader
from ..utils.errors import JavaError, NetworkError, OperationCancelled
from ..utils.fsutil import ensure_dir, extract_zip, human_size
from ..utils.log import get as get_logger
from ..utils.net import get_json
from ..utils.versions import runtime_platform_key

log = get_logger("java.runtime")

RUNTIME_INDEX_URLS = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json",
    "https://piston-meta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json",
)
ADOPTIUM_API = "https://api.adoptium.net/v3/assets/latest/{major}/hotspot"

#: Java 主版本 → Mojang component 名（兜底映射）
COMPONENT_BY_MAJOR = {
    8: "jre-legacy",
    11: "java-runtime-beta",
    16: "java-runtime-alpha",
    17: "java-runtime-gamma",
    21: "java-runtime-delta",
}


@dataclass
class RuntimePackage:
    component: str
    version_name: str
    manifest_url: str
    manifest_sha1: str
    manifest_size: int
    platform: str
    source: str = "mojang"

    @property
    def label(self) -> str:
        return f"{self.component} {self.version_name}（{self.platform}）"


def _cache_file(paths: AppPaths) -> Path:
    return paths.cache_dir / "runtime_index.json"


def fetch_runtime_index(selector, paths: AppPaths, *, force: bool = False) -> dict:
    cache = _cache_file(paths)
    if cache.exists() and not force:
        try:
            if time.time() - cache.stat().st_mtime < 86400:
                return json.loads(cache.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    last_error: Exception | None = None
    for url in RUNTIME_INDEX_URLS:
        for candidate in selector.candidates(url):
            try:
                data = get_json(candidate, retries=1)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                continue
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            return data
    raise JavaError("无法获取 Mojang Java 运行时清单", hint=str(last_error))


def _platform_entries(index: dict, platform_key: str) -> dict:
    entries = index.get(platform_key)
    if isinstance(entries, dict):
        return entries
    # 老清单可能用 windows-x86 / linux-i386 等 key，做一次宽松匹配
    for key, value in index.items():
        if not isinstance(value, dict):
            continue
        if key.split("-")[0] == platform_key.split("-")[0]:
            return value
    return {}


def find_package(
    index: dict,
    *,
    component: str | None,
    major: int,
    platform_key: str | None = None,
) -> RuntimePackage | None:
    platform_key = platform_key or runtime_platform_key()
    entries = _platform_entries(index, platform_key)

    preferred: list[str] = []
    if component and component in entries:
        preferred.append(component)
    fallback = COMPONENT_BY_MAJOR.get(major)
    if fallback and fallback in entries and fallback not in preferred:
        preferred.append(fallback)
    for name in entries:
        if name not in preferred and name.startswith("java-runtime"):
            preferred.append(name)

    for name in preferred:
        items = entries.get(name) or []
        if not isinstance(items, list):
            continue
        for item in items:
            manifest = (item or {}).get("manifest") or {}
            version = (item or {}).get("version") or {}
            if not manifest.get("url"):
                continue
            return RuntimePackage(
                component=name,
                version_name=str(version.get("name") or ""),
                manifest_url=str(manifest.get("url")),
                manifest_sha1=str(manifest.get("sha1") or ""),
                manifest_size=int(manifest.get("size") or 0),
                platform=platform_key,
            )
    return None


def _adoptium_package(selector, major: int) -> tuple[str, str, int]:
    """Adoptium 兜底：返回 (下载链接, 文件名, 大小)。"""
    os_name = {"windows": "windows", "osx": "mac", "linux": "linux"}.get(platform.system().lower(), "linux")
    machine = platform.machine().lower()
    arch = "x64" if machine in ("amd64", "x86_64", "x64") else "aarch64"
    url = ADOPTIUM_API.format(major=major)
    params = f"?architecture={arch}&image_type=jre&os={os_name}&vendor=eclipse"
    last_error: Exception | None = None
    for candidate in selector.candidates(url) or [url]:
        try:
            data = get_json(f"{candidate}{params}", retries=1)
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    else:
        raise JavaError(f"无法从 Adoptium 获取 Java {major}", hint=str(last_error))
    if not isinstance(data, list) or not data:
        raise JavaError(f"Adoptium 没有 Java {major} 的 JRE 包")
    package = ((data[0] or {}).get("binary") or {}).get("package") or {}
    link = str(package.get("link") or "")
    if not link:
        raise JavaError("Adoptium 返回的下载链接为空")
    return link, str(package.get("name") or Path(link).name), int(package.get("size") or 0)


def _java_executable(home: Path) -> Path | None:
    names = ("java.exe", "java") if sys_platform_windows() else ("java",)
    for name in names:
        candidate = home / "bin" / name
        if candidate.exists():
            return candidate
    for candidate in home.glob("**/bin/java*"):
        if candidate.is_file():
            return candidate
    return None


def sys_platform_windows() -> bool:
    import sys

    return sys.platform == "win32"


def install_runtime(
    selector,
    paths: AppPaths,
    *,
    component: str | None,
    major: int,
    progress: Callable[[DownloadProgress], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """下载并安装指定主版本的 JRE，返回 java 可执行文件路径。"""
    target_name = component or COMPONENT_BY_MAJOR.get(major, f"java-{major}")
    home = paths.runtimes_dir / target_name
    marker = home / ".lumen-runtime.json"

    if marker.exists():
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            existing = _java_executable(home)
            if existing is not None:
                log.info("复用已安装运行时：%s", home)
                return existing
        except (OSError, ValueError):
            pass

    if cancel_check is not None and cancel_check():
        raise OperationCancelled("已取消")

    index_error: Exception | None = None
    try:
        index = fetch_runtime_index(selector, paths)
        package = find_package(index, component=component, major=major)
    except Exception as exc:  # noqa: BLE001 - 清单不可用则走 Adoptium
        index_error = exc
        package = None

    if package is not None:
        try:
            java_path = _install_from_mojang(selector, paths, package, home, progress=progress, cancel_check=cancel_check)
            _write_marker(marker, package.component, major, "mojang", str(java_path))
            return java_path
        except OperationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败回落 Adoptium
            log.warning("Mojang 运行时安装失败，改用 Adoptium：%s", exc)
            index_error = exc

    log.info("回退到 Adoptium 运行时（Java %s）", major)
    java_path = _install_from_adoptium(selector, paths, major, home, progress=progress, cancel_check=cancel_check)
    _write_marker(marker, target_name, major, "adoptium", str(java_path))
    return java_path


def _write_marker(marker: Path, component: str, major: int, source: str, java_path: str) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps(
                {
                    "component": component,
                    "major": major,
                    "source": source,
                    "java": java_path,
                    "installed_at": time.time(),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _install_from_mojang(
    selector,
    paths: AppPaths,
    package: RuntimePackage,
    home: Path,
    *,
    progress: Callable[[DownloadProgress], None] | None,
    cancel_check: Callable[[], bool] | None,
) -> Path:
    manifest = get_json(package.manifest_url, retries=2)
    files = manifest.get("files") or {}
    if not isinstance(files, dict) or not files:
        raise JavaError("Mojang 运行时清单缺少 files 字段")
    ensure_dir(home)
    tasks: list[DownloadTask] = []
    for relative, entry in files.items():
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "file":
            continue
        downloads = entry.get("downloads") or {}
        raw = downloads.get("raw") or {}
        url = raw.get("url")
        if not url:
            continue
        target = home / str(relative).replace("\\", "/")
        tasks.append(
            DownloadTask(
                url=str(url),
                path=target,
                sha1=raw.get("sha1"),
                size=raw.get("size"),
                label=f"runtime:{relative}",
            )
        )
    if not tasks:
        raise JavaError("Mojang 运行时清单没有可下载文件")
    downloader = Downloader(selector, max_workers=8, retries=2)
    if cancel_check is not None:
        _watch_cancel(downloader, cancel_check)
    report = downloader.run(tasks, progress=progress, label=f"下载 Java 运行时 {package.component}")
    if report.cancelled:
        raise OperationCancelled("已取消")
    if report.failed:
        failed = report.errors[0][1] if report.errors else ""
        raise JavaError(f"运行时文件下载失败（{report.failed} 个）", hint=failed)
    java_path = _java_executable(home)
    if java_path is None:
        raise JavaError("运行时下载完成，但没有找到 bin/java")
    _make_executable(home)
    return java_path


def _install_from_adoptium(
    selector,
    paths: AppPaths,
    major: int,
    home: Path,
    *,
    progress: Callable[[DownloadProgress], None] | None,
    cancel_check: Callable[[], bool] | None,
) -> Path:
    link, filename, size = _adoptium_package(selector, major)
    ensure_dir(home)
    archive = paths.cache_dir / "runtimes" / filename
    ensure_dir(archive.parent)
    downloader = Downloader(selector, max_workers=4, retries=2)
    if cancel_check is not None:
        _watch_cancel(downloader, cancel_check)
    task = DownloadTask(url=link, path=archive, size=size or None, label=f"JRE {major}")
    report = downloader.run([task], progress=progress, label=f"下载 Java {major}（Adoptium）")
    if report.failed or not archive.exists():
        raise JavaError(f"Java {major} 下载失败", hint=report.errors[0][1] if report.errors else "")
    try:
        extract_zip(archive, home)
    except Exception as exc:  # noqa: BLE001
        raise JavaError(f"Java 运行时解压失败：{exc}") from exc
    # Adoptium 的 zip 里有一层 jdk-xx 目录，压平
    children = [p for p in home.iterdir() if p.is_dir()]
    if len(children) == 1 and not (home / "bin").exists():
        inner = children[0]
        for item in inner.iterdir():
            item.rename(home / item.name)
        inner.rmdir()
    java_path = _java_executable(home)
    if java_path is None:
        raise JavaError("Java 运行时解压后缺少 bin/java")
    _make_executable(home)
    return java_path


def _watch_cancel(downloader: Downloader, cancel_check: Callable[[], bool]) -> None:
    """轮询外部取消标志并转发给下载器（简单可靠，避免跨线程共享锁）。"""
    import threading

    def _poll() -> None:
        while not downloader.cancelled:
            if cancel_check():
                downloader.cancel()
                return
            time.sleep(0.2)

    threading.Thread(target=_poll, name="lumen-runtime-cancel", daemon=True).start()


def _make_executable(home: Path) -> None:
    if sys_platform_windows():
        return
    for candidate in home.rglob("*"):
        try:
            if candidate.is_file() and candidate.parent.name == "bin":
                mode = candidate.stat().st_mode
                candidate.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except OSError:
            continue


def list_managed_runtimes(paths: AppPaths) -> list[tuple[str, Path, int, str]]:
    """返回 [(component, java 路径, major, source)]，供设置页展示。"""
    result: list[tuple[str, Path, int, str]] = []
    root = paths.runtimes_dir
    if not root.is_dir():
        return result
    for home in sorted(root.iterdir()):
        if not home.is_dir():
            continue
        java = _java_executable(home)
        if java is None:
            continue
        major = 0
        source = ""
        marker = home / ".lumen-runtime.json"
        if marker.exists():
            try:
                info = json.loads(marker.read_text(encoding="utf-8"))
                major = int(info.get("major") or 0)
                source = str(info.get("source") or "")
            except (OSError, ValueError):
                pass
        result.append((home.name, java, major, source))
    return result


def runtime_disk_usage(paths: AppPaths) -> str:
    total = 0
    root = paths.runtimes_dir
    if root.is_dir():
        for item in root.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
    return human_size(total)
