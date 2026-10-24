"""实例相关对话框：新建实例、编辑实例参数。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from ...utils.errors import LumenError
from ...utils.sysinfo import physical_memory_gb
from ...utils.versions import required_java_major
from ..context import AppContext
from ..dialogs.common import BaseDialog, MessageDialog
from ..widgets import Segmented, label, make_button


class InstanceCreateDialog(BaseDialog):
    """新建实例：名称 + Minecraft 版本 + 加载器。创建后由调用方决定是否立即安装。"""

    def __init__(self, parent: QWidget | None, *, ctx: AppContext) -> None:
        super().__init__(parent, title="新建实例", width=520)
        self.ctx = ctx
        self._result: tuple[str, str, str] | None = None

        self._layout.addWidget(label("每个实例都有独立的游戏目录，mods / 存档 / 配置互不影响。", "muted", wrap=True))

        name_row = QHBoxLayout()
        name_row.setSpacing(10)
        name_row.addWidget(label("实例名称", "body"))
        name_row.addStretch(1)
        self._name = QLineEdit()
        self._name.setPlaceholderText("例如：冒险生存")
        self._name.setMinimumWidth(260)
        name_row.addWidget(self._name)
        self._layout.addLayout(name_row)

        version_row = QHBoxLayout()
        version_row.setSpacing(10)
        version_row.addWidget(label("Minecraft 版本", "body"))
        version_row.addStretch(1)
        self._version = QComboBox()
        self._version.setEditable(True)
        self._version.setMinimumWidth(260)
        self._fill_versions()
        version_row.addWidget(self._version)
        self._layout.addLayout(version_row)

        loader_row = QHBoxLayout()
        loader_row.setSpacing(10)
        loader_row.addWidget(label("加载器", "body"))
        loader_row.addStretch(1)
        self._loader = Segmented(
            [
                ("vanilla", "原版"),
                ("fabric", "Fabric"),
                ("quilt", "Quilt"),
                ("forge", "Forge"),
                ("neoforge", "NeoForge"),
            ],
            current="vanilla",
        )
        self._loader.changed.connect(self._on_loader_changed)
        loader_row.addWidget(self._loader)
        self._layout.addLayout(loader_row)

        self._hint = label("创建后如果该版本尚未安装，启动器会引导你完成下载。", "muted", wrap=True)
        self._layout.addWidget(self._hint)

        self.add_footer_button(make_button("取消", handler=self.reject))
        ok = self.add_footer_button(make_button("创建实例", role="primary", handler=self._accept))
        ok.setDefault(True)
        self.finish()
        self._name.setFocus()

    def _on_loader_changed(self, kind: str) -> None:
        if kind in ("forge", "neoforge"):
            self._hint.setText(
                "Forge/NeoForge 需要运行官方安装器，创建后会打开安装向导（需要本机 Java 17/21）。"
            )
        elif kind in ("fabric", "quilt"):
            self._hint.setText("Fabric/Quilt 通过 meta API 直接生成版本，安装速度快。")
        else:
            self._hint.setText("创建后如果该版本尚未安装，启动器会引导你完成下载。")

    def _fill_versions(self) -> None:
        manifest = self.ctx.manifest
        if manifest is not None:
            for version in manifest.filtered()[:60]:
                self._version.addItem(version.id, version.id)
            return
        for version_id in ("1.21.4", "1.21.1", "1.20.6", "1.20.1", "1.19.4", "1.18.2", "1.16.5", "1.12.2", "1.8.9"):
            self._version.addItem(version_id, version_id)

    def _accept(self) -> None:
        name = self._name.text().strip() or f"{self._version.currentText().strip()} 实例"
        version = self._version.currentText().strip()
        if not version:
            MessageDialog.tell(self, "缺少版本", "请填写或选择一个 Minecraft 版本。", level="warning")
            return
        self._result = (name, version, self._loader.current())
        self.accept()

    def values(self) -> tuple[str, str, str] | None:
        return self._result


class InstanceEditDialog(BaseDialog):
    """编辑实例：内存、Java、JVM 参数。"""

    def __init__(self, parent: QWidget | None, *, ctx: AppContext, instance) -> None:
        super().__init__(parent, title=f"编辑实例 · {instance.name}", width=540)
        self.ctx = ctx
        self.instance = instance

        self._layout.addWidget(label(f"{instance.mc_version} · {instance.display_loader}", "muted"))

        memory_row = QHBoxLayout()
        memory_row.setSpacing(10)
        memory_row.addWidget(label("分配内存", "body"))
        self._memory_label = label("", "muted")
        memory_row.addWidget(self._memory_label)
        memory_row.addStretch(1)
        self._layout.addLayout(memory_row)

        total_gb = physical_memory_gb()
        max_gb = max(2, min(32, int(total_gb) - 1 if total_gb > 2 else 8))
        self._slider = QSlider(Qt.Horizontal)
        self._slider.setRange(1, max_gb)
        current = int(instance.memory_gb or ctx.settings.memory_gb)
        self._slider.setValue(max(1, min(max_gb, current)))
        self._slider.valueChanged.connect(self._on_memory_changed)
        self._layout.addWidget(self._slider)
        self._on_memory_changed(self._slider.value())

        java_row = QHBoxLayout()
        java_row.setSpacing(10)
        java_row.addWidget(label("Java", "body"))
        java_row.addStretch(1)
        self._java = QComboBox()
        self._java.setMinimumWidth(300)
        self._java.addItem("跟随全局设置", "")
        for runtime in ctx.runtimes:
            self._java.addItem(f"Java {runtime.major} · {runtime.path}", str(runtime.path))
        if instance.java_path:
            index = self._java.findData(instance.java_path)
            if index >= 0:
                self._java.setCurrentIndex(index)
            else:
                self._java.addItem(f"自定义 · {instance.java_path}", instance.java_path)
                self._java.setCurrentIndex(self._java.count() - 1)
        java_row.addWidget(self._java)
        self._layout.addLayout(java_row)

        self._layout.addWidget(label("JVM 参数（留空则使用全局设置）", "body"))
        self._jvm = QLineEdit(instance.jvm_args or "")
        self._jvm.setPlaceholderText("-XX:+UseG1GC -Dfile.encoding=UTF-8")
        self._layout.addWidget(self._jvm)

        required = required_java_major(instance.mc_version)
        self._layout.addWidget(label(f"该版本建议 Java {required}", "muted"))

        self.add_footer_button(make_button("取消", handler=self.reject))
        ok = self.add_footer_button(make_button("保存", role="primary", handler=self._accept))
        ok.setDefault(True)
        self.finish()

    def _on_memory_changed(self, value: int) -> None:
        self._memory_label.setText(f"{value} GB")

    def _accept(self) -> None:
        try:
            self.instance.memory_gb = self._slider.value()
            self.instance.java_path = str(self._java.currentData() or "")
            self.instance.jvm_args = self._jvm.text().strip()
            self.instance.save()
        except LumenError as exc:
            MessageDialog.tell(self, "保存失败", str(exc), level="error")
            return
        self.accept()
