"""打包脚本：生成图标并调用 PyInstaller。

    python tools/build.py            # 在 dist/ 下生成 LumenLauncher/
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    python = sys.executable
    icon = ROOT / "assets" / "icon.ico"
    if not icon.exists():
        subprocess.run([python, str(ROOT / "tools" / "make_icon.py")], check=False)

    command = [
        python,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--name",
        "LumenLauncher",
        "--collect-submodules",
        "lumen",
        "--exclude-module",
        "tkinter",
        str(ROOT / "main.py"),
    ]
    if icon.exists():
        command[command.index("--name") : command.index("--name")] = ["--icon", str(icon)]

    print("执行：", " ".join(command))
    try:
        subprocess.run(command, cwd=str(ROOT), check=True)
    except FileNotFoundError:
        print("未找到 PyInstaller，请先执行：pip install pyinstaller")
        return 2
    print(f"\n打包完成：{ROOT / 'dist' / 'LumenLauncher'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
