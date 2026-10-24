"""整合包导入编排：识别格式 → 建实例 → 装游戏/加载器 → 解压覆盖目录 → 下载文件。

支持三种输入：
* ``.mrpack``（Modrinth）：index.json + overrides + 直链文件
* CurseForge：manifest.json + overrides + 需要 API 换直链，缺 Key 时降级为手动清单
* 通用 zip：没有元数据时按「解压到实例目录」处理，由用户指定 Minecraft 版本
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..config import AppPaths
from ..core.downloader import DownloadProgress, DownloadTask, Downloader
from ..core.instances import Instance, InstanceStore
from ..core.installer import InstallResult, VersionInstaller
from ..core.mirrors import MirrorSelector
from ..utils.errors import ModpackError, OperationCancelled
from ..utils.fsutil import ensure_dir, extract_zip, human_size
from ..utils.log import get as get_logger
from . import curseforge, mrpack

log = get_logger("modpack.importer")

PACK_META_FILE = "modpack.json"


@dataclass
class ModpackFileTask:
    path: Path
    url: str
    sha1: str = ""
    size: int = 0
    label: str = ""
    optional: bool = False


@dataclass
class ModpackPlan:
    source: str
    archive: Path
    name: str
    mc_version: str
    loader_type: str = ""
    loader_version: str = ""
    files: list[ModpackFileTask] = field(default_factory=list)
    override_prefixes: list[str] = field(default_factory=list)
    override_prefix_map: dict[str, str] = field(default_factory=dict)
    total_size: int = 0
    manual_required: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    raw: Any = None

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def display_loader(self) -> str:
        from ..utils.versions import loader_label

        return loader_label(self.loader_type or None, self.loader_version or None)

    def summary(self) -> dict[str, str]:
        return {
            "名称": self.name,
            "Minecraft": self.mc_version or "未知",
            "加载器": self.display_loader,
            "文件": f"{self.file_count} 个 / {human_size(self.total_size)}",
            "格式": {"mrpack": "Modrinth (.mrpack)", "curseforge": "CurseForge", "zip": "通用 zip"}.get(
                self.source, self.source
            ),
        }


@dataclass
class ImportProgress:
    stage: str = "prepare"
    message: str = "准备导入"
    percent: float = 0.0
    detail: str = ""


@dataclass
class ImportReport:
    instance: Instance
    install: InstallResult | None = None
    files_downloaded: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    manual_required: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    extracted: int = 0


# --------------------------------------------------------------------------- #
# 识别与预检
# --------------------------------------------------------------------------- #
def inspect_modpack(archive: Path, *, cf_api_key: str = "") -> ModpackPlan:
    archive = Path(archive)
    if not archive.exists():
        raise ModpackError("文件不存在", hint=str(archive))
    if archive.suffix.lower() not in (".zip", ".mrpack", ".jar", ".mcpack"):
        raise ModpackError(f"不支持的文件类型：{archive.suffix}", hint="请选择 .mrpack 或 .zip 整合包")

    index = mrpack.read_mrpack(archive)
    if index is not None:
        plan = ModpackPlan(
            source="mrpack",
            archive=archive,
            name=index.name,
            mc_version=index.mc_version,
            loader_type=index.loader_type,
            loader_version=index.loader_version,
            total_size=index.total_size,
            raw=index,
        )
        for item in index.client_files:
            plan.files.append(
                ModpackFileTask(
                    path=Path(item.path),
                    url=item.urls[0],
                    sha1=item.sha1,
                    size=item.size,
                    label=Path(item.path).name,
                )
            )
        plan.override_prefixes = mrpack.override_prefixes_in(archive)
        if not plan.mc_version:
            plan.notes.append("整合包里没有写明 Minecraft 版本，需要手动指定")
        if index.loader_type and not plan.loader_version:
            plan.notes.append("未写明加载器版本，将使用最新稳定版")
        return plan

    manifest = curseforge.read_curse_manifest(archive)
    if manifest is not None:
        if not curseforge.has_api_key(cf_api_key):
            plan_note = "未配置 CurseForge API Key：会先解压 overrides，mods 需要手动补齐"
        else:
            plan_note = ""
        manifest = curseforge.resolve_files(manifest, cf_api_key)
        plan = ModpackPlan(
            source="curseforge",
            archive=archive,
            name=manifest.name,
            mc_version=manifest.mc_version,
            loader_type=manifest.loader_type,
            loader_version=manifest.loader_version,
            raw=manifest,
        )
        for item in manifest.files:
            if not item.url:
                plan.manual_required.append(f"projectID={item.project_id} fileID={item.file_id}")
                continue
            filename = item.file_name or f"{item.project_id}-{item.file_id}.jar"
            plan.files.append(
                ModpackFileTask(
                    path=Path("mods") / filename,
                    url=item.url,
                    sha1=item.sha1,
                    size=item.size,
                    label=filename,
                    optional=not item.required,
                )
            )
            plan.total_size += item.size
        if manifest.overrides_prefix:
            plan.override_prefixes = [manifest.overrides_prefix]
        if plan_note:
            plan.notes.append(plan_note)
        if plan.manual_required:
            plan.notes.append(f"{len(plan.manual_required)} 个文件无法自动下载，导入后可手动放入 mods")
        return plan

    # 通用 zip：当作「解压到实例」处理
    plan = ModpackPlan(source="zip", archive=archive, name=archive.stem, mc_version="")
    plan.notes.append("未找到 Modrinth / CurseForge 元数据，将直接把压缩包内容解压到实例目录")
    plan.notes.append("请在下一步选择该整合包对应的 Minecraft 版本")
    return plan


# --------------------------------------------------------------------------- #
# 导入
# --------------------------------------------------------------------------- #
def import_modpack(
    selector: MirrorSelector,
    paths: AppPaths,
    store: InstanceStore,
    plan: ModpackPlan,
    *,
    mc_version: str,
    loader_type: str = "",
    loader_version: str = "",
    java_path: str = "",
    instance_name: str | None = None,
    progress: Callable[[ImportProgress], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> ImportReport:
    """执行导入。``mc_version`` 允许用户在 UI 里覆盖/补全。"""
    mc_version = (mc_version or plan.mc_version).strip()
    if not mc_version:
        raise ModpackError("缺少 Minecraft 版本", hint="请在导入对话框里选择目标版本")

    loader_type = (loader_type or plan.loader_type).strip()
    loader_version = (loader_version or plan.loader_version).strip()

    def _emit(stage: str, message: str, percent: float, detail: str = "") -> None:
        if progress is None:
            return
        try:
            progress(ImportProgress(stage=stage, message=message, percent=max(0.0, min(1.0, percent)), detail=detail))
        except Exception:  # pragma: no cover
            pass

    _emit("prepare", "创建实例", 0.02)
    instance = store.create(
        instance_name or plan.name,
        mc_version,
        loader_type=loader_type if loader_type in ("fabric", "quilt", "forge", "neoforge") else "",
        loader_version=loader_version,
        modpack={
            "source": plan.source,
            "name": plan.name,
            "archive": str(plan.archive),
            "loader": f"{loader_type}-{loader_version}" if loader_type else "",
            "version": str((plan.raw.version_id if plan.source == "mrpack" else getattr(plan.raw, "version", "")) or ""),
        },
    )

    installer = VersionInstaller(selector, paths, store, instance)
    report = ImportReport(instance=instance)

    def _install_progress(stage_progress) -> None:
        _emit("game", f"安装游戏：{stage_progress.message}", 0.05 + stage_progress.percent * 0.6)

    try:
        if loader_type in ("fabric", "quilt"):
            if not loader_version:
                from ..core.loaders import available_loader_versions, latest_loader

                versions = available_loader_versions(selector, loader_type, mc_version)
                loader_version = latest_loader(loader_type, versions) or ""
                if not loader_version:
                    raise ModpackError(f"{loader_type} 没有适用于 {mc_version} 的版本")
            _emit("game", f"安装 {mc_version} + {loader_type} {loader_version}", 0.05)
            report.install = installer.install_fabric(
                mc_version,
                loader_version,
                kind=loader_type,
                progress=_install_progress,
            )
        elif loader_type in ("forge", "neoforge"):
            if not java_path:
                raise ModpackError(
                    "安装 Forge/NeoForge 需要 Java",
                    hint="请先在设置里指定 Java，或改用 Fabric 版本",
                )
            if not loader_version:
                from ..core.loaders import available_loader_versions

                versions = available_loader_versions(selector, loader_type, mc_version)
                if not versions:
                    raise ModpackError(f"{loader_type} 没有适用于 {mc_version} 的版本")
                loader_version = versions[0].version
            _emit("game", f"安装 {mc_version} + {loader_type} {loader_version}", 0.05)
            report.install = installer.install_forge(
                mc_version,
                loader_version,
                java_path=java_path,
                kind=loader_type,
                progress=_install_progress,
            )
        else:
            _emit("game", f"安装 Minecraft {mc_version}", 0.05)
            report.install = installer.install(mc_version, progress=_install_progress)

        if cancel_check is not None and cancel_check():
            raise OperationCancelled("已取消导入")

        # ---- overrides ----
        game_dir = instance.game_dir
        ensure_dir(game_dir)
        extracted = 0
        if plan.override_prefixes:
            _emit("overrides", "解压整合包覆盖文件", 0.68)
            for prefix in plan.override_prefixes:
                try:
                    files = extract_zip(plan.archive, game_dir, prefix=prefix)
                    extracted += len(files)
                except Exception as exc:  # noqa: BLE001 - 覆盖目录失败不致命
                    report.warnings.append(f"覆盖目录 {prefix} 解压失败：{exc}")
        elif plan.source == "zip":
            _emit("overrides", "解压整合包内容", 0.68)
            try:
                files = extract_zip(plan.archive, game_dir)
                extracted += len(files)
            except Exception as exc:  # noqa: BLE001
                raise ModpackError(f"整合包解压失败：{exc}") from exc
        report.extracted = extracted
        # 导入后常见的目录补齐
        for folder in ("mods", "config", "saves", "resourcepacks", "shaderpacks"):
            ensure_dir(game_dir / folder)

        # ---- files ----
        if plan.files:
            _emit("files", f"下载整合包文件（{len(plan.files)} 个）", 0.72)
            tasks: list[DownloadTask] = []
            for item in plan.files:
                target = instance.game_dir / item.path
                tasks.append(
                    DownloadTask(
                        url=item.url,
                        path=target,
                        sha1=item.sha1 or None,
                        size=item.size or None,
                        label=item.label,
                        optional=item.optional,
                    )
                )

            def _dl_progress(snapshot: DownloadProgress) -> None:
                _emit(
                    "files",
                    f"下载整合包文件：{snapshot.done_files}/{snapshot.total_files}",
                    0.72 + snapshot.percent * 0.26,
                    human_size(snapshot.done_bytes),
                )

            downloader = Downloader(selector, max_workers=12, retries=3)
            if cancel_check is not None:
                _watch_cancel(downloader, cancel_check)
            dl_report = downloader.run(tasks, progress=_dl_progress, label="整合包文件")
            report.files_downloaded = dl_report.ok
            report.files_skipped = dl_report.skipped
            report.files_failed = dl_report.failed
            report.warnings.extend(f"{name}：{msg}" for name, msg in dl_report.errors[:10])
        else:
            _emit("files", "没有需要额外下载的文件", 0.95)

        # ---- 收尾 ----
        instance.modpack = {
            **instance.modpack,
            "imported_at": instance.created,
            "files": report.files_downloaded,
            "manual": len(plan.manual_required),
        }
        notes = []
        if plan.manual_required:
            notes.append(f"以下文件需要手动下载放入 mods：{', '.join(plan.manual_required[:20])}")
            (instance.root / "MANUAL_DOWNLOADS.txt").write_text(
                "# 以下文件无法自动下载，请手动获取后放入 minecraft/mods\n"
                + "\n".join(plan.manual_required),
                encoding="utf-8",
            )
        if report.warnings:
            notes.append(f"{len(report.warnings)} 个文件下载失败，详见日志")
        instance.notes = "\n".join(notes)
        instance.save()
        report.manual_required = list(plan.manual_required)
        _emit("done", "导入完成", 1.0)
        log.info(
            "整合包导入完成：%s（%s + %s），下载 %d，跳过 %d，失败 %d",
            instance.name,
            mc_version,
            loader_type or "原版",
            report.files_downloaded,
            report.files_skipped,
            report.files_failed,
        )
        return report
    except OperationCancelled:
        raise
    except Exception:
        # 导入失败时清理半成品实例，避免留下「空壳」让用户困惑
        try:
            store.delete(instance)
        except Exception:  # noqa: BLE001
            pass
        raise


def _watch_cancel(downloader: Downloader, cancel_check: Callable[[], bool]) -> None:
    import threading
    import time

    def _poll() -> None:
        while not downloader.cancelled:
            if cancel_check():
                downloader.cancel()
                return
            time.sleep(0.2)

    threading.Thread(target=_poll, name="lumen-import-cancel", daemon=True).start()


def write_plan_preview(plan: ModpackPlan, path: Path) -> None:
    """把导入计划写成 JSON（排障用）。"""
    payload = {
        "source": plan.source,
        "name": plan.name,
        "mc_version": plan.mc_version,
        "loader": f"{plan.loader_type}-{plan.loader_version}".strip("-"),
        "files": [{"path": str(item.path), "url": item.url, "size": item.size} for item in plan.files],
        "manual": plan.manual_required,
        "notes": plan.notes,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def cleanup_archive(plan: ModpackPlan) -> None:
    """导入完成后可选地删除临时压缩包（当前不删，保持用户文件不动）。"""
    return


def pack_display_size(plan: ModpackPlan) -> str:
    return human_size(plan.total_size)
