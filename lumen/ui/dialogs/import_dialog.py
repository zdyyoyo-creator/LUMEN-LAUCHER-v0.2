"""整合包导入对话框：解析预览 → 选择版本 → 进度 → 结果。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QProgressBar,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...modpack.importer import ImportProgress, ImportReport, ModpackPlan, import_modpack
from ...utils.errors import OperationCancelled
from ...utils.fsutil import human_size
from ...utils.versions import required_java_major
from ..context import AppContext
from ..dialogs.common import BaseDialog, MessageDialog
from ..widgets import Badge, Card, GlyphWidget, RingProgress, label, make_button
from ..workers import TaskManager, Worker

STAGE_LABELS = {
    "prepare": "准备导入",
    "game": "安装游戏与加载器",
    "overrides": "解压整合包文件",
    "files": "下载整合包资源",
    "done": "完成",
}


class ImportDialog(BaseDialog):
    def __init__(
        self,
        parent: QWidget | None,
        *,
        ctx: AppContext,
        tasks: TaskManager,
        plan: ModpackPlan,
    ) -> None:
        super().__init__(parent, title="导入整合包", width=620)
        self.ctx = ctx
        self.tasks = tasks
        self.plan = plan
        self.report: ImportReport | None = None
        self._worker: Worker | None = None
        self._finished = False

        self._stack = QStackedWidget()
        self.body().addWidget(self._stack, 1)
        self._build_preview()
        self._build_progress()
        self._build_result()
        self._stack.addWidget(self._preview_page)
        self._stack.addWidget(self._progress_page)
        self._stack.addWidget(self._result_page)
        self._stack.setCurrentWidget(self._preview_page)

        self._cancel_button = self.add_footer_button(make_button("取消", handler=self.reject))
        self._start_button = self.add_footer_button(
            make_button("开始导入", kind="download", role="primary", handler=self._begin)
        )
        self._start_button.setDefault(True)
        self.finish()

    # ------------------------------------------------------------------ #
    # 预览
    # ------------------------------------------------------------------ #
    def _build_preview(self) -> None:
        self._preview_page = QWidget()
        layout = QVBoxLayout(self._preview_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(GlyphWidget("modpack", 22, color=self._palette.primary, width=1.6))
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        title_box.addWidget(label(self.plan.name, "subtitle"))
        source_text = {"mrpack": "Modrinth 整合包", "curseforge": "CurseForge 整合包", "zip": "通用压缩包"}.get(
            self.plan.source, self.plan.source
        )
        title_box.addWidget(label(f"{source_text} · {self.plan.archive.name}", "muted"))
        head.addLayout(title_box, 1)
        layout.addLayout(head)

        facts = Card(margins=(14, 12, 14, 12), spacing=6, flat=True)
        for key, value in self.plan.summary().items():
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(label(key, "muted"))
            row.addStretch(1)
            row.addWidget(label(value, "body"))
            facts.body().addLayout(row)
        layout.addWidget(facts)

        version_row = QHBoxLayout()
        version_row.setSpacing(10)
        version_row.addWidget(label("Minecraft 版本", "body"))
        version_row.addStretch(1)
        self._version = QComboBox()
        self._version.setEditable(True)
        self._version.setMinimumWidth(220)
        self._fill_versions(self.plan.mc_version)
        version_row.addWidget(self._version)
        layout.addLayout(version_row)

        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        name_row.addWidget(label("实例名称", "body"))
        name_row.addStretch(1)
        self._name = QLineEdit(self.plan.name)
        self._name.setMinimumWidth(300)
        name_row.addWidget(self._name)
        layout.addLayout(name_row)

        if self.plan.loader_type:
            loader_text = f"将安装 {self.plan.display_loader}"
        else:
            loader_text = "原版（整合包未指定加载器）"
        layout.addWidget(label(loader_text, "muted"))

        for note in self.plan.notes:
            tip = Card(margins=(12, 10, 12, 10), spacing=4, flat=True)
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(GlyphWidget("warning", 16, color=self._palette.warning, width=1.7))
            row.addWidget(label(note, "muted", wrap=True), 1)
            tip.body().addLayout(row)
            layout.addWidget(tip)

        if self.plan.manual_required:
            manual = Card(margins=(12, 10, 12, 10), spacing=6, flat=True)
            head_row = QHBoxLayout()
            head_row.addWidget(GlyphWidget("warning", 16, color=self._palette.danger, width=1.7))
            head_row.addWidget(label(f"{len(self.plan.manual_required)} 个文件需要手动下载", "body"), 1)
            key_button = make_button("填写 API Key", handler=self._open_settings)
            key_button.setProperty("role", "ghost")
            head_row.addWidget(key_button)
            manual.body().addLayout(head_row)
            manual.body().addWidget(
                label(
                    "CurseForge 官方接口需要 API Key。填写后可自动换出下载直链；否则导入后会生成 MANUAL_DOWNLOADS.txt 供你手动补齐。",
                    "muted",
                    wrap=True,
                )
            )
            layout.addWidget(manual)

        layout.addStretch(1)

    @property
    def _palette(self):
        from .. import theme

        return theme.active()

    def _fill_versions(self, current: str) -> None:
        manifest = self.ctx.manifest
        options: list[str] = []
        if manifest is not None:
            options = [version.id for version in manifest.filtered()[:80]]
        if current and current not in options:
            options.insert(0, current)
        if not options:
            options = [current] if current else ["1.21.4"]
        self._version.addItems(options)
        if current:
            self._version.setCurrentText(current)

    def _open_settings(self) -> None:
        self.ctx.toast.emit("请在「设置 → 整合包」里填写 CurseForge API Key，然后重新导入")
        self.reject()

    # ------------------------------------------------------------------ #
    # 进度
    # ------------------------------------------------------------------ #
    def _build_progress(self) -> None:
        self._progress_page = QWidget()
        layout = QVBoxLayout(self._progress_page)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(14)

        top = QHBoxLayout()
        top.setSpacing(20)
        self._ring = RingProgress(96, 7)
        top.addWidget(self._ring, 0, Qt.AlignTop)
        text_box = QVBoxLayout()
        text_box.setSpacing(6)
        self._stage = label("准备导入", "subtitle")
        self._message = label("正在准备…", "body", wrap=True)
        self._detail = label("", "muted")
        text_box.addWidget(self._stage)
        text_box.addWidget(self._message)
        text_box.addWidget(self._detail)
        text_box.addStretch(1)
        top.addLayout(text_box, 1)
        layout.addLayout(top)

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(False)
        self._bar.setFixedHeight(8)
        layout.addWidget(self._bar)
        layout.addStretch(1)

        controls = QHBoxLayout()
        controls.addStretch(1)
        self._cancel_import = make_button("取消导入", role="danger", handler=self._request_cancel)
        controls.addWidget(self._cancel_import)
        layout.addLayout(controls)

    # ------------------------------------------------------------------ #
    # 结果
    # ------------------------------------------------------------------ #
    def _build_result(self) -> None:
        self._result_page = QWidget()
        layout = QVBoxLayout(self._result_page)
        layout.setContentsMargins(0, 20, 0, 0)
        layout.setSpacing(10)
        self._result_icon = GlyphWidget("check", 40, color=self._palette.success, width=1.4)
        layout.addWidget(self._result_icon, 0, Qt.AlignHCenter)
        self._result_title = label("导入完成", "subtitle")
        self._result_title.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._result_title)
        self._result_detail = label("", "muted", wrap=True)
        self._result_detail.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._result_detail)
        layout.addStretch(1)
        actions = QHBoxLayout()
        actions.addStretch(1)
        self._open_folder = make_button("打开实例文件夹", kind="folder", handler=self._open_result_folder)
        actions.addWidget(self._open_folder)
        self._launch_button = make_button("启动游戏", kind="play", role="primary", handler=self._launch)
        actions.addWidget(self._launch_button)
        layout.addLayout(actions)

    # ------------------------------------------------------------------ #
    def _begin(self) -> None:
        mc_version = self._version.currentText().strip()
        if not mc_version:
            MessageDialog.tell(self, "缺少版本", "请选择或填写 Minecraft 版本。", level="warning")
            return
        instance_name = self._name.text().strip() or self.plan.name
        java_path = ""
        if self.plan.loader_type in ("forge", "neoforge"):
            runtime = self.ctx.best_java(required_java_major(mc_version))
            if runtime is None:
                MessageDialog.tell(
                    self,
                    "需要 Java",
                    "该整合包使用 Forge/NeoForge，安装需要本机 Java。请先在设置里准备 Java 17 或 21。",
                    level="warning",
                )
                return
            java_path = str(runtime.path)

        self._stack.setCurrentWidget(self._progress_page)
        self._start_button.setVisible(False)
        self._cancel_button.setText("隐藏")

        self._worker = Worker(self._run_import, with_progress=True, with_cancel=True, label="导入整合包")
        self._worker.progressed.connect(self._on_progress)
        self._worker.succeeded.connect(self._on_ok)
        self._worker.failed.connect(self._on_failed)
        self._worker.cancelled.connect(self._on_cancelled)
        self.tasks.start(self._worker)

    def _run_import(self, progress=None, cancel_check=None):
        return import_modpack(
            self.ctx.selector,
            self.ctx.paths,
            self.ctx.store,
            self.plan,
            mc_version=self._version.currentText().strip(),
            loader_type=self.plan.loader_type,
            loader_version=self.plan.loader_version,
            java_path=self.ctx.settings.java_path if not self.plan.loader_type else self._java_hint(),
            instance_name=self._name.text().strip() or None,
            progress=progress,
            cancel_check=cancel_check,
        )

    def _java_hint(self) -> str:
        from ...utils.versions import required_java_major

        required = required_java_major(self._version.currentText().strip())
        runtime = self.ctx.best_java(required)
        return str(runtime.path) if runtime else ""

    # ------------------------------------------------------------------ #
    def _on_progress(self, stage: ImportProgress) -> None:
        self._ring.set_value(stage.percent)
        self._bar.setValue(int(stage.percent * 1000))
        self._stage.setText(STAGE_LABELS.get(stage.stage, stage.stage))
        self._message.setText(stage.message)
        self._detail.setText(stage.detail)

    def _on_ok(self, report: ImportReport) -> None:
        self.report = report
        self._finished = True
        self._stack.setCurrentWidget(self._result_page)
        self._cancel_button.setText("关闭")
        self.ctx.instances_changed.emit()
        self.ctx.select_instance(report.instance)
        detail = f"已创建实例「{report.instance.name}」"
        if report.files_downloaded or report.files_skipped:
            detail += f"，下载 {report.files_downloaded} 个文件"
        if report.files_failed:
            detail += f"，{report.files_failed} 个失败（可在日志中查看）"
        if report.manual_required:
            detail += f"\n还有 {len(report.manual_required)} 个文件需要手动下载（见 MANUAL_DOWNLOADS.txt）"
        self._result_detail.setText(detail)

    def _on_failed(self, message: str, hint: str) -> None:
        self._finished = True
        self._stack.setCurrentWidget(self._result_page)
        self._cancel_button.setText("关闭")
        self._result_icon.set_kind("warning")
        self._result_title.setText("导入失败")
        self._result_detail.setText(f"{message}\n{hint}".strip())
        self._open_folder.setVisible(False)
        self._launch_button.setVisible(False)

    def _on_cancelled(self) -> None:
        self._on_failed("已取消导入", "未完成的文件会保留，可重新导入。")

    def _request_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self._cancel_import.setEnabled(False)
        self._message.setText("正在取消…")

    # ------------------------------------------------------------------ #
    def _open_result_folder(self) -> None:
        if self.report is not None:
            self.ctx.open_instance_folder(self.report.instance)

    def _launch(self) -> None:
        if self.report is not None:
            self.ctx.request_launch.emit(self.report.instance)
            self.accept()

    def _open_text_hint(self) -> None:
        self.ctx.toast.emit("请在「设置 → 整合包」里填写 CurseForge API Key 后重新导入")
        self.reject()

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
