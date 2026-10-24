"""把项目打包成一个可直接上传 GitHub / 发给别人的 zip。

    python tools/make_repo_zip.py
    → dist/LumenLauncher-src.zip   （解压后是 LumenLauncher/ 目录）

只打包源码与文档，不含 data/、dist/、shorts/、缓存等运行时垃圾。
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"

EXCLUDE_DIRS = {
    ".git",
    ".github",  # CI 配置单独保留？这里保留，见下方 INCLUDE 覆盖
    "data",
    "dist",
    "build",
    "shots",
    "__pycache__",
    ".venv",
    "venv",
    "smoke-data",
    "cache",
    "runtimes",
}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log"}
ROOT_FOLDER = "LumenLauncher"


def iter_files():
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        if path.suffix in EXCLUDE_SUFFIX:
            continue
        if rel.name in {"launcher.log", ".DS_Store", "Thumbs.db"}:
            continue
        yield path, rel


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    target = DIST / "LumenLauncher-src.zip"
    files = list(iter_files())
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, rel in files:
            archive.write(path, arcname=str(Path(ROOT_FOLDER) / rel))
    size_mb = target.stat().st_size / 1024 / 1024
    print(f"已生成：{target}")
    print(f"  文件数：{len(files)}，大小：{size_mb:.2f} MB")
    print(f"  解压后目录：{ROOT_FOLDER}/（含 main.py、lumen/、README.md、LICENSE、.github/ 等）")
    print("  GitHub：新建仓库后把解压内容推上去即可（.gitignore 已排除运行时数据）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
