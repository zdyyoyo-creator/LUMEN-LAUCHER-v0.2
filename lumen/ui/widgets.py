"""通用 UI 组件：卡片、徽章、环形进度、骨架屏、空状态、分段控件、Toast。

这些组件只负责「样子」和少量交互，不掺业务逻辑；
颜色一律从 ``theme.active()`` 取，切换主题后调用 ``refresh_icon`` / ``update`` 即可。
"""

from __future__ import annotations

import math
from typing import Callable, Iterable, Sequence

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import theme
from .glyphs import glyph_icon, paint_glyph


def restyle(widget: QWidget) -> None:
    """属性变化后重算 QSS（Qt 不会自动刷新属性选择器）。"""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #
def label(text: str = "", role: str = "body", *, wrap: bool = False) -> QLabel:
    widget = QLabel(text)
    widget.setProperty("role", role)
    if wrap:
        widget.setWordWrap(True)
    widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return widget


def title(text: str) -> QLabel:
    return label(text, "title")


def subtitle(text: str) -> QLabel:
    return label(text, "subtitle")


def muted(text: str = "") -> QLabel:
    return label(text, "muted")


# --------------------------------------------------------------------------- #
# 布局小工具
# --------------------------------------------------------------------------- #
def hbox(*widgets: QWidget, spacing: int = 8, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        elif isinstance(widget, int):
            layout.addStretch(widget)
        else:
            layout.addWidget(widget)
    return layout


def vbox(*widgets: QWidget, spacing: int = 8, margins: tuple[int, int, int, int] = (0, 0, 0, 0)) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setContentsMargins(*margins)
    layout.setSpacing(spacing)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        elif isinstance(widget, int):
            layout.addStretch(widget)
        else:
            layout.addWidget(widget)
    return layout


# --------------------------------------------------------------------------- #
# 基础组件
# --------------------------------------------------------------------------- #
class Card(QFrame):
    """卡片容器：圆角 8px + 1px 描边。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        margins: tuple[int, int, int, int] = (16, 14, 16, 14),
        spacing: int = 8,
        flat: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("cardFlat" if flat else "card")
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(*margins)
        self._layout.setSpacing(spacing)

    def body(self) -> QVBoxLayout:
        return self._layout

    def add(self, widget: QWidget, stretch: int = 0) -> QWidget:
        self._layout.addWidget(widget, stretch)
        return widget


class Divider(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("divider")
        self.setFixedHeight(1)


class Badge(QLabel):
    """语义徽章：Release 绿 / Snapshot 黄 / Old 灰 / 错误红。"""

    def __init__(self, text: str = "", kind: str = "muted", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignCenter)
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.set_kind(kind)

    def set_kind(self, kind: str) -> None:
        self.setProperty("badge", kind)
        restyle(self)

    def set_text(self, text: str) -> None:
        self.setText(text)


class GlyphWidget(QWidget):
    """单独一个线性图标（用于空状态/提示条）。"""

    def __init__(
        self,
        kind: str,
        size: int = 22,
        color: str | None = None,
        width: float = 1.6,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._color = color
        self._width = width
        self.setFixedSize(size, size)

    def set_kind(self, kind: str) -> None:
        self._kind = kind
        self.update()

    def set_color(self, color: str | None) -> None:
        self._color = color
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(self._color or theme.active().text_muted)
        paint_glyph(painter, self._kind, QRectF(0, 0, self.width(), self.height()), color, self._width)
        painter.end()


class RingProgress(QWidget):
    """环形进度：用于安装 / 下载 / 导入的「主要进度」展示。"""

    def __init__(self, size: int = 84, thickness: int = 6, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._size = size
        self._thickness = thickness
        self._value = 0.0
        self._text = "0%"
        self._indeterminate = False
        self._phase = 0.0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.setInterval(40)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        if not self._indeterminate:
            return
        self._phase = (self._phase + 6.0) % 360.0
        self.update()

    def set_value(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._value) < 0.001:
            return
        self._value = value
        self._text = f"{int(round(value * 100))}%"
        self.update()

    def set_text(self, text: str) -> None:
        self._text = text
        self.update()

    def set_indeterminate(self, enabled: bool) -> None:
        self._indeterminate = enabled
        if enabled:
            self._text = "…"
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        palette = theme.active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        margin = self._thickness / 2 + 2
        rect = QRectF(margin, margin, self.width() - 2 * margin, self.height() - 2 * margin)

        track = QPen(QColor(palette.border), self._thickness, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(track)
        painter.drawArc(rect, 0, 360 * 16)

        accent = QPen(QColor(palette.primary), self._thickness, Qt.SolidLine, Qt.RoundCap)
        painter.setPen(accent)
        if self._indeterminate:
            painter.drawArc(rect, int((90 - self._phase) * 16), int(-110 * 16))
        elif self._value > 0:
            painter.drawArc(rect, 90 * 16, int(-360 * 16 * self._value))

        font = QFont(self.font())
        font.setPixelSize(max(11, int(self.width() * 0.2)))
        font.setWeight(QFont.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(palette.text))
        painter.drawText(QRectF(0, 0, self.width(), self.height()), Qt.AlignCenter, self._text)
        painter.end()


class SkeletonList(QWidget):
    """加载占位：几条会呼吸的圆角灰条（禁止花哨动效，只做轻微起伏）。"""

    def __init__(
        self,
        rows: int = 4,
        *,
        row_height: int = 64,
        spacing: int = 10,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._rows = max(1, rows)
        self._row_height = row_height
        self._spacing = spacing
        self._phase = 0.0
        self.setMinimumHeight(self._rows * row_height + (self._rows - 1) * spacing)
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _tick(self) -> None:
        self._phase = (self._phase + 0.08) % (2 * math.pi)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        palette = theme.active()
        alpha = 0.35 + 0.20 * (0.5 + 0.5 * math.sin(self._phase))
        color = QColor(palette.surface_alt)
        color.setAlphaF(alpha)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        width = self.width()
        y = 0.0
        for _ in range(self._rows):
            painter.drawRoundedRect(QRectF(0, y, width, self._row_height), 8.0, 8.0)
            y += self._row_height + self._spacing
        painter.end()


class EmptyState(QWidget):
    """空状态：线性图标 + 一句话说明 + 一个主操作。"""

    action = Signal()

    def __init__(
        self,
        kind: str,
        title_text: str,
        description: str,
        action_text: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 40, 24, 40)
        layout.setSpacing(10)
        layout.addStretch(1)

        icon = GlyphWidget(kind, 46, width=1.3)
        icon.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        layout.addWidget(icon, 0, Qt.AlignHCenter)

        heading = QLabel(title_text)
        heading.setProperty("role", "subtitle")
        heading.setAlignment(Qt.AlignCenter)
        layout.addWidget(heading, 0, Qt.AlignHCenter)

        description_label = label(description, "muted")
        description_label.setAlignment(Qt.AlignCenter)
        description_label.setWordWrap(True)
        description_label.setFixedWidth(430)
        description_label.setMinimumHeight(description_label.fontMetrics().lineSpacing() * 2 + 4)
        layout.addWidget(description_label, 0, Qt.AlignHCenter)

        if action_text:
            button = QPushButton(action_text)
            button.setProperty("role", "primary")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(self.action.emit)
            layout.addSpacing(8)
            layout.addWidget(button, 0, Qt.AlignHCenter)
        layout.addStretch(1)


class Segmented(QWidget):
    """分段控件：自动 / 官方 / 镜像 这类互斥选项。"""

    changed = Signal(str)

    def __init__(
        self,
        options: Sequence[tuple[str, str]],
        *,
        current: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("segmented")
        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3, 3, 3, 3)
        layout.setSpacing(2)
        for value, text in options:
            button = QPushButton(text)
            button.setCheckable(True)
            button.setProperty("seg", "true")
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, v=value: self._on_click(v))
            self._group.addButton(button)
            self._buttons[value] = button
            layout.addWidget(button)
        self._current = current or (options[0][0] if options else "")
        self.set_current(self._current, emit=False)

    def _on_click(self, value: str) -> None:
        if value == self._current:
            return
        self._current = value
        self.changed.emit(value)

    def set_current(self, value: str, *, emit: bool = False) -> None:
        button = self._buttons.get(value) or next(iter(self._buttons.values()), None)
        if button is None:
            return
        self._current = value
        button.setChecked(True)
        if emit:
            self.changed.emit(value)

    def current(self) -> str:
        return self._current


class NavButton(QToolButton):
    """左侧导航按钮：图标在上、文字在下。"""

    def __init__(self, kind: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._kind = kind
        self.setText(text)
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setIconSize(QSize(22, 22))
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("nav", "true")
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.refresh_icon()

    def refresh_icon(self) -> None:
        palette = theme.active()
        color = palette.primary if self.isChecked() else palette.text_muted
        self.setIcon(glyph_icon(self._kind, 22, color, 1.5))

    def setChecked(self, checked: bool) -> None:  # noqa: N802
        super().setChecked(checked)
        self.refresh_icon()


class GlyphButton(QPushButton):
    """带线性图标的文字按钮，主题切换后可自刷新图标颜色。"""

    def __init__(
        self,
        text: str,
        kind: str | None = None,
        *,
        role: str | None = None,
        icon_color: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self._kind = kind
        self._icon_color = icon_color
        if role:
            self.setProperty("role", role)
        self.setCursor(Qt.PointingHandCursor)
        self.setIconSize(QSize(16, 16))
        self.refresh_icon()

    def refresh_icon(self) -> None:
        if not self._kind:
            return
        palette = theme.active()
        if self._icon_color:
            color = self._icon_color
        elif self.property("role") == "primary":
            color = "#FFFFFF"
        else:
            color = palette.text_muted
        self.setIcon(glyph_icon(self._kind, 16, color, 1.6))


class ToolGlyphButton(QToolButton):
    """纯图标按钮（工具栏/卡片右上角）。"""

    def __init__(
        self,
        kind: str,
        tooltip: str = "",
        *,
        size: int = 32,
        icon_size: int = 18,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._kind = kind
        self._icon_size = icon_size
        if tooltip:
            self.setToolTip(tooltip)
        self.setFixedSize(size, size)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("role", "ghost")
        self.refresh_icon()

    def refresh_icon(self) -> None:
        self.setIcon(glyph_icon(self._kind, self._icon_size, theme.active().text_muted, 1.6))


class SettingRow(QWidget):
    """设置项：左侧标题 + 说明，右侧控件。"""

    def __init__(
        self,
        name: str,
        description: str = "",
        control: QWidget | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(16)

        text_box = QVBoxLayout()
        text_box.setSpacing(2)
        text_box.addWidget(label(name, "body"))
        if description:
            text_box.addWidget(label(description, "muted", wrap=True))
        layout.addLayout(text_box, 1)
        if control is not None:
            control.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
            layout.addWidget(control, 0, Qt.AlignRight | Qt.AlignVCenter)


class Toast(QLabel):
    """底部浮动提示，2.6 秒自动消失。"""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("toast")
        self.setAlignment(Qt.AlignCenter)
        self.setVisible(False)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

    def show_message(self, text: str, *, duration: int = 2600) -> None:
        self.setText(text)
        self.adjustSize()
        self.setFixedWidth(min(self.sizeHint().width() + 24, 560))
        self.adjustSize()
        self._place()
        self.show()
        self.raise_()
        self._timer.start(duration)

    def _place(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        x = max(12, (parent.width() - self.width()) // 2)
        y = parent.height() - self.height() - 96
        self.move(x, y)


class StepIndicator(QWidget):
    """向导顶部的步骤条：已完成打勾、当前高亮、未来灰显。

    只用最少的视觉元素（圆点 + 连接线 + 一行步骤名），
    与整体克制的卡片风格一致。
    """

    def __init__(self, steps: Sequence[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._steps = list(steps)
        self._current = 0
        self.setFixedHeight(56)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_current(self, index: int) -> None:
        index = max(0, min(len(self._steps) - 1, int(index)))
        if index == self._current:
            return
        self._current = index
        self.update()

    @property
    def current(self) -> int:
        return self._current

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self._steps:
            return
        palette = theme.active()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        count = len(self._steps)
        margin = 52.0
        center_y = 18.0
        span = max(1.0, self.width() - margin * 2)
        step = span / (count - 1) if count > 1 else 0.0
        xs = [margin + step * i for i in range(count)]

        # 连接线：已走过的段用主色
        for i in range(count - 1):
            done = i < self._current
            painter.setPen(QPen(QColor(palette.primary if done else palette.border), 2.0))
            painter.drawLine(QPointF(xs[i] + 13, center_y), QPointF(xs[i + 1] - 13, center_y))

        for i, name in enumerate(self._steps):
            x = xs[i]
            done = i < self._current
            active = i == self._current
            radius = 10.0 if active else 8.5
            if done:
                painter.setBrush(QColor(palette.primary))
                painter.setPen(Qt.NoPen)
                painter.drawEllipse(QPointF(x, center_y), radius, radius)
                painter.setBrush(Qt.NoBrush)
                painter.setPen(QPen(QColor("#FFFFFF"), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
                painter.drawLine(QPointF(x - 4, center_y), QPointF(x - 1, center_y + 3))
                painter.drawLine(QPointF(x - 1, center_y + 3), QPointF(x + 4, center_y - 3))
            else:
                painter.setBrush(QColor(palette.surface if active else palette.surface_alt))
                painter.setPen(
                    QPen(QColor(palette.primary if active else palette.border_strong), 2.0 if active else 1.5)
                )
                painter.drawEllipse(QPointF(x, center_y), radius, radius)
                font = QFont(self.font())
                font.setPixelSize(10)
                font.setWeight(QFont.DemiBold if active else QFont.Normal)
                painter.setFont(font)
                painter.setPen(QColor(palette.primary if active else palette.text_muted))
                painter.drawText(QRectF(x - 12, center_y - 8, 24, 16), Qt.AlignCenter, str(i + 1))

            font = QFont(self.font())
            font.setPixelSize(11)
            font.setWeight(QFont.DemiBold if (active or done) else QFont.Normal)
            painter.setFont(font)
            if active:
                color = palette.primary
            elif done:
                color = palette.text
            else:
                color = palette.text_muted
            painter.setPen(QColor(color))
            painter.drawText(QRectF(x - 100, center_y + 15, 200, 18), Qt.AlignHCenter, name)
        painter.end()


def make_button(
    text: str,
    *,
    kind: str | None = None,
    role: str | None = None,
    handler: Callable | None = None,
    icon_color: str | None = None,
    tooltip: str = "",
) -> GlyphButton:
    button = GlyphButton(text, kind, role=role, icon_color=icon_color)
    if tooltip:
        button.setToolTip(tooltip)
    if handler is not None:
        button.clicked.connect(handler)
    return button


def make_tool_button(kind: str, tooltip: str = "", handler: Callable | None = None) -> ToolGlyphButton:
    button = ToolGlyphButton(kind, tooltip)
    if handler is not None:
        button.clicked.connect(handler)
    return button


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()
        else:
            child = item.layout()
            if child is not None:
                clear_layout(child)
