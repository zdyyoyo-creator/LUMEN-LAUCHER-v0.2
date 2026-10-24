"""生成「资源」页截图（搜索真实结果 + CurseForge 需要 Key 的引导）。

    python tools/browse_preview.py
    → shots/browse-page.png、shots/browse-curseforge.png
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget  # noqa: E402

from lumen.config import AppPaths, Settings, resolve_data_root  # noqa: E402
from lumen.ui import theme  # noqa: E402
from lumen.ui.context import AppContext  # noqa: E402
from lumen.ui.pages.browse_page import BrowsePage  # noqa: E402
from lumen.ui.workers import TaskManager  # noqa: E402

SHOTS = ROOT / "shots"


def pump(app: QApplication, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.05)


def main() -> int:
    app = QApplication(sys.argv[:1])
    paths = AppPaths(data_root=resolve_data_root()).ensure()
    settings = Settings(paths)
    theme.apply_theme(app, settings.theme or "dark")
    ctx = AppContext(paths, settings)
    tasks = TaskManager()
    SHOTS.mkdir(parents=True, exist_ok=True)

    # 套一个带主题背景的容器窗口，否则独立窗口截图会缺少背景色
    container = QWidget()
    container.setObjectName("root")
    outer = QVBoxLayout(container)
    outer.setContentsMargins(0, 0, 0, 0)
    page = BrowsePage(ctx, tasks)
    outer.addWidget(page)
    container.resize(1180, 760)
    container.show()
    pump(app, 0.6)

    # 优先选带加载器的实例，模组才会生效
    instance = next((i for i in ctx.instances() if i.loader_type), None) or ctx.current_instance()
    if instance is not None:
        index = page._instance_combo.findData(instance.id)
        if index >= 0:
            page._instance_combo.setCurrentIndex(index)

    page._query.setText("sodium")
    page.search(reset=True)
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and (page._loading or not page._hits):
        app.processEvents()
        time.sleep(0.08)
    pump(app, 1.2)
    container.grab().save(str(SHOTS / "browse-page.png"))
    print(f"  saved browse-page.png（结果 {len(page._hits)} 条）")

    # CurseForge 无 Key 的引导
    page._source.set_current("curseforge")
    page.search(reset=True)
    pump(app, 0.8)
    container.grab().save(str(SHOTS / "browse-curseforge.png"))
    print("  saved browse-curseforge.png")

    container.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
