"""版本管理页：远程清单 + 本地已安装合并展示，支持安装 / 启动 / 删除。

性能取舍：清单有 900+ 条，一次性建 900 个卡片会明显掉帧。
这里采用「分页渲染 + 滚动到底自动追加」（每页 80 条），既能全量浏览也不卡。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QCheckBox, QComboBox, QHBoxLayout, QLineEdit, QMenu, QVBoxLayout, QWidget

from ...core.manifest import RemoteVersion
from ...core.mirrors import SOURCE_LABELS
from ...utils.errors import LumenError
from ...utils.fsutil import rmtree
from ..context import AppContext
from ..dialogs.common import MessageDialog, TextInputDialog
from ..widgets import (
    Badge,
    Card,
    EmptyState,
    SkeletonList,
    label,
    make_button,
    make_tool_button,
)
from ..workers import TaskManager, Worker
from .base import Page

PAGE_SIZE = 80


class VersionsPage(Page):
    def __init__(self, ctx: AppContext, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(ctx, tasks, title="版本管理", subtitle="从 Mojang 官方清单拉取全部版本，安装到当前实例", parent=parent)
        self._filtered: list[RemoteVersion] = []
        self._limit = PAGE_SIZE
        self._loading = False
        self._build_toolbar()
        self._show_skeleton()
        self.scroll.verticalScrollBar().valueChanged.connect(self._maybe_load_more)

    # ------------------------------------------------------------------ #
    # 工具栏
    # ------------------------------------------------------------------ #
    def _build_toolbar(self) -> None:
        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索版本号，如 1.21")
        self._search.setClearButtonEnabled(True)
        self._search.setFixedWidth(210)
        self._search.textChanged.connect(self._apply_filter)

        self._show_snapshots = QCheckBox("快照")
        self._show_snapshots.setChecked(bool(self.ctx.settings.get("show_snapshots")))
        self._show_snapshots.toggled.connect(self._on_filter_toggle)

        self._show_old = QCheckBox("远古版本")
        self._show_old.setChecked(bool(self.ctx.settings.get("show_old_versions")))
        self._show_old.toggled.connect(self._on_filter_toggle)

        self._source = QComboBox()
        self._source.addItem("自动选源", "auto")
        self._source.addItem("仅官方源", "official")
        self._source.addItem("仅镜像源", "mirror")
        index = max(0, self._source.findData(self.ctx.settings.download_source))
        self._source.setCurrentIndex(index)
        self._source.currentIndexChanged.connect(self._on_source_changed)
        self._source.setFixedWidth(120)
        self._source.setToolTip("下载源策略：自动会按测速结果择优，失败自动回退")

        self._refresh_button = make_button("刷新", kind="refresh", handler=self.refresh_remote)

        self.add_header_action(self._search)
        self.add_header_action(self._show_snapshots)
        self.add_header_action(self._show_old)
        self.add_header_action(self._source)
        self.add_header_action(self._refresh_button)

    def _on_filter_toggle(self) -> None:
        self.ctx.settings.set("show_snapshots", self._show_snapshots.isChecked())
        self.ctx.settings.set("show_old_versions", self._show_old.isChecked())
        self._limit = PAGE_SIZE
        self._apply_filter()

    def _on_source_changed(self) -> None:
        policy = str(self._source.currentData() or "auto")
        self.ctx.settings.download_source = policy
        label_text = {"auto": "自动选源", "official": "仅官方源", "mirror": "仅镜像源"}.get(policy, policy)
        self.ctx.toast.emit(f"下载源已切换为：{label_text}")

    # ------------------------------------------------------------------ #
    # 数据
    # ------------------------------------------------------------------ #
    def on_show(self) -> None:
        current = self.ctx.current_instance()
        self.set_subtitle(
            f"当前实例：{current.name} · {current.mc_version} · {current.display_loader}" if current else "尚未创建实例"
        )
        if self.ctx.manifest is None and not self._loading:
            self.refresh_remote(force=False)
        else:
            self._apply_filter()

    def refresh_remote(self, *, force: bool = True) -> None:
        if self._loading:
            return
        self._loading = True
        self._show_skeleton("正在获取版本清单…")
        worker = Worker(
            lambda **_: self.ctx.ensure_manifest(force=force),
            label="版本清单",
        )
        worker.succeeded.connect(self._on_manifest_ready)
        worker.failed.connect(self._on_manifest_failed)
        self.tasks.start(worker)

    def _on_manifest_ready(self, manifest) -> None:
        self._loading = False
        self.ctx.manifest = manifest
        self.ctx.manifest_changed.emit()
        self._apply_filter()

    def _on_manifest_failed(self, message: str, hint: str) -> None:
        self._loading = False
        self.clear_content()
        empty = EmptyState(
            "globe",
            "无法获取版本清单",
            f"{message}\n{hint or '请检查网络连接，或在设置里切换下载源'}",
            "重试",
        )
        empty.action.connect(lambda: self.refresh_remote(force=True))
        self.content_layout.addWidget(empty)
        self.content_layout.addStretch(1)

    def _apply_filter(self) -> None:
        manifest = self.ctx.manifest
        if manifest is None:
            return
        keyword = self._search.text().strip().lower()
        show_snapshots = self._show_snapshots.isChecked()
        show_old = self._show_old.isChecked()
        versions = manifest.filtered(snapshots=show_snapshots, old=show_old)
        if keyword:
            versions = [item for item in versions if keyword in item.id.lower()]
        self._filtered = versions
        self._limit = PAGE_SIZE
        self._render()

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _show_skeleton(self, message: str = "") -> None:
        self.clear_content()
        if message:
            self.content_layout.addWidget(label(message, "muted"))
        self.content_layout.addWidget(SkeletonList(6, row_height=62))
        self.content_layout.addStretch(1)

    def _render(self) -> None:
        self.clear_content()
        manifest = self.ctx.manifest
        if manifest is None:
            self._show_skeleton("正在获取版本清单…")
            return
        if not self._filtered:
            empty = EmptyState(
                "search",
                "没有匹配的版本",
                "换个关键词试试，或勾选「快照 / 远古版本」扩大范围。",
            )
            self.content_layout.addWidget(empty)
            self.content_layout.addStretch(1)
            return

        counts = manifest.counts()
        source_hint = SOURCE_LABELS.get(self.ctx.settings.download_source, "自动选源")
        status = label(
            f"共 {len(self._filtered)} 个版本（正式 {counts['release']} · 快照 {counts['snapshot']} · 远古 {counts['old']}）"
            f" · 下载源：{source_hint}",
            "muted",
        )
        self.content_layout.addWidget(status)

        current = self.ctx.current_instance()
        installed = self.ctx.installed_versions(current) if current else set()
        for version in self._filtered[: self._limit]:
            self.content_layout.addWidget(self._version_row(version, installed, current))

        remaining = len(self._filtered) - self._limit
        if remaining > 0:
            more = make_button(f"加载更多（还有 {remaining} 个）", handler=self._load_more)
            self.content_layout.addWidget(more, 0, Qt.AlignHCenter)
        self.content_layout.addStretch(1)

    def _load_more(self) -> None:
        self._limit += PAGE_SIZE
        self._render()

    def _maybe_load_more(self, value: int) -> None:
        bar = self.scroll.verticalScrollBar()
        if bar.maximum() > 0 and value >= bar.maximum() - 40 and self._limit < len(self._filtered):
            self._load_more()

    def _version_row(self, version: RemoteVersion, installed: set[str], current) -> QWidget:
        card = Card(margins=(16, 10, 16, 10), spacing=4)
        row = QHBoxLayout()
        row.setSpacing(12)

        text_box = QVBoxLayout()
        text_box.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(label(version.id, "version"))
        category = version.category
        badge_text = {"release": "正式版", "snapshot": "快照", "old": "远古", "unknown": "未知"}.get(category, category)
        badge_kind = {"release": "success", "snapshot": "warning", "old": "muted", "unknown": "muted"}.get(category, "muted")
        name_row.addWidget(Badge(badge_text, badge_kind))
        latest = self.ctx.manifest.latest.get("release") if self.ctx.manifest else ""
        if version.id == latest:
            name_row.addWidget(Badge("最新", "primary"))
        if self.ctx.is_installed(version.id, current):
            name_row.addWidget(Badge("已安装", "primary"))
        name_row.addStretch(1)
        text_box.addLayout(name_row)
        type_text = {"release": "正式版", "snapshot": "快照", "old_beta": "Beta", "old_alpha": "Alpha"}.get(
            version.type, version.type
        )
        text_box.addWidget(label(f"{version.date}  ·  {type_text}", "muted"))
        row.addLayout(text_box, 1)

        is_installed = version.id in installed or any(item.startswith(f"{version.id}-") for item in installed)
        is_current_installed = self.ctx.is_installed(version.id, current)
        if is_current_installed:
            launch = make_button("启动", kind="play", role="primary", handler=lambda: self._launch(version.id))
            row.addWidget(launch)
        else:
            install = make_button("安装", kind="download", handler=lambda: self._install(version.id))
            row.addWidget(install)

        more = make_tool_button("chevron", "更多操作", handler=lambda: self._show_menu(more, version))
        more.setFixedSize(34, 34)
        row.addWidget(more)

        card.body().addLayout(row)
        return card

    # ------------------------------------------------------------------ #
    # 操作
    # ------------------------------------------------------------------ #
    def _show_menu(self, anchor: QWidget, version: RemoteVersion) -> None:
        menu = QMenu(self)
        install_action = QAction("安装到其它实例…", menu)
        install_action.triggered.connect(lambda: self._install(version.id, pick_instance=True))
        menu.addAction(install_action)
        current = self.ctx.current_instance()
        if current is not None and self.ctx.is_installed(version.id, current):
            folder_action = QAction("打开版本文件夹", menu)
            folder_action.triggered.connect(lambda: self._open_version_folder(current, version.id))
            menu.addAction(folder_action)
            delete_action = QAction("删除本地版本", menu)
            delete_action.triggered.connect(lambda: self._delete_local(current, version.id))
            menu.addAction(delete_action)
        menu.addSeparator()
        copy_action = QAction(f"复制版本号 {version.id}", menu)
        copy_action.triggered.connect(lambda: self._copy_id(version.id))
        menu.addAction(copy_action)
        menu.exec(anchor.mapToGlobal(anchor.rect().bottomLeft()))

    def _copy_id(self, version_id: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(version_id)
        self.ctx.toast.emit(f"已复制 {version_id}")

    def _open_version_folder(self, instance, version_id: str) -> None:
        path = instance.version_dir(version_id)
        if path.exists():
            self.ctx.open_path(path)
        else:
            self.ctx.toast.emit("版本文件夹不存在")

    def _delete_local(self, instance, version_id: str) -> None:
        if not MessageDialog.ask(
            self,
            "删除本地版本",
            f"将从实例「{instance.name}」删除 {version_id} 的本地文件（不影响共享的 libraries/assets）。",
            ok_text="删除",
            danger=True,
            level="warning",
        ):
            return
        target = instance.version_dir(version_id)
        try:
            rmtree(target)
        except LumenError as exc:
            MessageDialog.tell(self, "删除失败", str(exc), level="error")
            return
        self.ctx.toast.emit(f"已删除 {version_id}")
        self._render()

    def _launch(self, version_id: str) -> None:
        instance = self.ctx.current_instance()
        if instance is None:
            return
        if not self.ctx.is_installed(version_id, instance):
            MessageDialog.tell(self, "尚未安装", f"{version_id} 还没有安装到该实例。", level="warning")
            return
        self.ctx.request_launch.emit(instance)

    def _install(self, version_id: str, *, pick_instance: bool = False) -> None:
        from ..dialogs.install_dialog import InstallDialog

        instances = self.ctx.instances()
        instance = self.ctx.current_instance()
        if pick_instance and instances:
            from PySide6.QtWidgets import QDialog

            from ..dialogs.common import InstancePickerDialog

            picker = InstancePickerDialog(self, instances=instances)
            if picker.exec() != QDialog.Accepted:
                return
            instance = picker.selected_instance()
        elif instance is None:
            name = TextInputDialog.ask(
                self,
                title="新建实例",
                label_text="还没有实例，先创建一个：",
                value=f"{version_id} 原版",
                description="每个实例拥有独立的 mods / saves / 配置，互不影响。",
            )
            if not name:
                return
            instance = self.ctx.store.create(name, version_id)
            self.ctx.select_instance(instance)
            self.ctx.instances_changed.emit()

        dialog = InstallDialog(self, ctx=self.ctx, tasks=self.tasks, version_id=version_id, instance=instance)
        dialog.exec()
        self.ctx.instances_changed.emit()
        self._render()
