"""整合包页：拖拽/选择文件导入，展示已导入的整合包实例。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)

from ...core.instances import Instance
from ...modpack.importer import inspect_modpack
from ...utils.errors import LumenError
from ..context import AppContext
from ..dialogs.common import MessageDialog
from ..dialogs.import_dialog import ImportDialog
from ..glyphs import glyph_pixmap
from ..widgets import (
    Badge,
    Card,
    GlyphWidget,
    label,
    make_button,
    make_tool_button,
    restyle,
)
from ..workers import TaskManager, Worker
from .base import Page

SUPPORTED_SUFFIXES = (".mrpack", ".zip")


class DropZone(QFrame):
    """虚线拖拽区：拖入文件或点击选择。"""

    dropped = Signal(str)
    clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("dropzone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(132)
        self.setCursor(Qt.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(8)
        layout.setAlignment(Qt.AlignCenter)

        icon = GlyphWidget("modpack", 34, width=1.4)
        layout.addWidget(icon, 0, Qt.AlignHCenter)
        heading = label("把整合包拖到这里", "subtitle")
        heading.setAlignment(Qt.AlignCenter)
        layout.addWidget(heading)
        hint = label("支持 Modrinth .mrpack、CurseForge 整合包与普通 .zip；也可以点击选择文件", "muted", wrap=True)
        hint.setAlignment(Qt.AlignCenter)
        hint.setMinimumWidth(520)
        layout.addWidget(hint, 0, Qt.AlignHCenter)

    # ------------------------------------------------------------------ #
    def _supported(self, mime) -> str | None:
        if not mime.hasUrls():
            return None
        for url in mime.urls():
            path = url.toLocalFile()
            if path and path.lower().endswith(SUPPORTED_SUFFIXES):
                return path
        return None

    def dragEnterEvent(self, event):  # noqa: N802
        if self._supported(event.mimeData()):
            event.acceptProposedAction()
            self.setProperty("hover", "true")
            restyle(self)

    def dragMoveEvent(self, event):  # noqa: N802
        if self._supported(event.mimeData()):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):  # noqa: N802
        self.setProperty("hover", "false")
        restyle(self)

    def dropEvent(self, event):  # noqa: N802
        self.setProperty("hover", "false")
        restyle(self)
        path = self._supported(event.mimeData())
        if path:
            event.acceptProposedAction()
            self.dropped.emit(path)

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ModpacksPage(Page):
    def __init__(self, ctx: AppContext, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent, ctx=ctx, tasks=tasks, title="整合包", subtitle="导入 Modrinth / CurseForge 整合包，自动补齐版本与加载器")
        self.add_header_action(make_button("在线浏览", kind="globe", handler=self._browse_online))
        self.add_header_action(make_button("导入文件", kind="download", role="primary", handler=self._browse))
        self.add_header_action(make_tool_button("refresh", "刷新", self.refresh))
        self.ctx.instances_changed.connect(self._schedule_refresh)
        self._pending = False

        self._dropzone = DropZone()
        self._dropzone.dropped.connect(self._import_path)
        self._dropzone.clicked.connect(self._browse)
        self.content_layout.addWidget(self._dropzone)

        self._list_host = QWidget()
        self._list_layout = QVBoxLayout(self._list_host)
        self._list_layout.setContentsMargins(0, 4, 0, 0)
        self._list_layout.setSpacing(8)
        self.content_layout.addWidget(self._list_host)
        self.content_layout.addStretch(1)
        self.refresh()

    # ------------------------------------------------------------------ #
    def _schedule_refresh(self) -> None:
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
        from ..widgets import clear_layout

        clear_layout(self._list_layout)
        packs = [instance for instance in self.ctx.instances() if instance.modpack]
        self.set_subtitle(f"已导入 {len(packs)} 个整合包" if packs else "导入 Modrinth / CurseForge 整合包，自动补齐版本与加载器")
        if not packs:
            tip = label("还没有导入过整合包。导入后会自动安装对应版本与加载器，并把 overrides 解压到实例目录。", "muted", wrap=True)
            self._list_layout.addWidget(tip)
            return
        self._list_layout.addWidget(label("已导入的整合包", "section"))
        for instance in packs:
            self._list_layout.addWidget(self._pack_row(instance))

    def _pack_row(self, instance: Instance) -> QWidget:
        card = Card(margins=(16, 12, 16, 12), spacing=6)
        row = QHBoxLayout()
        row.setSpacing(12)

        text_box = QVBoxLayout()
        text_box.setSpacing(3)
        name_row = QHBoxLayout()
        name_row.setSpacing(8)
        name_row.addWidget(label(instance.modpack.get("name") or instance.name, "version"))
        source = str(instance.modpack.get("source") or "")
        kind = {"mrpack": "Modrinth", "curseforge": "CurseForge", "zip": "压缩包"}.get(source, "整合包")
        name_row.addWidget(Badge(kind, "primary"))
        name_row.addStretch(1)
        text_box.addLayout(name_row)
        meta = f"{instance.mc_version} · {instance.display_loader} · {instance.last_played_text}"
        text_box.addWidget(label(meta, "muted"))
        files = instance.modpack.get("files")
        if files:
            text_box.addWidget(label(f"已下载 {files} 个文件", "muted"))
        row.addLayout(text_box, 1)

        mods_button = make_button("mods", kind="mods", handler=lambda: self._open_mods(instance))
        row.addWidget(mods_button)
        folder = make_button("文件夹", kind="folder", handler=lambda: self.ctx.open_instance_folder(instance))
        row.addWidget(folder)
        launch = make_button("启动", kind="play", role="primary", handler=lambda: self._launch(instance))
        row.addWidget(launch)
        card.body().addLayout(row)
        return card

    # ------------------------------------------------------------------ #
    def _launch(self, instance: Instance) -> None:
        self.ctx.select_instance(instance)
        self.ctx.request_launch.emit(instance)

    def _open_mods(self, instance: Instance) -> None:
        from ..dialogs.mods_dialog import ModsDialog

        ModsDialog(self, ctx=self.ctx, instance=instance).exec()

    def _browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择整合包",
            str(Path.home()),
            "整合包 (*.mrpack *.zip);;所有文件 (*.*)",
        )
        if path:
            self._import_path(path)

    def _browse_online(self) -> None:
        """跳转到「资源」页，预设类型=整合包。"""
        self.ctx.request_browse.emit({"kind": "modpack"})

    def _import_path(self, path: str) -> None:
        archive = Path(path)
        if not archive.exists():
            MessageDialog.tell(self, "文件不存在", path, level="error")
            return
        self.ctx.toast.emit("正在解析整合包…")
        api_key = str(self.ctx.settings.get("cf_api_key") or "")
        worker = Worker(
            lambda **_: inspect_modpack(archive, cf_api_key=api_key),
            label="解析整合包",
        )
        worker.succeeded.connect(self._on_parsed)
        worker.failed.connect(self._on_parse_failed)
        self.tasks.start(worker)

    def _on_parsed(self, plan) -> None:
        dialog = ImportDialog(self, ctx=self.ctx, tasks=self.tasks, plan=plan)
        dialog.exec()
        self.refresh()

    def _on_parse_failed(self, message: str, hint: str) -> None:
        MessageDialog.tell(self, "无法解析整合包", message, details=hint, level="error")
