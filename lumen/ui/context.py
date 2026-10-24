"""应用上下文：把 settings / 实例库 / 镜像选择器 / Java / 账号 / 游戏进程串起来。

页面只依赖这个上下文，不直接 new 业务对象，这样：
* 主题切换、数据刷新只有一处入口；
* 后台任务统一走 ``TaskManager``，界面不会因为网络阻塞而假死。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from ..auth.account import Account, load_account, offline_account, validate_username
from ..config import AppPaths, Settings
from ..core.crash import CrashFinding, analyze_lines
from ..core.instances import Instance, InstanceStore
from ..core.installer import installed_versions
from ..core.launcher import PreparedLaunch
from ..core.launcher import GameProcess
from ..core.manifest import VersionManifest, load_manifest
from ..core.mirrors import MirrorSelector
from ..core.preflight import LaunchPlan, build_launch_plan, finish_prepare
from ..java.detect import JavaRuntime, invalidate_cache, scan_java_runtimes
from ..utils.errors import LumenError
from ..utils.fsutil import open_path
from ..utils.log import get as get_logger
from ..utils.sysinfo import physical_memory_gb
from . import theme

log = get_logger("ui.context")


class GameSession(QObject):
    """一次游戏运行的会话：把日志/退出事件用 Qt 信号送回主线程。"""

    line = Signal(str)
    exited = Signal(int, float)

    def __init__(self, prepared: PreparedLaunch, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.prepared = prepared
        self._lines: list[str] = []
        self._process = GameProcess(
            prepared,
            on_line=self._on_line,
            on_exit=self._on_exit,
        )

    def _on_line(self, text: str) -> None:
        self._lines.append(text)
        if len(self._lines) > 3000:
            del self._lines[:1000]
        self.line.emit(text)

    def _on_exit(self, code: int, elapsed: float) -> None:
        self.exited.emit(code, elapsed)

    def start(self) -> "GameSession":
        self._process.start()
        return self

    def stop(self) -> None:
        self._process.kill()

    @property
    def running(self) -> bool:
        return self._process.running

    @property
    def pid(self) -> int | None:
        return self._process.pid

    @property
    def log_file(self) -> Path:
        return self.prepared.log_file

    def recent_lines(self, limit: int = 800) -> list[str]:
        return self._lines[-limit:]

    def analyze(self, *, exit_code: int | None = None, required_java: int = 0) -> list[CrashFinding]:
        from ..core.crash import analyze_lines as _analyze

        return _analyze(
            self.recent_lines(1500),
            exit_code=exit_code,
            required_java=required_java or None,
        )


class AppContext(QObject):
    instances_changed = Signal()
    manifest_changed = Signal()
    theme_changed = Signal(str)
    status_changed = Signal(str)
    toast = Signal(str)
    game_started = Signal()
    game_exited = Signal(int, float)
    #: 页面请求主窗口执行「启动这个实例」
    request_launch = Signal(object)
    #: 页面请求打开日志面板
    request_logs = Signal(object)
    #: 页面请求切换到另一个页面（如从「资源」跳到「设置」填 API Key）
    request_page = Signal(str)
    #: 页面请求打开「资源下载」并预设条件（kind / instance / query）
    request_browse = Signal(dict)

    def __init__(self, paths: AppPaths, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.paths = paths
        self.settings = settings
        self.store = InstanceStore(paths)
        self.selector = MirrorSelector(settings, paths)
        self.manifest: VersionManifest | None = None
        self.manifest_time = 0.0
        self.runtimes: list[JavaRuntime] = []
        self.java_scanned_at = 0.0
        self._account = load_account(settings.get("account"))
        self.session: GameSession | None = None

    # ------------------------------------------------------------------ #
    # 账号
    # ------------------------------------------------------------------ #
    @property
    def account(self) -> Account:
        return self._account

    def set_account_username(self, username: str) -> str:
        """更新离线用户名，返回错误信息（空 = 成功）。"""
        problem = validate_username(username)
        if problem:
            return problem
        self._account = offline_account(username)
        self.settings.set_account(self._account.to_dict())
        return ""

    def set_account(self, account: Account) -> None:
        self._account = account
        self.settings.set_account(account.to_dict())

    # ------------------------------------------------------------------ #
    # 实例
    # ------------------------------------------------------------------ #
    def instances(self) -> list[Instance]:
        return self.store.sorted_by_recent()

    def current_instance(self) -> Instance | None:
        instance_id = str(self.settings.get("current_instance") or "")
        instance = self.store.get(instance_id) if instance_id else None
        if instance is not None:
            return instance
        items = self.instances()
        if items:
            self.select_instance(items[0], save=True)
            return items[0]
        return None

    def select_instance(self, instance: Instance | None, *, save: bool = True) -> None:
        if instance is None:
            return
        if save:
            self.settings.set("current_instance", instance.id)
        self.instances_changed.emit()

    def refresh_instances(self) -> None:
        self.instances_changed.emit()

    def installed_versions(self, instance: Instance | None = None) -> set[str]:
        target = instance or self.current_instance()
        if target is None:
            return set()
        return installed_versions(target)

    def is_installed(self, version_id: str, instance: Instance | None = None) -> bool:
        target = instance or self.current_instance()
        if target is None:
            return False
        # 带加载器的版本（1.21.4-fabric）也算「已安装」
        installed = self.installed_versions(target)
        return version_id in installed or any(item.startswith(f"{version_id}-") for item in installed)

    def open_instance_folder(self, instance: Instance, sub: str = "") -> None:
        try:
            self.store.open_folder(instance, sub)
        except LumenError as exc:
            self.toast.emit(str(exc))

    def open_path(self, path: Path) -> None:
        try:
            open_path(path)
        except LumenError as exc:
            self.toast.emit(str(exc))

    # ------------------------------------------------------------------ #
    # 版本清单
    # ------------------------------------------------------------------ #
    def ensure_manifest(self, *, force: bool = False, progress=None) -> VersionManifest:
        import time

        if not force and self.manifest is not None and time.time() - self.manifest_time < 120:
            return self.manifest
        manifest = load_manifest(self.selector, self.paths, force=force)
        self.manifest = manifest
        self.manifest_time = time.time()
        return manifest

    # ------------------------------------------------------------------ #
    # Java
    # ------------------------------------------------------------------ #
    def ensure_java(self, *, force: bool = False, progress=None) -> list[JavaRuntime]:
        import time

        if not force and self.runtimes and time.time() - self.java_scanned_at < 60:
            return self.runtimes
        invalidate_cache()
        runtimes = scan_java_runtimes(self.paths)
        self.runtimes = runtimes
        self.java_scanned_at = time.time()
        return runtimes

    def best_java(self, required: int) -> JavaRuntime | None:
        from ..java.detect import pick_java

        return pick_java(self.runtimes, required)

    # ------------------------------------------------------------------ #
    # 启动
    # ------------------------------------------------------------------ #
    def build_plan(self, instance: Instance, *, progress=None, allow_download_java: bool = True) -> LaunchPlan:
        return build_launch_plan(
            self.paths,
            self.settings,
            instance,
            selector=self.selector,
            runtimes=self.runtimes,
            allow_download_java=allow_download_java,
            progress=progress,
        )

    def start_game(self, plan: LaunchPlan, *, jvm_args: str = "") -> GameSession:
        prepared = finish_prepare(self.paths, self.settings, plan, self.account, jvm_args=jvm_args)
        session = GameSession(prepared, self)
        session.start()
        session.exited.connect(self._on_game_exit)
        self.session = session
        self.settings.set("current_instance", plan.instance.id)
        self.settings.set("last_version", plan.instance.mc_version)
        self.game_started.emit()
        return session

    def _on_game_exit(self, code: int, elapsed: float) -> None:
        self.game_exited.emit(code, elapsed)

    @property
    def game_running(self) -> bool:
        return self.session is not None and self.session.running

    def stop_game(self) -> None:
        if self.session is not None:
            self.session.stop()

    # ------------------------------------------------------------------ #
    # 主题
    # ------------------------------------------------------------------ #
    def apply_theme(self, name: str) -> None:
        app = QApplication.instance()
        if app is not None:
            theme.apply_theme(app, name)
        self.settings.set("theme", name)
        self.theme_changed.emit(name)

    def current_theme(self) -> str:
        return str(self.settings.get("theme") or "dark")

    def toggle_theme(self) -> str:
        new_theme = "light" if self.current_theme() == "dark" else "dark"
        self.apply_theme(new_theme)
        return new_theme

    # ------------------------------------------------------------------ #
    # 尺寸信息
    # ------------------------------------------------------------------ #
    def memory_info(self) -> tuple[float, int]:
        """返回 (物理内存 GB, 推荐内存 GB)。"""
        total = physical_memory_gb()
        from ..utils.sysinfo import recommend_memory_gb

        return total, recommend_memory_gb(total)
