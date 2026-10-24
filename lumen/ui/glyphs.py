"""线性图标：用 QPainter 在 24×24 网格上自绘，随主题取色。

不引入图标字体/SVG 资源，避免打包体积与平台缺字问题；
每个图标都是简单几何路径，深色/浅色下共用同一套笔画。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

from . import theme

GRID = 24.0


def _pen(color: QColor, width: float) -> QPen:
    pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    return pen


def paint_glyph(painter: QPainter, kind: str, rect: QRectF, color: QColor, width: float = 1.6) -> None:
    """在 rect 内绘制指定图标（保持等比缩放并居中）。"""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    scale = min(rect.width(), rect.height()) / GRID
    offset_x = rect.x() + (rect.width() - GRID * scale) / 2
    offset_y = rect.y() + (rect.height() - GRID * scale) / 2
    painter.translate(offset_x, offset_y)
    painter.scale(scale, scale)
    painter.setPen(_pen(color, width))
    painter.setBrush(Qt.NoBrush)

    if kind == "versions":
        for y in (4.0, 9.5, 15.0):
            painter.drawRoundedRect(QRectF(3.5, y, 17.0, 4.0), 1.6, 1.6)
    elif kind == "modpack":
        path = QPainterPath()
        path.moveTo(12, 3)
        path.lineTo(20.5, 7.5)
        path.lineTo(20.5, 16.5)
        path.lineTo(12, 21)
        path.lineTo(3.5, 16.5)
        path.lineTo(3.5, 7.5)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(3.5, 7.5), QPointF(12, 12))
        painter.drawLine(QPointF(20.5, 7.5), QPointF(12, 12))
        painter.drawLine(QPointF(12, 12), QPointF(12, 21))
    elif kind == "instances":
        for x, y in ((3.5, 3.5), (13.0, 3.5), (3.5, 13.0), (13.0, 13.0)):
            painter.drawRoundedRect(QRectF(x, y, 7.5, 7.5), 2.0, 2.0)
    elif kind == "settings":
        painter.drawEllipse(QPointF(12, 12), 3.1, 3.1)
        painter.drawEllipse(QPointF(12, 12), 7.4, 7.4)
        for angle in range(0, 360, 45):
            rad = math.radians(angle)
            inner = QPointF(12 + 7.4 * math.cos(rad), 12 + 7.4 * math.sin(rad))
            outer = QPointF(12 + 9.6 * math.cos(rad), 12 + 9.6 * math.sin(rad))
            painter.drawLine(inner, outer)
    elif kind == "play":
        path = QPainterPath()
        path.moveTo(8.5, 5.2)
        path.lineTo(19.0, 12.0)
        path.lineTo(8.5, 18.8)
        path.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)
    elif kind == "stop":
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(7, 7, 10, 10), 2.0, 2.0)
    elif kind == "pause":
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(7.5, 5.5, 3.2, 13.0), 1.4, 1.4)
        painter.drawRoundedRect(QRectF(13.3, 5.5, 3.2, 13.0), 1.4, 1.4)
    elif kind == "download":
        painter.drawLine(QPointF(12, 4), QPointF(12, 15))
        painter.drawLine(QPointF(7.6, 10.8), QPointF(12, 15.2))
        painter.drawLine(QPointF(16.4, 10.8), QPointF(12, 15.2))
        painter.drawLine(QPointF(5, 19.5), QPointF(19, 19.5))
    elif kind == "folder":
        path = QPainterPath()
        path.moveTo(3.5, 7.0)
        path.lineTo(9.2, 7.0)
        path.lineTo(11.2, 9.2)
        path.lineTo(20.5, 9.2)
        path.lineTo(20.5, 18.5)
        path.lineTo(3.5, 18.5)
        path.closeSubpath()
        painter.drawPath(path)
    elif kind == "refresh":
        painter.drawArc(QRectF(4.5, 4.5, 15.0, 15.0), 55 * 16, 265 * 16)
        painter.drawLine(QPointF(17.6, 4.2), QPointF(18.4, 8.6))
        painter.drawLine(QPointF(13.4, 5.6), QPointF(18.4, 8.6))
    elif kind == "search":
        painter.drawEllipse(QPointF(10.5, 10.5), 5.6, 5.6)
        painter.drawLine(QPointF(14.7, 14.7), QPointF(20.0, 20.0))
    elif kind == "plus":
        painter.drawLine(QPointF(12, 5.5), QPointF(12, 18.5))
        painter.drawLine(QPointF(5.5, 12), QPointF(18.5, 12))
    elif kind == "trash":
        painter.drawLine(QPointF(4.8, 7.2), QPointF(19.2, 7.2))
        painter.drawLine(QPointF(9.4, 4.6), QPointF(14.6, 4.6))
        path = QPainterPath()
        path.moveTo(6.6, 7.2)
        path.lineTo(7.6, 20.2)
        path.lineTo(16.4, 20.2)
        path.lineTo(17.4, 7.2)
        painter.drawPath(path)
        painter.drawLine(QPointF(10.2, 10.6), QPointF(10.2, 17.0))
        painter.drawLine(QPointF(13.8, 10.6), QPointF(13.8, 17.0))
    elif kind == "copy":
        painter.drawRoundedRect(QRectF(8.5, 8.5, 11.5, 11.5), 2.0, 2.0)
        path = QPainterPath()
        path.moveTo(5.5, 15.5)
        path.lineTo(4.6, 15.0)
        path.lineTo(4.0, 4.6)
        path.lineTo(15.0, 4.6)
        path.lineTo(15.5, 5.5)
        painter.drawPath(path)
    elif kind == "edit":
        painter.drawLine(QPointF(5.0, 19.0), QPointF(8.0, 18.4))
        painter.drawLine(QPointF(8.0, 18.4), QPointF(19.2, 7.2))
        painter.drawLine(QPointF(19.2, 7.2), QPointF(16.8, 4.8))
        painter.drawLine(QPointF(16.8, 4.8), QPointF(5.6, 16.0))
        painter.drawLine(QPointF(5.6, 16.0), QPointF(5.0, 19.0))
    elif kind == "close":
        painter.drawLine(QPointF(6.5, 6.5), QPointF(17.5, 17.5))
        painter.drawLine(QPointF(17.5, 6.5), QPointF(6.5, 17.5))
    elif kind == "check":
        painter.drawLine(QPointF(5.5, 12.5), QPointF(10.0, 17.0))
        painter.drawLine(QPointF(10.0, 17.0), QPointF(18.5, 7.0))
    elif kind == "warning":
        path = QPainterPath()
        path.moveTo(12, 4.2)
        path.lineTo(20.6, 19.4)
        path.lineTo(3.4, 19.4)
        path.closeSubpath()
        painter.drawPath(path)
        painter.drawLine(QPointF(12, 10.0), QPointF(12, 14.2))
        painter.drawPoint(QPointF(12, 16.8))
    elif kind == "info":
        painter.drawEllipse(QPointF(12, 12), 8.0, 8.0)
        painter.drawLine(QPointF(12, 11.2), QPointF(12, 16.4))
        painter.drawPoint(QPointF(12, 8.2))
    elif kind == "java":
        painter.drawRoundedRect(QRectF(5.0, 9.0, 10.5, 9.5), 1.8, 1.8)
        painter.drawArc(QRectF(15.0, 11.0, 4.6, 5.0), -80 * 16, 160 * 16)
        painter.drawLine(QPointF(3.8, 20.6), QPointF(17.6, 20.6))
        for x in (8.4, 12.0):
            painter.drawLine(QPointF(x, 5.6), QPointF(x, 7.8))
    elif kind == "log":
        painter.drawRoundedRect(QRectF(4.6, 3.8, 14.8, 16.4), 2.0, 2.0)
        for y in (8.6, 12.0, 15.4):
            painter.drawLine(QPointF(7.6, y), QPointF(16.4, y))
    elif kind == "mods":
        path = QPainterPath()
        path.addRoundedRect(QRectF(4.0, 4.0, 16.0, 16.0), 2.4, 2.4)
        painter.drawPath(path)
        painter.drawLine(QPointF(12, 4.2), QPointF(12, 19.8))
        painter.drawLine(QPointF(4.2, 12), QPointF(19.8, 12))
    elif kind == "account":
        painter.drawEllipse(QPointF(12, 8.6), 4.0, 4.0)
        path = QPainterPath()
        path.moveTo(4.6, 20.2)
        path.arcTo(QRectF(4.6, 12.4, 14.8, 15.6), 180, -180)
        painter.drawPath(path)
    elif kind == "theme":
        painter.drawEllipse(QPointF(12, 12), 7.8, 7.8)
        path = QPainterPath()
        path.moveTo(12, 4.6)
        path.arcTo(QRectF(4.2, 4.2, 15.6, 15.6), 90, -180)
        path.closeSubpath()
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawPath(path)
    elif kind == "clock":
        painter.drawEllipse(QPointF(12, 12), 7.8, 7.8)
        painter.drawLine(QPointF(12, 12), QPointF(12, 7.4))
        painter.drawLine(QPointF(12, 12), QPointF(15.4, 13.6))
    elif kind == "memory":
        painter.drawRoundedRect(QRectF(3.4, 7.0, 17.2, 10.0), 2.0, 2.0)
        for x in (7.6, 11.2, 14.8):
            painter.drawLine(QPointF(x, 17.0), QPointF(x, 20.0))
    elif kind == "globe":
        painter.drawEllipse(QPointF(12, 12), 8.0, 8.0)
        painter.drawEllipse(QPointF(12, 12), 3.6, 8.0)
        painter.drawLine(QPointF(4.0, 12), QPointF(20.0, 12))
    elif kind == "external":
        painter.drawRoundedRect(QRectF(4.5, 4.5, 9.5, 9.5), 1.8, 1.8)
        painter.drawLine(QPointF(12.0, 12.0), QPointF(20.0, 4.0))
        painter.drawLine(QPointF(14.6, 4.0), QPointF(20.0, 4.0))
        painter.drawLine(QPointF(20.0, 4.0), QPointF(20.0, 9.4))
    elif kind == "chevron":
        painter.drawLine(QPointF(7.0, 10.0), QPointF(12.0, 15.0))
        painter.drawLine(QPointF(12.0, 15.0), QPointF(17.0, 10.0))
    elif kind == "spark":
        path = QPainterPath()
        path.moveTo(12, 3.6)
        path.lineTo(13.9, 9.4)
        path.lineTo(19.8, 11.3)
        path.lineTo(13.9, 13.2)
        path.lineTo(12, 19.0)
        path.lineTo(10.1, 13.2)
        path.lineTo(4.2, 11.3)
        path.lineTo(10.1, 9.4)
        path.closeSubpath()
        painter.drawPath(path)
    else:  # 未知图标画一个圆，避免静默显示空白
        painter.drawEllipse(QPointF(12, 12), 7.0, 7.0)
    painter.restore()


def glyph_pixmap(
    kind: str,
    size: int = 22,
    color: str | QColor = "#8A93A6",
    width: float = 1.6,
    ratio: float = 2.0,
) -> QPixmap:
    """生成高 DPI 位图（内部按 ratio 放大，避免发虚）。"""
    px = max(8, int(size * ratio))
    pixmap = QPixmap(px, px)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    paint_glyph(painter, kind, QRectF(0, 0, px, px), QColor(color), width * ratio)
    painter.end()
    pixmap.setDevicePixelRatio(ratio)
    return pixmap


def glyph_icon(kind: str, size: int = 20, color: str | QColor | None = None, width: float = 1.6) -> QIcon:
    color = color if color is not None else theme.active().text_muted
    return QIcon(glyph_pixmap(kind, size, color, width))


def icon_size(size: int = 20) -> QSize:
    return QSize(size, size)
