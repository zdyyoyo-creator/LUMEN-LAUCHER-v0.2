"""资源目录链路验证：Modrinth 搜索 → 选版本 → 真实下载并校验。

    python tools/catalog_check.py --data-dir D:\\LumenLauncher\\data --query sodium

可加 ``--pack "fabulously optimized"`` 额外验证整合包的搜索与版本获取（不下载整合包）。
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lumen.config import AppPaths, Settings, resolve_data_root  # noqa: E402
from lumen.core.catalog import (  # noqa: E402
    CurseForgeClient,
    ModrinthClient,
    make_client,
    pick_best_version,
)
from lumen.core.downloader import DownloadTask, Downloader  # noqa: E402
from lumen.core.mirrors import MirrorSelector  # noqa: E402
from lumen.utils import log as log_utils  # noqa: E402
from lumen.utils.fsutil import ensure_dir, human_size  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="资源目录链路验证")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--query", default="sodium")
    parser.add_argument("--loader", default="fabric")
    parser.add_argument("--mc", default="1.21.4")
    parser.add_argument("--pack", default="fabulously optimized")
    parser.add_argument("--no-download", action="store_true")
    args = parser.parse_args()

    paths = AppPaths(data_root=resolve_data_root(args.data_dir)).ensure()
    log_utils.setup(paths.logs_dir)
    settings = Settings(paths)
    selector = MirrorSelector(settings, paths)
    client = ModrinthClient()

    print(f"数据目录：{paths.data_root}")
    print("=" * 90)

    # ---- 1. 搜索 ----
    t0 = time.monotonic()
    hits = client.search(args.query, project_type="mod", loader=args.loader, game_version=args.mc, limit=8)
    print(f"搜索「{args.query}」：{len(hits)} 个结果（{time.monotonic() - t0:.1f}s）")
    for hit in hits[:5]:
        print(f"  · {hit.title:<28} ↓{hit.downloads_text:<8} [{', '.join(hit.categories[:3])}]")
    assert hits, "搜索没有结果"

    target = hits[0]
    print(f"\n选中：{target.title}（{target.slug}）")

    # ---- 2. 版本 ----
    t0 = time.monotonic()
    versions = client.versions(target.project_id, loader=args.loader, game_version=args.mc)
    print(f"版本列表：{len(versions)} 个（{time.monotonic() - t0:.1f}s）")
    for version in versions[:4]:
        print(f"  · {version.label}  {human_size(version.file_size)}  {version.filename}")
    assert versions, "没有可用版本"
    best = pick_best_version(versions, loader=args.loader, game_version=args.mc)
    assert best is not None and best.download_url, "没有可下载的文件"
    print(f"自动选择：{best.label} → {best.filename}（{human_size(best.file_size)}）")

    # ---- 3. 真实下载 + 校验 ----
    if not args.no_download:
        target_dir = ensure_dir(paths.cache_dir / "catalog-test")
        destination = target_dir / (best.filename or "download.jar")
        task = DownloadTask(
            url=best.download_url,
            path=destination,
            sha1=best.sha1 or None,
            size=best.file_size or None,
            label=best.filename or target.title,
        )
        t0 = time.monotonic()
        report = Downloader(selector, max_workers=2, retries=2).run([task], label="目录下载测试")
        elapsed = time.monotonic() - t0
        assert report.failed == 0, report.errors
        assert destination.exists(), "下载后文件不存在"
        actual = hashlib.sha1(destination.read_bytes()).hexdigest()
        if best.sha1:
            assert actual == best.sha1.lower(), f"SHA1 不匹配：{actual} != {best.sha1}"
        print(f"\n下载完成：{destination.name}（{human_size(destination.stat().st_size)}，{elapsed:.1f}s）")
        print(f"  SHA1 校验：{'通过' if best.sha1 else '无哈希可校验'}")
        destination.unlink()

    # ---- 4. 整合包 ----
    if args.pack:
        t0 = time.monotonic()
        packs = client.search(args.pack, project_type="modpack", limit=5)
        print(f"\n整合包搜索「{args.pack}」：{len(packs)} 个结果（{time.monotonic() - t0:.1f}s）")
        assert packs, "整合包搜索没有结果"
        pack = packs[0]
        pack_versions = client.versions(pack.project_id)
        assert pack_versions, "整合包没有版本"
        latest = pack_versions[0]
        print(f"  · {pack.title} 最新 {latest.version_number}  {human_size(latest.file_size)}")
        print(f"    下载链接：{latest.download_url[:80]}…")
        assert latest.download_url, "整合包缺少下载链接"

    # ---- 5. CurseForge 无 Key 的行为 ----
    cf = make_client("curseforge", curseforge_key="")
    assert isinstance(cf, CurseForgeClient) and not cf.available
    try:
        cf.search("jei")
    except Exception as exc:  # noqa: BLE001
        print(f"\nCurseForge 无 Key 提示：{type(exc).__name__}: {str(exc)[:50]}…")
    else:
        print("\n[WARN] CurseForge 无 Key 竟然没有报错")

    print("\n资源目录链路：OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
