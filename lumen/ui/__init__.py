"""界面层：PySide6 组件、页面、对话框。"""

from .theme import PALETTES, Palette, apply_theme, build_qss, palette_for  # noqa: F401

__all__ = ["apply_theme", "palette_for", "build_qss", "Palette", "PALETTES"]
