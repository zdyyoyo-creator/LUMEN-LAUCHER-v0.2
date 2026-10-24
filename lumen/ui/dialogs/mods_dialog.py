"""mods 管理：添加（本地 / 在线）/ 启用 / 禁用 / 删除，全部就地操作实例的 mods 目录。"""

from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QListWidget, QListWidgetItem, QWidget

from ...core.instances import Instance, ModEntry
from ...utils.errors import LumenError
from ...utils.fsutil import human_size
from ..context import AppContext
from ..dialogs.common import BaseDialog, MessageDialog
from ..glyphs import glyph_icon
from ..widgets import label, make_button


class ModRow(QWidget):
    def __init__(self, mod: ModEntry, dialog: "ModsDialog") -> None:
        super().__init__()
        self.mod = mod
        self.dialog = dialog
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(10)

        from PySide6.QtWidgets import QCheckBox

        self.check = QCheckBox()
        self.check.setChecked(mod.enabled)
        self.check.setToolTip("勾选 = 启用，取消 = 禁用（重命名为 .disabled）")
        self.check.toggled.connect(self._on_toggle)
        layout.addWidget(self.check)

        name = label(mod.display_name, "body")
        name.setToolTip(mod.path.name)
        layout.addWidget(name, 1)

        size = label(human_size(mod.size), "muted")
        size.setFixedWidth(80)
        layout.addWidget(size)

        self.delete_button = make_button("删除", role="danger", handler=self._delete)
        self.delete_button.setFixedWidth(70)
        layout.addWidget(self.delete_button)

    def _on_toggle(self, checked: bool) -> None:
        try:
            updated = self.dialog.ctx.store.toggle_mod(self.mod)
        except LumenError as exc:
            MessageDialog.tell(self, "操作失败", str(exc), level="error")
            self.check.blockSignals(True)
            self.check.setChecked(not checked)
            self.check.blockSignals(False)
            return
        self.mod = updated
        self.dialog.ctx.toast.emit(("已启用 " if checked else "已禁用 ") + updated.display_name)

    def _delete(self) -> None:
        if not MessageDialog.ask(
            self,
            "删除模组",
            f"确定删除 {self.mod.display_name} 吗？文件会从 mods 目录移除。",
            ok_text="删除",
            danger=True,
            level="warning",
        ):
            return
        try:
            self.dialog.ctx.store.delete_mod(self.mod)
        except LumenError as exc:
            MessageDialog.tell(self, "删除失败", str(exc), level="error")
            return
        self.dialog.reload()


class ModsDialog(BaseDialog):
    def __init__(self, parent: QWidget | None, *, ctx: AppContext, instance: Instance) -> None:
        super().__init__(parent, title=f"mods · {instance.name}", width=680)
        self.ctx = ctx
        self.instance = instance

        self._summary = label("", "muted")
        self._layout.addWidget(self._summary)

        self._list = QListWidget()
        self._list.setMinimumHeight(320)
        self._list.setAlternatingRowColors(False)
        self._layout.addWidget(self._list, 1)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        add_row.addWidget(make_button("从文件添加…", kind="plus", role="primary", handler=self._add_files))
        add_row.addWidget(make_button("从 Modrinth / CurseForge 下载", kind="download", handler=self._browse_online))
        add_row.addStretch(1)
        self._layout.addLayout(add_row)

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(make_button("打开 mods 文件夹", kind="folder", handler=self._open_folder))
        row.addWidget(make_button("刷新", kind="refresh", handler=self.reload))
        row.addStretch(1)
        row.addWidget(make_button("全部启用", handler=lambda: self._bulk(True)))
        row.addWidget(make_button("全部禁用", handler=lambda: self._bulk(False)))
        self._layout.addLayout(row)

        self.add_footer_button(make_button("关闭", handler=self.accept))
        self.finish()
        self.reload()

    # ------------------------------------------------------------------ #
    def _add_files(self) -> None:
        """把本地 .jar 复制进 mods 目录（支持多选，重名自动跳过）。"""
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择模组文件",
            str(Path.home()),
            "模组或资源包 (*.jar *.zip);;所有文件 (*.*)",
        )
        if not paths:
            return
        ensure = self.instance.mods_dir
        ensure.mkdir(parents=True, exist_ok=True)
        added = 0
        skipped = 0
        failed: list[str] = []
        for raw in paths:
            source = Path(raw)
            target = ensure / source.name
            if target.exists():
                skipped += 1
                continue
            try:
                shutil.copy2(source, target)
                added += 1
            except OSError as exc:
                failed.append(f"{source.name}：{exc}")
        self.reload()
        message = f"已添加 {added} 个文件"
        if skipped:
            message += f"，{skipped} 个同名文件已跳过"
        self.ctx.toast.emit(message)
        if failed:
            MessageDialog.tell(self, "部分文件添加失败", "\n".join(failed[:6]), level="warning")

    def _browse_online(self) -> None:
        """跳转到「资源」页，预设类型=模组、目标实例=当前实例。"""
        self.accept()
        self.ctx.request_browse.emit({"kind": "mod", "instance": self.instance})

    # ------------------------------------------------------------------ #
    def reload(self) -> None:
        self._list.clear()
        mods = self.ctx.store.list_mods(self.instance)
        enabled = sum(1 for mod in mods if mod.enabled)
        self._summary.setText(f"共 {len(mods)} 个模组，{enabled} 个启用" if mods else "mods 目录是空的")
        if not mods:
            item = QListWidgetItem("还没有安装任何 mod。下载的 .jar 放进 mods 目录后点「刷新」。")
            item.setFlags(Qt.NoItemFlags)
            self._list.addItem(item)
            return
        for mod in mods:
            row = ModRow(mod, self)
            item = QListWidgetItem()
            item.setSizeHint(row.sizeHint())
            self._list.addItem(item)
            self._list.setItemWidget(item, row)

    def _bulk(self, enabled: bool) -> None:
        changed = 0
        for mod in self.ctx.store.list_mods(self.instance):
            if mod.enabled == enabled:
                continue
            try:
                self.ctx.store.toggle_mod(mod)
                changed += 1
            except LumenError:
                continue
        self.reload()
        self.ctx.toast.emit(f"已{'启用' if enabled else '禁用'} {changed} 个模组")

    def _open_folder(self) -> None:
        self.ctx.open_instance_folder(self.instance, "mods")
