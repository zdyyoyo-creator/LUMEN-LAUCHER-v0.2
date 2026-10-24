"""Forge / NeoForge 安装向导。

四步引导，每一步都有明确的状态与进度：

    ① 确认参数   目标实例 / 加载器版本 / Java，并做环境自检
    ② 准备原版   客户端、依赖库、资源、natives（复用已有文件，已校验的会跳过）
    ③ 安装加载器 下载官方安装器 → 运行 → 实时输出日志 + 解析进度 + 计时
    ④ 完成       结果摘要，可打开目录或直接启动

进度条完全由 ``VersionInstaller.install_forge`` 的 StageProgress 驱动：
0.68 之前属于「准备原版」，之后属于「安装加载器」，因此步骤切换与进度天然对齐。
"""

from __future__ import annotations

import shutil
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.installer import InstallResult, StageProgress, VersionInstaller
from ...core.loaders import LOADER_LABELS, available_loader_versions
from ...java.detect import JavaRuntime
from ...utils.errors import LumenError
from ...utils.fsutil import human_size, human_speed
from ...utils.versions import required_java_major
from ..context import AppContext
from ..dialogs.common import BaseDialog, MessageDialog, TextInputDialog
from ..widgets import (
    Card,
    Divider,
    GlyphWidget,
    RingProgress,
    StepIndicator,
    label,
    make_button,
)
from ..workers import TaskManager, Worker

STEPS = ("确认参数", "准备原版", "安装加载器", "完成")

STAGE_TEXT = {
    "prepare": "准备版本信息",
    "client": "下载客户端",
    "libraries": "下载依赖库",
    "asset_index": "获取资源索引",
    "assets": "下载游戏资源",
    "natives": "解压本地库",
    "installer": "安装加载器",
    "finish": "收尾",
}

#: 0.68 是「准备原版」与「安装加载器」的分界（与 installer.install_forge 的权重一致）
PREPARE_BOUNDARY = 0.675
FINISH_BOUNDARY = 0.965


class ForgeWizardDialog(BaseDialog):
    """Forge / NeoForge 的引导式安装。"""

    #: 安装器输出行（后台线程 → 主线程排队）
    output_line = Signal(str)

    def __init__(
        self,
        parent: QWidget | None,
        *,
        ctx: AppContext,
        tasks: TaskManager,
        kind: str = "forge",
        mc_version: str = "",
        instance=None,
    ) -> None:
        title = f"{LOADER_LABELS.get(kind, 'Forge')} 安装向导"
        super().__init__(parent, title=title, width=820)
        self.ctx = ctx
        self.tasks = tasks
        self.kind = kind
        self.mc_version = mc_version or (instance.mc_version if instance is not None else "")
        self._instance = instance
        self._loader_versions: list = []
        self._installer: VersionInstaller | None = None
        self._worker: Worker | None = None
        self._result: InstallResult | None = None
        self._java_path = ""
        self._loader_version = ""
        self._started_at = 0.0
        self._pending_lines: list[str] = []
        self._failed = False
        self._step = 0

        self._indicator = StepIndicator(STEPS)
        self.body().addWidget(self._indicator)

        self._stack = QStackedWidget()
        self.body().addWidget(self._stack, 1)
        self._build_config()
        self._build_prepare()
        self._build_install()
        self._build_done()
        for page in (self._config_page, self._prepare_page, self._install_page, self._done_page):
            self._stack.addWidget(page)

        self._cancel_button = self.add_footer_button(make_button("取消", handler=self._on_cancel))
        self._retry_button = self.add_footer_button(make_button("重试", kind="refresh", handler=self._retry))
        self._open_folder_button = self.add_footer_button(
            make_button("打开实例目录", kind="folder", handler=self._open_folder)
        )
        self._launch_button = self.add_footer_button(
            make_button("启动游戏", kind="play", role="primary", handler=self._launch)
        )
        self._retry_button.setVisible(False)
        self._open_folder_button.setVisible(False)
        self._launch_button.setVisible(False)
        self._primary_button = self.add_footer_button(
            make_button("开始安装", kind="download", role="primary", handler=self._start)
        )
        self._primary_button.setDefault(True)
        self.finish()

        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._update_clock)

        self.output_line.connect(self._queue_line)
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(120)
        self._flush_timer.timeout.connect(self._flush_lines)
        self._flush_timer.start()

        self._goto(0)
        self._load_loader_versions()
        self._refresh_checks()
        # 主窗口启动时会扫描 Java；这里兜底一次，避免从其它入口打开时列表为空
        if not self.ctx.runtimes:
            self._scan_java()

    # ------------------------------------------------------------------ #
    # ① 确认参数
    # ------------------------------------------------------------------ #
    def _build_config(self) -> None:
        self._config_page = QWidget()
        layout = QVBoxLayout(self._config_page)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(12)

        intro = Card(margins=(14, 12, 14, 12), spacing=6, flat=True)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(GlyphWidget("mods", 20, color=self._palette.primary, width=1.6))
        head.addWidget(label(f"{LOADER_LABELS.get(self.kind, 'Forge')} · Minecraft {self.mc_version}", "subtitle"), 1)
        intro.body().addLayout(head)
        intro.body().addWidget(
            label(
                "Forge/NeoForge 没有 profile 接口，需要调用官方安装器在本地生成版本文件。"
                "安装过程会下载约 25MB 安装器，再由安装器补齐它自己的依赖（视网络可能需要 2–5 分钟）。",
                "muted",
                wrap=True,
            )
        )
        layout.addWidget(intro)

        instance_row = QHBoxLayout()
        instance_row.setSpacing(10)
        instance_row.addWidget(label("目标实例", "body"))
        instance_row.addStretch(1)
        self._instance_combo = QComboBox()
        self._instance_combo.setMinimumWidth(300)
        self._instance_combo.currentIndexChanged.connect(self._on_instance_changed)
        self._fill_instances()
        instance_row.addWidget(self._instance_combo)
        layout.addLayout(instance_row)

        version_row = QHBoxLayout()
        version_row.setSpacing(10)
        version_row.addWidget(label("加载器版本", "body"))
        version_row.addStretch(1)
        self._loader_combo = QComboBox()
        self._loader_combo.setMinimumWidth(300)
        self._loader_combo.addItem("正在获取版本列表…", "")
        self._loader_combo.setEnabled(False)
        self._loader_combo.currentIndexChanged.connect(self._on_loader_changed)
        version_row.addWidget(self._loader_combo)
        layout.addLayout(version_row)

        java_row = QHBoxLayout()
        java_row.setSpacing(10)
        java_row.addWidget(label("运行安装器的 Java", "body"))
        java_row.addStretch(1)
        self._java_combo = QComboBox()
        self._java_combo.setMinimumWidth(300)
        self._fill_java()
        self._java_combo.currentIndexChanged.connect(self._on_java_changed)
        java_row.addWidget(self._java_combo)
        layout.addLayout(java_row)

        checks = Card(margins=(14, 12, 14, 12), spacing=8, flat=True)
        checks.body().addWidget(label("环境检查", "section"))
        self._check_rows: list[tuple[GlyphWidget, QLabel]] = []
        for _ in range(4):
            row = QHBoxLayout()
            row.setSpacing(8)
            icon = GlyphWidget("check", 15, color=self._palette.success, width=1.6)
            text = label("", "muted", wrap=True)
            row.addWidget(icon, 0, Qt.AlignTop)
            row.addWidget(text, 1)
            checks.body().addLayout(row)
            self._check_rows.append((icon, text))
        layout.addWidget(checks)
        layout.addStretch(1)

    @property
    def _palette(self):
        from .. import theme

        return theme.active()

    def _fill_instances(self) -> None:
        combo = self._instance_combo
        combo.blockSignals(True)
        combo.clear()
        for instance in self.ctx.instances():
            combo.addItem(f"{instance.name}  ·  {instance.mc_version}", instance.id)
        combo.addItem("＋ 新建实例…", "__new__")
        if self._instance is not None:
            index = combo.findData(self._instance.id)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _fill_java(self) -> None:
        combo = self._java_combo
        combo.blockSignals(True)
        combo.clear()
        required = required_java_major(self.mc_version) if self.mc_version else 17
        if not self.ctx.runtimes:
            combo.addItem("未发现 Java（请先获取 Java 17/21）", "")
            combo.setEnabled(False)
        else:
            combo.setEnabled(True)
            candidates = [runtime for runtime in self.ctx.runtimes if runtime.major >= 8]
            for runtime in candidates:
                combo.addItem(f"Java {runtime.major} · {runtime.vendor or '未知'} · {runtime.path}", str(runtime.path))
            # 默认选中最接近需求的 Java
            best = None
            for index in range(combo.count()):
                path = combo.itemData(index)
                runtime = next((r for r in candidates if str(r.path) == path), None)
                if runtime is None:
                    continue
                if runtime.major == required:
                    best = index
                    break
                if best is None and runtime.major >= required:
                    best = index
            if best is None and combo.count() > 0:
                best = 0
            if best is not None:
                combo.setCurrentIndex(best)
        combo.blockSignals(False)

    def _on_instance_changed(self) -> None:
        if self._instance_combo.currentData() == "__new__":
            name = TextInputDialog.ask(
                self,
                title="新建实例",
                label_text="实例名称",
                value=f"{self.mc_version} {LOADER_LABELS.get(self.kind, 'Forge')}",
                description="每个实例有独立的 mods / 存档 / 配置。",
            )
            if not name:
                index = self._instance_combo.findData(self._instance.id) if self._instance else 0
                self._instance_combo.setCurrentIndex(max(0, index))
                return
            try:
                instance = self.ctx.store.create(name, self.mc_version, loader_type=self.kind)
            except LumenError as exc:
                MessageDialog.tell(self, "创建失败", str(exc), level="error")
                return
            self._instance = instance
            self.ctx.select_instance(instance)
            self.ctx.instances_changed.emit()
            self._fill_instances()
        self._refresh_checks()

    def _on_loader_changed(self) -> None:
        self._refresh_checks()

    def _on_java_changed(self) -> None:
        self._refresh_checks()

    def _scan_java(self) -> None:
        self._java_combo.blockSignals(True)
        self._java_combo.clear()
        self._java_combo.addItem("正在扫描本机 Java…", "")
        self._java_combo.setEnabled(False)
        self._java_combo.blockSignals(False)
        worker = Worker(lambda **_: self.ctx.ensure_java(force=True), label="扫描 Java")
        worker.succeeded.connect(self._on_java_scanned)
        worker.failed.connect(lambda message, hint: self.ctx.toast.emit(f"扫描 Java 失败：{message}"))
        self.tasks.start(worker)

    def _on_java_scanned(self, runtimes) -> None:
        self._fill_java()
        self._refresh_checks()

    # ------------------------------------------------------------------ #
    def _load_loader_versions(self) -> None:
        worker = Worker(
            lambda **_: available_loader_versions(self.ctx.selector, self.kind, self.mc_version),
            label=f"{self.kind} 版本列表",
        )
        worker.succeeded.connect(self._on_versions)
        worker.failed.connect(self._on_versions_failed)
        self.tasks.start(worker)

    def _on_versions(self, versions: list) -> None:
        self._loader_versions = versions
        self._loader_combo.blockSignals(True)
        self._loader_combo.clear()
        if not versions:
            self._loader_combo.addItem(f"没有适用于 {self.mc_version} 的 {LOADER_LABELS.get(self.kind, '')} 版本", "")
            self._loader_combo.setEnabled(False)
        else:
            for item in versions[:80]:
                suffix = "（推荐）" if item is versions[0] else ""
                self._loader_combo.addItem(f"{item.version}{suffix}", item.version)
            self._loader_combo.setEnabled(True)
            self._loader_combo.setCurrentIndex(0)
        self._loader_combo.blockSignals(False)
        self._refresh_checks()

    def _on_versions_failed(self, message: str, hint: str) -> None:
        self._loader_combo.blockSignals(True)
        self._loader_combo.clear()
        self._loader_combo.addItem("获取版本列表失败", "")
        self._loader_combo.setEnabled(False)
        self._loader_combo.blockSignals(False)
        self._refresh_checks()
        self._install_message.setText(f"{message} {hint}".strip())
        self.ctx.toast.emit(f"无法获取 {LOADER_LABELS.get(self.kind, '')} 版本列表：{message}")

    # ------------------------------------------------------------------ #
    def _selected_instance(self):
        data = self._instance_combo.currentData()
        if data and data != "__new__":
            instance = self.ctx.store.get(str(data))
            if instance is not None:
                return instance
        return self._instance

    def _selected_java(self) -> JavaRuntime | None:
        data = str(self._java_combo.currentData() or "")
        if not data:
            return None
        return next((runtime for runtime in self.ctx.runtimes if str(runtime.path) == data), None)

    def _refresh_checks(self) -> None:
        instance = self._selected_instance()
        java = self._selected_java()
        loader_version = str(self._loader_combo.currentData() or "")

        rows = []
        if instance is None:
            rows.append((False, "目标实例：未选择"))
        elif instance.mc_version and instance.mc_version != self.mc_version:
            rows.append(
                (
                    False,
                    f"目标实例：{instance.name} 当前是 {instance.mc_version}，安装后主版本会切换为 {self.mc_version}",
                )
            )
        else:
            rows.append((True, f"目标实例：{instance.name}（{instance.mc_version}）"))

        if java is None:
            rows.append((False, "Java：未选择，安装器无法运行"))
        else:
            note = "（建议 17/21）" if java.major < 17 else ""
            rows.append((True, f"Java：{java.major} · {java.vendor or '未知发行版'}{note}"))

        try:
            free = shutil.disk_usage(str(self.ctx.paths.data_root)).free
            ok = free > 800 * 1024 * 1024
            rows.append((ok, f"磁盘可用：{human_size(free)}（安装器与依赖约需 300MB+）"))
        except OSError:
            rows.append((True, "磁盘可用：无法检测"))

        if not loader_version:
            rows.append((False, "加载器版本：尚未选择"))
        else:
            rows.append((True, f"加载器版本：{loader_version}"))

        for index, (icon, text) in enumerate(self._check_rows):
            if index < len(rows):
                ok, message = rows[index]
                icon.set_kind("check" if ok else "warning")
                icon.set_color(self._palette.success if ok else self._palette.warning)
                text.setText(message)
            else:
                icon.set_kind("info")
                icon.set_color(self._palette.text_muted)
                text.setText("")

        ready = instance is not None and java is not None and bool(loader_version)
        self._primary_button.setEnabled(ready)

    # ------------------------------------------------------------------ #
    # ② 准备原版 / ③ 安装加载器
    # ------------------------------------------------------------------ #
    def _progress_page(
        self, caption: str, hint: str, tip: str = ""
    ) -> tuple[QWidget, RingProgress, QLabel, QLabel, QLabel]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(22)
        ring = RingProgress(104, 8)
        top.addWidget(ring, 0, Qt.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(6)
        stage = label(caption, "subtitle")
        message = label(hint, "body", wrap=True)
        detail = label("", "muted")
        text_box.addWidget(stage)
        text_box.addWidget(message)
        text_box.addWidget(detail)
        text_box.addStretch(1)
        top.addLayout(text_box, 1)
        layout.addLayout(top)
        if tip:
            tip_label = label(tip, "muted", wrap=True)
            layout.addWidget(tip_label)
        layout.addStretch(1)
        return page, ring, stage, message, detail

    def _build_prepare(self) -> None:
        self._prepare_page, self._prepare_ring, self._prepare_stage, self._prepare_message, self._prepare_detail = (
            self._progress_page(
                "准备原版文件",
                "正在下载客户端与依赖库，已存在且校验通过的文件会自动跳过",
                tip="这一步与普通原版安装完全相同；完成后才会运行 Forge/NeoForge 官方安装器。",
            )
        )

    def _build_install(self) -> None:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(12)

        top = QHBoxLayout()
        top.setSpacing(22)
        self._install_ring = RingProgress(104, 8)
        top.addWidget(self._install_ring, 0, Qt.AlignTop)
        text_box = QVBoxLayout()
        text_box.setSpacing(6)
        self._install_stage = label("安装加载器", "subtitle")
        self._install_message = label("正在启动安装器…", "body", wrap=True)
        self._install_detail = label("", "muted")
        self._elapsed_label = label("已运行 0 秒", "muted")
        text_box.addWidget(self._install_stage)
        text_box.addWidget(self._install_message)
        text_box.addWidget(self._install_detail)
        text_box.addWidget(self._elapsed_label)
        text_box.addStretch(1)
        top.addLayout(text_box, 1)
        layout.addLayout(top)

        layout.addWidget(label("安装器实时输出", "section"))
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(4000)
        self._log_view.setMinimumHeight(210)
        self._log_view.setPlaceholderText("安装器输出会显示在这里…")
        layout.addWidget(self._log_view, 1)
        self._install_page = page

    # ------------------------------------------------------------------ #
    # ④ 完成
    # ------------------------------------------------------------------ #
    def _build_done(self) -> None:
        self._done_page = QWidget()
        layout = QVBoxLayout(self._done_page)
        layout.setContentsMargins(0, 24, 0, 0)
        layout.setSpacing(12)

        self._done_icon = GlyphWidget("check", 44, color=self._palette.success, width=1.4)
        layout.addWidget(self._done_icon, 0, Qt.AlignHCenter)
        self._done_title = label("安装完成", "subtitle")
        self._done_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._done_title)
        self._done_detail = label("", "muted", wrap=True)
        self._done_detail.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._done_detail)
        layout.addStretch(1)

    # ------------------------------------------------------------------ #
    # 步骤切换
    # ------------------------------------------------------------------ #
    def _goto(self, index: int) -> None:
        self._step = index
        self._stack.setCurrentIndex(index)
        self._indicator.set_current(index)
        if index == 0:
            self._cancel_button.setText("取消")
            self._cancel_button.setVisible(True)
            self._primary_button.setVisible(True)
            self._open_folder_button.setVisible(False)
            self._launch_button.setVisible(False)
        elif index in (1, 2):
            self._cancel_button.setText("取消安装")
            self._cancel_button.setVisible(True)
            self._primary_button.setVisible(False)
            self._open_folder_button.setVisible(False)
            self._launch_button.setVisible(False)
        else:
            self._cancel_button.setText("关闭")
            self._cancel_button.setVisible(True)
            self._primary_button.setVisible(False)
            # 具体显示哪几个按钮由 _on_done / _on_failed / _on_cancelled 决定
            self._retry_button.setVisible(False)
            self._open_folder_button.setVisible(False)
            self._launch_button.setVisible(False)

    def _stage_text(self, stage: str) -> str:
        return STAGE_TEXT.get(stage, stage or "安装加载器")

    def _detail_text(self, progress: StageProgress) -> str:
        chunks: list[str] = []
        if progress.files_total:
            chunks.append(f"{progress.files_done}/{progress.files_total} 个文件")
        if progress.bytes_total:
            chunks.append(f"{human_size(progress.bytes_done)} / {human_size(progress.bytes_total)}")
        elif progress.bytes_done:
            chunks.append(human_size(progress.bytes_done))
        if progress.speed:
            chunks.append(human_speed(progress.speed))
        # 运行时长统一由 _elapsed_label 显示，避免和 progress.detail 重复
        return " · ".join(chunks)

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def _start(self) -> None:
        instance = self._selected_instance()
        java = self._selected_java()
        loader_version = str(self._loader_combo.currentData() or "")
        if instance is None:
            MessageDialog.tell(self, "没有目标实例", "请选择一个实例，或新建一个。", level="warning")
            return
        if not loader_version:
            MessageDialog.tell(self, "缺少加载器版本", "请等待版本列表加载完成。", level="warning")
            return
        if java is None:
            MessageDialog.tell(
                self,
                "缺少 Java",
                "Forge/NeoForge 需要本机 Java 来运行官方安装器。请先在「设置 → Java」里指定或自动获取 Java 17/21。",
                level="warning",
            )
            return

        self._java_path = str(java.path)
        self._loader_version = loader_version
        self._installer = VersionInstaller(self.ctx.selector, self.ctx.paths, self.ctx.store, instance)
        self._started_at = time.monotonic()
        self._failed = False
        self._log_view.clear()
        self._pending_lines.clear()
        self._goto(1)
        self._prepare_ring.set_value(0.0)
        self._install_ring.set_value(0.0)
        self._install_ring.set_indeterminate(False)
        self._elapsed_label.setText("已运行 0 秒")
        self._clock.start()

        self._worker = Worker(self._run_install, with_progress=True, label=f"{self.kind} 安装")
        self._worker.progressed.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self.tasks.start(self._worker)

    def _run_install(self, progress=None):
        assert self._installer is not None
        return self._installer.install_forge(
            self.mc_version,
            self._loader_version,
            java_path=self._java_path,
            kind=self.kind,
            progress=progress,
            on_output=self.output_line.emit,
        )

    def _on_progress(self, progress: StageProgress) -> None:
        percent = float(progress.percent)
        if percent < PREPARE_BOUNDARY:
            if self._step != 1:
                self._goto(1)
            self._prepare_ring.set_value(min(1.0, percent / PREPARE_BOUNDARY))
            self._prepare_stage.setText(self._stage_text(progress.stage))
            self._prepare_message.setText(progress.message)
            self._prepare_detail.setText(self._detail_text(progress))
            return

        if self._step != 2:
            self._goto(2)
            self._install_ring.set_indeterminate(False)
        inner = (percent - PREPARE_BOUNDARY) / max(0.001, FINISH_BOUNDARY - PREPARE_BOUNDARY)
        self._install_ring.set_value(max(0.0, min(1.0, inner)))
        self._install_stage.setText(self._stage_text(progress.stage) or "安装加载器")
        self._install_message.setText(progress.message or "安装器运行中…")
        self._install_detail.setText(self._detail_text(progress))

    def _update_clock(self) -> None:
        if not self._started_at:
            return
        elapsed = int(time.monotonic() - self._started_at)
        minutes, seconds = divmod(elapsed, 60)
        self._elapsed_label.setText(f"已运行 {minutes:02d}:{seconds:02d}")

    # ------------------------------------------------------------------ #
    # 日志（后台线程 → 信号 → 主线程批量刷新，避免逐行重绘卡 UI）
    # ------------------------------------------------------------------ #
    def _queue_line(self, text: str) -> None:
        if not text:
            return
        self._pending_lines.append(text)
        if len(self._pending_lines) > 4000:
            del self._pending_lines[:2000]

    def _flush_lines(self) -> None:
        if not self._pending_lines:
            return
        block = "\n".join(self._pending_lines)
        self._pending_lines.clear()
        self._log_view.appendPlainText(block)
        bar = self._log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    # ------------------------------------------------------------------ #
    # 结果
    # ------------------------------------------------------------------ #
    def _on_done(self, result: InstallResult) -> None:
        self._result = result
        self._failed = False
        self._clock.stop()
        self._goto(3)
        self._done_icon.set_kind("check")
        self._done_icon.set_color(self._palette.success)
        self._done_title.setText("安装完成")
        instance = self._selected_instance()
        summary = result.summary()
        detail = f"{LOADER_LABELS.get(self.kind, 'Forge')} {self._loader_version} 已安装到「{instance.name}」\n版本目录：{result.version_id}"
        if summary:
            detail += f"\n{summary}"
        if result.warnings:
            detail += f"\n注意：{result.warnings[0]}"
        self._done_detail.setText(detail)
        self._retry_button.setVisible(False)
        self._open_folder_button.setVisible(True)
        self._launch_button.setVisible(True)
        self.ctx.instances_changed.emit()
        if instance is not None:
            self.ctx.select_instance(instance)

    def _on_failed(self, message: str, hint: str) -> None:
        self._failed = True
        self._clock.stop()
        self._flush_lines()
        self._goto(3)
        self._done_icon.set_kind("warning")
        self._done_icon.set_color(self._palette.danger)
        self._done_title.setText("安装失败")
        self._done_detail.setText(f"{message}\n{hint}\n\n上方日志里有安装器的完整输出，可以据此排查（常见原因：Java 版本不符、网络超时）。")
        self._retry_button.setVisible(True)
        self._open_folder_button.setVisible(False)
        self._launch_button.setVisible(False)

    def _on_cancelled(self) -> None:
        self._failed = True
        self._clock.stop()
        self._goto(3)
        self._done_icon.set_kind("info")
        self._done_icon.set_color(self._palette.warning)
        self._done_title.setText("已取消")
        self._done_detail.setText("安装已中止；已下载的文件会保留，下次可以继续。")
        self._retry_button.setVisible(True)
        self._open_folder_button.setVisible(False)
        self._launch_button.setVisible(False)

    # ------------------------------------------------------------------ #
    def _retry(self) -> None:
        self._failed = False
        self._result = None
        self._goto(0)
        self._refresh_checks()

    def _open_folder(self) -> None:
        instance = self._selected_instance()
        if instance is not None:
            self.ctx.open_instance_folder(instance)

    def _launch(self) -> None:
        instance = self._selected_instance()
        if instance is not None:
            self.ctx.request_launch.emit(instance)
            self.accept()

    def _on_cancel(self) -> None:
        if self._step in (1, 2) and not self._failed:
            self._request_cancel()
            return
        self.reject()

    def _request_cancel(self) -> None:
        if self._installer is not None:
            self._installer.cancel()
        if self._worker is not None:
            self._worker.cancel()
        self._cancel_button.setEnabled(False)
        self._install_message.setText("正在取消，请稍候…")

    # ------------------------------------------------------------------ #
    def reject(self) -> None:
        if self._step in (1, 2) and self._worker is not None and self._worker.isRunning():
            self._request_cancel()
            self._worker.wait(6000)
        self._clock.stop()
        self._flush_timer.stop()
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._step in (1, 2) and self._worker is not None and self._worker.isRunning():
            self._request_cancel()
            self._worker.wait(6000)
        self._clock.stop()
        super().closeEvent(event)
