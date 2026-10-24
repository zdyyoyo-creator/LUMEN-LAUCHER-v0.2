"""UI 冒烟：在离屏模式下构造所有对话框与关键页面并截图，捕获运行时异常。

    python tools/ui_smoke.py                 # 全部
    python tools/ui_smoke.py --only install  # 只跑某个

产物：shots/dialog-*.png（便于人工检查观感，也方便 CI 做像素回归）
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

from PySide6.QtWidgets import QApplication  # noqa: E402

from lumen.config import AppPaths, Settings, resolve_data_root  # noqa: E402
from lumen.core.preflight import Issue  # noqa: E402
from lumen.modpack.importer import ModpackPlan, ModpackFileTask  # noqa: E402
from lumen.ui import theme  # noqa: E402
from lumen.ui.context import AppContext  # noqa: E402
from lumen.ui.dialogs.common import (  # noqa: E402
    ConfirmDeleteDialog,
    DetailDialog,
    InstancePickerDialog,
    IssueDialog,
    MessageDialog,
    TextInputDialog,
)
from lumen.ui.dialogs.import_dialog import ImportDialog  # noqa: E402
from lumen.ui.dialogs.install_dialog import InstallDialog  # noqa: E402
from lumen.ui.dialogs.forge_wizard import ForgeWizardDialog  # noqa: E402
from lumen.ui.dialogs.java_dialog import JavaDialog  # noqa: E402
from lumen.ui.dialogs.log_dialog import LogDialog  # noqa: E402
from lumen.ui.dialogs.mods_dialog import ModsDialog  # noqa: E402
from lumen.ui.workers import TaskManager  # noqa: E402

SHOTS = ROOT / "shots"


def build_factories(ctx, tasks, instance):
    fake_plan = ModpackPlan(
        source="mrpack",
        archive=Path("F:/downloads/example-pack.mrpack"),
        name="Create: Above and Beyond",
        mc_version="1.20.1",
        loader_type="fabric",
        loader_version="0.15.11",
        files=[ModpackFileTask(path=Path("mods/a.jar"), url="https://example.invalid/a.jar", size=1024 * 1024)],
        override_prefixes=["overrides"],
        total_size=48 * 1024 * 1024,
        notes=["整合包里没有写明 Minecraft 版本，需要手动指定"],
    )
    return {
        "install": lambda: InstallDialog(None, ctx=ctx, tasks=tasks, version_id="1.21.4", instance=instance),
        "forge_wizard": lambda: ForgeWizardDialog(
            None, ctx=ctx, tasks=tasks, kind="forge", mc_version="1.21.4", instance=instance
        ),
        "import": lambda: ImportDialog(None, ctx=ctx, tasks=tasks, plan=fake_plan),
        "java": lambda: JavaDialog(None, ctx=ctx, tasks=tasks),
        "log": lambda: LogDialog(None, ctx=ctx, tasks=tasks, instance=instance),
        "mods": lambda: ModsDialog(None, ctx=ctx, instance=instance),
        "message": lambda: MessageDialog(
            None,
            title="缺少可用的 Java 21",
            message="可以在设置里手动指定 java.exe，或让启动器自动下载 Mojang 官方运行时。",
            level="warning",
            ok_text="去设置",
            cancel_text="稍后",
        ),
        "input": lambda: TextInputDialog(
            None, title="新建实例", label_text="实例名称", value="冒险生存", description="每个实例有独立的 mods 与存档"
        ),
        "issues": lambda: IssueDialog(
            None,
            issues=[
                Issue("warning", "分配内存偏大", "当前 8 GB / 物理 15.7 GB，建议留 2GB 给系统"),
                Issue("error", "缺少 natives（本地库未解压）", "重新安装该版本即可自动解压"),
                Issue("warning", "实例目录存在兼容风险", "路径包含空格，个别旧版本库可能解析异常"),
            ],
            allow_continue=False,
        ),
        "delete": lambda: ConfirmDeleteDialog(None, instance=instance),
        "detail": lambda: DetailDialog(None, title="崩溃报告", text="java.lang.OutOfMemoryError: Java heap space\n" * 30),
        "picker": lambda: InstancePickerDialog(None, instances=ctx.instances() or [instance]),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default=None)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()

    app = QApplication(sys.argv[:1])
    paths = AppPaths(data_root=resolve_data_root(args.data_dir)).ensure()
    settings = Settings(paths)
    theme.apply_theme(app, settings.theme)
    ctx = AppContext(paths, settings)
    tasks = TaskManager()
    SHOTS.mkdir(parents=True, exist_ok=True)

    instance = ctx.current_instance()
    if instance is None:
        instance = ctx.store.create("UI 冒烟实例", "1.21.4")
        ctx.select_instance(instance)

    factories = build_factories(ctx, tasks, instance)
    if args.only:
        factories = {k: v for k, v in factories.items() if k == args.only}

    failures = 0
    for name, factory in factories.items():
        try:
            dialog = factory()
            dialog.show()
            # 给异步任务（Java 扫描 / 版本列表）一点时间，截图才有代表性的内容
            deadline = time.monotonic() + 1.6
            while time.monotonic() < deadline:
                app.processEvents()
                time.sleep(0.05)
            pixmap = dialog.grab()
            target = SHOTS / f"dialog-{name}.png"
            pixmap.save(str(target))
            print(f"  [OK]   {name:<10} → {target.name} ({pixmap.width()}x{pixmap.height()})")
            dialog.close()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {name:<10} {exc.__class__.__name__}: {exc}")
            traceback.print_exc(limit=6)
    print(f"\n对话框冒烟：{len(factories) - failures}/{len(factories)} 通过")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
