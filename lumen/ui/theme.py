"""主题：一套 token 生成浅色 / 深色两套 QSS。

设计约束（对应需求里的界面规范）：
* 主色 + 中性灰阶 + 语义色（成功/警告/危险），不用高饱和荧光色；
* 圆角统一 8px、胶囊 999px；字号三级（19/14/12）；
* 悬停/按下/禁用三态都有明确色值；
* QSS 用 ``string.Template`` 渲染，避免与 f-string 的花括号冲突。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, asdict
from string import Template

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


def _rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _mix(color: str, other: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    r1, g1, b1 = _rgb(color)
    r2, g2, b2 = _rgb(other)
    return "#%02X%02X%02X" % (
        round(r1 + (r2 - r1) * ratio),
        round(g1 + (g2 - g1) * ratio),
        round(b1 + (b2 - b1) * ratio),
    )


def lighten(color: str, amount: float) -> str:
    return _mix(color, "#FFFFFF", amount)


def darken(color: str, amount: float) -> str:
    return _mix(color, "#000000", amount)


def font_family() -> str:
    if sys.platform == "win32":
        return '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", sans-serif'
    if sys.platform == "darwin":
        return '"PingFang SC", "Helvetica Neue", sans-serif'
    return '"Noto Sans CJK SC", "Source Han Sans SC", "Segoe UI", sans-serif'


_FONTS_LOADED = False


def ensure_fonts() -> None:
    """兜底加载中文字体。

    桌面环境下系统字体可用；但在离屏渲染（QT_QPA_PLATFORM=offscreen）、
    精简系统或容器里，Qt 可能找不到中文字体导致文字显示为方块。
    这里显式加载常见中文字体，失败也不影响启动。
    """
    global _FONTS_LOADED
    if _FONTS_LOADED:
        return
    _FONTS_LOADED = True
    from PySide6.QtGui import QFontDatabase

    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Light.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for path in candidates:
        try:
            from pathlib import Path as _Path

            if _Path(path).exists():
                QFontDatabase.addApplicationFont(path)
        except Exception:  # noqa: BLE001 - 字体加载失败不阻塞启动
            continue


@dataclass(frozen=True)
class Palette:
    name: str
    is_dark: bool
    bg: str
    surface: str
    surface_alt: str
    surface_hover: str
    surface_pressed: str
    border: str
    border_strong: str
    text: str
    text_muted: str
    primary: str
    primary_hover: str
    primary_pressed: str
    primary_soft: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    scroll: str

    def as_dict(self) -> dict[str, str]:
        data = asdict(self)
        data["font"] = font_family()
        data["is_dark"] = "1" if self.is_dark else "0"
        return {key: str(value) for key, value in data.items()}


def _build_dark() -> Palette:
    bg = "#14161B"
    surface = "#1B1E25"
    surface_alt = "#22262F"
    border = "#2A2F3A"
    primary = "#5B7CFA"
    return Palette(
        name="dark",
        is_dark=True,
        bg=bg,
        surface=surface,
        surface_alt=surface_alt,
        surface_hover=lighten(surface_alt, 0.06),
        surface_pressed=darken(surface_alt, 0.10),
        border=border,
        border_strong=lighten(border, 0.14),
        text="#E8EAF0",
        text_muted="#8A93A6",
        primary=primary,
        primary_hover=lighten(primary, 0.10),
        primary_pressed=darken(primary, 0.12),
        primary_soft=_mix(surface, primary, 0.20),
        success="#4CAF72",
        success_soft=_mix(surface, "#4CAF72", 0.20),
        warning="#D9A02B",
        warning_soft=_mix(surface, "#D9A02B", 0.20),
        danger="#E06064",
        danger_soft=_mix(surface, "#E06064", 0.20),
        scroll="#3A4150",
    )


def _build_light() -> Palette:
    bg = "#F4F5F7"
    surface = "#FFFFFF"
    surface_alt = "#F1F3F7"
    border = "#E2E6EE"
    primary = "#4C6FEA"
    return Palette(
        name="light",
        is_dark=False,
        bg=bg,
        surface=surface,
        surface_alt=surface_alt,
        surface_hover=darken(surface_alt, 0.04),
        surface_pressed=darken(surface_alt, 0.08),
        border=border,
        border_strong=darken(border, 0.10),
        text="#1B1E24",
        text_muted="#6B7280",
        primary=primary,
        primary_hover=lighten(primary, 0.10),
        primary_pressed=darken(primary, 0.12),
        primary_soft=_mix(surface, primary, 0.12),
        success="#2F9E56",
        success_soft=_mix(surface, "#2F9E56", 0.12),
        warning="#C08A1E",
        warning_soft=_mix(surface, "#C08A1E", 0.14),
        danger="#D2484E",
        danger_soft=_mix(surface, "#D2484E", 0.12),
        scroll="#C7CDD9",
    )


PALETTES = {"dark": _build_dark(), "light": _build_light()}

_active: Palette = PALETTES["dark"]


def palette_for(theme: str) -> Palette:
    return PALETTES.get((theme or "dark").lower(), PALETTES["dark"])


def set_active(palette: Palette) -> None:
    global _active
    _active = palette


def active() -> Palette:
    """当前主题调色板（自绘组件用它取色）。"""
    return _active


QSS = Template(
    """
QWidget {
    font-family: $font;
    font-size: 13px;
    color: $text;
    background: transparent;
}
QMainWindow, QDialog, QWidget#root {
    background-color: $bg;
}
QWidget#sidenav {
    background-color: $bg;
    border-right: 1px solid $border;
}
QWidget#launchbar {
    background-color: $surface_alt;
    border-top: 1px solid $border;
}
QFrame#card {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 8px;
}
QFrame#card:hover {
    border-color: $border_strong;
}
QFrame#cardFlat {
    background-color: $surface_alt;
    border: 1px solid transparent;
    border-radius: 8px;
}
QFrame#divider {
    background-color: $border;
    max-height: 1px;
    min-height: 1px;
    border: none;
}
QLabel { background: transparent; }
QLabel[role="title"] { font-size: 19px; font-weight: 600; }
QLabel[role="subtitle"] { font-size: 15px; font-weight: 600; }
QLabel[role="body"] { font-size: 13px; }
QLabel[role="muted"] { font-size: 12px; color: $text_muted; }
QLabel[role="section"] { font-size: 12px; font-weight: 600; color: $text_muted; }
QLabel[role="version"] { font-size: 15px; font-weight: 600; }
QLabel[role="mono"] { font-family: "Cascadia Code", Consolas, "DejaVu Sans Mono", monospace; font-size: 12px; }

QLabel[badge] {
    border-radius: 9px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}
QLabel[badge="primary"] { background-color: $primary_soft; color: $primary; }
QLabel[badge="success"] { background-color: $success_soft; color: $success; }
QLabel[badge="warning"] { background-color: $warning_soft; color: $warning; }
QLabel[badge="danger"] { background-color: $danger_soft; color: $danger; }
QLabel[badge="muted"] { background-color: $surface_alt; color: $text_muted; }

QPushButton {
    background-color: $surface_alt;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 7px 14px;
    color: $text;
}
QPushButton:hover { background-color: $surface_hover; border-color: $border_strong; }
QPushButton:pressed { background-color: $surface_pressed; }
QPushButton:disabled { color: $text_muted; border-color: $border; background-color: $surface_alt; }
QPushButton[role="primary"] {
    background-color: $primary;
    border: 1px solid $primary;
    color: #FFFFFF;
    font-weight: 600;
    padding: 9px 20px;
}
QPushButton[role="primary"]:hover { background-color: $primary_hover; border-color: $primary_hover; }
QPushButton[role="primary"]:pressed { background-color: $primary_pressed; border-color: $primary_pressed; }
QPushButton[role="primary"]:disabled { background-color: $surface_alt; border-color: $border; color: $text_muted; }
QPushButton[role="ghost"] { background: transparent; border: 1px solid transparent; color: $text_muted; }
QPushButton[role="ghost"]:hover { background-color: $surface_alt; color: $text; }
QPushButton[role="danger"] { color: $danger; }
QPushButton[role="danger"]:hover { background-color: $danger_soft; border-color: $danger; }
QPushButton[role="launch"] {
    background-color: $primary;
    border: 1px solid $primary;
    border-radius: 10px;
    color: #FFFFFF;
    font-size: 15px;
    font-weight: 600;
    padding: 0 34px;
    min-height: 44px;
}
QPushButton[role="launch"]:hover { background-color: $primary_hover; border-color: $primary_hover; }
QPushButton[role="launch"]:pressed { background-color: $primary_pressed; }
QPushButton[role="launch"]:disabled { background-color: $surface_alt; border-color: $border; color: $text_muted; }

QPushButton[nav="true"] {
    background: transparent;
    border: none;
    border-radius: 10px;
    color: $text_muted;
    text-align: center;
    padding: 10px 0;
}
QPushButton[nav="true"]:hover { background-color: $surface_alt; color: $text; }
QPushButton[nav="true"]:checked {
    background-color: $primary_soft;
    color: $primary;
    font-weight: 600;
}
QToolButton[nav="true"] {
    background: transparent;
    border: none;
    border-radius: 10px;
    color: $text_muted;
    padding: 8px 2px;
    font-size: 11px;
}
QToolButton[nav="true"]:hover { background-color: $surface_alt; color: $text; }
QToolButton[nav="true"]:checked { background-color: $primary_soft; color: $primary; font-weight: 600; }

QWidget#segmented {
    background-color: $surface_alt;
    border: 1px solid $border;
    border-radius: 8px;
}
QPushButton[seg="true"] {
    background: transparent;
    border: none;
    border-radius: 7px;
    padding: 6px 14px;
    color: $text_muted;
}
QPushButton[seg="true"]:hover { background-color: $surface_hover; color: $text; }
QPushButton[seg="true"]:checked {
    background-color: $primary_soft;
    color: $primary;
    font-weight: 600;
}

QToolButton[role="ghost"] { background: transparent; border: none; border-radius: 8px; padding: 4px; }
QToolButton[role="ghost"]:hover { background-color: $surface_alt; }
QToolButton[role="ghost"]:pressed { background-color: $surface_pressed; }
QToolButton[role="ghost"]:disabled { background: transparent; }

QLabel#toast {
    background-color: $surface;
    border: 1px solid $border_strong;
    border-radius: 10px;
    padding: 10px 18px;
    color: $text;
    font-size: 12px;
}
QFrame#dropzone {
    background-color: transparent;
    border: 2px dashed $border_strong;
    border-radius: 10px;
}
QFrame#dropzone[hover="true"] {
    border-color: $primary;
    background-color: $primary_soft;
}

QLineEdit, QSpinBox, QComboBox, QDoubleSpinBox {
    background-color: $surface_alt;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 7px 10px;
    min-height: 18px;
    selection-background-color: $primary;
    selection-color: #FFFFFF;
}
QLineEdit:hover, QSpinBox:hover, QComboBox:hover { border-color: $border_strong; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: $primary; }
QLineEdit:disabled, QComboBox:disabled { color: $text_muted; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox::down-arrow {
    width: 0; height: 0;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid $text_muted;
    margin-right: 8px;
}
QComboBox QAbstractItemView {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 4px;
    outline: none;
    selection-background-color: $surface_alt;
    selection-color: $text;
}
QSpinBox::up-button, QSpinBox::down-button, QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {
    width: 0px;
    height: 0px;
    border: none;
    background: transparent;
}
QCheckBox { spacing: 8px; color: $text; }
QCheckBox::indicator {
    width: 17px; height: 17px;
    border-radius: 4px;
    border: 1px solid $border_strong;
    background-color: $surface_alt;
}
QCheckBox::indicator:hover { border-color: $primary; }
QCheckBox::indicator:checked {
    background-color: $primary;
    border-color: $primary;
    image: url("$check_icon");
}
QRadioButton { spacing: 8px; }
QSlider::groove:horizontal { height: 4px; background-color: $border; border-radius: 2px; }
QSlider::sub-page:horizontal { background-color: $primary; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 16px; height: 16px; margin: -7px 0;
    border-radius: 8px; background-color: $surface;
    border: 2px solid $primary;
}
QSlider::handle:horizontal:hover { background-color: $primary_soft; }

QScrollArea { border: none; background: transparent; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 2px 0; }
QScrollBar::handle:vertical { background-color: $scroll; border-radius: 4px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background-color: $text_muted; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 0 2px; }
QScrollBar::handle:horizontal { background-color: $scroll; border-radius: 4px; min-width: 28px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }

QProgressBar {
    background-color: $surface_alt;
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk { background-color: $primary; border-radius: 4px; }

QToolTip {
    background-color: $surface;
    color: $text;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 6px 8px;
}
QMenu {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 6px;
}
QMenu::item { padding: 7px 22px; border-radius: 6px; }
QMenu::item:selected { background-color: $surface_alt; }
QMenu::separator { height: 1px; background-color: $border; margin: 6px 8px; }

QTextEdit, QPlainTextEdit {
    background-color: $surface_alt;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 8px;
    font-family: "Cascadia Code", Consolas, "DejaVu Sans Mono", monospace;
    font-size: 12px;
    selection-background-color: $primary;
}
QTextEdit:focus, QPlainTextEdit:focus { border-color: $primary; }

QListWidget, QTreeWidget, QTableWidget {
    background-color: transparent;
    border: none;
    outline: none;
}
QListWidget::item { border-radius: 6px; padding: 6px; }
QListWidget::item:selected { background-color: $primary_soft; color: $text; }
QHeaderView::section {
    background-color: $surface_alt;
    color: $text_muted;
    border: none;
    padding: 6px;
    font-size: 12px;
}

QSplitter::handle { background-color: $border; }
QTabWidget::pane { border: 1px solid $border; border-radius: 8px; }
QTabBar::tab {
    background: transparent; color: $text_muted;
    padding: 7px 16px; border-radius: 7px; margin-right: 4px;
}
QTabBar::tab:selected { background-color: $primary_soft; color: $primary; font-weight: 600; }

QMessageBox { background-color: $surface; }
QMessageBox QLabel { color: $text; }
"""
)


_CHECK_ICON_CACHE: dict[str, str] = {}


def _check_icon_path(color: str = "#FFFFFF") -> str:
    """QSS 的 checkbox 对勾需要图片文件：运行时生成一张小白勾缓存到临时目录。"""
    key = color.lower()
    if key in _CHECK_ICON_CACHE:
        return _CHECK_ICON_CACHE[key]
    try:
        import tempfile
        from pathlib import Path as _Path

        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QColor as _QColor, QPainter as _QPainter, QPen as _QPen, QPixmap as _QPixmap

        folder = _Path(tempfile.gettempdir()) / "lumen-ui"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"check-{key.lstrip('#')}.png"
        if not path.exists():
            pixmap = _QPixmap(36, 36)
            pixmap.fill(_Qt.transparent)
            painter = _QPainter(pixmap)
            painter.setRenderHint(_QPainter.Antialiasing, True)
            painter.setPen(_QPen(_QColor(color), 4.4, _Qt.SolidLine, _Qt.RoundCap, _Qt.RoundJoin))
            painter.drawLine(8, 18, 15, 25)
            painter.drawLine(15, 25, 28, 11)
            painter.end()
            pixmap.save(str(path), "PNG")
        _CHECK_ICON_CACHE[key] = path.as_posix()
        return _CHECK_ICON_CACHE[key]
    except Exception:  # noqa: BLE001 - 生成失败时只是没有对勾，不影响可用性
        return ""


def build_qss(palette: Palette) -> str:
    data = palette.as_dict()
    data["check_icon"] = _check_icon_path("#FFFFFF")
    return QSS.substitute(data)


def apply_theme(app: QApplication, theme: str) -> Palette:
    """给整个应用套用主题（QSS + 调色板 + 字体）。"""
    ensure_fonts()
    palette = palette_for(theme)
    app.setStyleSheet(build_qss(palette))

    qt_palette = QPalette()
    bg = QColor(palette.bg)
    surface = QColor(palette.surface)
    text = QColor(palette.text)
    muted = QColor(palette.text_muted)
    primary = QColor(palette.primary)
    qt_palette.setColor(QPalette.Window, bg)
    qt_palette.setColor(QPalette.WindowText, text)
    qt_palette.setColor(QPalette.Base, surface)
    qt_palette.setColor(QPalette.AlternateBase, QColor(palette.surface_alt))
    qt_palette.setColor(QPalette.Text, text)
    qt_palette.setColor(QPalette.PlaceholderText, muted)
    qt_palette.setColor(QPalette.Button, surface)
    qt_palette.setColor(QPalette.ButtonText, text)
    qt_palette.setColor(QPalette.Highlight, primary)
    qt_palette.setColor(QPalette.HighlightedText, QColor("#FFFFFF"))
    qt_palette.setColor(QPalette.ToolTipBase, surface)
    qt_palette.setColor(QPalette.ToolTipText, text)
    qt_palette.setColor(QPalette.Disabled, QPalette.Text, muted)
    qt_palette.setColor(QPalette.Disabled, QPalette.ButtonText, muted)
    app.setPalette(qt_palette)

    font = QFont()
    family = font_family().split(",")[0].strip().strip('"')
    font.setFamily(family)
    font.setPointSizeF(9.5)
    app.setFont(font)
    set_active(palette)
    return palette


def badge_color(kind: str) -> str:
    """供自绘组件取语义色。"""
    return {
        "primary": "primary",
        "success": "success",
        "warning": "warning",
        "danger": "danger",
    }.get(kind, "text_muted")
