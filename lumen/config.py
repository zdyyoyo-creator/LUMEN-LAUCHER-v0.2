"""应用路径解析与启动器设置（JSON 持久化）。

路径策略（不写死任何绝对路径）：
1. 显式环境变量 ``LUMEN_DATA_DIR`` 优先（便于测试/多开/便携部署）；
2. 便携模式：程序目录下 ``data`` 目录可写时直接用（免安装、C 盘零占用）；
3. 兜底：用户配置目录（Windows %APPDATA%\\LumenLauncher，macOS ~/Library/Application Support，
   Linux $XDG_CONFIG_HOME）+ 用户数据目录。
"""

from __future__ import annotations

import json
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import APP_ID, APP_VERSION
from .utils.errors import ConfigError
from .utils.log import get as get_logger
from .utils.sysinfo import recommend_memory_gb

log = get_logger("config")

CONFIG_SCHEMA = 1
SHARED_DIR_NAME = "shared"


def app_root() -> Path:
    """程序所在目录（源码运行 = 仓库根；PyInstaller 打包 = exe 所在目录；

    zipapp 单文件（.pyz）= pyz 文件所在目录，因为包内路径不是真实目录）。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    module_root = Path(__file__).resolve().parent.parent
    if module_root.is_dir():
        return module_root
    if sys.argv and sys.argv[0]:
        launcher = Path(sys.argv[0]).resolve()
        if launcher.is_file():
            return launcher.parent
    return module_root


def _user_config_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_ID
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_ID
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_ID.lower()


def _user_data_dir() -> Path:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / APP_ID
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_ID
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP_ID.lower()


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".probe-{os.getpid()}"
        probe.write_text("1", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def resolve_data_root(explicit: str | Path | None = None) -> Path:
    if explicit:
        root = Path(explicit).expanduser()
        if _writable(root):
            return root.resolve()
        raise ConfigError(f"数据目录不可写：{root}", hint="请换一个有写权限的目录")
    env = os.environ.get("LUMEN_DATA_DIR")
    if env:
        root = Path(env).expanduser()
        if _writable(root):
            return root.resolve()
    portable = app_root() / "data"
    if _writable(portable):
        return portable.resolve()
    fallback = _user_data_dir()
    if _writable(fallback):
        return fallback.resolve()
    raise ConfigError("找不到可写的数据目录", hint=f"尝试过：{app_root() / 'data'}、{fallback}")


@dataclass(frozen=True)
class AppPaths:
    """集中管理所有子目录，避免模块里散落字符串拼接。"""

    data_root: Path

    @property
    def config_file(self) -> Path:
        return self.data_root / "config.json"

    @property
    def instances_dir(self) -> Path:
        return self.data_root / "instances"

    @property
    def shared_dir(self) -> Path:
        return self.data_root / SHARED_DIR_NAME

    @property
    def libraries_dir(self) -> Path:
        return self.shared_dir / "libraries"

    @property
    def assets_dir(self) -> Path:
        return self.shared_dir / "assets"

    @property
    def runtimes_dir(self) -> Path:
        return self.data_root / "runtimes"

    @property
    def logs_dir(self) -> Path:
        return self.data_root / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_root / "cache"

    @property
    def meta_cache_dir(self) -> Path:
        return self.cache_dir / "meta"

    def ensure(self) -> "AppPaths":
        for folder in (
            self.data_root,
            self.instances_dir,
            self.shared_dir,
            self.libraries_dir,
            self.assets_dir,
            self.runtimes_dir,
            self.logs_dir,
            self.meta_cache_dir,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        return self


DEFAULT_ACCOUNT = {
    "type": "offline",
    "username": "Steve",
    "uuid": "",
    "access_token": "",
}

DEFAULTS: dict[str, Any] = {
    "schema": CONFIG_SCHEMA,
    "app_version": APP_VERSION,
    "download_source": "auto",  # auto | official | mirror
    "theme": "dark",  # dark | light
    "language": "zh_CN",
    "memory_gb": recommend_memory_gb(),
    "jvm_args": "",
    "java_path": "",
    "java_auto_download": True,
    "max_workers": 12,
    "show_snapshots": False,
    "show_old_versions": False,
    "last_version": "",
    "current_instance": "",
    "cf_api_key": "",
    "account": dict(DEFAULT_ACCOUNT),
    "mirror_endpoint": "https://bmclapi2.bangbang93.com",
    "mirror_probe_ttl": 900,
    "window": {"width": 1180, "height": 760, "maximized": False},
    "last_page": "versions",
    "debug": False,
    "keep_log_days": 7,
}


class Settings:
    """全局设置：内存中的 dict + 原子落盘，线程安全。

    只做「读改写」，不做业务校验（业务侧的合法性由各模块自己判断）。
    """

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._lock = threading.RLock()
        self._data: dict[str, Any] = json.loads(json.dumps(DEFAULTS))
        self.load()

    # ------------------------------------------------------------------ #
    # 读写
    # ------------------------------------------------------------------ #
    def load(self) -> None:
        path = self.paths.config_file
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ConfigError("config.json 顶层不是对象")
        except (OSError, ValueError) as exc:
            log.warning("配置读取失败，将被备份并重建：%s", exc)
            try:
                path.replace(path.with_suffix(".json.corrupt"))
            except OSError:
                pass
            return
        with self._lock:
            merged = json.loads(json.dumps(DEFAULTS))
            merged.update({k: v for k, v in raw.items() if v is not None})
            # account 做字段级合并，避免旧配置缺字段
            if isinstance(raw.get("account"), dict):
                merged["account"] = {**DEFAULT_ACCOUNT, **raw["account"]}
            if isinstance(raw.get("window"), dict):
                merged["window"] = {**DEFAULTS["window"], **raw["window"]}
            self._data = merged

    def save(self) -> None:
        with self._lock:
            payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        path = self.paths.config_file
        tmp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise ConfigError(f"设置保存失败：{exc}", hint=str(path)) from exc

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, *, save: bool = True) -> None:
        with self._lock:
            if self._data.get(key) == value:
                return
            self._data[key] = value
        if save:
            self.save()

    def update(self, values: dict[str, Any], *, save: bool = True) -> None:
        with self._lock:
            self._data.update(values)
        if save:
            self.save()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    # ------------------------------------------------------------------ #
    # 常用便捷属性
    # ------------------------------------------------------------------ #
    @property
    def download_source(self) -> str:
        return str(self.get("download_source", "auto"))

    @download_source.setter
    def download_source(self, value: str) -> None:
        self.set("download_source", value)

    @property
    def theme(self) -> str:
        return str(self.get("theme", "dark"))

    @theme.setter
    def theme(self, value: str) -> None:
        self.set("theme", value)

    @property
    def memory_gb(self) -> int:
        try:
            return max(1, int(self.get("memory_gb", 4)))
        except (TypeError, ValueError):
            return 4

    @memory_gb.setter
    def memory_gb(self, value: int) -> None:
        self.set("memory_gb", max(1, int(value)))

    @property
    def java_path(self) -> str:
        return str(self.get("java_path", "") or "")

    @java_path.setter
    def java_path(self, value: str) -> None:
        self.set("java_path", value or "")

    @property
    def jvm_args(self) -> str:
        return str(self.get("jvm_args", "") or "")

    @jvm_args.setter
    def jvm_args(self, value: str) -> None:
        self.set("jvm_args", value or "")

    @property
    def account(self) -> dict[str, Any]:
        account = self.get("account") or {}
        return {**DEFAULT_ACCOUNT, **account}

    def set_account(self, account: dict[str, Any]) -> None:
        self.set("account", {**DEFAULT_ACCOUNT, **account})

    @property
    def max_workers(self) -> int:
        try:
            return max(1, min(32, int(self.get("max_workers", 12))))
        except (TypeError, ValueError):
            return 12

    def window_size(self) -> tuple[int, int]:
        window = self.get("window") or {}
        try:
            width = max(960, int(window.get("width", 1180)))
            height = max(640, int(window.get("height", 760)))
        except (TypeError, ValueError):
            width, height = 1180, 760
        return width, height

    def remember_window(self, width: int, height: int, maximized: bool) -> None:
        self.set("window", {"width": int(width), "height": int(height), "maximized": bool(maximized)})
