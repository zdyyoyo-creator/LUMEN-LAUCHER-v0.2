"""版本安装编排：客户端 / 依赖库 / 资源文件 / natives 解压，以及安装后的完整性校验。

设计要点：
* 所有下载都走 ``core.downloader``，这里只负责「算清楚要下什么、放到哪、下完做什么」；
* 进度按阶段加权聚合，UI 拿到的是一个 0..1 的总进度 + 阶段文案 + 实时速度；
* 任何一步失败都会抛语义化异常，绝不让半成品被误认为「已安装」。
"""

from __future__ import annotations

import json
import math
import platform
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import AppPaths
from ..utils.errors import InstallError, LumenError, NetworkError, OperationCancelled
from ..utils.fsutil import ensure_dir, extract_zip, human_size, human_speed, sha1_file
from ..utils.log import get as get_logger
from ..utils.net import get_json
from .downloader import DownloadProgress, DownloadTask, Downloader
from .instances import Instance, InstanceStore
from .manifest import (
    VersionDetail,
    VersionManifest,
    RemoteVersion,
    asset_index_files,
    client_library_files,
    fetch_version_json,
    load_manifest,
    merge_version_data,
    native_library_files,
    total_asset_size,
)
from .mirrors import MirrorSelector

log = get_logger("core.installer")

#: 安装阶段及权重（总和为 1.0）
STAGES: tuple[tuple[str, str, float], ...] = (
    ("prepare", "准备版本信息", 0.02),
    ("client", "下载客户端", 0.06),
    ("libraries", "下载依赖库", 0.12),
    ("asset_index", "获取资源索引", 0.01),
    ("assets", "下载游戏资源", 0.72),
    ("natives", "解压本地库", 0.04),
    ("finish", "收尾", 0.03),
)
STAGE_MESSAGES = {key: label for key, label, _ in STAGES}
STAGE_WEIGHTS = {key: weight for key, _, weight in STAGES}


@dataclass
class StageProgress:
    stage: str = "prepare"
    message: str = "准备中"
    percent: float = 0.0
    files_done: int = 0
    files_total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    speed: float = 0.0
    current: tuple[str, ...] = ()
    paused: bool = False
    #: 附加说明（如「已运行 42 秒」），向导里单独显示
    detail: str = ""


@dataclass
class InstallResult:
    version_id: str
    mc_version: str = ""
    loader_type: str = ""
    loader_version: str = ""
    files_downloaded: int = 0
    files_skipped: int = 0
    files_failed: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return f"完成（新下载 {self.files_downloaded} 个，校验通过 {self.files_skipped} 个）"


class VersionInstaller:
    """针对单个实例的安装器。可复用于「原版 / Fabric / Quilt / Forge」。"""

    def __init__(self, selector: MirrorSelector, paths: AppPaths, store: InstanceStore, instance: Instance) -> None:
        self.selector = selector
        self.paths = paths
        self.store = store
        self.instance = instance
        self.downloader = Downloader(selector, max_workers=12, retries=3)
        self._progress_cb: Callable[[StageProgress], None] | None = None
        #: Forge 向导这类自带阶段划分的流程用它发「绝对进度」，避免再被阶段权重二次换算
        self._raw_progress_cb: Callable[[StageProgress], None] | None = None
        self._output_cb: Callable[[str], None] | None = None
        self._stage_index = 0
        self._stage_percent = 0.0
        self._stage_snapshot: DownloadProgress | None = None
        self._message = ""
        self._warnings: list[str] = []

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        self.downloader.cancel()

    def pause(self) -> None:
        self.downloader.pause()

    def resume(self) -> None:
        self.downloader.resume()

    # ------------------------------------------------------------------ #
    # 进度
    # ------------------------------------------------------------------ #
    def _completed_weight(self) -> float:
        return sum(STAGE_WEIGHTS[key] for key, _, _ in STAGES[: self._stage_index])

    def _emit(self, *, stage: str, percent: float, message: str, snapshot: DownloadProgress | None = None) -> None:
        if self._progress_cb is None:
            return
        try:
            index = next((i for i, (key, _, _) in enumerate(STAGES) if key == stage), self._stage_index)
            weight = STAGE_WEIGHTS.get(stage, 0.0)
            total = sum(STAGE_WEIGHTS[key] for key, _, _ in STAGES[:index]) + weight * max(0.0, min(1.0, percent))
        except Exception:  # pragma: no cover
            total = 0.0
        snap = snapshot or self._stage_snapshot
        self._progress_cb(
            StageProgress(
                stage=stage,
                message=message,
                percent=max(0.0, min(1.0, total)),
                files_done=snap.done_files if snap else 0,
                files_total=snap.total_files if snap else 0,
                bytes_done=snap.done_bytes if snap else 0,
                bytes_total=snap.total_bytes if snap else 0,
                speed=snap.speed if snap else 0.0,
                current=snap.active if snap else (),
                paused=self.downloader.paused,
            )
        )

    def _emit_absolute(
        self,
        *,
        stage: str,
        percent: float,
        message: str,
        detail: str = "",
        snapshot: DownloadProgress | None = None,
    ) -> None:
        """按 0..1 的「总进度」直接发事件（Forge 向导用）。

        优先使用安装流程注册的原始回调：``install()`` 会把 ``_progress_cb``
        替换成自己的映射函数，绝对进度不能再走那层换算。
        """
        callback = self._raw_progress_cb or self._progress_cb
        if callback is None:
            return
        snap = snapshot

        def _field(name: str, fallback: str, default: object) -> object:
            if snap is None:
                return default
            return getattr(snap, name, getattr(snap, fallback, default))

        try:
            callback(
                StageProgress(
                    stage=stage,
                    message=message,
                    percent=max(0.0, min(1.0, float(percent))),
                    files_done=int(_field("files_done", "done_files", 0)),
                    files_total=int(_field("files_total", "total_files", 0)),
                    bytes_done=int(_field("bytes_done", "done_bytes", 0)),
                    bytes_total=int(_field("bytes_total", "total_bytes", 0)),
                    speed=float(_field("speed", "speed", 0.0)),
                    current=tuple(_field("current", "active", ())),
                    paused=self.downloader.paused,
                    detail=detail,
                )
            )
        except Exception:  # noqa: BLE001 - 进度回调异常不能影响安装
            pass

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def install(
        self,
        version_id: str,
        *,
        progress: Callable[[StageProgress], None] | None = None,
        with_assets: bool = True,
        force: bool = False,
    ) -> InstallResult:
        """安装一个原版版本到当前实例。"""
        self._progress_cb = progress
        result = InstallResult(version_id=version_id, mc_version=version_id)
        try:
            # ---- 1. 准备 ----
            self._emit(stage="prepare", percent=0.1, message="读取版本清单")
            manifest = load_manifest(self.selector, self.paths, force=force)
            remote = manifest.get(version_id)
            if remote is None:
                raise InstallError(f"版本清单中找不到 {version_id}")
            self._emit(stage="prepare", percent=0.4, message=f"获取 {version_id} 的版本信息")
            data = fetch_version_json(self.selector, self.paths, remote)
            detail = VersionDetail(data, version_id=version_id)
            self.instance.loader_type = ""
            self.instance.loader_version = ""
            self.instance.version_id_override = ""
            self.instance.mc_version = version_id
            self.store.ensure_layout(self.instance)

            version_dir = ensure_dir(self.instance.version_dir(version_id))
            json_path = version_dir / f"{version_id}.json"
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._emit(stage="prepare", percent=1.0, message="版本信息就绪")

            # ---- 2. 客户端 jar ----
            client = detail.client_download
            if not client.get("url"):
                raise InstallError(f"{version_id} 缺少客户端下载地址")
            jar_path = self.instance.version_jar(version_id)
            client_task = DownloadTask(
                url=str(client["url"]),
                path=jar_path,
                sha1=client.get("sha1"),
                size=client.get("size"),
                label=f"{version_id}.jar",
            )
            report = self._run_stage("client", [client_task], "下载客户端")
            result.files_downloaded += report.ok
            result.files_skipped += report.skipped
            result.errors.extend(msg for _n, msg in report.errors)
            self._emit(stage="client", percent=1.0, message=f"客户端就绪（{human_size(jar_path.stat().st_size)}）")

            # ---- 3. 依赖库 + natives ----
            cp_files = client_library_files(self.paths, detail)
            native_files = native_library_files(self.paths, detail)
            native_libs = detail.classpath_and_natives()[1]
            lib_tasks = [
                DownloadTask(url=f.url, path=f.path, sha1=f.sha1, size=f.size, label=f.label)
                for f in (cp_files + native_files)
            ]
            report = self._run_stage("libraries", lib_tasks, f"下载依赖库（{len(lib_tasks)} 个）")
            result.files_downloaded += report.ok
            result.files_skipped += report.skipped
            result.errors.extend(msg for _n, msg in report.errors)
            if report.failed:
                raise InstallError(
                    f"{report.failed} 个依赖库下载失败",
                    hint=report.errors[0][1] if report.errors else "请检查网络或切换下载源",
                )

            # ---- 4/5. 资源索引与资源 ----
            index_id = str(detail.asset_index.get("id") or detail.assets or version_id)
            if with_assets:
                objects_data = self._install_assets(detail, result)
                if objects_data is not None:
                    self._materialize_legacy_assets(detail, index_id, objects_data)
            else:
                self._emit(stage="assets", percent=1.0, message="已跳过资源下载")

            # ---- 6. natives 解压 ----
            self._emit(stage="natives", percent=0.1, message="解压本地库")
            self._extract_natives(version_id, native_libs)
            self._emit(stage="natives", percent=1.0, message="本地库就绪")

            # ---- 7. 收尾 ----
            self._emit(stage="finish", percent=0.5, message="写入实例配置")
            self.instance.save()
            self._emit(stage="finish", percent=1.0, message="安装完成")
            result.warnings.extend(self._warnings)
            return result
        except OperationCancelled:
            raise
        except LumenError:
            raise
        except Exception as exc:  # noqa: BLE001 - 包装成可读错误
            raise InstallError(f"{version_id} 安装失败：{exc}", hint=exc.__class__.__name__) from exc

    # ------------------------------------------------------------------ #
    # 分阶段执行
    # ------------------------------------------------------------------ #
    def _run_stage(self, stage: str, tasks: list[DownloadTask], message: str):
        self._emit(stage=stage, percent=0.0, message=message)

        def _on_progress(snapshot: DownloadProgress) -> None:
            self._stage_snapshot = snapshot
            percent = snapshot.percent if snapshot.total_bytes else snapshot.percent_by_files
            self._emit(stage=stage, percent=percent, message=message, snapshot=snapshot)

        report = self.downloader.run(tasks, progress=_on_progress, label=message)
        self._stage_snapshot = None
        return report

    def _install_assets(self, detail: VersionDetail, result: InstallResult) -> dict | None:
        asset_index = detail.asset_index
        index_url = asset_index.get("url")
        index_id = str(asset_index.get("id") or detail.assets or detail.id)
        if not index_url:
            self._warnings.append("该版本没有 assetIndex，已跳过资源下载")
            self._emit(stage="assets", percent=1.0, message="无资源索引，已跳过")
            return None

        indexes_dir = ensure_dir(self.paths.assets_dir / "indexes")
        index_path = indexes_dir / f"{index_id}.json"
        self._emit(stage="asset_index", percent=0.2, message="下载资源索引")
        task = DownloadTask(
            url=str(index_url),
            path=index_path,
            sha1=asset_index.get("sha1"),
            size=asset_index.get("size"),
            label=f"{index_id}.json",
        )
        report = self._run_stage("asset_index", [task], "下载资源索引")
        result.files_downloaded += report.ok
        result.files_skipped += report.skipped
        if not index_path.exists():
            raise InstallError("资源索引下载失败", hint=report.errors[0][1] if report.errors else "")

        try:
            index_data = json.loads(index_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise InstallError(f"资源索引解析失败：{exc}") from exc

        tasks = [
            DownloadTask(url=f.url, path=f.path, sha1=f.sha1, size=f.size, label=f.label)
            for f in asset_index_files(index_data, self.paths.assets_dir)
        ]
        total = total_asset_size(index_data)
        self._emit(stage="assets", percent=0.0, message=f"下载游戏资源（{len(tasks)} 个，{human_size(total)}）")
        report = self._run_stage("assets", tasks, "下载游戏资源")
        result.files_downloaded += report.ok
        result.files_skipped += report.skipped
        result.errors.extend(msg for _n, msg in report.errors)
        if report.failed:
            self._warnings.append(f"{report.failed} 个资源文件下载失败，游戏内可能出现材质缺失")
        return index_data

    def _materialize_legacy_assets(self, detail: VersionDetail, index_id: str, index_data: dict) -> None:
        """1.6 及以前的资源是扁平结构：virtual/legacy 或 game/resources 需要实体副本。"""
        legacy = str(detail.data.get("assets") or "").lower() == "legacy" or index_id.lower() == "legacy"
        if not legacy:
            return
        map_to_resources = bool(detail.data.get("map_to_resources"))
        objects = (index_data or {}).get("objects") or {}
        if not objects:
            return
        target_root = (
            self.instance.game_dir / "resources"
            if map_to_resources
            else self.paths.assets_dir / "virtual" / "legacy"
        )
        ensure_dir(target_root)
        copied = 0
        for name, entry in objects.items():
            digest = str((entry or {}).get("hash") or "")
            if len(digest) < 2:
                continue
            source = self.paths.assets_dir / "objects" / digest[:2] / digest
            if not source.exists():
                continue
            target = target_root / name
            try:
                if target.exists() and target.stat().st_size == source.stat().st_size:
                    continue
                ensure_dir(target.parent)
                shutil.copyfile(source, target)
                copied += 1
            except OSError as exc:
                log.debug("legacy 资源复制失败：%s（%s）", name, exc)
        if copied:
            log.info("已生成 legacy 资源副本 %d 个 → %s", copied, target_root)
            self._emit(stage="assets", percent=1.0, message=f"已生成旧版资源映射（{copied} 个）")

    def _extract_natives(self, version_id: str, native_libs: list) -> None:
        """把 natives 分类器的 jar 解压到 versions/<vid>/natives。

        ``native_libs`` 是 ``LibraryFile`` 列表（含 relative_path / coordinate）。
        """
        natives_dir = ensure_dir(self.instance.natives_dir(version_id))
        # 清理上次残留（只清空目录内容，不动目录本身）
        for item in natives_dir.iterdir():
            try:
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    item.unlink()
            except OSError:
                pass
        extracted = 0
        for entry in native_libs:
            relative_path = getattr(entry, "relative_path", None)
            coordinate = getattr(entry, "coordinate", str(entry))
            if not relative_path:
                continue
            jar = self.paths.libraries_dir / relative_path
            if not jar.exists():
                self._warnings.append(f"本地库缺失：{coordinate}")
                continue
            try:
                with zipfile.ZipFile(jar) as zf:
                    for info in zf.infolist():
                        name = info.filename.replace("\\", "/")
                        if info.is_dir() or name.startswith("META-INF/"):
                            continue
                        # natives 里可能带多级路径（如 windows/ 前缀），压平到根目录
                        filename = name.rsplit("/", 1)[-1]
                        if not filename:
                            continue
                        target = natives_dir / filename
                        if target.exists() and target.stat().st_size == info.file_size:
                            continue
                        ensure_dir(target.parent)
                        with zf.open(info) as src, open(target, "wb") as out:
                            shutil.copyfileobj(src, out)
                        extracted += 1
            except (zipfile.BadZipFile, OSError) as exc:
                self._warnings.append(f"本地库解压失败：{entry.coordinate}（{exc}）")
        if extracted:
            log.info("解压 natives %d 个文件 → %s", extracted, natives_dir)

    # ------------------------------------------------------------------ #
    # 加载器
    # ------------------------------------------------------------------ #
    def install_fabric(
        self,
        mc_version: str,
        loader_version: str,
        *,
        kind: str = "fabric",
        progress: Callable[[StageProgress], None] | None = None,
        with_assets: bool = True,
    ) -> InstallResult:
        from .loaders import quilt_profile, fabric_profile

        self._progress_cb = progress
        # 1) 先确保原版完整
        base = self.install(mc_version, progress=progress, with_assets=with_assets)
        # 2) 拿加载器 profile
        self._emit(stage="finish", percent=0.2, message=f"获取 {kind} 安装信息")
        profile = quilt_profile(self.selector, mc_version, loader_version) if kind == "quilt" else fabric_profile(
            self.selector, mc_version, loader_version
        )
        version_id = f"{mc_version}-{kind}"
        vanilla_json = self.instance.version_json(mc_version)
        vanilla_data = json.loads(vanilla_json.read_text(encoding="utf-8"))
        merged = merge_version_data(vanilla_data, profile, version_id=version_id)
        merged.pop("inheritsFrom", None)
        # 客户端 jar 沿用原版
        merged.setdefault("downloads", {})
        merged["downloads"]["client"] = vanilla_data.get("downloads", {}).get("client", {})
        vdir = ensure_dir(self.instance.version_dir(version_id))
        (vdir / f"{version_id}.json").write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")

        # 3) 下载加载器自身的库
        detail = VersionDetail(merged, version_id=version_id)
        cp_files = client_library_files(self.paths, detail)
        native_files = native_library_files(self.paths, detail)
        tasks = [
            DownloadTask(url=f.url, path=f.path, sha1=f.sha1, size=f.size, label=f.label, optional=True)
            for f in (cp_files + native_files)
        ]
        report = self._run_stage("finish", tasks, f"下载 {kind} 依赖")
        base.files_downloaded += report.ok
        base.files_skipped += report.skipped
        base.errors.extend(msg for _n, msg in report.errors)

        # 加载器版本的 natives 单独解压一份，避免与原版目录互相干扰
        self._emit(stage="finish", percent=0.9, message="解压加载器 natives")
        self._extract_natives(version_id, detail.classpath_and_natives()[1])

        self.instance.loader_type = kind
        self.instance.loader_version = loader_version
        self.instance.mc_version = mc_version
        self.instance.version_id_override = version_id
        self.instance.save()
        self._emit(stage="finish", percent=1.0, message=f"{kind} {loader_version} 安装完成")
        base.loader_type = kind
        base.loader_version = loader_version
        return base

    def install_forge(
        self,
        mc_version: str,
        forge_version: str,
        *,
        java_path: str,
        kind: str = "forge",
        progress: Callable[[StageProgress], None] | None = None,
        on_output: Callable[[str], None] | None = None,
    ) -> InstallResult:
        """Forge / NeoForge：下载官方安装器 → headless 安装 → 整理产物。

        进度划分（按 0..1 总进度）：
            0.02–0.68  原版文件（客户端 / 依赖库 / 资源 / natives）
            0.68–0.78  下载加载器安装器
            0.78–0.96  运行安装器（实时输出 + 解析百分比；解析不到用耗时渐近）
            0.96–1.00  整理产物、解压 natives、写实例元数据

        ``on_output`` 会把安装器的每一行输出回调出去（向导里的实时日志面板）。
        """
        from .loaders import forge_installer_url, parse_installer_progress, run_forge_installer
        from .version_store import resolve_version

        self._progress_cb = progress
        self._raw_progress_cb = progress
        self._output_cb = on_output

        # ---- 1) 原版文件 0.02 → 0.68 ----
        def _vanilla_progress(inner: StageProgress) -> None:
            self._emit_absolute(
                stage=inner.stage,
                percent=0.02 + inner.percent * 0.66,
                message=f"准备原版：{inner.message}",
                detail=inner.detail,
                snapshot=inner,
            )

        base = self.install(mc_version, progress=_vanilla_progress, with_assets=True)

        # ---- 2) 下载安装器 0.68 → 0.78 ----
        url = forge_installer_url(mc_version, forge_version, kind=kind)
        installer_jar = self.paths.cache_dir / "installers" / Path(url).name
        ensure_dir(installer_jar.parent)
        self._emit_absolute(
            stage="installer",
            percent=0.685,
            message=f"准备下载 {kind} 安装器",
            detail=installer_jar.name,
        )
        task = DownloadTask(url=url, path=installer_jar, label=installer_jar.name, optional=False)

        def _on_download(snapshot: DownloadProgress) -> None:
            self._emit_absolute(
                stage="installer",
                percent=0.68 + snapshot.percent * 0.095,
                message=f"下载 {kind} 安装器 {int(snapshot.percent * 100)}%",
                detail=(
                    f"{human_size(snapshot.done_bytes)} / {human_size(snapshot.total_bytes)}"
                    f" · {human_speed(snapshot.speed)}"
                ),
                snapshot=snapshot,
            )

        report = self.downloader.run([task], progress=_on_download, label=f"{kind} 安装器")
        if report.failed:
            raise InstallError(
                f"{kind} 安装器下载失败",
                hint=report.errors[0][1] if report.errors else url,
            )
        if not installer_jar.exists():
            raise InstallError(f"{kind} 安装器下载后不存在", hint=str(installer_jar))
        self._emit_absolute(
            stage="installer",
            percent=0.78,
            message="安装器已就绪，开始运行",
            detail=f"{installer_jar.name} · {human_size(installer_jar.stat().st_size)}",
        )

        # ---- 3) 运行安装器 0.78 → 0.96 ----
        started = time.monotonic()
        emit_state = {"last": 0.0, "elapsed": 0.0}

        def _on_line(text: str) -> None:
            if self._output_cb is not None:
                try:
                    self._output_cb(text)
                except Exception:  # noqa: BLE001 - 日志回调异常不能中断安装
                    pass
            now = time.monotonic()
            if now - emit_state["last"] < 0.35:
                return
            emit_state["last"] = now
            elapsed = now - started
            emit_state["elapsed"] = elapsed
            parsed = parse_installer_progress(text)
            # 解析不到百分比时用「耗时渐近」推进，避免进度条看起来卡死
            inner = parsed if parsed is not None else 1.0 - math.exp(-elapsed / 90.0)
            inner = max(0.0, min(0.98, inner))
            message = self._compact_line(text) or "安装器运行中…"
            self._emit_absolute(
                stage="installer",
                percent=0.78 + inner * 0.17,
                message=message,
                detail=f"已运行 {int(elapsed)} 秒",
            )

        instance_libraries = self.instance.game_dir / "libraries"
        # Forge/NeoForge 安装器要求目标目录看起来像官方 .minecraft：
        # 必须存在 launcher_profiles.json 与 versions/<mc>/<mc>.json，
        # 否则会直接报 "There is no Minecraft launcher profile in "." " 并退出。
        staged = self._stage_installer_target(mc_version)
        try:
            run_forge_installer(
                java_path,
                installer_jar,
                self.instance.game_dir,
                cancel_check=lambda: self.downloader.cancelled,
                on_output=_on_line,
            )
        finally:
            self._cleanup_staged_targets(staged)

        # ---- 4) 整理产物 0.96 → 1.00 ----
        self._emit_absolute(
            stage="finish",
            percent=0.96,
            message="整理安装产物",
            detail="移动 libraries / versions",
        )
        if instance_libraries.is_dir():
            from ..utils.fsutil import copy_tree

            copy_tree(instance_libraries, self.paths.libraries_dir)
            shutil.rmtree(instance_libraries, ignore_errors=True)

        produced = (
            [
                p
                for p in (self.instance.game_dir / "versions").glob("*")
                if p.is_dir() and (p / f"{p.name}.json").exists()
            ]
            if (self.instance.game_dir / "versions").is_dir()
            else []
        )
        target_version_id = ""
        for folder in produced:
            from ..utils.fsutil import move_into

            move_into(folder, self.instance.versions_dir / folder.name)
            target_version_id = folder.name
        if not target_version_id:
            raise InstallError(
                f"{kind} 安装器没有生成版本文件",
                hint="请确认 Java 版本与安装器兼容，或改用 Fabric",
            )

        self._emit_absolute(
            stage="finish",
            percent=0.985,
            message="解压 natives 并写入实例配置",
            detail=target_version_id,
        )
        merged = resolve_version(self.instance, target_version_id)
        native_detail = VersionDetail(merged, version_id=target_version_id)
        self._extract_natives(target_version_id, native_detail.classpath_and_natives()[1])

        self.instance.mc_version = mc_version
        self.instance.loader_type = kind
        self.instance.loader_version = forge_version
        self.instance.version_id_override = target_version_id
        self.instance.save()

        base.loader_type = kind
        base.loader_version = forge_version
        base.version_id = target_version_id
        self._emit_absolute(
            stage="finish",
            percent=1.0,
            message=f"{kind} {forge_version} 安装完成",
            detail=f"版本目录：{target_version_id}（耗时 {int(time.monotonic() - started)} 秒）",
        )
        log.info(
            "%s 安装完成：%s（%s + %s），耗时 %.0fs",
            kind,
            target_version_id,
            mc_version,
            forge_version,
            time.monotonic() - started,
        )
        return base

    @staticmethod
    def _compact_line(text: str, limit: int = 90) -> str:
        """把安装器输出压成一行，给进度条做文案。"""
        cleaned = " ".join((text or "").replace("\r", " ").replace("\t", " ").split())
        return cleaned[:limit]

    def _stage_installer_target(self, mc_version: str) -> list[Path]:
        """给 Forge/NeoForge 安装器准备一个可识别的 .minecraft 目录。

        官方安装器会检查 ``launcher_profiles.json`` 与 ``versions/<mc>/<mc>.json``，
        缺少任意一个都会失败。这里临时补齐，安装结束后清理（见 ``_cleanup_staged_targets``）。
        """
        created: list[Path] = []
        game_dir = self.instance.game_dir
        ensure_dir(game_dir)

        profiles = game_dir / "launcher_profiles.json"
        if not profiles.exists():
            try:
                profiles.write_text(
                    json.dumps({"profiles": {}, "settings": {}, "version": 3}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                created.append(profiles)
            except OSError as exc:
                log.warning("无法写入 launcher_profiles.json：%s", exc)

        target_dir = game_dir / "versions" / mc_version
        target_json = target_dir / f"{mc_version}.json"
        if not target_json.exists():
            source = self.instance.version_json(mc_version)
            if source.exists():
                try:
                    ensure_dir(target_dir)
                    shutil.copy2(source, target_json)
                    created.append(target_dir)
                except OSError as exc:
                    log.warning("无法准备 versions/%s：%s", mc_version, exc)
        return created

    def _cleanup_staged_targets(self, staged: list[Path]) -> None:
        for path in staged:
            try:
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                elif path.exists():
                    path.unlink()
            except OSError:
                continue


# --------------------------------------------------------------------------- #
# 安装状态检查
# --------------------------------------------------------------------------- #
def installed_versions(instance: Instance) -> set[str]:
    """实例里已经存在 version.json 的版本 id 集合。"""
    result: set[str] = set()
    if not instance.versions_dir.is_dir():
        return result
    for folder in instance.versions_dir.iterdir():
        if folder.is_dir() and (folder / f"{folder.name}.json").exists():
            result.add(folder.name)
    return result


def version_ready(instance: Instance, version_id: str) -> bool:
    """版本是否「可启动」的粗检：json + 客户端 jar + natives 目录。"""
    if not instance.version_json(version_id).exists():
        return False
    jar = instance.version_jar(version_id)
    if not jar.exists():
        # 加载器版本复用原版 jar
        parent = version_id.split("-")[0]
        if not instance.version_jar(parent).exists():
            return False
    return True


def verify_version(
    instance: Instance,
    version_id: str,
    paths: AppPaths,
    *,
    detail_data: dict | None = None,
) -> list[str]:
    """返回问题列表（空列表 = 通过）。用于启动前自检。"""
    problems: list[str] = []
    json_path = instance.version_json(version_id)
    if not json_path.exists():
        problems.append(f"缺少版本文件：{json_path.name}")
        return problems
    try:
        from .version_store import resolve_version

        data = detail_data or resolve_version(instance, version_id)
    except InstallError as exc:
        problems.append(str(exc))
        return problems
    except (OSError, ValueError) as exc:
        problems.append(f"版本文件损坏：{exc}")
        return problems
    detail = VersionDetail(data, version_id=version_id)

    jar = instance.version_jar(version_id)
    if not jar.exists():
        parent_id = detail.inherits_from or version_id.split("-")[0]
        parent_jar = instance.version_jar(parent_id)
        if not parent_jar.exists():
            problems.append("缺少客户端 jar（.minecraft/versions 里没有对应文件）")

    classpath, natives = detail.classpath_and_natives()
    missing_libs = [
        lib.coordinate
        for lib in classpath
        if not (paths.libraries_dir / lib.relative_path).exists()
    ]
    if missing_libs:
        preview = "、".join(missing_libs[:3])
        problems.append(f"缺少 {len(missing_libs)} 个依赖库（如 {preview}）")
    if natives:
        natives_dir = instance.natives_dir(version_id)
        if not natives_dir.is_dir() or not any(natives_dir.iterdir()):
            problems.append("缺少 natives（本地库未解压）")
    return problems


def repair_version(
    selector: MirrorSelector,
    paths: AppPaths,
    store: InstanceStore,
    instance: Instance,
    version_id: str,
    *,
    progress: Callable[[StageProgress], None] | None = None,
) -> InstallResult:
    """重新安装以补齐缺失文件（幂等：已校验通过的文件会跳过）。"""
    installer = VersionInstaller(selector, paths, store, instance)
    return installer.install(version_id, progress=progress)
