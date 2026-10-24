"""实例管理：多实例并存、目录隔离、mods 启停、复制/重命名/删除。

磁盘布局（每个实例完全独立，互不干扰）::

    <data>/instances/<id>/
        instance.json           实例元数据
        minecraft/              gameDir：mods、config、saves、resourcepacks、shaderpacks…
        versions/<vid>/         <vid>.json、<vid>.jar、natives/
        logs/                   每个实例独立的启动日志

共享目录（只读复用，省磁盘）：<data>/shared/libraries、<data>/shared/assets
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ..config import AppPaths
from ..utils.errors import InstallError, LumenError
from ..utils.fsutil import (
    copy_tree,
    ensure_dir,
    human_size,
    open_path,
    rmtree,
    safe_filename,
    unique_path,
)
from ..utils.log import get as get_logger

log = get_logger("core.instances")

INSTANCE_FILE = "instance.json"
GAME_DIR_NAME = "minecraft"
DEFAULT_FOLDERS = (
    "mods",
    "config",
    "saves",
    "resourcepacks",
    "shaderpacks",
    "logs",
    "crash-reports",
    "screenshots",
)

ACCENT_COLORS = ("#5B7CFA", "#43B581", "#E0A32E", "#C567E0", "#3FA9D6", "#E06C6C")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Instance:
    id: str
    name: str
    mc_version: str
    loader_type: str = ""
    loader_version: str = ""
    created: str = field(default_factory=_now_iso)
    last_played: str = ""
    memory_gb: int | None = None
    java_path: str = ""
    jvm_args: str = ""
    color: str = ACCENT_COLORS[0]
    modpack: dict = field(default_factory=dict)
    notes: str = ""
    total_play_seconds: int = 0
    #: 实际版本目录名。Forge/NeoForge 的版本 id 由安装器生成（如 1.20.1-forge-47.2.0），
    #: 不能靠 "{mc}-{loader}" 推算，必须显式记录；为空时回退到旧规则。
    version_id_override: str = ""
    #: 实例根目录（运行时注入，不参与序列化与比较）
    _root: str = field(default="", repr=False, compare=False)

    # ------------------------------------------------------------------ #
    # 路径
    # ------------------------------------------------------------------ #
    @property
    def root(self) -> Path:
        if not self._root:
            raise LumenError(f"实例 {self.id} 未绑定目录")
        return Path(self._root)

    @property
    def game_dir(self) -> Path:
        return self.root / GAME_DIR_NAME

    @property
    def versions_dir(self) -> Path:
        return self.root / "versions"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def mods_dir(self) -> Path:
        return self.game_dir / "mods"

    def version_dir(self, version_id: str) -> Path:
        return self.versions_dir / version_id

    def version_json(self, version_id: str) -> Path:
        return self.version_dir(version_id) / f"{version_id}.json"

    def version_jar(self, version_id: str) -> Path:
        return self.version_dir(version_id) / f"{version_id}.jar"

    def natives_dir(self, version_id: str) -> Path:
        return self.version_dir(version_id) / "natives"

    # ------------------------------------------------------------------ #
    @property
    def version_id(self) -> str:
        """启动时使用的版本目录名。

        优先使用安装时记录的真实 id（Forge/NeoForge 必须），否则按加载器规则推算。
        """
        if self.version_id_override:
            return self.version_id_override
        if self.loader_type and self.loader_type in ("fabric", "quilt", "forge", "neoforge"):
            return f"{self.mc_version}-{self.loader_type}"
        return self.mc_version

    @property
    def display_loader(self) -> str:
        from ..utils.versions import loader_label

        return loader_label(self.loader_type or None, self.loader_version or None)

    @property
    def last_played_text(self) -> str:
        return _friendly_time(self.last_played)

    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "mc_version": self.mc_version,
            "loader_type": self.loader_type,
            "loader_version": self.loader_version,
            "created": self.created,
            "last_played": self.last_played,
            "memory_gb": self.memory_gb,
            "java_path": self.java_path,
            "jvm_args": self.jvm_args,
            "color": self.color,
            "modpack": self.modpack,
            "notes": self.notes,
            "total_play_seconds": self.total_play_seconds,
            "version_id_override": self.version_id_override,
        }

    @classmethod
    def from_dict(cls, data: dict, root: Path) -> "Instance":
        instance = cls(
            id=str(data.get("id") or root.name),
            name=str(data.get("name") or root.name),
            mc_version=str(data.get("mc_version") or ""),
            loader_type=str(data.get("loader_type") or ""),
            loader_version=str(data.get("loader_version") or ""),
            created=str(data.get("created") or _now_iso()),
            last_played=str(data.get("last_played") or ""),
            memory_gb=data.get("memory_gb"),
            java_path=str(data.get("java_path") or ""),
            jvm_args=str(data.get("jvm_args") or ""),
            color=str(data.get("color") or ACCENT_COLORS[0]),
            modpack=dict(data.get("modpack") or {}),
            notes=str(data.get("notes") or ""),
            total_play_seconds=int(data.get("total_play_seconds") or 0),
            version_id_override=str(data.get("version_id_override") or ""),
        )
        instance._root = str(root)
        return instance

    def save(self) -> None:
        ensure_dir(self.root)
        ensure_dir(self.game_dir)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        target = self.root / INSTANCE_FILE
        tmp = target.with_suffix(".json.tmp")
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, target)
        except OSError as exc:
            raise LumenError(f"实例配置保存失败：{exc}", hint=str(target)) from exc

    def touch_played(self) -> None:
        self.last_played = _now_iso()


@dataclass
class ModEntry:
    name: str
    path: Path
    enabled: bool
    size: int
    modified: float

    @property
    def display_name(self) -> str:
        return self.name[:-9] if self.name.endswith(".disabled") else self.name


def _friendly_time(iso_text: str) -> str:
    if not iso_text:
        return "从未游玩"
    try:
        stamp = datetime.fromisoformat(iso_text)
    except ValueError:
        return iso_text[:16]
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    delta = (datetime.now().astimezone() - stamp).total_seconds()
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)} 天前"
    return stamp.strftime("%Y-%m-%d")


class InstanceStore:
    """实例的增删改查。所有写操作都保证目录与元数据一致。"""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        ensure_dir(self.paths.instances_dir)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def roots(self) -> list[Path]:
        try:
            return sorted(
                (p for p in self.paths.instances_dir.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return []

    def list_instances(self) -> list[Instance]:
        result: list[Instance] = []
        for root in self.roots():
            meta = root / INSTANCE_FILE
            if not meta.exists():
                continue
            try:
                data = json.loads(meta.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("instance.json 不是对象")
            except (OSError, ValueError) as exc:
                log.warning("跳过损坏的实例 %s：%s", root.name, exc)
                continue
            result.append(Instance.from_dict(data, root))
        return result

    def get(self, instance_id: str) -> Instance | None:
        if not instance_id:
            return None
        for instance in self.list_instances():
            if instance.id == instance_id:
                return instance
        return None

    def sorted_by_recent(self) -> list[Instance]:
        def key(instance: Instance) -> tuple[str, str]:
            return (instance.last_played or instance.created or "", instance.name)

        return sorted(self.list_instances(), key=key, reverse=True)

    # ------------------------------------------------------------------ #
    # 创建 / 复制 / 重命名 / 删除
    # ------------------------------------------------------------------ #
    def create(
        self,
        name: str,
        mc_version: str,
        *,
        loader_type: str = "",
        loader_version: str = "",
        modpack: dict | None = None,
    ) -> Instance:
        base = safe_filename(name, fallback=f"{mc_version}")
        root = unique_path(self.paths.instances_dir, base)
        ensure_dir(root)
        instance = Instance(
            id=root.name,
            name=name.strip() or root.name,
            mc_version=mc_version,
            loader_type=loader_type,
            loader_version=loader_version,
            color=ACCENT_COLORS[len(self.roots()) % len(ACCENT_COLORS)],
            modpack=dict(modpack or {}),
        )
        instance._root = str(root)  # type: ignore[attr-defined]
        self.ensure_layout(instance)
        instance.save()
        log.info("创建实例：%s（%s %s）", instance.name, mc_version, loader_type)
        return instance

    def ensure_layout(self, instance: Instance) -> None:
        ensure_dir(instance.game_dir)
        ensure_dir(instance.versions_dir)
        ensure_dir(instance.logs_dir)
        for folder in DEFAULT_FOLDERS:
            ensure_dir(instance.game_dir / folder)

    def clone(self, instance: Instance, new_name: str | None = None) -> Instance:
        base = safe_filename(new_name or f"{instance.name} 副本")
        target = unique_path(self.paths.instances_dir, base)
        copy_tree(instance.root, target)
        clone_file = target / INSTANCE_FILE
        try:
            data = json.loads(clone_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InstallError(f"复制失败：{exc}") from exc
        data["id"] = target.name
        data["name"] = new_name or f"{instance.name} 副本"
        data["created"] = _now_iso()
        data["last_played"] = ""
        clone_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        cloned = Instance.from_dict(data, target)
        self.ensure_layout(cloned)
        log.info("复制实例：%s → %s", instance.name, cloned.name)
        return cloned

    def rename(self, instance: Instance, new_name: str) -> Instance:
        cleaned = new_name.strip()
        if not cleaned:
            raise InstallError("实例名称不能为空")
        instance.name = cleaned
        instance.save()
        return instance

    def delete(self, instance: Instance, *, remove_files: bool = True) -> None:
        if remove_files:
            rmtree(instance.root)
        log.info("删除实例：%s（remove_files=%s）", instance.name, remove_files)

    def duplicate_version_files(self, source: Instance, target: Instance) -> None:
        if source.versions_dir.exists():
            copy_tree(source.versions_dir, target.versions_dir)

    # ------------------------------------------------------------------ #
    # 打开 / mods
    # ------------------------------------------------------------------ #
    def open_folder(self, instance: Instance, sub: str = "") -> None:
        target = instance.game_dir / sub if sub else instance.game_dir
        ensure_dir(target)
        open_path(target)

    def list_mods(self, instance: Instance) -> list[ModEntry]:
        mods_dir = instance.mods_dir
        ensure_dir(mods_dir)
        entries: list[ModEntry] = []
        for item in sorted(mods_dir.iterdir(), key=lambda p: p.name.lower()):
            if not item.is_file():
                continue
            name = item.name
            if name.endswith(".jar.disabled"):
                enabled = False
            elif name.endswith(".jar"):
                enabled = True
            else:
                continue
            try:
                stat = item.stat()
                entries.append(ModEntry(name=name, path=item, enabled=enabled, size=stat.st_size, modified=stat.st_mtime))
            except OSError:
                continue
        return entries

    def toggle_mod(self, mod: ModEntry) -> ModEntry:
        path = mod.path
        if mod.enabled:
            target = path.with_name(path.name + ".disabled")
            path.rename(target)
        else:
            if not path.name.endswith(".jar.disabled"):
                return mod
            target = path.with_name(path.name[: -len(".disabled")])
            path.rename(target)
        stat = target.stat()
        return ModEntry(name=target.name, path=target, enabled=target.name.endswith(".jar"), size=stat.st_size, modified=stat.st_mtime)

    def delete_mod(self, mod: ModEntry) -> None:
        try:
            mod.path.unlink()
        except OSError as exc:
            raise LumenError(f"删除失败：{exc}", hint=str(mod.path)) from exc

    def mods_summary(self, instance: Instance) -> str:
        mods = self.list_mods(instance)
        if not mods:
            return "无 mod"
        enabled = sum(1 for m in mods if m.enabled)
        return f"{enabled}/{len(mods)} 个 mod 启用"

    def folder_stats(self, instance: Instance) -> dict[str, tuple[int, str]]:
        """统计关键子目录 (文件数, 人类可读大小)，用于实例卡片。"""
        result: dict[str, tuple[int, str]] = {}
        for folder in ("mods", "saves", "resourcepacks", "shaderpacks"):
            target = instance.game_dir / folder
            count = 0
            total = 0
            if target.is_dir():
                for item in target.rglob("*"):
                    if item.is_file():
                        count += 1
                        try:
                            total += item.stat().st_size
                        except OSError:
                            pass
            result[folder] = (count, human_size(total))
        return result


def instance_version_json(paths: AppPaths, instance_id: str, version_id: str) -> Path:
    return paths.instances_dir / instance_id / "versions" / version_id / f"{version_id}.json"
