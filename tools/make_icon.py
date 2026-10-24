"""生成 assets/icon.ico（用 Qt 自绘，不依赖外部素材）。

    python tools/make_icon.py

产物：assets/icon.ico（多尺寸）+ assets/icon.png（256×256，方便预览）
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

PRIMARY = "#5B7CFA"


def draw(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(PRIMARY))
    radius = size * 0.24
    painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    font = QFont()
    font.setPixelSize(int(size * 0.52))
    font.setWeight(QFont.Bold)
    painter.setFont(font)
    painter.setPen(QColor("#FFFFFF"))
    painter.drawText(pixmap.rect(), Qt.AlignCenter, "L")
    painter.end()
    return pixmap


def main() -> int:
    app = QApplication(sys.argv[:1])  # noqa: F841 - QPixmap 需要 QGuiApplication
    out_dir = ROOT / "assets"
    out_dir.mkdir(parents=True, exist_ok=True)

    png_path = out_dir / "icon.png"
    draw(256).save(str(png_path), "PNG")
    print(f"已生成 {png_path}")

    ico_path = out_dir / "icon.ico"
    saved = draw(256).save(str(ico_path), "ICO")
    if saved:
        print(f"已生成 {ico_path}")
    else:
        print("当前 Qt 无法写出 ICO（可跳过，PyInstaller 也接受 PNG）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
