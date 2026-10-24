"""安装对话框：目标实例 / 加载器选择 → 进度（环形 + 速度 + 暂停取消）→ 结果。

阶段完全由 ``VersionInstaller`` 的 StageProgress 驱动，
UI 只把「阶段 + 百分比 + 文件数 + 速度」翻译成文案。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QProgressBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.installer import STAGE_MESSAGES, InstallResult, StageProgress, VersionInstaller
from ...core.loaders import LoaderVersion, available_loader_versions, latest_loader
from ...utils.errors import LumenError
from ...utils.fsutil import human_size, human_speed
from ...utils.versions import loader_label
from ..context import AppContext
from ..dialogs.common import BaseDialog, MessageDialog, TextInputDialog
from ..widgets import Card, GlyphWidget, RingProgress, Segmented, label, make_button
from ..workers import TaskManager, Worker

LOADER_OPTIONS = [
    ("vanilla", "原版"),
    ("fabric", "Fabric"),
    ("quilt", "Quilt"),
    ("forge", "Forge"),
    ("neoforge", "NeoForge"),
]


class InstallDialog(BaseDialog):
    def __init__(
        self,
        parent: QWidget | None,
        *,
        ctx: AppContext,
        tasks: TaskManager,
        version_id: str,
        instance=None,
        preset_kind: str = "vanilla",
    ) -> None:
        super().__init__(parent, title=f"安装 {version_id}", width=580)
        self.ctx = ctx
        self.tasks = tasks
        self.version_id = version_id
        self._instance = instance
        self._installer: VersionInstaller | None = None
        self._worker: Worker | None = None
        self._result: InstallResult | None = None
        self._loader_versions: list[LoaderVersion] = []
        self._paused = False
        self._finished = False
        self._preset_kind = preset_kind if preset_kind in ("vanilla", "fabric", "quilt", "forge", "neoforge") else "vanilla"

        self._stack = QStackedWidget()
        self.body().addWidget(self._stack, 1)
        self._build_form()
        self._build_progress()
        self._build_result()
        self._stack.addWidget(self._form_page)
        self._stack.addWidget(self._progress_page)
        self._stack.addWidget(self._result_page)
        self._stack.setCurrentWidget(self._form_page)

        self._cancel_button = self.add_footer_button(make_button("取消", handler=self.reject))
        self._start_button = self.add_footer_button(
            make_button("开始安装", kind="download", role="primary", handler=self._begin)
        )
        self._start_button.setDefault(True)
        self.finish()
        self._loader.set_current(self._preset_kind)
        self._sync_loader_controls(self._preset_kind)
        if self._preset_kind != "vanilla":
            # 预设了加载器：自动把版本列表拉起来，省一次点击
            self._on_loader_changed(self._preset_kind)

    # ------------------------------------------------------------------ #
    # 表单
    # ------------------------------------------------------------------ #
    def _build_form(self) -> None:
        self._form_page = QWidget()
        layout = QVBoxLayout(self._form_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        info = Card(margins=(14, 12, 14, 12), spacing=6, flat=True)
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(GlyphWidget("versions", 20, color=self._palette.primary, width=1.6))
        head.addWidget(label(self.version_id, "subtitle"), 1)
        version = self.ctx.manifest.get(self.version_id) if self.ctx.manifest else None
        if version is not None:
            head.addWidget(label(version.date, "muted"))
        info.body().addLayout(head)
        info.body().addWidget(
            label(
                "安装会下载客户端、依赖库与资源文件，首次通常 300–600MB；已校验通过的文件会自动跳过。",
                "muted",
                wrap=True,
            )
        )
        layout.addWidget(info)

        instance_row = QHBoxLayout()
        instance_row.setSpacing(10)
        instance_row.addWidget(label("目标实例", "body"))
        instance_row.addStretch(1)
        self._instance_combo = QComboBox()
        self._instance_combo.setMinimumWidth(280)
        self._instance_combo.currentIndexChanged.connect(self._on_instance_changed)
        self._fill_instances()
        instance_row.addWidget(self._instance_combo)
        layout.addLayout(instance_row)

        loader_row = QHBoxLayout()
        loader_row.setSpacing(10)
        loader_row.addWidget(label("模组加载器", "body"))
        loader_row.addStretch(1)
        self._loader = Segmented(LOADER_OPTIONS, current="vanilla")
        self._loader.changed.connect(self._on_loader_changed)
        loader_row.addWidget(self._loader)
        layout.addLayout(loader_row)

        version_row = QHBoxLayout()
        version_row.setSpacing(10)
        self._loader_version_label = label("加载器版本", "body")
        version_row.addWidget(self._loader_version_label)
        version_row.addStretch(1)
        self._loader_combo = QComboBox()
        self._loader_combo.setMinimumWidth(280)
        version_row.addWidget(self._loader_combo)
        layout.addLayout(version_row)

        self._hint = label("", "muted", wrap=True)
        layout.addWidget(self._hint)
        layout.addStretch(1)

    @property
    def _palette(self):
        from .. import theme

        return theme.active()

    def _fill_instances(self) -> None:
        combo = self._instance_combo
        combo.blockSignals(True)
        combo.clear()
        instances = self.ctx.instances()
        for instance in instances:
            combo.addItem(f"{instance.name}  ·  {instance.mc_version}", instance.id)
        combo.addItem("＋ 新建实例…", "__new__")
        if self._instance is not None:
            index = combo.findData(self._instance.id)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)

    def _on_instance_changed(self) -> None:
        if self._instance_combo.currentData() == "__new__":
            name = TextInputDialog.ask(
                self,
                title="新建实例",
                label_text="实例名称",
                value=f"{self.version_id} {loader_label(None, None)}",
                description="每个实例有独立的 mods / saves / 配置目录。",
            )
            if not name:
                index = self._instance_combo.findData(self._instance.id) if self._instance else 0
                self._instance_combo.setCurrentIndex(max(0, index))
                return
            try:
                instance = self.ctx.store.create(name, self.version_id)
            except LumenError as exc:
                MessageDialog.tell(self, "创建失败", str(exc), level="error")
                return
            self._instance = instance
            self.ctx.select_instance(instance)
            self.ctx.instances_changed.emit()
            self._fill_instances()

    def _selected_instance(self):
        data = self._instance_combo.currentData()
        if data and data != "__new__":
            return self.ctx.store.get(str(data))
        return self._instance

    def _on_loader_changed(self, kind: str) -> None:
        self._sync_loader_controls(kind)
        if kind == "vanilla":
            return
        self._loader_combo.clear()
        self._loader_combo.addItem("正在获取版本列表…", "")
        self._loader_combo.setEnabled(False)
        worker = Worker(
            lambda **_: available_loader_versions(self.ctx.selector, kind, self.version_id),
            label=f"{kind} 版本列表",
        )
        worker.succeeded.connect(lambda versions, k=kind: self._on_loader_list(k, versions))
        worker.failed.connect(lambda message, hint, k=kind: self._on_loader_list_failed(k, message, hint))
        self.tasks.start(worker)

    def _sync_loader_controls(self, kind: str) -> None:
        visible = kind != "vanilla"
        self._loader_version_label.setVisible(visible)
        self._loader_combo.setVisible(visible)
        if kind in ("forge", "neoforge"):
            self._hint.setText("Forge/NeoForge 需要本机已有 Java 才能运行官方安装器；安装耗时约 2–5 分钟。")
        elif kind in ("fabric", "quilt"):
            self._hint.setText("Fabric/Quilt 通过 meta API 直接生成版本，无需运行安装器，速度快。")
        else:
            self._hint.setText("原版安装完成后即可启动；建议先确认设置里的下载源是否为「自动」。")

    def _on_loader_list(self, kind: str, versions: list[LoaderVersion]) -> None:
        self._loader_versions = versions
        self._loader_combo.clear()
        if not versions:
            self._loader_combo.addItem("没有可用版本", "")
            self._loader_combo.setEnabled(False)
            return
        for item in versions[:40]:
            self._loader_combo.addItem(item.label, item.version)
        self._loader_combo.setEnabled(True)
        latest = latest_loader(kind, versions)
        if latest:
            index = self._loader_combo.findData(latest)
            if index >= 0:
                self._loader_combo.setCurrentIndex(index)

    def _on_loader_list_failed(self, kind: str, message: str, hint: str) -> None:
        self._loader_combo.clear()
        self._loader_combo.addItem("获取失败", "")
        self._hint.setText(f"{message} {hint}".strip())

    # ------------------------------------------------------------------ #
    # 进度
    # ------------------------------------------------------------------ #
    def _build_progress(self) -> None:
        self._progress_page = QWidget()
        layout = QVBoxLayout(self._progress_page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(16)

        top = QHBoxLayout()
        top.setSpacing(20)
        self._ring = RingProgress(96, 7)
        top.addWidget(self._ring, 0, Qt.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(6)
        self._stage_label = label("准备中", "subtitle")
        self._message_label = label("正在准备安装…", "body", wrap=True)
        self._detail_label = label("", "muted")
        text_box.addWidget(self._stage_label)
        text_box.addWidget(self._message_label)
        text_box.addWidget(self._detail_label)
        text_box.addStretch(1)
        top.addLayout(text_box, 1)
        layout.addLayout(top)

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setValue(0)
        self._bar.setFixedHeight(8)
        self._bar.setTextVisible(False)
        layout.addWidget(self._bar)
        layout.addStretch(1)

        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addStretch(1)
        self._pause_button = make_button("暂停", kind="pause", handler=self._toggle_pause)
        self._pause_button.setEnabled(False)
        self._progress_cancel = make_button("取消安装", role="danger", handler=self._request_cancel)
        controls.addWidget(self._pause_button)
        controls.addWidget(self._progress_cancel)
        layout.addLayout(controls)

    def _show_progress(self) -> None:
        self._stack.setCurrentWidget(self._progress_page)
        self._start_button.setVisible(False)
        self._cancel_button.setText("隐藏")
        self._pause_button.setEnabled(True)
        self._ring.set_indeterminate(False)
        self._ring.set_value(0.0)

    # ------------------------------------------------------------------ #
    # 结果
    # ------------------------------------------------------------------ #
    def _build_result(self) -> None:
        self._result_page = QWidget()
        layout = QVBoxLayout(self._result_page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(12)
        self._result_icon = GlyphWidget("check", 40, color=self._palette.success, width=1.4)
        layout.addWidget(self._result_icon, 0, Qt.AlignHCenter)
        self._result_title = label("安装完成", "subtitle")
        self._result_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._result_title)
        self._result_detail = label("", "muted", wrap=True)
        self._result_detail.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._result_detail)
        layout.addStretch(1)
        self._result_actions = QHBoxLayout()
        self._result_actions.setSpacing(8)
        self._result_actions.addStretch(1)
        self._open_folder_button = make_button("打开实例文件夹", kind="folder", handler=self._open_folder)
        self._open_folder_button.setVisible(False)
        self._result_actions.addWidget(self._open_folder_button)
        layout.addLayout(self._result_actions)

    def _show_result(self, ok: bool, message: str = "", hint: str = "") -> None:
        self._finished = True
        self._stack.setCurrentWidget(self._result_page)
        self._start_button.setVisible(False)
        self._cancel_button.setText("关闭")
        self._progress_cancel.setEnabled(False)
        self._pause_button.setEnabled(False)
        if ok:
            self._result_icon.set_kind("check")
            self._result_title.setText("安装完成")
            detail = message or "现在可以把该实例设为当前实例并启动游戏。"
            self._result_detail.setText(detail)
            self._open_folder_button.setVisible(True)
            self.ctx.instances_changed.emit()
        else:
            self._result_icon.set_kind("warning")
            self._result_title.setText("安装失败")
            self._result_detail.setText(f"{message}\n{hint}".strip())
            self._open_folder_button.setVisible(False)

    def _open_folder(self) -> None:
        instance = self._selected_instance()
        if instance is not None:
            self.ctx.open_instance_folder(instance)

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def _begin(self) -> None:
        instance = self._selected_instance()
        if instance is None:
            MessageDialog.tell(self, "没有目标实例", "请先选择一个实例，或新建一个。", level="warning")
            return
        kind = self._loader.current()
        if kind in ("forge", "neoforge"):
            # Forge/NeoForge 需要运行官方安装器，交给专用向导（分步 + 实时日志）
            self.accept()
            QTimer.singleShot(0, lambda: self._open_forge_wizard(instance, kind))
            return
        loader_version = ""
        if kind != "vanilla":
            data = self._loader_combo.currentData()
            if not data:
                MessageDialog.tell(self, "缺少加载器版本", "请等待版本列表加载完成，或换一个加载器。", level="warning")
                return
            loader_version = str(data)
        if kind in ("forge", "neoforge") and self._forge_java() is None:
            MessageDialog.tell(
                self,
                "需要 Java",
                "安装 Forge/NeoForge 需要本机 Java 来运行官方安装器。请先在「设置 → Java」里指定或自动获取。",
                level="warning",
            )
            return

        self._installer = VersionInstaller(self.ctx.selector, self.ctx.paths, self.ctx.store, instance)
        self._show_progress()
        self._worker = Worker(self._run_install, with_progress=True, label=f"安装 {self.version_id}")
        self._worker.progressed.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_install_ok)
        self._worker.failed.connect(self._on_install_failed)
        self._worker.cancelled.connect(self._on_install_cancelled)
        self.tasks.start(self._worker)

    def _open_forge_wizard(self, instance, kind: str) -> None:
        """关闭当前对话框后打开 Forge/NeoForge 安装向导。"""
        from .forge_wizard import ForgeWizardDialog

        dialog = ForgeWizardDialog(
            self.parentWidget(),
            ctx=self.ctx,
            tasks=self.tasks,
            kind=kind,
            mc_version=self.version_id,
            instance=instance,
        )
        dialog.exec()
        self.ctx.instances_changed.emit()

    def _run_install(self, progress=None):
        assert self._installer is not None
        kind = self._loader.current()
        loader_version = str(self._loader_combo.currentData() or "")
        if kind == "vanilla":
            return self._installer.install(self.version_id, progress=progress)
        if kind in ("fabric", "quilt"):
            return self._installer.install_fabric(
                self.version_id,
                loader_version,
                kind=kind,
                progress=progress,
            )
        return self._installer.install_forge(
            self.version_id,
            loader_version,
            java_path=str(self._forge_java()),
            kind=kind,
            progress=progress,
        )

    def _forge_java(self) -> str | None:
        from ...utils.versions import required_java_major

        required = required_java_major(self.version_id)
        runtime = self.ctx.best_java(required)
        if runtime is not None:
            return str(runtime.path)
        if self.ctx.settings.java_path:
            return self.ctx.settings.java_path
        if self.ctx.runtimes:
            return str(self.ctx.runtimes[0].path)
        return None

    # ------------------------------------------------------------------ #
    def _on_progress(self, stage: StageProgress) -> None:
        self._ring.set_value(stage.percent)
        self._bar.setValue(int(stage.percent * 1000))
        stage_name = STAGE_MESSAGES.get(stage.stage, stage.stage)
        self._stage_label.setText(stage_name)
        self._message_label.setText(stage.message)
        chunks: list[str] = []
        if stage.files_total:
            chunks.append(f"{stage.files_done}/{stage.files_total} 个文件")
        if stage.bytes_total:
            chunks.append(f"{human_size(stage.bytes_done)} / {human_size(stage.bytes_total)}")
        elif stage.bytes_done:
            chunks.append(human_size(stage.bytes_done))
        if stage.speed:
            chunks.append(human_speed(stage.speed))
        if stage.paused:
            chunks.append("已暂停")
        self._detail_label.setText(" · ".join(chunks))

    def _on_install_ok(self, result: InstallResult) -> None:
        self._result = result
        detail = result.summary()
        if result.warnings:
            detail += f"\n注意：{result.warnings[0]}"
        self._show_result(True, detail)

    def _on_install_failed(self, message: str, hint: str) -> None:
        self._result = None
        self._show_result(False, message, hint)

    def _on_install_cancelled(self) -> None:
        self._show_result(False, "安装已取消", "已下载的文件会保留，下次安装可断点续传。")

    def _toggle_pause(self) -> None:
        if self._installer is None:
            return
        if self._paused:
            self._installer.resume()
            self._paused = False
            self._pause_button.setText("暂停")
        else:
            self._installer.pause()
            self._paused = True
            self._pause_button.setText("继续")

    def _request_cancel(self) -> None:
        if self._installer is not None:
            self._installer.cancel()
        if self._worker is not None:
            self._worker.cancel()
        self._progress_cancel.setEnabled(False)
        self._message_label.setText("正在取消…")

    # ------------------------------------------------------------------ #
    def reject(self) -> None:
        if not self._finished and self._worker is not None and self._worker.isRunning():
            self._request_cancel()
            self._worker.wait(4000)
        super().reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._finished and self._worker is not None and self._worker.isRunning():
            self._request_cancel()
            self._worker.wait(4000)
        super().closeEvent(event)
