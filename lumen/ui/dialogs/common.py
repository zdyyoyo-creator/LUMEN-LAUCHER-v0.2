"""对话框公共件：统一的提示 / 确认 / 输入 / 问题列表，保证观感一致。"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from .. import theme
from ...core.preflight import Issue
from ...core.instances import Instance
from ..widgets import Badge, Card, GlyphWidget, Divider, label, make_button


def level_color(level: str) -> str:
    palette = theme.active()
    return {
        "info": palette.primary,
        "success": palette.success,
        "warning": palette.warning,
        "error": palette.danger,
        "danger": palette.danger,
    }.get(level, palette.primary)


def level_glyph(level: str) -> str:
    return {"info": "info", "success": "check", "warning": "warning", "error": "warning", "danger": "warning"}.get(
        level, "info"
    )


class BaseDialog(QDialog):
    """统一内边距与按钮排布，避免每个对话框各写一套。"""

    def __init__(self, parent: QWidget | None = None, *, title: str = "", width: int = 520) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(width)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(24, 22, 24, 18)
        self._layout.setSpacing(14)
        self._footer = QHBoxLayout()
        self._footer.setSpacing(10)
        self._footer.addStretch(1)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add_footer_button(self, button: QPushButton) -> QPushButton:
        button.setCursor(Qt.PointingHandCursor)
        self._footer.addWidget(button)
        return button

    def finish(self) -> None:
        self._layout.addSpacing(2)
        self._layout.addLayout(self._footer)


class MessageDialog(BaseDialog):
    """图标 + 标题 + 正文 +（可选）详情 + 按钮。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "提示",
        message: str = "",
        details: str = "",
        icon_kind: str | None = None,
        level: str = "info",
        ok_text: str = "好的",
        cancel_text: str | None = None,
        danger: bool = False,
    ) -> None:
        super().__init__(parent, title=title, width=460)
        header = QHBoxLayout()
        header.setSpacing(12)
        icon = GlyphWidget(icon_kind or level_glyph(level), 24, color=level_color(level), width=1.7)
        header.addWidget(icon, 0, Qt.AlignTop)
        text_box = QVBoxLayout()
        text_box.setSpacing(6)
        text_box.addWidget(label(title, "subtitle", wrap=True))
        if message:
            text_box.addWidget(label(message, "body", wrap=True))
        header.addLayout(text_box, 1)
        self._layout.addLayout(header)

        if details:
            view = QPlainTextEdit(details)
            view.setReadOnly(True)
            view.setMaximumHeight(180)
            view.setMinimumHeight(90)
            self._layout.addWidget(view)

        cancel = None
        if cancel_text:
            cancel = self.add_footer_button(make_button(cancel_text, handler=self.reject))
        ok_button = self.add_footer_button(
            make_button(ok_text, role="danger" if danger else "primary", handler=self.accept)
        )
        ok_button.setDefault(True)
        self.finish()

    # ------------------------------------------------------------------ #
    @classmethod
    def tell(cls, parent, title: str, message: str, *, details: str = "", level: str = "info") -> None:
        cls(parent, title=title, message=message, details=details, level=level).exec()

    @classmethod
    def ask(
        cls,
        parent,
        title: str,
        message: str,
        *,
        ok_text: str = "确定",
        cancel_text: str = "取消",
        danger: bool = False,
        level: str = "info",
    ) -> bool:
        dialog = cls(
            parent,
            title=title,
            message=message,
            ok_text=ok_text,
            cancel_text=cancel_text,
            danger=danger,
            level=level,
        )
        return dialog.exec() == QDialog.Accepted


class TextInputDialog(BaseDialog):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "输入",
        label_text: str = "",
        value: str = "",
        placeholder: str = "",
        ok_text: str = "确定",
        description: str = "",
    ) -> None:
        super().__init__(parent, title=title, width=420)
        if label_text:
            self._layout.addWidget(label(label_text, "body"))
        if description:
            self._layout.addWidget(label(description, "muted", wrap=True))
        self._edit = QLineEdit(value)
        self._edit.setPlaceholderText(placeholder)
        self._edit.selectAll()
        self._layout.addWidget(self._edit)
        self.add_footer_button(make_button("取消", handler=self.reject))
        ok_button = self.add_footer_button(make_button(ok_text, role="primary", handler=self.accept))
        ok_button.setDefault(True)
        self.finish()
        self._edit.returnPressed.connect(self.accept)

    def value(self) -> str:
        return self._edit.text().strip()

    @classmethod
    def ask(
        cls,
        parent,
        *,
        title: str,
        label_text: str,
        value: str = "",
        placeholder: str = "",
        description: str = "",
    ) -> str | None:
        dialog = cls(
            parent,
            title=title,
            label_text=label_text,
            value=value,
            placeholder=placeholder,
            description=description,
        )
        if dialog.exec() == QDialog.Accepted:
            return dialog.value()
        return None


class IssueDialog(BaseDialog):
    """启动前自检结果：按级别列出问题，必要时允许「仍然启动」。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        issues: Iterable[Issue],
        title: str = "启动前检查",
        allow_continue: bool = False,
        summary: str = "",
    ) -> None:
        super().__init__(parent, title=title, width=560)
        issue_list = list(issues)
        errors = [item for item in issue_list if item.is_error]
        warnings = [item for item in issue_list if item.level == "warning"]

        header = QHBoxLayout()
        header.setSpacing(12)
        if errors:
            icon = GlyphWidget("warning", 24, color=level_color("error"), width=1.7)
            headline = "发现问题，暂时无法启动"
        elif warnings:
            icon = GlyphWidget("warning", 24, color=level_color("warning"), width=1.7)
            headline = "有几点需要注意"
        else:
            icon = GlyphWidget("check", 24, color=level_color("success"), width=1.7)
            headline = "检查通过"
        header.addWidget(icon, 0, Qt.AlignTop)
        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        text_box.addWidget(label(headline, "subtitle"))
        if summary:
            text_box.addWidget(label(summary, "muted", wrap=True))
        header.addLayout(text_box, 1)
        self._layout.addLayout(header)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setMaximumHeight(320)
        container = QWidget()
        items_layout = QVBoxLayout(container)
        items_layout.setContentsMargins(0, 0, 6, 0)
        items_layout.setSpacing(8)
        for issue in issue_list:
            card = Card(margins=(12, 10, 12, 10), spacing=4, flat=True)
            head = QHBoxLayout()
            head.setSpacing(8)
            head.addWidget(GlyphWidget(level_glyph(issue.level), 16, color=level_color(issue.level), width=1.7))
            head.addWidget(label(issue.title, "body", wrap=True), 1)
            head.addWidget(Badge("错误" if issue.is_error else "提示", "danger" if issue.is_error else "warning"))
            card.body().addLayout(head)
            if issue.detail:
                card.body().addWidget(label(issue.detail, "muted", wrap=True))
            items_layout.addWidget(card)
        items_layout.addStretch(1)
        scroll.setWidget(container)
        self._layout.addWidget(scroll)

        self._continue = False
        self.add_footer_button(make_button("取消", handler=self.reject))
        if allow_continue or not errors:
            ok_text = "仍然启动" if errors else "开始游戏"
            ok_button = self.add_footer_button(make_button(ok_text, role="primary", handler=self._accept_continue))
            ok_button.setDefault(True)
        else:
            ok_button = self.add_footer_button(make_button("知道了", role="primary", handler=self.reject))
            ok_button.setDefault(True)
        self.finish()

    def _accept_continue(self) -> None:
        self._continue = True
        self.accept()

    @property
    def continue_requested(self) -> bool:
        return self._continue


class InstancePickerDialog(BaseDialog):
    """让用户选择「装到哪个实例」。"""

    def __init__(self, parent: QWidget | None = None, *, instances: list[Instance], title: str = "选择实例") -> None:
        super().__init__(parent, title=title, width=460)
        self._instances = instances
        self._selected: Instance | None = None
        self._layout.addWidget(label("请选择要安装到的实例：", "body"))

        self._list = QListWidget()
        self._list.setMinimumHeight(220)
        for instance in instances:
            item = QListWidgetItem(f"{instance.name}   ·   {instance.mc_version} · {instance.display_loader}")
            item.setData(Qt.UserRole, instance.id)
            self._list.addItem(item)
        if instances:
            self._list.setCurrentRow(0)
        self._layout.addWidget(self._list)

        self.add_footer_button(make_button("取消", handler=self.reject))
        ok_button = self.add_footer_button(make_button("安装到此实例", role="primary", handler=self._accept))
        ok_button.setDefault(True)
        self.finish()
        self._list.itemDoubleClicked.connect(lambda _item: self._accept())

    def _accept(self) -> None:
        row = self._list.currentRow()
        if 0 <= row < len(self._instances):
            self._selected = self._instances[row]
            self.accept()

    def selected_instance(self) -> Instance | None:
        return self._selected


class ConfirmDeleteDialog(BaseDialog):
    """危险操作确认，要求输入实例名以防误删。"""

    def __init__(self, parent: QWidget | None, *, instance: Instance) -> None:
        super().__init__(parent, title="删除实例", width=440)
        self._instance = instance
        self._layout.addWidget(
            label(f"即将删除实例「{instance.name}」及其全部游戏文件（mods、存档、配置）。", "body", wrap=True)
        )
        self._layout.addWidget(label("这个操作不可撤销。请输入实例名称以确认：", "muted", wrap=True))
        self._edit = QLineEdit()
        self._edit.setPlaceholderText(instance.name)
        self._layout.addWidget(self._edit)
        self._ok = self.add_footer_button(make_button("永久删除", role="danger", handler=self._accept))
        self._ok.setEnabled(False)
        self.add_footer_button(make_button("取消", handler=self.reject))
        self.finish()
        self._edit.textChanged.connect(self._on_changed)

    def _on_changed(self, text: str) -> None:
        self._ok.setEnabled(text.strip() == self._instance.name)

    def _accept(self) -> None:
        if self._edit.text().strip() == self._instance.name:
            self.accept()


class DetailDialog(BaseDialog):
    """长文本查看（崩溃报告 / 完整日志）。"""

    def __init__(self, parent: QWidget | None = None, *, title: str = "详情", text: str = "", width: int = 720) -> None:
        super().__init__(parent, title=title, width=width)
        view = QPlainTextEdit(text)
        view.setReadOnly(True)
        view.setMinimumHeight(420)
        view.setStyleSheet("font-family: Consolas, 'Cascadia Code', monospace; font-size: 12px;")
        self._layout.addWidget(view, 1)
        self.add_footer_button(make_button("关闭", handler=self.accept))
        self.finish()
