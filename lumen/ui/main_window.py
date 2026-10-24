"""主窗口：左侧细导航 + 主内容区 + 底部固定启动条。

布局比例（窗口 1180×760 时）：
    侧栏 92px 固定 | 主区自适应 | 底栏 76px 全宽
底栏是视觉权重最高的区域：主按钮「启动游戏」168×44。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import APP_DISPLAY_NAME, APP_VERSION
from ..core.preflight import LaunchPlan
from ..utils.errors import LumenError
from ..utils.versions import required_java_major
from . import theme
from .context import AppContext
from .dialogs.common import IssueDialog, MessageDialog
from .dialogs.log_dialog import LogDialog
from .glyphs import glyph_icon, glyph_pixmap
from .pages import BrowsePage, InstancesPage, ModpacksPage, SettingsPage, VersionsPage
from .widgets import Toast, label, make_button, make_tool_button, ToolGlyphButton
from .workers import TaskManager, Worker

NAV_ITEMS = (
    ("versions", "versions", "版本"),
    ("browse", "download", "资源"),
    ("modpacks", "modpack", "整合包"),
    ("instances", "instances", "实例"),
    ("settings", "settings", "设置"),
)


# --------------------------------------------------------------------------- #
# 小部件
# --------------------------------------------------------------------------- #
class BrandBadge(QWidget):
    def __init__(self, size: int = 36, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self.setFixedSize(size, size)

    def paintEvent(self, event) -> None:  # noqa: N802
        palette = theme.active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(palette.primary))
        painter.drawRoundedRect(QRectF(0, 0, self._size, self._size), 10, 10)
        font = QFont(self.font())
        font.setPixelSize(int(self._size * 0.52))
        font.setWeight(QFont.Bold)
        painter.setFont(font)
        painter.setPen(QColor("#FFFFFF"))
        painter.drawText(self.rect(), Qt.AlignCenter, "L")
        painter.end()


class StatusDot(QWidget):
    def __init__(self, size: int = 9, color: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color or theme.active().success)
        self.setFixedSize(size, size)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(self.rect().adjusted(1, 1, -1, -1))
        painter.end()


class NavItem(QPushButton):
    """侧栏导航项：图标在上、文字在下，选中态左侧有主色指示条。"""

    def __init__(self, key: str, kind: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.key = key
        self._kind = kind
        self.setText(text)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(62)
        self.setProperty("nav", "true")
        self.setIconSize(QSize(23, 23))
        self.toggled.connect(lambda _checked: self.refresh_icon())
        self.refresh_icon()

    def refresh_icon(self) -> None:
        palette = theme.active()
        color = palette.primary if self.isChecked() else palette.text_muted
        self.setIcon(glyph_icon(self._kind, 23, color, 1.5))


class SideNav(QWidget):
    page_selected = Signal(str)
    theme_toggle = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("sidenav")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(92)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 16, 10, 14)
        layout.setSpacing(6)

        brand_row = QVBoxLayout()
        brand_row.setSpacing(4)
        brand_row.addWidget(BrandBadge(36), 0, Qt.AlignHCenter)
        name = QLabel("LUMEN")
        name.setAlignment(Qt.AlignCenter)
        name.setProperty("role", "section")
        brand_row.addWidget(name)
        layout.addLayout(brand_row)
        layout.addSpacing(14)

        self._items: dict[str, NavItem] = {}
        for key, kind, text in NAV_ITEMS:
            item = NavItem(key, kind, text)
            item.clicked.connect(lambda _checked=False, k=key: self.page_selected.emit(k))
            self._items[key] = item
            layout.addWidget(item)

        layout.addStretch(1)
        self._theme_button = ToolGlyphButton("theme", "切换浅色 / 深色主题", size=40, icon_size=19)
        self._theme_button.clicked.connect(self.theme_toggle.emit)
        layout.addWidget(self._theme_button, 0, Qt.AlignHCenter)

    def set_current(self, key: str) -> None:
        for item_key, item in self._items.items():
            item.setChecked(item_key == key)

    def refresh_icons(self) -> None:
        for item in self._items.values():
            item.refresh_icon()
        self._theme_button.refresh_icon()


class LaunchBar(QWidget):
    launch_clicked = Signal()
    stop_clicked = Signal()
    log_clicked = Signal()
    folder_clicked = Signal()

    def __init__(self, ctx: AppContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.setObjectName("launchbar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(76)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 12, 24, 12)
        layout.setSpacing(12)

        self._dot = StatusDot(9)
        dot_holder = QWidget()
        dot_layout = QVBoxLayout(dot_holder)
        dot_layout.setContentsMargins(0, 0, 0, 0)
        dot_layout.addWidget(self._dot, 0, Qt.AlignVCenter)
        layout.addWidget(dot_holder, 0, Qt.AlignVCenter)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        self._primary = label("准备就绪", "body")
        self._secondary = label("", "muted")
        text_box.addWidget(self._primary)
        text_box.addWidget(self._secondary)
        layout.addLayout(text_box, 1)

        self._log_button = make_button("日志", kind="log", role="ghost", handler=self.log_clicked.emit)
        self._folder_button = make_button("目录", kind="folder", role="ghost", handler=self.folder_clicked.emit)
        layout.addWidget(self._log_button)
        layout.addWidget(self._folder_button)

        self._launch_button = QPushButton("启动游戏")
        self._launch_button.setProperty("role", "launch")
        self._launch_button.setCursor(Qt.PointingHandCursor)
        self._launch_button.setFixedHeight(44)
        self._launch_button.setMinimumWidth(168)
        self._launch_button.clicked.connect(self._on_launch_clicked)
        layout.addWidget(self._launch_button)

        self._running = False
        self._busy_text = ""
        self.refresh()

    # ------------------------------------------------------------------ #
    def _on_launch_clicked(self) -> None:
        if self._running:
            self.stop_clicked.emit()
        else:
            self.launch_clicked.emit()

    def refresh(self) -> None:
        instance = self.ctx.current_instance()
        if instance is None:
            self._primary.setText("还没有实例")
            self._secondary.setText("到「实例」页创建一个，或直接在版本管理里安装")
            self._dot.set_color(theme.active().warning)
            self._launch_button.setEnabled(False)
            return
        required = required_java_major(instance.mc_version)
        runtime = self.ctx.best_java(required)
        java_text = f"Java {runtime.major}" if runtime is not None else f"Java {required}（未就绪）"
        memory = instance.memory_gb or self.ctx.settings.memory_gb
        self._primary.setText(instance.name)
        self._secondary.setText(f"{instance.mc_version} · {instance.display_loader} · {java_text} · {memory} GB")
        self._dot.set_color(theme.active().success if runtime is not None else theme.active().warning)
        self._launch_button.setEnabled(True)
        if not self._running:
            self._launch_button.setText("启动游戏")

    def set_busy(self, text: str) -> None:
        self._busy_text = text
        if text:
            self._dot.set_color(theme.active().primary)
            self._launch_button.setEnabled(False)
            self._launch_button.setText(text if len(text) <= 12 else "启动中…")
        else:
            self._launch_button.setText("启动游戏" if not self._running else "结束游戏")
            self._launch_button.setEnabled(True)
            self.refresh()

    def set_running(self, running: bool) -> None:
        self._running = running
        if running:
            self._dot.set_color(theme.active().primary)
            self._launch_button.setText("结束游戏")
            self._launch_button.setEnabled(True)
            self._launch_button.setProperty("role", "primary")
            self._secondary.setText("游戏运行中 · 日志会实时写入实例 logs 目录")
        else:
            self._launch_button.setText("启动游戏")
            self._launch_button.setProperty("role", "launch")
            self.refresh()
        self._launch_button.style().unpolish(self._launch_button)
        self._launch_button.style().polish(self._launch_button)

    def refresh_icons(self) -> None:
        for button in (self._log_button, self._folder_button):
            button.refresh_icon()
        self.refresh()


# --------------------------------------------------------------------------- #
# 主窗口
# --------------------------------------------------------------------------- #
class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.tasks = TaskManager(self)
        self._launch_worker: Worker | None = None
        self._plan: LaunchPlan | None = None

        self.setWindowTitle(f"{APP_DISPLAY_NAME} {APP_VERSION}")
        width, height = ctx.settings.window_size()
        self.resize(width, height)
        self.setMinimumSize(980, 660)
        self.setWindowIcon(QIcon(glyph_pixmap("spark", 64, theme.active().primary, 1.4)))

        self._build()
        self._connect()
        self._apply_theme(ctx.current_theme(), initial=True)
        self._restore_page()
        self._bootstrap_async()

    # ------------------------------------------------------------------ #
    def _build(self) -> None:
        central = QWidget()
        central.setObjectName("root")
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self.nav = SideNav()
        body.addWidget(self.nav)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 6, 0, 0)
        right_layout.setSpacing(0)
        self.stack = QStackedWidget()
        right_layout.addWidget(self.stack)
        body.addWidget(right, 1)
        outer.addLayout(body, 1)

        self.launch_bar = LaunchBar(self.ctx)
        outer.addWidget(self.launch_bar)
        self.setCentralWidget(central)

        self.pages = {
            "versions": VersionsPage(self.ctx, self.tasks),
            "browse": BrowsePage(self.ctx, self.tasks),
            "modpacks": ModpacksPage(self.ctx, self.tasks),
            "instances": InstancesPage(self.ctx, self.tasks),
            "settings": SettingsPage(self.ctx, self.tasks),
        }
        for page in self.pages.values():
            self.stack.addWidget(page)

        self.toast = Toast(self)

    def _connect(self) -> None:
        self.nav.page_selected.connect(self._switch_page)
        self.nav.theme_toggle.connect(self._toggle_theme)
        self.launch_bar.launch_clicked.connect(self._launch_current)
        self.launch_bar.stop_clicked.connect(self._stop_game)
        self.launch_bar.log_clicked.connect(self._open_logs)
        self.launch_bar.folder_clicked.connect(self._open_folder)

        self.ctx.toast.connect(self._show_toast)
        self.ctx.instances_changed.connect(self._on_instances_changed)
        self.ctx.request_launch.connect(self._launch_instance)
        self.ctx.request_logs.connect(self._open_logs)
        self.ctx.request_page.connect(self._switch_page)
        self.ctx.request_browse.connect(self._open_browse)
        self.ctx.theme_changed.connect(lambda name: self._apply_theme(name))
        self.ctx.game_started.connect(self._on_game_started)
        self.ctx.game_exited.connect(self._on_game_exited)

    # ------------------------------------------------------------------ #
    # 初始化
    # ------------------------------------------------------------------ #
    def _restore_page(self) -> None:
        key = str(self.ctx.settings.get("last_page") or "versions")
        if key not in self.pages:
            key = "versions"
        self._switch_page(key)

    def _bootstrap_async(self) -> None:
        self.launch_bar.set_busy("正在扫描本机 Java…")
        worker = Worker(lambda **_: self.ctx.ensure_java(), label="扫描 Java")
        worker.succeeded.connect(self._on_java_scanned)
        worker.failed.connect(lambda message, hint: self.launch_bar.set_busy(""))
        self.tasks.start(worker)

    def _on_java_scanned(self, runtimes) -> None:
        self.launch_bar.set_busy("")
        self.pages["settings"]._refresh_java_status()
        if not runtimes:
            self._show_toast("未发现 Java，启动游戏时会自动下载或提示你指定")

    # ------------------------------------------------------------------ #
    # 主题 / 页面
    # ------------------------------------------------------------------ #
    def _apply_theme(self, name: str, *, initial: bool = False) -> None:
        """QSS 由 AppContext.apply_theme 统一应用；这里只刷新自绘图标与文字颜色。"""
        for page in self.pages.values():
            page.refresh_icons()
            page.on_theme_changed()
        self.nav.refresh_icons()
        self.launch_bar.refresh_icons()
        self.toast.style().unpolish(self.toast)
        self.toast.style().polish(self.toast)
        self.update()

    def _toggle_theme(self) -> None:
        name = self.ctx.toggle_theme()
        self._show_toast("已切换到深色主题" if name == "dark" else "已切换到浅色主题")

    def _switch_page(self, key: str) -> None:
        if key not in self.pages:
            return
        self.stack.setCurrentWidget(self.pages[key])
        self.nav.set_current(key)
        self.ctx.settings.set("last_page", key)
        page = self.pages[key]
        try:
            page.on_show()
        except Exception as exc:  # noqa: BLE001 - 页面刷新失败不应导致崩溃
            MessageDialog.tell(self, "页面加载失败", str(exc), level="error")

    def show_page(self, key: str) -> None:
        """外部（冒烟测试/深链）切换页面的公开入口。"""
        self._switch_page(key)

    def _open_browse(self, payload: dict | None = None) -> None:
        """跳转到资源页并按需预设条件（类型 / 实例 / 关键词）。"""
        self._switch_page("browse")
        page = self.pages.get("browse")
        preset = getattr(page, "preset", None)
        if callable(preset):
            try:
                preset(**(payload or {}))
            except TypeError:
                preset()

    # ------------------------------------------------------------------ #
    # 启动流程
    # ------------------------------------------------------------------ #
    def _launch_current(self) -> None:
        instance = self.ctx.current_instance()
        if instance is None:
            self._show_toast("请先创建一个实例")
            self._switch_page("instances")
            return
        self._launch_instance(instance)

    def _launch_instance(self, instance) -> None:
        if self.ctx.game_running:
            self._show_toast("游戏已经在运行了")
            return
        self.ctx.select_instance(instance)
        self.launch_bar.set_busy("自检中…")
        self._show_toast(f"正在检查 {instance.name} 的运行环境…")
        worker = Worker(
            lambda progress=None: self.ctx.build_plan(instance, progress=progress),
            with_progress=True,
            label="启动自检",
        )
        worker.progressed.connect(lambda stage: self.launch_bar.set_busy(getattr(stage, "message", "自检中…")))
        worker.succeeded.connect(self._on_plan_ready)
        worker.failed.connect(self._on_launch_failed)
        self._launch_worker = self.tasks.start(worker)

    def _on_plan_ready(self, plan: LaunchPlan) -> None:
        self.launch_bar.set_busy("")
        self._plan = plan
        if plan.issues:
            dialog = IssueDialog(
                self,
                issues=plan.issues,
                allow_continue=not plan.has_error,
                summary=f"{plan.instance.name} · {plan.version_id} · Java {plan.required_java} · {plan.memory_mb} MB",
            )
            accepted = dialog.exec() == QDialog.Accepted and dialog.continue_requested
            if not accepted:
                return
        self._start_game(plan)

    def _start_game(self, plan: LaunchPlan) -> None:
        try:
            session = self.ctx.start_game(plan)
        except LumenError as exc:
            MessageDialog.tell(self, "启动失败", exc.message, details=exc.hint, level="error")
            self.launch_bar.set_busy("")
            return
        self._session = session
        self.launch_bar.set_running(True)
        self._show_toast("游戏启动中，日志已写入实例 logs 目录")

    def _on_launch_failed(self, message: str, hint: str) -> None:
        self.launch_bar.set_busy("")
        MessageDialog.tell(
            self,
            "启动失败",
            message,
            details=hint,
            level="error",
        )

    def _stop_game(self) -> None:
        if not self.ctx.game_running:
            return
        if not MessageDialog.ask(
            self,
            "结束游戏",
            "强制结束游戏进程可能丢失未保存的进度，确定继续吗？",
            ok_text="结束游戏",
            danger=True,
            level="warning",
        ):
            return
        self.ctx.stop_game()
        self._show_toast("已请求结束游戏进程")

    def _on_game_started(self) -> None:
        self.launch_bar.set_running(True)

    def _on_game_exited(self, code: int, elapsed: float) -> None:
        self.launch_bar.set_running(False)
        session = self.ctx.session
        if code == 0:
            self._show_toast(f"游戏正常退出，本次运行 {elapsed:.0f} 秒")
            return
        findings = []
        if session is not None:
            instance = self.ctx.current_instance()
            required = required_java_major(instance.mc_version) if instance else 0
            findings = session.analyze(exit_code=code, required_java=required)
        if findings:
            title = findings[0].title
            detail = "\n".join(f"· {item.title}：{item.suggestion}" for item in findings[:3])
            open_log = MessageDialog.ask(
                self,
                f"游戏异常退出（{title}）",
                detail,
                ok_text="查看完整日志",
                cancel_text="关闭",
                level="error",
            )
        else:
            open_log = MessageDialog.ask(
                self,
                f"游戏异常退出（退出码 {code}）",
                "日志里没有匹配到常见错误特征，建议查看完整日志或 crash-reports。",
                ok_text="查看日志",
                cancel_text="关闭",
                level="warning",
            )
        if open_log:
            self._open_logs()

    # ------------------------------------------------------------------ #
    # 其它入口
    # ------------------------------------------------------------------ #
    def _open_logs(self, *_args) -> None:
        session = self.ctx.session if self.ctx.game_running else None
        instance = self.ctx.current_instance()
        LogDialog(self, ctx=self.ctx, tasks=self.tasks, instance=instance, session=session).exec()

    def _open_folder(self) -> None:
        instance = self.ctx.current_instance()
        if instance is None:
            self.ctx.open_path(self.ctx.paths.data_root)
            return
        self.ctx.open_instance_folder(instance)

    def _on_instances_changed(self) -> None:
        self.launch_bar.refresh()

    def _show_toast(self, text: str) -> None:
        if not text:
            return
        self.toast.show_message(text)

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:  # noqa: N802
        if self.ctx.game_running:
            if not MessageDialog.ask(
                self,
                "退出启动器",
                "游戏仍在运行。退出启动器会结束游戏进程，确定要退出吗？",
                ok_text="退出",
                cancel_text="继续游玩",
                danger=True,
                level="warning",
            ):
                event.ignore()
                return
            self.ctx.stop_game()
        self.tasks.shutdown(4000)
        try:
            self.ctx.settings.remember_window(self.width(), self.height(), self.isMaximized())
        except Exception:  # noqa: BLE001
            pass
        event.accept()
