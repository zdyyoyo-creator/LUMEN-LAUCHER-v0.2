"""把启动器打包成「单个 .pyz 文件」（Python zipapp）。

    python tools/make_zipapp.py
    python dist/LumenLauncher.pyz            # 直接运行（需要已装 PySide6 + requests）

好处：只有一个文件，方便放到任意文件夹、U 盘或直接发给别人。
注意：zipapp 只打包本项目代码，Python 解释器与 PySide6 仍需在目标机器上安装；
      想要免安装的独立 exe，请用 tools/build.py（PyInstaller）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipapp
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "dist"


def main() -> int:
    target = BUILD / "LumenLauncher.pyz"
    BUILD.mkdir(parents=True, exist_ok=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo")

    with tempfile.TemporaryDirectory(prefix="lumen-zipapp-") as tmp:
        staging = Path(tmp) / "app"
        staging.mkdir(parents=True)

        # zipapp 的入口必须是 __main__.py
        shutil.copy2(ROOT / "main.py", staging / "__main__.py")
        shutil.copytree(ROOT / "lumen", staging / "lumen", ignore=ignore)

        # 图标等资源可选（不存在也不影响运行）
        assets = ROOT / "assets"
        if assets.is_dir():
            shutil.copytree(assets, staging / "assets", ignore=ignore)

        zipapp.create_archive(
            staging,
            target,
            interpreter="/usr/bin/env python3",
            compressed=True,
        )

    size_kb = target.stat().st_size / 1024
    print(f"已生成：{target}（{size_kb:.0f} KB）")
    print(f"运行：python \"{target}\"")
    print("说明：目标机器需要 Python 3.10+ 与 pip install -r requirements.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
