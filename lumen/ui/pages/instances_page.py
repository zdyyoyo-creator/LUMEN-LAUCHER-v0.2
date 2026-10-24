"""实例页：卡片网格展示多实例，支持新建 / 复制 / 重命名 / 删除 / 打开目录。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QPainter
from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QMenu, QVBoxLayout, QWidget

from ...core.instances import Instance
from ...utils.errors import LumenError
from ..context import AppContext
from ..dialogs.common import ConfirmDeleteDialog, MessageDialog, TextInputDialog
from ..dialogs.instance_dialog import InstanceCreateDialog, InstanceEditDialog
from ..widgets import (
    Badge,
    Card,
    EmptyState,
    GlyphWidget,
    label,
    make_button,
    make_tool_button,
)
from ..workers import TaskManager
from .base import Page


class AccentBar(QWidget):
    """卡片左侧的实例色条（自绘圆角）。"""

    def __init__(self, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = QColor(color)
        self.setFixedWidth(6)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(self._color)
        painter.drawRoundedRect(self.rect().adjusted(0, 2, 0, -2), 3, 3)
        painter.end()


class InstancesPage(Page):
    def __init__(self, ctx: AppContext, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(ctx, tasks, title="实例", subtitle="每个实例拥有独立的游戏目录，互不干扰", parent=parent)
        self.add_header_action(make_button("新建实例", kind="plus", role="primary", handler=self._create))
        self.add_header_action(make_tool_button("refresh", "刷新实例列表", self.refresh))
        self.ctx.instances_changed.connect(self._schedule_refresh)
        self._pending = False
        self.refresh()

    # ------------------------------------------------------------------ #
    def _schedule_refresh(self) -> None:
        """避免信号在同一轮事件里触发多次重建。"""
        if self._pending:
            return
        self._pending = True
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, self._do_refresh)

    def _do_refresh(self) -> None:
        self._pending = False
        self.refresh()

    def on_show(self) -> None:
        self.refresh()

    def refresh(self) -> None:
        self.clear_content()
        instances = self.ctx.instances()
        self.set_subtitle(
            f"共 {len(instances)} 个实例" if instances else "还没有实例，点击右上角「新建实例」开始"
        )
        if not instances:
            empty = EmptyState(
                "instances",
                "还没有实例",
                "一个实例 = 一套独立的版本、mods 和存档。\n先创建一个实例，再到「版本管理」里安装对应的游戏版本。",
                "新建实例",
            )
            empty.action.connect(self._create)
            self.content_layout.addWidget(empty)
            self.content_layout.addStretch(1)
            return

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(12)
        for index, instance in enumerate(instances):
            grid.addWidget(self._instance_card(instance), index // 2, index % 2)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        self.content_layout.addWidget(grid_host)
        self.content_layout.addStretch(1)

    # ------------------------------------------------------------------ #
    def _instance_card(self, instance: Instance) -> QWidget:
        card = Card(margins=(0, 0, 0, 0), spacing=0)
        row = QHBoxLayout()
        row.setContentsMargins(10, 12, 14, 12)
        row.setSpacing(12)
        row.addWidget(AccentBar(instance.color))

        content = QVBoxLayout()
        content.setSpacing(6)

        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(label(instance.name, "version"))
        current = self.ctx.current_instance()
        if current is not None and current.id == instance.id:
            title_row.addWidget(Badge("当前", "primary"))
        if instance.modpack:
            title_row.addWidget(Badge("整合包", "muted"))
        title_row.addStretch(1)
        content.addLayout(title_row)

        meta = f"{instance.mc_version} · {instance.display_loader} · {instance.last_played_text}"
        content.addWidget(label(meta, "muted"))

        stats = self.ctx.store.folder_stats(instance)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        chips.addWidget(label(f"mods {stats['mods'][0]}", "muted"))
        chips.addWidget(label(f"存档 {stats['saves'][0]}", "muted"))
        chips.addWidget(label(f"资源包 {stats['resourcepacks'][0]}", "muted"))
        chips.addStretch(1)
        content.addLayout(chips)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        launch = make_button("启动", kind="play", role="primary", handler=lambda: self._launch(instance))
        actions.addWidget(launch)
        folder = make_button("文件夹", kind="folder", handler=lambda: self.ctx.open_instance_folder(instance))
        actions.addWidget(folder)
        mods = make_button("mods", kind="mods", handler=lambda: self._open_mods(instance))
        actions.addWidget(mods)
        more = make_tool_button("chevron", "更多操作", handler=lambda: self._show_menu(more, instance))
        actions.addWidget(more)
        actions.addStretch(1)
        content.addLayout(actions)

        row.addLayout(content, 1)
        card.body().addLayout(row)
        return card

    # ------------------------------------------------------------------ #
    def _launch(self, instance: Instance) -> None:
        self.ctx.select_instance(instance)
        self.ctx.request_launch.emit(instance)

    def _open_mods(self, instance: Instance) -> None:
        from ..dialogs.mods_dialog import ModsDialog

        ModsDialog(self, ctx=self.ctx, instance=instance).exec()

    def _open_logs(self, instance: Instance) -> None:
        from ..dialogs.log_dialog import LogDialog

        LogDialog(self, ctx=self.ctx, instance=instance).exec()

    def _show_menu(self, anchor: QWidget, instance: Instance) -> None:
        menu = QMenu(self)
        launch = QAction("启动游戏", menu)
        launch.triggered.connect(lambda: self._launch(instance))
        menu.addAction(launch)
        edit = QAction("编辑设置（内存 / Java）", menu)
        edit.triggered.connect(lambda: self._edit(instance))
        menu.addAction(edit)
        logs = QAction("查看日志与崩溃分析", menu)
        logs.triggered.connect(lambda: self._open_logs(instance))
        menu.addAction(logs)
        menu.addSeparator()
        clone = QAction("复制实例", menu)
        clone.triggered.connect(lambda: self._clone(instance))
        menu.addAction(clone)
        rename = QAction("重命名", menu)
        rename.triggered.connect(lambda: self._rename(instance))
        menu.addAction(rename)
        menu.addSeparator()
        delete = QAction("删除实例…", menu)
        delete.triggered.connect(lambda: self._delete(instance))
        menu.addAction(delete)
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _create(self) -> None:
        dialog = InstanceCreateDialog(self, ctx=self.ctx)
        if dialog.exec() != QDialog.Accepted:
            return
        values = dialog.values()
        if not values:
            return
        name, version, loader = values
        try:
            instance = self.ctx.store.create(name, version)
        except LumenError as exc:
            MessageDialog.tell(self, "创建实例失败", str(exc), level="error")
            return
        self.ctx.select_instance(instance)
        self.ctx.instances_changed.emit()
        self.refresh()
        # 未安装时引导安装（把新建时选的加载器带进安装对话框）
        if not self.ctx.is_installed(version, instance):
            from ..dialogs.install_dialog import InstallDialog

            InstallDialog(
                self,
                ctx=self.ctx,
                tasks=self.tasks,
                version_id=version,
                instance=instance,
                preset_kind=loader,
            ).exec()
            self.refresh()

    def _edit(self, instance: Instance) -> None:
        if InstanceEditDialog(self, ctx=self.ctx, instance=instance).exec() == QDialog.Accepted:
            self.ctx.instances_changed.emit()
            self.refresh()

    def _clone(self, instance: Instance) -> None:
        name = TextInputDialog.ask(
            self,
            title="复制实例",
            label_text="新实例名称",
            value=f"{instance.name} 副本",
            description="会完整复制 gameDir（含 mods 与存档），大实例可能需要一会儿。",
        )
        if not name:
            return
        try:
            clone = self.ctx.store.clone(instance, name)
        except LumenError as exc:
            MessageDialog.tell(self, "复制失败", str(exc), level="error")
            return
        self.ctx.instances_changed.emit()
        self.refresh()
        self.ctx.toast.emit(f"已复制为「{clone.name}」")

    def _rename(self, instance: Instance) -> None:
        name = TextInputDialog.ask(self, title="重命名实例", label_text="新的实例名称", value=instance.name)
        if not name:
            return
        try:
            self.ctx.store.rename(instance, name)
        except LumenError as exc:
            MessageDialog.tell(self, "重命名失败", str(exc), level="error")
            return
        self.ctx.instances_changed.emit()
        self.refresh()

    def _delete(self, instance: Instance) -> None:
        if ConfirmDeleteDialog(self, instance=instance).exec() != QDialog.Accepted:
            return
        try:
            self.ctx.store.delete(instance)
        except LumenError as exc:
            MessageDialog.tell(self, "删除失败", str(exc), level="error")
            return
        if self.ctx.settings.get("current_instance") == instance.id:
            remaining = [item for item in self.ctx.instances() if item.id != instance.id]
            if remaining:
                self.ctx.select_instance(remaining[0])
        self.ctx.instances_changed.emit()
        self.refresh()
        self.ctx.toast.emit(f"已删除实例「{instance.name}」")
