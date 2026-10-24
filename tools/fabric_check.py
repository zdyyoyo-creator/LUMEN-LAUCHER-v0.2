"""Fabric / Quilt 加载器安装验证。

    python tools/fabric_check.py --data-dir D:\\LumenLauncher\\data --mc 1.21.4

流程：拉 meta 版本列表 → 创建实例 → 安装原版（复用已有文件）→ 合并 Fabric profile
→ 下载加载器库 → 解压 natives → 启动前自检 → 打印结论。默认结束后删除测试实例。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lumen.auth.account import offline_account  # noqa: E402
from lumen.config import AppPaths, Settings, resolve_data_root  # noqa: E402
from lumen.core.instances import InstanceStore  # noqa: E402
from lumen.core.installer import VersionInstaller  # noqa: E402
from lumen.core.loaders import available_loader_versions  # noqa: E402
from lumen.core.mirrors import MirrorSelector  # noqa: E402
from lumen.core.preflight import build_launch_plan, finish_prepare  # noqa: E402
from lumen.java.detect import scan_java_runtimes  # noqa: E402
from lumen.utils import log as log_utils  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--mc", default="1.21.4")
    parser.add_argument("--kind", choices=["fabric", "quilt"], default="fabric")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    paths = AppPaths(data_root=resolve_data_root(args.data_dir)).ensure()
    log_utils.setup(paths.logs_dir)
    settings = Settings(paths)
    selector = MirrorSelector(settings, paths)
    store = InstanceStore(paths)
    print(f"数据目录：{paths.data_root}")

    versions = available_loader_versions(selector, args.kind, args.mc)
    if not versions:
        print(f"没有 {args.kind} 适用于 {args.mc} 的版本")
        return 2
    print(f"{args.kind} 可用版本 {len(versions)} 个，最新：{versions[0].version}")

    instance = store.create(f"{args.kind} 验证（可删除）", args.mc)
    installer = VersionInstaller(selector, paths, store, instance)
    last_stage = [""]

    def on_progress(stage) -> None:
        percent = int(stage.percent * 100)
        if percent % 20 == 0 and last_stage[0] != f"{stage.stage}{percent}":
            last_stage[0] = f"{stage.stage}{percent}"
            print(f"  [{percent:3d}%] {stage.stage:<10} {stage.message[:50]}")

    try:
        result = installer.install_fabric(
            args.mc,
            versions[0].version,
            kind=args.kind,
            progress=on_progress,
            with_assets=False,  # 资源已在原版实例里装过，避免重复下载
        )
        print(f"安装结果：{result.summary()}")

        version_id = f"{args.mc}-{args.kind}"
        json_path = instance.version_json(version_id)
        assert json_path.exists(), f"缺少合并后的版本文件 {json_path}"
        import json

        data = json.loads(json_path.read_text(encoding="utf-8"))
        print(f"  版本文件：{json_path.name}，libraries={len(data.get('libraries', []))}，mainClass={data.get('mainClass')}")
        assert data.get("mainClass"), "合并后的版本缺少 mainClass"
        assert not data.get("inheritsFrom"), "合并后不应再有 inheritsFrom"
        assert instance.loader_type == args.kind, instance.loader_type
        print("  [PASS] profile 合并与实例元数据")
        assert (instance.natives_dir(version_id) / "lwjgl.dll").exists() or any(
            instance.natives_dir(version_id).iterdir()
        ), "natives 未解压"
        print("  [PASS] natives 解压")

        runtimes = scan_java_runtimes(paths)
        plan = build_launch_plan(
            paths,
            settings,
            instance,
            selector=selector,
            runtimes=runtimes,
            allow_download_java=False,
        )
        errors = [issue for issue in plan.issues if issue.is_error]
        for issue in plan.issues:
            print(f"    {issue.level:<7} {issue.title}：{issue.detail}")
        assert not errors, f"自检发现 {len(errors)} 个错误"
        print(f"  [PASS] 启动前自检（Java {plan.required_java}，内存 {plan.memory_mb}MB）")

        prepared = finish_prepare(paths, settings, plan, offline_account("FabricTester"))
        joined = " ".join(prepared.command)
        assert version_id in joined, "命令行里没有加载器版本号"
        assert "net.fabricmc" in joined or "quilt" in joined.lower(), "classpath 里没有加载器库"
        assert "-cp" in joined or "-classpath" in joined
        print(f"  [PASS] 启动命令组装（{len(prepared.command)} 个参数，classpath 含加载器）")
    except AssertionError as exc:
        print(f"  [FAIL] {exc}")
        return 1
    finally:
        if not args.keep:
            store.delete(instance)
            print(f"已清理测试实例：{instance.name}")

    print(f"\n{args.kind} 安装链路：OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
