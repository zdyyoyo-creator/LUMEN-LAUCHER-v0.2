"""启动前环境自检：Java 版本、文件完整性、路径风险、内存上限。

输出是一组「人话」问题（error 阻止启动，warning 允许但提示），
UI 只需要把 issues 渲染成列表，不需要理解底层细节。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import AppPaths, Settings
from ..java.detect import JavaRuntime, pick_java, probe_java
from ..java.runtime import install_runtime
from ..utils.errors import JavaError, LumenError
from ..utils.log import get as get_logger
from ..utils.sysinfo import physical_memory_gb
from ..utils.versions import required_java_major
from .instances import Instance
from .installer import verify_version
from .launcher import LaunchOptions, PreparedLaunch, load_version_json, prepare_launch
from .manifest import RemoteFile, VersionDetail

log = get_logger("core.preflight")


@dataclass
class Issue:
    level: str  # error | warning | info
    title: str
    detail: str = ""

    @property
    def is_error(self) -> bool:
        return self.level == "error"

    def text(self) -> str:
        return f"{self.title}：{self.detail}" if self.detail else self.title


@dataclass
class LaunchPlan:
    instance: Instance
    version_id: str
    required_java: int = 21
    memory_mb: int = 4096
    java: JavaRuntime | None = None
    issues: list[Issue] = field(default_factory=list)
    prepared: PreparedLaunch | None = None

    @property
    def has_error(self) -> bool:
        return any(issue.is_error for issue in self.issues)

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.is_error]

    @property
    def warnings(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == "warning"]


def _looks_risky(path: Path) -> str:
    text = str(path)
    if any(ord(ch) > 127 for ch in text):
        return "路径包含中文或其它非 ASCII 字符，部分旧版本/模组可能无法正确读取"
    if " " in text:
        return "路径包含空格，个别旧版本库可能解析异常"
    return ""


@dataclass
class LaunchStage:
    """启动自检/准备阶段的进度事件（UI 直接显示 message）。"""

    stage: str = "prepare"
    message: str = ""
    percent: float = 0.0
    detail: str = ""


def build_launch_plan(
    paths: AppPaths,
    settings: Settings,
    instance: Instance,
    *,
    selector,
    runtimes: list[JavaRuntime] | None = None,
    allow_download_java: bool = True,
    progress: Callable | None = None,
    width: int = 0,
    height: int = 0,
    fullscreen: bool = False,
) -> LaunchPlan:
    """组装启动前自检结果（不会启动进程）。"""
    version_id = instance.version_id
    plan = LaunchPlan(instance=instance, version_id=version_id)
    issues = plan.issues

    # ---- 1. 版本文件 ----
    json_path = instance.version_json(version_id)
    if not json_path.exists():
        issues.append(
            Issue(
                "error",
                "版本尚未安装完成",
                f"缺少 {version_id}.json，请先在版本管理里安装或修复该版本",
            )
        )
        plan.required_java = required_java_major(instance.mc_version)
        return plan
    try:
        data = load_version_json(instance, version_id)
        detail = VersionDetail(data, version_id=version_id)
    except LumenError as exc:
        issues.append(Issue("error", "版本文件无法读取", str(exc)))
        return plan

    required = required_java_major(
        instance.mc_version,
        detail_major=detail.java_major,
        loader=instance.loader_type or None,
    )
    plan.required_java = required

    # ---- 2. 内存 ----
    memory_gb = instance.memory_gb or settings.memory_gb
    memory_mb = max(512, int(memory_gb) * 1024)
    plan.memory_mb = memory_mb
    total_gb = physical_memory_gb()
    if total_gb > 0:
        if memory_mb / 1024 >= total_gb:
            issues.append(
                Issue("warning", "分配内存接近物理内存上限", f"当前 {memory_gb} GB / 物理 {total_gb:.1f} GB，可能导致系统卡死")
            )
        elif memory_mb / 1024 > total_gb * 0.7:
            issues.append(
                Issue("warning", "分配内存偏大", f"当前 {memory_gb} GB / 物理 {total_gb:.1f} GB，建议留 2GB 给系统")
            )
    if memory_mb < 1024:
        issues.append(Issue("warning", "内存分配过小", f"{memory_mb} MB 可能不足以加载游戏，建议至少 2048 MB"))

    # ---- 3. Java ----
    runtimes = list(runtimes or [])
    java = _resolve_java_with_selector(
        paths,
        settings,
        instance,
        required,
        runtimes,
        selector=selector,
        allow_download=allow_download_java,
        progress=progress,
        issues=issues,
        component=str(detail.java_version.get("component") or "") or None,
    )
    plan.java = java
    if java is None:
        issues.append(
            Issue(
                "error",
                f"缺少可用的 Java {required}",
                "可以在「设置 → Java」里手动指定，或打开「自动获取 Java」让启动器下载",
            )
        )
    else:
        if java.major < required:
            issues.append(
                Issue(
                    "error",
                    f"Java 版本过低（当前 {java.major}，需要 {required}）",
                    "启动几乎必定失败，请换用匹配的 Java",
                )
            )
        elif required <= 8 and java.major >= 17:
            issues.append(
                Issue(
                    "warning",
                    f"旧版本游戏配高版本 Java（{java.major}）",
                    "1.16.5 及更早版本推荐 Java 8；高版本 Java 可能导致 LWJGL 崩溃",
                )
            )

    # ---- 4. 文件完整性 ----
    problems = verify_version(instance, version_id, paths, detail_data=data)
    for problem in problems:
        issues.append(Issue("error", "游戏文件不完整", problem))

    # ---- 5. 路径风险 ----
    for target, name in ((instance.game_dir, "实例目录"), (Path(plan.java.path) if plan.java else None, "Java 路径")):
        if target is None:
            continue
        risk = _looks_risky(target)
        if risk:
            issues.append(Issue("warning", f"{name}存在兼容风险", risk))

    # ---- 6. 资源索引可用性 ----
    asset_id = str(detail.asset_index.get("id") or "")
    if asset_id:
        index_file = paths.assets_dir / "indexes" / f"{asset_id}.json"
        if not index_file.exists():
            issues.append(Issue("error", "资源索引缺失", f"assets/indexes/{asset_id}.json 不存在，需重新安装"))
    else:
        issues.append(Issue("warning", "该版本没有资源索引", "可能无法加载声音与语言文件"))

    return plan


def _resolve_java_with_selector(
    paths: AppPaths,
    settings: Settings,
    instance: Instance,
    required: int,
    runtimes: list[JavaRuntime],
    *,
    selector,
    allow_download: bool,
    progress: Callable | None,
    issues: list[Issue],
    component: str | None,
) -> JavaRuntime | None:
    # 1) 实例级 / 全局指定
    for label, raw in (("实例指定的 Java", instance.java_path), ("全局设置的 Java", settings.java_path)):
        if not raw:
            continue
        if not Path(raw).exists():
            issues.append(Issue("warning", f"{label}路径已失效", raw))
            continue
        runtime = probe_java(Path(raw), source=label, managed=False)
        if runtime is None:
            issues.append(Issue("warning", f"{label}无法识别", f"执行 java -version 失败：{raw}"))
            continue
        return runtime

    # 2) 本机已扫描到的运行时
    picked = pick_java(runtimes, required)
    if picked is not None:
        return picked

    # 3) 自动下载（Mojang 清单 → 回退 Adoptium）
    if allow_download and bool(settings.get("java_auto_download", True)):
        _emit_stage(progress, "java", f"正在自动获取 Java {required}")
        try:
            from .downloader import DownloadProgress

            def _download_progress(snapshot: DownloadProgress) -> None:
                _emit_stage(
                    progress,
                    "java",
                    f"下载 Java 运行时 {int(snapshot.percent * 100)}%",
                    snapshot.percent,
                    f"{snapshot.done_files}/{snapshot.total_files} 文件",
                )

            java_path = install_runtime(
                selector,
                paths,
                component=component,
                major=required,
                progress=_download_progress,
            )
        except LumenError as exc:
            issues.append(Issue("error", f"自动获取 Java {required} 失败", str(exc)))
            return None
        runtime = probe_java(java_path, source="自动下载", managed=True)
        if runtime is None:
            issues.append(Issue("error", "自动下载的 Java 无法运行", str(java_path)))
        return runtime
    return None


def _emit_stage(progress: Callable | None, stage: str, message: str, percent: float = 0.0, detail: str = "") -> None:
    if progress is None:
        return
    try:
        progress(LaunchStage(stage=stage, message=message, percent=percent, detail=detail))
    except Exception:  # noqa: BLE001 - 进度回调异常不能影响主流程
        pass


def finish_prepare(
    paths: AppPaths,
    settings: Settings,
    plan: LaunchPlan,
    account,
    *,
    jvm_args: str = "",
) -> PreparedLaunch:
    """自检通过后组装启动命令。"""
    if plan.java is None:
        raise LumenError("没有可用的 Java")
    options = LaunchOptions(
        java_path=str(plan.java.path),
        memory_mb=plan.memory_mb,
        jvm_args=jvm_args or plan.instance.jvm_args or settings.jvm_args,
    )
    prepared = prepare_launch(paths, plan.instance, account, options, version_id=plan.version_id)
    plan.prepared = prepared
    return prepared
