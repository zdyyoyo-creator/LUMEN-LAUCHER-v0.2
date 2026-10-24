"""Java 管理对话框：扫描本机 / 手动添加 / 自动获取 Mojang 运行时。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QListWidget, QListWidgetItem, QVBoxLayout, QWidget

from ...java.detect import JavaRuntime, invalidate_cache, probe_java, scan_java_runtimes
from ...java.runtime import install_runtime
from ...utils.fsutil import human_size
from ..context import AppContext
from ..dialogs.common import BaseDialog, MessageDialog
from ..widgets import GlyphWidget, label, make_button
from ..workers import TaskManager, Worker


class JavaDialog(BaseDialog):
    def __init__(self, parent: QWidget | None, *, ctx: AppContext, tasks: TaskManager) -> None:
        super().__init__(parent, title="Java 运行时", width=680)
        self.ctx = ctx
        self.tasks = tasks
        self._runtimes: list[JavaRuntime] = []
        self._selected: str | None = None
        self._busy = False

        self._layout.addWidget(
            label(
                "启动器会自动扫描 PATH、JAVA_HOME、注册表与常见安装目录；找不到时可按游戏版本自动下载 Mojang 官方运行时。",
                "muted",
                wrap=True,
            )
        )

        self._status = label("", "muted")
        self._layout.addWidget(self._status)

        self._list = QListWidget()
        self._list.setMinimumHeight(260)
        self._list.itemDoubleClicked.connect(lambda _item: self._accept_selected())
        self._layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        row.setSpacing(8)
        self._scan_button = make_button("重新扫描", kind="refresh", handler=self._scan)
        row.addWidget(self._scan_button)
        self._add_button = make_button("手动添加", kind="folder", handler=self._add_manual)
        row.addWidget(self._add_button)
        row.addStretch(1)
        self._dl21 = make_button("自动获取 Java 21", kind="download", handler=lambda: self._download(21))
        self._dl17 = make_button("Java 17", kind="download", handler=lambda: self._download(17))
        self._dl8 = make_button("Java 8", kind="download", handler=lambda: self._download(8))
        for button in (self._dl21, self._dl17, self._dl8):
            row.addWidget(button)
        self._layout.addLayout(row)

        self.add_footer_button(make_button("关闭", handler=self.reject))
        self._use_button = self.add_footer_button(make_button("设为全局 Java", role="primary", handler=self._accept_selected))
        self.finish()

        self._refresh(list(ctx.runtimes))
        if not ctx.runtimes:
            self._scan()

    # ------------------------------------------------------------------ #
    def _refresh(self, runtimes: list[JavaRuntime]) -> None:
        self._runtimes = runtimes
        self._list.clear()
        if not runtimes:
            item = QListWidgetItem("还没有发现可用的 Java，点「重新扫描」或「自动获取 Java 21」")
            item.setFlags(Qt.NoItemFlags)
            self._list.addItem(item)
            self._status.setText("未发现 Java")
            self._use_button.setEnabled(False)
            return
        current = self.ctx.settings.java_path
        current_row = -1
        for index, runtime in enumerate(runtimes):
            text = f"Java {runtime.major}　·　{runtime.vendor or '未知发行版'}　·　{runtime.version}"
            item = QListWidgetItem(text)
            item.setToolTip(str(runtime.path))
            item.setData(Qt.UserRole, str(runtime.path))
            self._list.addItem(item)
            if current and str(runtime.path) == current:
                current_row = index
        if current_row >= 0:
            self._list.setCurrentRow(current_row)
        else:
            self._list.setCurrentRow(0)
        managed = sum(1 for item in runtimes if item.managed)
        self._status.setText(f"共 {len(runtimes)} 个 Java，其中 {managed} 个由启动器管理")
        self._use_button.setEnabled(True)

    # ------------------------------------------------------------------ #
    def _scan(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._scan_button.setEnabled(False)
        self._status.setText("正在扫描本机 Java…")
        invalidate_cache()
        worker = Worker(lambda **_: scan_java_runtimes(self.ctx.paths), label="扫描 Java")
        worker.succeeded.connect(self._on_scanned)
        worker.failed.connect(self._on_failed)
        self.tasks.start(worker)

    def _on_scanned(self, runtimes: list[JavaRuntime]) -> None:
        self._busy = False
        self._scan_button.setEnabled(True)
        self.ctx.runtimes = runtimes
        import time

        self.ctx.java_scanned_at = time.time()
        self._refresh(runtimes)

    def _on_failed(self, message: str, hint: str) -> None:
        self._busy = False
        self._scan_button.setEnabled(True)
        self._status.setText("扫描失败")
        MessageDialog.tell(self, "扫描失败", message, details=hint, level="error")

    def _add_manual(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 java 可执行文件",
            str(Path.home()),
            "Java 可执行文件 (java.exe java javaw.exe);;所有文件 (*.*)",
        )
        if not path:
            return
        runtime = probe_java(Path(path), source="手动添加", managed=False)
        if runtime is None:
            MessageDialog.tell(self, "无法识别", f"执行 {path} -version 失败，请确认这是一个可用的 Java。", level="error")
            return
        runtimes = [runtime] + [item for item in self._runtimes if str(item.path) != str(runtime.path)]
        self._refresh(runtimes)
        self._list.setCurrentRow(0)
        self.ctx.toast.emit(f"已添加 Java {runtime.major}")

    def _download(self, major: int) -> None:
        if self._busy:
            return
        if not MessageDialog.ask(
            self,
            f"自动获取 Java {major}",
            f"将从 Mojang 官方运行时清单下载 Java {major}（约 40–60MB），失败时自动改用 Adoptium。继续吗？",
            ok_text="开始下载",
        ):
            return
        self._busy = True
        self._set_buttons_enabled(False)
        self._status.setText(f"正在下载 Java {major}…（进度见启动器日志）")
        worker = Worker(
            lambda **_: install_runtime(self.ctx.selector, self.ctx.paths, component=None, major=major),
            label=f"下载 Java {major}",
        )
        worker.succeeded.connect(lambda path, m=major: self._on_downloaded(Path(path), m))
        worker.failed.connect(self._on_download_failed)
        self.tasks.start(worker)

    def _on_downloaded(self, path: Path, major: int) -> None:
        self._busy = False
        self._set_buttons_enabled(True)
        runtime = probe_java(path, source="自动下载", managed=True)
        if runtime is None:
            MessageDialog.tell(self, "下载完成但无法运行", str(path), level="error")
            return
        runtimes = [runtime] + [item for item in self._runtimes if str(item.path) != str(runtime.path)]
        self._refresh(runtimes)
        index = next((i for i, item in enumerate(runtimes) if str(item.path) == str(runtime.path)), 0)
        self._list.setCurrentRow(index)
        self.ctx.toast.emit(f"Java {major} 已就绪")

    def _on_download_failed(self, message: str, hint: str) -> None:
        self._busy = False
        self._set_buttons_enabled(True)
        self._status.setText("下载失败")
        MessageDialog.tell(self, "下载失败", message, details=hint, level="error")

    def _set_buttons_enabled(self, enabled: bool) -> None:
        for button in (self._scan_button, self._add_button, self._dl8, self._dl17, self._dl21):
            button.setEnabled(enabled)

    # ------------------------------------------------------------------ #
    def _current_path(self) -> str | None:
        item = self._list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.UserRole)
        return str(data) if data else None

    def _accept_selected(self) -> None:
        path = self._current_path()
        if not path:
            MessageDialog.tell(self, "没有选中 Java", "请先在列表里选择一个 Java。", level="warning")
            return
        self._selected = path
        self.accept()

    @property
    def selected_path(self) -> str | None:
        return self._selected
