"""文件系统工具：哈希、安全解压、跨平台打开、尺寸格式化。"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .errors import InstallError, LumenError
from .log import get as get_logger

log = get_logger("utils.fs")

CHUNK = 1024 * 1024


# --------------------------------------------------------------------------- #
# 目录 / 文件
# --------------------------------------------------------------------------- #
def ensure_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_writable_dir(path: Path) -> bool:
    """真实写一次探测（比 os.access 可靠，尤其是 Windows ACL / 只读盘）。"""
    try:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".lumen-write-test-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


def sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def file_size(path: Path) -> int:
    try:
        return Path(path).stat().st_size
    except OSError:
        return -1


def human_size(num: float | int | None) -> str:
    if num is None or num < 0:
        return "?"
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def human_speed(bytes_per_sec: float | None) -> str:
    if not bytes_per_sec or bytes_per_sec <= 0:
        return "0 B/s"
    return human_size(bytes_per_sec) + "/s"


def rmtree(path: Path, *, ignore_errors: bool = True) -> None:
    """删除目录，处理 Windows 只读文件导致的删除失败。"""

    def _on_error(func, target, _exc):  # noqa: ANN001
        try:
            os.chmod(target, 0o700)
            func(target)
        except OSError:
            if not ignore_errors:
                raise

    path = Path(path)
    if path.is_dir():
        shutil.rmtree(path, onerror=_on_error)
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            if not ignore_errors:
                raise


def copy_tree(src: Path, dst: Path, *, overwrite: bool = True) -> None:
    src, dst = Path(src), Path(dst)
    ensure_dir(dst)
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            if target.is_dir():
                copy_tree(item, target, overwrite=overwrite)
            elif overwrite or not target.exists():
                shutil.copytree(item, target)
        else:
            if target.exists() and not overwrite:
                continue
            ensure_dir(target.parent)
            shutil.copy2(item, target)


def move_into(src: Path, dst: Path) -> None:
    """尽量原子移动，跨盘时退化为复制后删除。"""
    src, dst = Path(src), Path(dst)
    ensure_dir(dst.parent)
    try:
        if dst.exists():
            rmtree(dst) if dst.is_dir() else dst.unlink()
        shutil.move(str(src), str(dst))
    except OSError:
        copy_tree(src, dst)
        rmtree(src)


def safe_filename(name: str, fallback: str = "instance") -> str:
    """把任意用户输入变成安全目录名（保留中文，去掉非法字符）。"""
    cleaned = "".join(ch for ch in str(name) if ch not in '<>:"/\\|?*' and ord(ch) >= 32)
    cleaned = cleaned.strip().strip(".").strip()
    return cleaned or fallback


def unique_path(parent: Path, base: str, suffix: str = "") -> Path:
    candidate = parent / f"{base}{suffix}"
    index = 2
    while candidate.exists():
        candidate = parent / f"{base} ({index}){suffix}"
        index += 1
    return candidate


# --------------------------------------------------------------------------- #
# Zip（防 Zip Slip，支持 overrides 前缀剔除）
# --------------------------------------------------------------------------- #
def _safe_target(root: Path, member: str) -> Path:
    normalized = PurePosixPath(member.replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise InstallError(f"压缩包内存在非法路径：{member}")
    target = (root / Path(*normalized.parts)).resolve()
    root_resolved = root.resolve()
    try:
        target.relative_to(root_resolved)
    except ValueError as exc:
        raise InstallError(f"压缩包内存在越界路径：{member}") from exc
    return target


def extract_zip(
    archive: Path,
    dest: Path,
    *,
    prefix: str | None = None,
    filter_fn: Callable[[str], bool] | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> list[Path]:
    """解压 zip。

    prefix      只解压该前缀下的条目，并剥掉前缀（用于 mrpack 的 overrides/）
    filter_fn   对剥前缀后的相对路径做额外过滤
    progress    回调 (已处理条目, 总条目)
    """
    archive, dest = Path(archive), Path(dest)
    ensure_dir(dest)
    written: list[Path] = []
    prefix_norm = prefix.replace("\\", "/").strip("/") + "/" if prefix else ""
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if prefix_norm:
                infos = [i for i in infos if i.filename.replace("\\", "/").startswith(prefix_norm)]
            total = len(infos)
            for index, info in enumerate(infos, start=1):
                name = info.filename.replace("\\", "/")
                if prefix_norm:
                    name = name[len(prefix_norm):]
                if not name:
                    continue
                if filter_fn is not None and not filter_fn(name):
                    continue
                target = _safe_target(dest, name)
                if target.is_dir():
                    continue
                ensure_dir(target.parent)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out, CHUNK)
                written.append(target)
                if progress is not None:
                    progress(index, total)
    except zipfile.BadZipFile as exc:
        raise InstallError(f"不是有效的 zip 压缩包：{archive.name}") from exc
    return written


def zip_names(archive: Path) -> list[str]:
    with zipfile.ZipFile(archive) as zf:
        return [i.filename for i in zf.infolist()]


def read_zip_member(archive: Path, member: str, *, limit: int = 8 * 1024 * 1024) -> bytes:
    with zipfile.ZipFile(archive) as zf:
        try:
            info = zf.getinfo(member)
        except KeyError as exc:
            raise InstallError(f"压缩包内缺少 {member}") from exc
        if info.file_size > limit:
            raise InstallError(f"{member} 体积异常（{human_size(info.file_size)}）")
        with zf.open(info) as fh:
            return fh.read(limit)


# --------------------------------------------------------------------------- #
# 跨平台打开
# --------------------------------------------------------------------------- #
def open_path(path: Path) -> None:
    """用系统文件管理器打开目录，或默认程序打开文件。"""
    path = Path(path)
    if not path.exists():
        raise LumenError(f"路径不存在：{path}")
    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError as exc:
        raise LumenError(f"无法打开路径：{path}", hint=str(exc)) from exc


def dir_listing(root: Path, *, max_entries: int = 5000) -> list[tuple[str, int, float]]:
    """浅层列目录：返回 (名称, 大小, mtime)。"""
    root = Path(root)
    if not root.is_dir():
        return []
    rows: list[tuple[str, int, float]] = []
    for index, item in enumerate(sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))):
        if index >= max_entries:
            break
        try:
            stat = item.stat()
            rows.append((item.name + ("/" if item.is_dir() else ""), stat.st_size, stat.st_mtime))
        except OSError:
            rows.append((item.name, -1, 0.0))
    return rows


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False
