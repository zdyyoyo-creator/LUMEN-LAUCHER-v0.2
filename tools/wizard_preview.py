"""生成 Forge 向导各步骤的预览截图（不触发真实安装）。

    python tools/wizard_preview.py

用于文档/回归：
* step-1 确认参数（真实读取实例/Java/加载器版本）
* step-2 准备原版（注入示例进度）
* step-3 安装加载器（注入示例进度 + 安装器输出）
* step-4 完成页
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

from PySide6.QtWidgets import QApplication  # noqa: E402

from lumen.config import AppPaths, Settings, resolve_data_root  # noqa: E402
from lumen.core.installer import InstallResult, StageProgress  # noqa: E402
from lumen.ui import theme  # noqa: E402
from lumen.ui.context import AppContext  # noqa: E402
from lumen.ui.dialogs.forge_wizard import ForgeWizardDialog  # noqa: E402
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
    instance = ctx.current_instance()
    if instance is None:
        print("没有实例可用，请先创建或指定 --data-dir")
        return 2

    SHOTS.mkdir(parents=True, exist_ok=True)
    dialog = ForgeWizardDialog(
        None,
        ctx=ctx,
        tasks=tasks,
        kind="forge",
        mc_version=instance.mc_version,
        instance=instance,
    )
    dialog.show()
    pump(app, 2.5)

    def shot(name: str) -> None:
        dialog.grab().save(str(SHOTS / name))
        print(f"  saved {name}")

    shot("wizard-step1.png")

    # ② 准备原版
    dialog._goto(1)
    for percent, stage, message, done, total in (
        (0.06, "client", "下载客户端", 0, 1),
        (0.22, "libraries", "下载依赖库（68 个）", 24, 68),
        (0.45, "assets", "下载游戏资源（4039 个，403.1 MiB）", 1200, 4039),
    ):
        dialog._on_progress(
            StageProgress(
                stage=stage,
                message=message,
                percent=percent,
                files_done=done,
                files_total=total,
                bytes_done=int(percent * 500 * 1024 * 1024),
                bytes_total=500 * 1024 * 1024,
                speed=2.4 * 1024 * 1024,
            )
        )
        pump(app, 0.15)
    shot("wizard-step2.png")

    # ③ 安装加载器
    dialog._goto(2)
    for line in (
        "[main/INFO] Extracting json (1.21.4-forge-54.1.22)",
        "Downloading libraries",
        "  Downloading library from https://maven.creeperhost.net/net/minecraftforge/fmlcore/1.21.4-54.1.22/fmlcore-1.21.4-54.1.22.jar",
        "  Download completed: Checksum validated.",
        "Patching net/minecraft/Util$4 1/1",
        "  Copying net/minecraft/world/level/timers/TimerQueue$Event.class",
    ):
        dialog._queue_line(line)
    dialog._flush_lines()
    dialog._install_ring.set_value(0.62)
    dialog._install_message.setText("Patching net/minecraft/Util$4 1/1")
    dialog._install_detail.setText("已运行 128 秒")
    dialog._elapsed_label.setText("已运行 02:08")
    pump(app, 0.3)
    shot("wizard-step3.png")

    # ④ 完成
    dialog._goto(3)
    dialog._done_icon.set_kind("check")
    dialog._done_icon.set_color(theme.active().success)
    dialog._done_title.setText("安装完成")
    dialog._done_detail.setText(
        f"Forge {dialog._loader_combo.currentData() or '54.1.22'} 已安装到「{instance.name}」\n"
        "版本目录：1.21.4-forge-54.1.22\n完成（新下载 1 个，校验通过 4108 个）"
    )
    dialog._retry_button.setVisible(False)
    dialog._open_folder_button.setVisible(True)
    dialog._launch_button.setVisible(True)
    dialog._cancel_button.setText("关闭")
    pump(app, 0.3)
    shot("wizard-step4.png")

    dialog._clock.stop()
    dialog._flush_timer.stop()
    dialog.close()
    print("向导预览截图完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
