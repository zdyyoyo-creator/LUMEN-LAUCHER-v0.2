"""资源页：从 Modrinth / CurseForge 搜索并下载模组与整合包。

设计要点：
* 目标实例决定「加载器 + Minecraft 版本」筛选，避免下载到不兼容的文件；
* 模组直接落进实例 ``mods/``（带 SHA1 校验），整合包下载后复用 ``ImportDialog``；
* CurseForge 需要 API Key：没填时给出可点击的引导，而不是报错卡住；
* 图标异步加载（后台下载字节，主线程转 QPixmap），列表滚动不卡。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import QObject, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QProgressDialog,
    QVBoxLayout,
    QWidget,
)

from ...core.catalog import (
    SOURCE_LABELS,
    TYPE_LABELS,
    CatalogError,
    CatalogHit,
    CatalogVersion,
    CurseForgeClient,
    ModrinthClient,
    make_client,
    pick_best_version,
)
from ...core.downloader import DownloadTask, Downloader
from ...core.instances import Instance
from ...modpack.importer import inspect_modpack
from ...utils.errors import LumenError
from ...utils.fsutil import ensure_dir, human_size
from ...utils.log import get as get_logger
from ...utils.net import get_bytes
from .. import theme
from ..context import AppContext
from ..dialogs.common import MessageDialog
from ..dialogs.import_dialog import ImportDialog
from ..widgets import (
    Badge,
    Card,
    EmptyState,
    GlyphWidget,
    SkeletonList,
    label,
    make_button,
)
from ..workers import TaskManager, Worker
from .base import Page

log = get_logger("ui.browse")

LOADER_CHOICES = [
    ("", "跟随实例"),
    ("fabric", "Fabric"),
    ("quilt", "Quilt"),
    ("forge", "Forge"),
    ("neoforge", "NeoForge"),
]
TYPE_CHOICES = [("mod", "模组"), ("modpack", "整合包")]
SOURCE_CHOICES = [("modrinth", "Modrinth"), ("curseforge", "CurseForge")]


# --------------------------------------------------------------------------- #
# 图标加载
# --------------------------------------------------------------------------- #
class IconCache(QObject):
    """异步下载图标（后台取字节，主线程解码成 QPixmap）。"""

    ready = Signal(str, bytes)
    loaded = Signal(str)

    def __init__(self, parent: QObject | None = None, *, limit: int = 400) -> None:
        super().__init__(parent)
        self._pixmaps: dict[str, QPixmap] = {}
        self._pending: set[str] = set()
        self._limit = limit
        self._pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="lumen-icon")
        self.ready.connect(self._on_ready)

    def get(self, url: str) -> QPixmap | None:
        return self._pixmaps.get(url)

    def request(self, url: str) -> None:
        if not url or url in self._pixmaps or url in self._pending:
            return
        if len(self._pixmaps) > self._limit:
            self._pixmaps.clear()
        self._pending.add(url)
        self._pool.submit(self._fetch, url)

    def _fetch(self, url: str) -> None:
        try:
            data = get_bytes(url, retries=0, timeout=10, limit=4 * 1024 * 1024)
        except Exception as exc:  # noqa: BLE001 - 图标失败无所谓
            log.debug("图标下载失败：%s（%s）", url, exc)
            self._pending.discard(url)
            return
        self.ready.emit(url, data)

    def _on_ready(self, url: str, data: bytes) -> None:
        self._pending.discard(url)
        pixmap = QPixmap()
        if pixmap.loadFromData(data):
            self._pixmaps[url] = pixmap
            self.loaded.emit(url)


class IconLabel(QWidget):
    """圆角图标：没有图时显示首字母。"""

    def __init__(self, size: int = 44, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._pixmap: QPixmap | None = None
        self._text = ""
        self.setFixedSize(size, size)

    def set_text(self, text: str) -> None:
        self._text = (text or "?")[:1].upper()
        self.update()

    def set_pixmap(self, pixmap: QPixmap | None) -> None:
        self._pixmap = pixmap
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        palette = theme.active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(0, 0, self._size, self._size)
        path = QPainterPath()
        path.addRoundedRect(rect, 9, 9)
        painter.setClipPath(path)
        painter.fillPath(path, QColor(palette.surface_alt))
        if self._pixmap is not None and not self._pixmap.isNull():
            scaled = self._pixmap.scaled(
                int(self._size), int(self._size), Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation
            )
            painter.drawPixmap(rect.toRect(), scaled)
        else:
            font = QFont(self.font())
            font.setPixelSize(int(self._size * 0.42))
            font.setWeight(QFont.DemiBold)
            painter.setFont(font)
            painter.setPen(QColor(palette.text_muted))
            painter.drawText(rect, Qt.AlignCenter, self._text)
        painter.end()


# --------------------------------------------------------------------------- #
# 版本选择
# --------------------------------------------------------------------------- #
class VersionPickerDialog(QDialog):
    def __init__(self, parent: QWidget | None, *, hit: CatalogHit, versions: list[CatalogVersion]) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"选择版本 · {hit.title}")
        self.setMinimumSize(620, 420)
        self._selected: CatalogVersion | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(10)
        layout.addWidget(label(f"{hit.title} · {hit.source_label}", "subtitle"))
        layout.addWidget(label("选择一个具体文件版本（已按稳定性与匹配度排序）", "muted"))

        self._list = QListWidget()
        for version in versions:
            text = f"{version.label}    {human_size(version.file_size)}"
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, version)
            self._list.addItem(item)
        if versions:
            self._list.setCurrentRow(0)
        self._list.itemDoubleClicked.connect(lambda _item: self._accept())
        layout.addWidget(self._list, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(make_button("取消", handler=self.reject))
        row.addWidget(make_button("下载", role="primary", handler=self._accept))
        layout.addLayout(row)

    def _accept(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        self._selected = item.data(Qt.UserRole)
        self.accept()

    def selected_version(self) -> CatalogVersion | None:
        return self._selected


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
class BrowsePage(Page):
    """资源下载页（左导航「资源」）。"""

    def __init__(self, ctx: AppContext, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(
            ctx,
            tasks,
            title="资源",
            subtitle="从 Modrinth / CurseForge 下载模组与整合包",
            parent=parent,
        )
        self.ctx = ctx
        self.tasks = tasks
        self._hits: list[CatalogHit] = []
        self._offset = 0
        self._total = 0
        self._loading = False
        self._pending_kind = "mod"
        self._icons = IconCache(self)
        self._icons.loaded.connect(self._on_icon_ready)
        self._icon_targets: dict[str, list[IconLabel]] = {}

        self._build_toolbar()
        self._build_filters()
        self._show_placeholder("输入关键词开始搜索，或直接点「搜索」看热门内容")

    # ------------------------------------------------------------------ #
    def _build_toolbar(self) -> None:
        self._query = QLineEdit()
        self._query.setPlaceholderText("搜索模组 / 整合包，例如 sodium、JEI、Fabulously Optimized")
        self._query.setClearButtonEnabled(True)
        self._query.setMinimumWidth(320)
        self._query.returnPressed.connect(lambda: self.search(reset=True))
        self.add_header_action(self._query)
        self.add_header_action(make_button("搜索", kind="search", role="primary", handler=lambda: self.search(reset=True)))

    def _build_filters(self) -> None:
        card = Card(margins=(16, 12, 16, 12), spacing=8)
        row = QHBoxLayout()
        row.setSpacing(18)

        source_box = QVBoxLayout()
        source_box.setSpacing(4)
        source_box.addWidget(label("来源", "section"))
        from ..widgets import Segmented

        self._source = Segmented(SOURCE_CHOICES, current="modrinth")
        self._source.changed.connect(lambda _v: self.search(reset=True))
        source_box.addWidget(self._source)
        row.addLayout(source_box)

        type_box = QVBoxLayout()
        type_box.setSpacing(4)
        type_box.addWidget(label("类型", "section"))
        self._kind = Segmented(TYPE_CHOICES, current="mod")
        self._kind.changed.connect(self._on_kind_changed)
        type_box.addWidget(self._kind)
        row.addLayout(type_box)

        loader_box = QVBoxLayout()
        loader_box.setSpacing(4)
        loader_box.addWidget(label("加载器", "section"))
        self._loader = QComboBox()
        for value, text in LOADER_CHOICES:
            self._loader.addItem(text, value)
        self._loader.setMinimumWidth(130)
        self._loader.currentIndexChanged.connect(lambda _i: self.search(reset=True))
        loader_box.addWidget(self._loader)
        row.addLayout(loader_box)

        instance_box = QVBoxLayout()
        instance_box.setSpacing(4)
        instance_box.addWidget(label("目标实例", "section"))
        self._instance_combo = QComboBox()
        self._instance_combo.setMinimumWidth(220)
        self._instance_combo.currentIndexChanged.connect(self._on_instance_changed)
        instance_box.addWidget(self._instance_combo)
        row.addLayout(instance_box)

        row.addStretch(1)
        self._hint = label("", "muted", wrap=True)
        card.body().addLayout(row)
        card.body().addWidget(self._hint)
        self.content_layout.addWidget(card)

        # 结果区单独一个容器：切换搜索条件时只重建这里，筛选卡保持不动
        self._results_host = QWidget()
        self._results_layout = QVBoxLayout(self._results_host)
        self._results_layout.setContentsMargins(0, 2, 0, 0)
        self._results_layout.setSpacing(10)
        self.content_layout.addWidget(self._results_host, 1)
        self._fill_instances()

    def _fill_instances(self) -> None:
        combo = self._instance_combo
        combo.blockSignals(True)
        combo.clear()
        for instance in self.ctx.instances():
            combo.addItem(f"{instance.name} · {instance.mc_version} · {instance.display_loader}", instance.id)
        current = self.ctx.current_instance()
        if current is not None:
            index = combo.findData(current.id)
            if index >= 0:
                combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._on_instance_changed()

    def _on_instance_changed(self) -> None:
        instance = self._target_instance()
        if instance is None:
            self._hint.setText("还没有实例：下载的模组需要放进某个实例。请先创建实例。")
            return
        loader = instance.loader_type or ""
        index = self._loader.findData(loader)
        if index >= 0 and self._loader.currentIndex() != index:
            self._loader.blockSignals(True)
            self._loader.setCurrentIndex(index)
            self._loader.blockSignals(False)
        if not loader:
            self._hint.setText(
                f"目标实例「{instance.name}」是原版（{instance.mc_version}），没有加载器，模组不会生效。"
                "建议先给实例安装 Fabric/Forge 等加载器。"
            )
        else:
            self._hint.setText(
                f"目标实例「{instance.name}」：Minecraft {instance.mc_version} · {instance.display_loader}"
            )

    def _on_kind_changed(self, kind: str) -> None:
        self._pending_kind = kind
        self.search(reset=True)

    # ------------------------------------------------------------------ #
    def preset(self, *, kind: str = "mod", instance: Instance | None = None, query: str = "") -> None:
        """从其它页面跳转过来时预设条件。"""
        if instance is not None:
            index = self._instance_combo.findData(instance.id)
            if index >= 0:
                self._instance_combo.setCurrentIndex(index)
        self._kind.set_current(kind)
        self._pending_kind = kind
        if query:
            self._query.setText(query)
        self.search(reset=True)

    def on_show(self) -> None:
        self._fill_instances()

    def on_theme_changed(self) -> None:
        self.update()

    def refresh_icons(self) -> None:
        super().refresh_icons()

    # ------------------------------------------------------------------ #
    def _target_instance(self) -> Instance | None:
        data = self._instance_combo.currentData()
        if not data:
            return self.ctx.current_instance()
        return self.ctx.store.get(str(data)) or self.ctx.current_instance()

    def _client(self):
        source = self._source.current()
        return make_client(source, curseforge_key=str(self.ctx.settings.get("cf_api_key") or ""))

    def _filters(self) -> tuple[str, str, str]:
        instance = self._target_instance()
        kind = self._kind.current()
        loader = str(self._loader.currentData() or "")
        game_version = instance.mc_version if instance is not None else ""
        return kind, loader, game_version

    # ------------------------------------------------------------------ #
    def search(self, *, reset: bool = True) -> None:
        if self._loading:
            return
        client = self._client()
        if isinstance(client, CurseForgeClient) and not client.available:
            self._show_curseforge_hint()
            return
        kind, loader, game_version = self._filters()
        query = self._query.text().strip()
        self._loading = True
        self._offset = 0 if reset else self._offset
        if reset:
            self._hits = []
            self._clear_results()
            self._results_layout.addWidget(SkeletonList(3, row_height=84))
        worker = Worker(
            lambda **_: client.search(
                query,
                project_type=kind,
                loader=loader,
                game_version=game_version,
                limit=20,
                offset=self._offset,
            ),
            label="资源搜索",
        )
        worker.succeeded.connect(self._on_results)
        worker.failed.connect(self._on_search_failed)
        self.tasks.start(worker)

    def _on_results(self, hits: list[CatalogHit]) -> None:
        self._loading = False
        self._clear_results()
        self._hits = list(hits)
        if not hits:
            empty = EmptyState(
                "search",
                "没有找到结果",
                "换个关键词，或把「加载器」改成「跟随实例」以外的选项再试。",
            )
            self._results_layout.addWidget(empty)
            self._results_layout.addStretch(1)
            return
        self._results_layout.addWidget(
            label(f"共 {len(hits)} 个结果 · 来源 {SOURCE_LABELS.get(self._source.current(), '')}", "muted")
        )
        for hit in hits:
            self._results_layout.addWidget(self._result_card(hit))
        self._results_layout.addStretch(1)

    def _on_search_failed(self, message: str, hint: str) -> None:
        self._loading = False
        self._clear_results()
        card = Card(margins=(16, 14, 16, 14), spacing=8)
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(GlyphWidget("warning", 20, color=theme.active().warning, width=1.7), 0, Qt.AlignTop)
        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        text_box.addWidget(label("搜索失败", "subtitle"))
        text_box.addWidget(label(message, "body", wrap=True))
        if hint:
            text_box.addWidget(label(hint, "muted", wrap=True))
        row.addLayout(text_box, 1)
        card.body().addLayout(row)
        self._results_layout.addWidget(card)
        self._results_layout.addStretch(1)

    def _show_curseforge_hint(self) -> None:
        self._clear_results()
        card = Card(margins=(18, 16, 18, 16), spacing=8)
        card.body().addWidget(label("CurseForge 需要 API Key", "subtitle"))
        card.body().addWidget(
            label(
                "CurseForge 的搜索与下载接口都要求 API Key。到「设置 → 整合包」填入后即可使用；"
                "Modrinth 不需要 Key，可以直接搜索和下载。",
                "muted",
                wrap=True,
            )
        )
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        buttons.addStretch(1)
        buttons.addWidget(make_button("改用 Modrinth", handler=lambda: self._switch_to_modrinth()))
        buttons.addWidget(make_button("去设置填 Key", kind="settings", role="primary", handler=self._goto_settings))
        card.body().addLayout(buttons)
        self._results_layout.addWidget(card)
        self._results_layout.addStretch(1)

    def _switch_to_modrinth(self) -> None:
        self._source.set_current("modrinth")
        self.search(reset=True)

    def _goto_settings(self) -> None:
        self.ctx.toast.emit("请在「设置 → 整合包」里填写 CurseForge API Key")
        self.ctx.request_page.emit("settings")

    # ------------------------------------------------------------------ #
    def _result_card(self, hit: CatalogHit) -> QWidget:
        card = Card(margins=(14, 12, 14, 12), spacing=6)
        row = QHBoxLayout()
        row.setSpacing(12)

        icon = IconLabel(46)
        icon.set_text(hit.title)
        if hit.icon_url:
            self._icon_targets.setdefault(hit.icon_url, []).append(icon)
            cached = self._icons.get(hit.icon_url)
            if cached is not None:
                icon.set_pixmap(cached)
            else:
                self._icons.request(hit.icon_url)
        row.addWidget(icon, 0, Qt.AlignTop)

        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        title_row = QHBoxLayout()
        title_row.setSpacing(8)
        title_row.addWidget(label(hit.title, "version"))
        title_row.addWidget(Badge(hit.source_label, "primary"))
        title_row.addWidget(label(f"↓ {hit.downloads_text}", "muted"))
        title_row.addStretch(1)
        text_box.addLayout(title_row)

        description = hit.description.strip().replace("\n", " ")
        if len(description) > 160:
            description = description[:160] + "…"
        text_box.addWidget(label(description or "（无简介）", "muted", wrap=True))

        tags = [tag for tag in hit.categories[:5] if tag]
        if tags:
            text_box.addWidget(label(" · ".join(tags), "muted"))
        row.addLayout(text_box, 1)

        actions = QVBoxLayout()
        actions.setSpacing(6)
        actions.addWidget(make_button("安装", kind="download", role="primary", handler=lambda: self._install(hit)))
        actions.addWidget(make_button("选择版本", handler=lambda: self._pick_version(hit)))
        row.addLayout(actions)

        card.body().addLayout(row)
        return card

    def _on_icon_ready(self, url: str) -> None:
        pixmap = self._icons.get(url)
        if pixmap is None:
            return
        for target in self._icon_targets.get(url, []):
            target.set_pixmap(pixmap)

    # ------------------------------------------------------------------ #
    def _install(self, hit: CatalogHit) -> None:
        instance = self._target_instance()
        if instance is None:
            MessageDialog.tell(self, "没有目标实例", "请先在「实例」页创建一个实例。", level="warning")
            return
        client = self._client()
        kind = hit.project_type or self._kind.current()
        _, loader, game_version = self._filters()
        if kind == "mod" and not instance.loader_type:
            MessageDialog.tell(
                self,
                "这个实例没有模组加载器",
                f"「{instance.name}」是原版实例，装进去的模组不会生效。\n可以先在版本管理里给实例安装 Fabric/Forge 等加载器。",
                level="warning",
            )
            return

        self.ctx.toast.emit(f"正在获取 {hit.title} 的版本信息…")
        worker = Worker(
            lambda **_: client.versions(hit.project_id, loader=loader, game_version=game_version),
            label=f"{hit.title} 版本",
        )
        worker.succeeded.connect(lambda versions: self._install_with_versions(hit, versions, instance))
        worker.failed.connect(lambda message, hint: MessageDialog.tell(self, "获取版本失败", message, details=hint, level="error"))
        self.tasks.start(worker)

    def _install_with_versions(self, hit: CatalogHit, versions: list[CatalogVersion], instance: Instance) -> None:
        _, loader, game_version = self._filters()
        version = pick_best_version(versions, loader=loader, game_version=game_version)
        if version is None or not version.download_url:
            MessageDialog.tell(
                self,
                "没有可下载的文件",
                f"{hit.title} 没有匹配当前实例（{instance.mc_version} / {instance.display_loader}）的版本。",
                level="warning",
            )
            return
        if hit.project_type == "modpack" or self._kind.current() == "modpack":
            self._download_pack(hit, version)
        else:
            self._download_mod(hit, version, instance)

    def _download_mod(self, hit: CatalogHit, version: CatalogVersion, instance: Instance) -> None:
        ensure_dir(instance.mods_dir)
        target = instance.mods_dir / (version.filename or f"{hit.slug or hit.project_id}.jar")
        task = DownloadTask(
            url=version.download_url,
            path=target,
            sha1=version.sha1 or None,
            size=version.file_size or None,
            label=version.filename or hit.title,
        )
        self.ctx.toast.emit(f"正在下载 {hit.title}…")
        worker = Worker(
            lambda progress=None: self._run_download([task], progress),
            with_progress=True,
            label=f"下载 {hit.title}",
        )
        worker.succeeded.connect(lambda report: self._on_mod_done(hit, version, target, report))
        worker.failed.connect(lambda message, hint: MessageDialog.tell(self, "下载失败", message, details=hint, level="error"))
        self.tasks.start(worker)

    def _run_download(self, tasks: list[DownloadTask], progress):
        downloader = Downloader(self.ctx.selector, max_workers=4, retries=3)
        return downloader.run(tasks, progress=progress, label="资源下载")

    def _on_mod_done(self, hit: CatalogHit, version: CatalogVersion, target, report) -> None:
        if report.failed:
            message = report.errors[0][1] if report.errors else "未知错误"
            MessageDialog.tell(self, "下载失败", message, level="error")
            return
        self.ctx.toast.emit(f"已安装 {hit.title} {version.version_number} → mods/")
        self.ctx.instances_changed.emit()

    def _download_pack(self, hit: CatalogHit, version: CatalogVersion) -> None:
        filename = version.filename or f"{hit.slug or hit.project_id}.mrpack"
        if not filename.lower().endswith((".mrpack", ".zip")):
            filename += ".mrpack"
        target = self.ctx.paths.cache_dir / "packs" / filename
        ensure_dir(target.parent)
        task = DownloadTask(
            url=version.download_url,
            path=target,
            sha1=version.sha1 or None,
            size=version.file_size or None,
            label=filename,
        )
        dialog = QProgressDialog(f"正在下载整合包：{hit.title}", "取消", 0, 100, self)
        dialog.setWindowTitle("下载整合包")
        dialog.setWindowModality(Qt.WindowModal)
        dialog.setMinimumDuration(0)
        dialog.setAutoClose(False)
        dialog.setAutoReset(False)

        def on_progress(snapshot) -> None:
            dialog.setValue(int(snapshot.percent * 100))
            dialog.setLabelText(
                f"正在下载整合包：{hit.title}\n{snapshot.done_files}/{snapshot.total_files} 文件 · {human_size(snapshot.done_bytes)}"
            )

        worker = Worker(
            lambda progress=None: self._run_download([task], progress),
            with_progress=True,
            label=f"下载整合包 {hit.title}",
        )
        worker.progressed.connect(on_progress)
        worker.succeeded.connect(lambda report: self._on_pack_done(hit, target, report, dialog))
        worker.failed.connect(lambda message, hint: self._on_pack_failed(message, hint, dialog))
        dialog.canceled.connect(worker.cancel)
        self.tasks.start(worker)

    def _on_pack_done(self, hit: CatalogHit, archive, report, dialog: QProgressDialog) -> None:
        dialog.close()
        if report.failed:
            message = report.errors[0][1] if report.errors else "未知错误"
            MessageDialog.tell(self, "整合包下载失败", message, level="error")
            return
        try:
            plan = inspect_modpack(archive, cf_api_key=str(self.ctx.settings.get("cf_api_key") or ""))
        except LumenError as exc:
            MessageDialog.tell(self, "无法解析整合包", exc.message, details=exc.hint, level="error")
            return
        plan.name = plan.name if plan.name and plan.name != "未命名整合包" else hit.title
        self.ctx.toast.emit(f"整合包已下载：{plan.name}")
        ImportDialog(self, ctx=self.ctx, tasks=self.tasks, plan=plan).exec()
        self.ctx.instances_changed.emit()

    def _on_pack_failed(self, message: str, hint: str, dialog: QProgressDialog) -> None:
        dialog.close()
        MessageDialog.tell(self, "整合包下载失败", message, details=hint, level="error")

    # ------------------------------------------------------------------ #
    def _pick_version(self, hit: CatalogHit) -> None:
        client = self._client()
        _, loader, game_version = self._filters()
        self.ctx.toast.emit(f"正在获取 {hit.title} 的版本列表…")
        worker = Worker(
            lambda **_: client.versions(hit.project_id, loader=loader, game_version=game_version),
            label=f"{hit.title} 版本",
        )
        worker.succeeded.connect(lambda versions: self._show_version_dialog(hit, versions))
        worker.failed.connect(lambda message, hint: MessageDialog.tell(self, "获取版本失败", message, details=hint, level="error"))
        self.tasks.start(worker)

    def _show_version_dialog(self, hit: CatalogHit, versions: list[CatalogVersion]) -> None:
        if not versions:
            MessageDialog.tell(
                self,
                "没有版本",
                f"{hit.title} 没有匹配当前筛选条件的文件，试试把「加载器」改成其它选项。",
                level="warning",
            )
            return
        dialog = VersionPickerDialog(self, hit=hit, versions=versions)
        if dialog.exec() != QDialog.Accepted:
            return
        version = dialog.selected_version()
        if version is None:
            return
        instance = self._target_instance()
        if hit.project_type == "modpack":
            self._download_pack(hit, version)
        elif instance is not None:
            self._download_mod(hit, version, instance)

    # ------------------------------------------------------------------ #
    def _show_placeholder(self, text: str) -> None:
        self._clear_results()
        empty = EmptyState("modpack", "搜索模组或整合包", text, "看看热门内容")
        empty.action.connect(lambda: (self._query.setText(""), self.search(reset=True)))
        self._results_layout.addWidget(empty)
        self._results_layout.addStretch(1)

    def _clear_results(self) -> None:
        from ..widgets import clear_layout

        clear_layout(self._results_layout)
        self._icon_targets.clear()
