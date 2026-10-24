"""页面基类：统一滚动容器、标题栏与主题刷新钩子。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from .. import theme
from ..context import AppContext
from ..glyphs import glyph_icon
from ..widgets import GlyphButton, label, make_button
from ..workers import TaskManager


class Page(QWidget):
    """所有页面共享的骨架：标题 + 说明 + 右侧操作区 + 可滚动内容。"""

    def __init__(self, ctx: AppContext, tasks: TaskManager, *, title: str, subtitle: str = "", parent=None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.tasks = tasks
        self._title = title
        self._subtitle = subtitle
        self._actions: list[QWidget] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(28, 22, 28, 18)
        outer.setSpacing(16)

        header = QHBoxLayout()
        header.setSpacing(12)
        text_box = QVBoxLayout()
        text_box.setSpacing(3)
        self._title_label = label(title, "title")
        self._subtitle_label = label(subtitle, "muted")
        text_box.addWidget(self._title_label)
        if subtitle:
            text_box.addWidget(self._subtitle_label)
        header.addLayout(text_box, 1)
        self._header_actions = QHBoxLayout()
        self._header_actions.setSpacing(8)
        header.addLayout(self._header_actions)
        outer.addLayout(header)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content = QWidget()
        self.content_layout = QVBoxLayout(self.content)
        self.content_layout.setContentsMargins(0, 2, 8, 8)
        self.content_layout.setSpacing(10)
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)

    # ------------------------------------------------------------------ #
    def set_subtitle(self, text: str) -> None:
        self._subtitle_label.setText(text)

    def add_header_action(self, widget: QWidget) -> QWidget:
        self._header_actions.addWidget(widget)
        self._actions.append(widget)
        return widget

    def clear_content(self) -> None:
        from ..widgets import clear_layout

        clear_layout(self.content_layout)

    # ------------------------------------------------------------------ #
    # 生命周期钩子
    # ------------------------------------------------------------------ #
    def on_show(self) -> None:
        """页面被切换到前台时调用。"""

    def on_theme_changed(self) -> None:
        """主题切换后刷新自绘图标。"""

    def refresh_icons(self) -> None:
        for widget in self.findChildren(QWidget):
            refresh = getattr(widget, "refresh_icon", None)
            if callable(refresh):
                try:
                    refresh()
                except Exception:  # noqa: BLE001 - 单个组件刷新失败不影响其它
                    pass
        for action in self._actions:
            refresh = getattr(action, "refresh_icon", None)
            if callable(refresh):
                refresh()


def section_title(text: str) -> QWidget:
    from ..widgets import label as _label

    return _label(text, "section")
