"""设置页：下载源 / Java / 内存 / 外观 / 账号 / 整合包 / 关于。

所有改动即改即存（JSON 落盘），不需要「保存」按钮。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLineEdit,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ... import APP_DISPLAY_NAME, APP_VERSION
from ...utils.fsutil import human_size
from ...utils.sysinfo import machine_summary
from ..context import AppContext
from ..dialogs.common import MessageDialog
from ..dialogs.java_dialog import JavaDialog
from ..widgets import (
    Badge,
    Card,
    Segmented,
    SettingRow,
    label,
    make_button,
)
from ..workers import TaskManager, Worker
from .base import Page


class SettingsPage(Page):
    def __init__(self, ctx: AppContext, tasks: TaskManager, parent: QWidget | None = None) -> None:
        super().__init__(parent=parent, ctx=ctx, tasks=tasks, title="设置", subtitle="下载源、Java、内存与外观")
        self._speed_worker: Worker | None = None
        self._build_download()
        self._build_java()
        self._build_performance()
        self._build_appearance()
        self._build_account()
        self._build_modpack()
        self._build_about()
        self.content_layout.addStretch(1)

    # ------------------------------------------------------------------ #
    def _card(self, title_text: str, description: str = "") -> Card:
        card = Card(margins=(18, 16, 18, 16), spacing=4)
        head = QVBoxLayout()
        head.setSpacing(2)
        head.addWidget(label(title_text, "subtitle"))
        if description:
            head.addWidget(label(description, "muted", wrap=True))
        card.body().addLayout(head)
        card.body().addSpacing(4)
        self.content_layout.addWidget(card)
        return card

    # ------------------------------------------------------------------ #
    def _build_download(self) -> None:
        card = self._card("下载", "官方源与 BMCLAPI 镜像并存，可自动测速择优，失败会自动回退到另一个源。")

        self._source = Segmented(
            [("auto", "自动选择"), ("official", "官方源"), ("mirror", "镜像源")],
            current=self.ctx.settings.download_source,
        )
        self._source.changed.connect(self._on_source_changed)
        card.body().addWidget(SettingRow("下载源策略", "自动：按实时测速结果选择更快的源", self._source))

        speed_row = QWidget()
        speed_layout = QHBoxLayout(speed_row)
        speed_layout.setContentsMargins(0, 0, 0, 0)
        speed_layout.setSpacing(8)
        self._speed_label = label("尚未测速", "muted")
        speed_layout.addWidget(self._speed_label)
        self._speed_button = make_button("测速", kind="spark", handler=self._run_speedtest)
        speed_layout.addWidget(self._speed_button)
        card.body().addWidget(SettingRow("源延迟与吞吐", "对官方与镜像各采样一个小文件", speed_row))

        self._endpoint = QLineEdit(str(self.ctx.settings.get("mirror_endpoint") or ""))
        self._endpoint.setMinimumWidth(320)
        self._endpoint.editingFinished.connect(self._on_endpoint_changed)
        card.body().addWidget(SettingRow("镜像根地址", "BMCLAPI 兼容服务地址，留空使用默认", self._endpoint))

        self._workers_spin = QSpinBox()
        self._workers_spin.setRange(1, 32)
        self._workers_spin.setValue(self.ctx.settings.max_workers)
        self._workers_spin.valueChanged.connect(lambda value: self.ctx.settings.set("max_workers", int(value)))
        card.body().addWidget(SettingRow("并发下载数", "文件级并发，网络较差时建议 8 以下", self._workers_spin))

    def _on_source_changed(self, policy: str) -> None:
        self.ctx.settings.download_source = policy
        pretty = {"auto": "自动选择", "official": "官方源", "mirror": "镜像源"}.get(policy, policy)
        self.ctx.toast.emit(f"下载源：{pretty}")

    def _on_endpoint_changed(self) -> None:
        value = self._endpoint.text().strip()
        self.ctx.settings.set("mirror_endpoint", value or "https://bmclapi2.bangbang93.com")
        self.ctx.toast.emit("镜像地址已保存")

    def _run_speedtest(self) -> None:
        if self._speed_worker is not None and self._speed_worker.isRunning():
            return
        self._speed_label.setText("正在测速…")
        self._speed_button.setEnabled(False)
        worker = Worker(lambda **_: self.ctx.selector.probe_all(force=True), label="镜像测速")
        worker.succeeded.connect(self._on_speedtest)
        worker.failed.connect(self._on_speedtest_failed)
        self._speed_worker = self.tasks.start(worker)

    def _on_speedtest(self, probes) -> None:
        self._speed_button.setEnabled(True)
        self._speed_label.setText(self.ctx.selector.describe_sources())
        from ...core.mirrors import SOURCE_LABELS

        chosen = self.ctx.selector.preferred_source()
        self.ctx.toast.emit(f"当前优选：{SOURCE_LABELS.get(chosen, chosen)}")

    def _on_speedtest_failed(self, message: str, hint: str) -> None:
        self._speed_button.setEnabled(True)
        self._speed_label.setText("测速失败")
        MessageDialog.tell(self, "测速失败", message, details=hint, level="warning")

    # ------------------------------------------------------------------ #
    def _build_java(self) -> None:
        card = self._card("Java", "按游戏版本自动匹配；缺 Java 时可一键下载 Mojang 官方运行时。")

        self._java_status = label("", "muted")
        status_box = QWidget()
        status_layout = QHBoxLayout(status_box)
        status_layout.setContentsMargins(0, 0, 0, 0)
        status_layout.setSpacing(8)
        status_layout.addWidget(self._java_status)
        manage = make_button("管理 Java", kind="java", handler=self._open_java_dialog)
        status_layout.addWidget(manage)
        card.body().addWidget(SettingRow("已发现的 Java", "双击可设为全局 Java", status_box))

        self._auto_java = QCheckBox("缺少 Java 时自动下载")
        self._auto_java.setChecked(bool(self.ctx.settings.get("java_auto_download", True)))
        self._auto_java.toggled.connect(lambda value: self.ctx.settings.set("java_auto_download", bool(value)))
        card.body().addWidget(self._auto_java)
        self._refresh_java_status()

    def _refresh_java_status(self) -> None:
        runtimes = self.ctx.runtimes
        if not runtimes:
            self._java_status.setText("尚未扫描或本机没有 Java")
        else:
            current = self.ctx.settings.java_path
            chosen = next((item for item in runtimes if str(item.path) == current), None)
            if chosen is None:
                chosen = runtimes[0]
            self._java_status.setText(f"{len(runtimes)} 个 · 默认 Java {chosen.major}（{chosen.vendor or '未知'}）")

    def _open_java_dialog(self) -> None:
        dialog = JavaDialog(self, ctx=self.ctx, tasks=self.tasks)
        dialog.exec()
        if dialog.selected_path:
            self.ctx.settings.java_path = dialog.selected_path
            self._refresh_java_status()
            self.ctx.toast.emit("已设为全局 Java")

    # ------------------------------------------------------------------ #
    def _build_performance(self) -> None:
        card = self._card("性能", "内存按物理内存的推荐值预填，可在实例里单独覆盖。")
        total_gb, recommended = self.ctx.memory_info()
        max_gb = max(2, min(32, int(total_gb) - 1 if total_gb > 2 else 8))

        memory_row = QWidget()
        memory_layout = QHBoxLayout(memory_row)
        memory_layout.setContentsMargins(0, 0, 0, 0)
        memory_layout.setSpacing(10)
        self._memory_slider = QSlider(Qt.Horizontal)
        self._memory_slider.setRange(1, max_gb)
        self._memory_slider.setValue(max(1, min(max_gb, self.ctx.settings.memory_gb)))
        self._memory_slider.setFixedWidth(220)
        self._memory_value = label(f"{self._memory_slider.value()} GB", "body")
        memory_layout.addWidget(self._memory_slider)
        memory_layout.addWidget(self._memory_value)
        self._memory_slider.valueChanged.connect(self._on_memory_changed)
        card.body().addWidget(
            SettingRow(
                "默认内存分配",
                f"物理内存 {total_gb:.1f} GB，推荐 {recommended} GB",
                memory_row,
            )
        )

        self._jvm_args = QLineEdit(self.ctx.settings.jvm_args)
        self._jvm_args.setPlaceholderText("-XX:+UseG1GC")
        self._jvm_args.setMinimumWidth(320)
        self._jvm_args.editingFinished.connect(self._on_jvm_args_changed)
        card.body().addWidget(SettingRow("全局 JVM 参数", "实例可单独覆盖；支持引号包裹带空格的参数", self._jvm_args))

    def _on_memory_changed(self, value: int) -> None:
        self._memory_value.setText(f"{value} GB")
        self.ctx.settings.memory_gb = value
        self.ctx.instances_changed.emit()

    def _on_jvm_args_changed(self) -> None:
        self.ctx.settings.jvm_args = self._jvm_args.text().strip()
        self.ctx.toast.emit("JVM 参数已保存")

    # ------------------------------------------------------------------ #
    def _build_appearance(self) -> None:
        card = self._card("外观", "浅色 / 深色主题即时切换，所有页面同步生效。")
        self._theme = Segmented([("dark", "深色"), ("light", "浅色")], current=self.ctx.current_theme())
        self._theme.changed.connect(self._on_theme_changed)
        card.body().addWidget(SettingRow("主题", "默认深色，夜间更柔和", self._theme))

    def _on_theme_changed(self, name: str) -> None:
        self.ctx.apply_theme(name)

    # ------------------------------------------------------------------ #
    def _build_account(self) -> None:
        card = self._card("账号", "离线账号可进入离线服务器；微软正版登录为预留接口。")
        account = self.ctx.account

        name_row = QWidget()
        name_layout = QHBoxLayout(name_row)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(8)
        self._username = QLineEdit(account.username)
        self._username.setMinimumWidth(220)
        self._username.setMaxLength(16)
        name_layout.addWidget(self._username)
        apply_button = make_button("应用", role="primary", handler=self._apply_username)
        name_layout.addWidget(apply_button)
        card.body().addWidget(SettingRow("离线用户名", "3–16 位，字母数字下划线", name_row))

        uuid_row = QHBoxLayout()
        uuid_row.setSpacing(8)
        uuid_row.addWidget(label(f"UUID：{account.uuid}", "muted"))
        uuid_row.addStretch(1)
        account_badge = Badge("离线账号" if account.is_offline else "微软正版", "muted")
        uuid_row.addWidget(account_badge)
        card.body().addLayout(uuid_row)

        microsoft_row = QHBoxLayout()
        microsoft_row.setSpacing(8)
        microsoft_row.addWidget(label("微软正版登录（预留）", "body"))
        microsoft_row.addStretch(1)
        ms_button = make_button("了解详情", kind="external", handler=self._show_microsoft_info)
        microsoft_row.addWidget(ms_button)
        card.body().addLayout(microsoft_row)

    def _apply_username(self) -> None:
        problem = self.ctx.set_account_username(self._username.text())
        if problem:
            MessageDialog.tell(self, "用户名不合法", problem, level="warning")
            return
        self.ctx.toast.emit(f"离线账号已切换为 {self.ctx.account.username}")
        self.ctx.instances_changed.emit()

    def _show_microsoft_info(self) -> None:
        MessageDialog.tell(
            self,
            "微软正版登录",
            "设备码授权流程的接口已实现（请求设备码 + 轮询令牌），"
            "但 Xbox Live / Minecraft 换票链路需要你在 Azure 注册应用并填入 client_id 后扩展。",
            details=(
                "当前版本推荐使用离线账号。\n"
                "扩展位置：lumen/auth/microsoft.py 的 complete_sign_in()。"
            ),
            level="info",
        )

    # ------------------------------------------------------------------ #
    def _build_modpack(self) -> None:
        card = self._card("整合包", "CurseForge 的 manifest 只含 projectID/fileID，需要 API Key 才能换取下载直链。")
        self._cf_key = QLineEdit(str(self.ctx.settings.get("cf_api_key") or ""))
        self._cf_key.setEchoMode(QLineEdit.Password)
        self._cf_key.setPlaceholderText("$2a$10$...（来自 CurseForge 开发者控制台）")
        self._cf_key.setMinimumWidth(320)
        self._cf_key.editingFinished.connect(self._save_cf_key)
        card.body().addWidget(SettingRow("CurseForge API Key", "留空时仍可导入，但 mods 需要手动下载", self._cf_key))

    def _save_cf_key(self) -> None:
        self.ctx.settings.set("cf_api_key", self._cf_key.text().strip())
        self.ctx.toast.emit("API Key 已保存")

    # ------------------------------------------------------------------ #
    def _build_about(self) -> None:
        card = self._card("关于", "Lumen Launcher 是一款轻量的 Minecraft Java 版第三方启动器。")
        info = machine_summary()
        card.body().addWidget(
            label(
                f"{APP_DISPLAY_NAME} v{APP_VERSION} · 数据目录：{self.ctx.paths.data_root}",
                "muted",
                wrap=True,
            )
        )
        card.body().addWidget(
            label(f"系统：{info['os']} · {info['arch']} · Python {info['python']} · 内存 {info['memory_gb']} GB", "muted")
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(make_button("打开数据目录", kind="folder", handler=lambda: self.ctx.open_path(self.ctx.paths.data_root)))
        row.addWidget(make_button("启动器日志", kind="log", handler=self._open_logs))
        row.addStretch(1)
        card.body().addLayout(row)

    def _open_logs(self) -> None:
        from ..dialogs.log_dialog import LogDialog

        LogDialog(self, ctx=self.ctx, tasks=self.tasks).exec()

    # ------------------------------------------------------------------ #
    def on_show(self) -> None:
        self._refresh_java_status()
        self._theme.set_current(self.ctx.current_theme())
        self._source.set_current(self.ctx.settings.download_source)
