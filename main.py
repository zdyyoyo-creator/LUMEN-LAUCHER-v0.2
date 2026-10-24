"""Lumen Launcher 入口。

用法：
    python main.py                       # 正常启动 GUI
    python main.py --data-dir D:\\data    # 指定数据目录（便携/多开）
    python main.py --debug               # 打开调试日志
    python main.py --smoke --screenshot shot.png   # 冒烟测试：启动 → 截图 → 退出
"""

from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from lumen import APP_DISPLAY_NAME, APP_ID, APP_VERSION
from lumen.config import AppPaths, Settings, resolve_data_root
from lumen.ui import theme
from lumen.utils import log as log_utils
from lumen.utils.errors import LumenError

log = log_utils.get("main")


def _install_excepthook() -> None:
    """兜底：任何未捕获异常都弹窗 + 记日志，而不是让进程静默退出。"""

    def _hook(exc_type, exc, tb) -> None:  # noqa: ANN001
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            log.error("未捕获异常：%s\n%s", exc, detail)
        except Exception:  # noqa: BLE001
            pass
        try:
            from lumen.ui.dialogs.common import MessageDialog

            MessageDialog.tell(
                None,
                "启动器遇到了一个异常",
                f"{exc_type.__name__}: {exc}",
                details=detail,
                level="error",
            )
        except Exception:  # noqa: BLE001 - 连弹窗都失败时只能落到日志
            print(detail, file=sys.stderr)

    sys.excepthook = _hook


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lumen", description=f"{APP_DISPLAY_NAME} {APP_VERSION}")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认：程序目录下 data，或用户数据目录）")
    parser.add_argument("--debug", action="store_true", help="输出调试日志")
    parser.add_argument("--theme", choices=["dark", "light"], default=None, help="临时覆盖主题")
    parser.add_argument("--page", choices=["versions", "browse", "modpacks", "instances", "settings"], default=None, help="启动后切换到的页面")
    parser.add_argument("--smoke", action="store_true", help="冒烟测试：启动后自动退出")
    parser.add_argument("--smoke-ms", type=int, default=2500, help="冒烟测试等待毫秒数")
    parser.add_argument("--screenshot", default=None, help="冒烟测试时把窗口截图保存到该路径")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ---- 路径与日志 ----
    try:
        data_root = resolve_data_root(args.data_dir)
    except LumenError as exc:
        print(f"无法初始化数据目录：{exc}", file=sys.stderr)
        return 2
    paths = AppPaths(data_root=data_root).ensure()
    log_utils.setup(paths.logs_dir, debug=args.debug)
    log.info("%s %s 启动，数据目录：%s", APP_DISPLAY_NAME, APP_VERSION, paths.data_root)

    # ---- Qt ----
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_ID)
    _install_excepthook()

    settings = Settings(paths)
    if args.theme:
        settings.set("theme", args.theme)

    from lumen.ui.context import AppContext
    from lumen.ui.glyphs import glyph_pixmap
    from lumen.ui.main_window import MainWindow

    ctx = AppContext(paths, settings)
    palette = theme.apply_theme(app, settings.theme)
    app.setWindowIcon(QIcon(glyph_pixmap("spark", 64, palette.primary, 1.4)))

    window = MainWindow(ctx)
    window.show()
    if args.page:
        window.show_page(args.page)

    if args.smoke:
        def _finish() -> None:
            if args.screenshot:
                try:
                    target = Path(args.screenshot).expanduser().resolve()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    pixmap = window.grab()
                    pixmap.save(str(target))
                    log.info("窗口截图已保存：%s（%s）", target, pixmap.size())
                except Exception as exc:  # noqa: BLE001
                    log.error("截图失败：%s", exc)
            log.info("冒烟测试结束，退出")
            app.quit()

        QTimer.singleShot(max(500, int(args.smoke_ms)), _finish)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
