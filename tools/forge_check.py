"""Forge / NeoForge 安装链路验证：真实下载安装器、运行官方安装器、校验产物。

    python tools/forge_check.py --data-dir D:\\LumenLauncher\\data --mc 1.21.4 --kind forge

会打印安装器实时输出与阶段进度，最后验证：
1. 安装器生成的版本 json 能沿 inheritsFrom 合并出完整 libraries/arguments；
2. 版本目录、natives、实例元数据（version_id_override）都正确；
3. 启动前自检无 error。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lumen.auth.account import offline_account  # noqa: E402
from lumen.config import AppPaths, Settings, resolve_data_root  # noqa: E402
from lumen.core.installer import VersionInstaller, verify_version  # noqa: E402
from lumen.core.instances import InstanceStore  # noqa: E402
from lumen.core.loaders import LOADER_LABELS, available_loader_versions  # noqa: E402
from lumen.core.mirrors import MirrorSelector  # noqa: E402
from lumen.core.preflight import build_launch_plan, finish_prepare  # noqa: E402
from lumen.core.version_store import inherited_chain, resolve_version  # noqa: E402
from lumen.java.detect import pick_java, scan_java_runtimes  # noqa: E402
from lumen.utils import log as log_utils  # noqa: E402
from lumen.utils.versions import required_java_major  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Forge/NeoForge 安装验证")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--mc", default="1.21.4")
    parser.add_argument("--kind", choices=["forge", "neoforge"], default="forge")
    parser.add_argument("--version", default="", help="加载器版本（默认取最新）")
    parser.add_argument("--keep", action="store_true", help="保留测试实例")
    parser.add_argument("--launch", action="store_true", help="安装完顺便启动验证")
    parser.add_argument("--wait", type=int, default=40)
    args = parser.parse_args()

    paths = AppPaths(data_root=resolve_data_root(args.data_dir)).ensure()
    log_utils.setup(paths.logs_dir, debug=True)
    settings = Settings(paths)
    selector = MirrorSelector(settings, paths)
    store = InstanceStore(paths)
    label = LOADER_LABELS.get(args.kind, args.kind)
    print(f"数据目录：{paths.data_root}")

    # ---- 1. 版本 ----
    t0 = time.monotonic()
    versions = available_loader_versions(selector, args.kind, args.mc)
    if not versions:
        print(f"没有适用于 {args.mc} 的 {label} 版本")
        return 2
    target = args.version or versions[0].version
    print(f"{label} 可用 {len(versions)} 个版本（{time.monotonic() - t0:.1f}s），选用 {target}")

    # ---- 2. 实例 ----
    instance = store.create(f"{label} 验证（可删除）", args.mc)
    print(f"已创建实例：{instance.name} → {instance.game_dir}")

    # ---- 3. 安装 ----
    java_runtimes = scan_java_runtimes(paths)
    required = required_java_major(args.mc)
    java = pick_java(java_runtimes, required)
    if java is None:
        print(f"没有可用 Java（需要 {required}），无法运行安装器")
        return 3
    print(f"使用 Java：{java.major} · {java.path}")

    installer = VersionInstaller(selector, paths, store, instance)
    last_stage = [""]
    line_count = [0]

    def on_progress(stage) -> None:
        key = f"{stage.stage}:{int(stage.percent * 100) // 5}"
        if key == last_stage[0]:
            return
        last_stage[0] = key
        detail = f" · {stage.detail}" if stage.detail else ""
        print(f"  [{stage.percent * 100:5.1f}%] {stage.message[:70]}{detail}", flush=True)

    def on_output(line: str) -> None:
        line_count[0] += 1
        if line_count[0] % 25 == 0 or "onProgress" in line or "Extracting" in line or "Downloading" in line:
            print(f"    │ {line[:150]}", flush=True)

    started = time.monotonic()
    try:
        result = installer.install_forge(
            args.mc,
            target,
            java_path=str(java.path),
            kind=args.kind,
            progress=on_progress,
            on_output=on_output,
        )
    except Exception as exc:  # noqa: BLE001
        hint = getattr(exc, "hint", "")
        print(f"\n安装失败：{exc}")
        if hint:
            print(f"提示：{hint[:600]}")
        return 4
    elapsed = time.monotonic() - started
    print(f"\n安装完成：{result.summary()}，用时 {elapsed:.1f}s，安装器输出 {line_count[0]} 行")
    print(f"版本目录：{result.version_id}")

    # ---- 4. 校验 ----
    try:
        chain = inherited_chain(instance, result.version_id)
        print(f"继承链：{' → '.join(chain)}")
        merged = resolve_version(instance, result.version_id)
        print(f"合并后：libraries={len(merged.get('libraries', []))}, mainClass={merged.get('mainClass')}")
        assert merged.get("mainClass"), "合并后缺少 mainClass"
        assert len(merged.get("libraries", [])) > 5, "合并后 libraries 过少"
        assert instance.version_id == result.version_id, f"实例启动版本不一致：{instance.version_id}"
        natives = instance.natives_dir(result.version_id)
        assert natives.is_dir() and any(natives.iterdir()), "natives 未解压"
        print("  [PASS] inheritsFrom 合并 / 版本目录 / natives / 实例元数据")

        problems = verify_version(instance, result.version_id, paths)
        assert not problems, f"完整性检查失败：{problems}"
        print("  [PASS] 完整性检查")

        plan = build_launch_plan(paths, settings, instance, selector=selector, runtimes=java_runtimes)
        errors = [issue for issue in plan.issues if issue.is_error]
        for issue in plan.issues:
            print(f"    {issue.level:<7} {issue.title}：{issue.detail}")
        assert not errors, f"启动前自检失败：{[i.title for i in errors]}"
        prepared = finish_prepare(paths, settings, plan, offline_account("ForgeTester"))
        joined = " ".join(prepared.command)
        assert result.version_id in joined, "启动命令里的版本号不正确"
        print(f"  [PASS] 启动前自检 + 命令组装（{len(prepared.command)} 个参数）")
    except AssertionError as exc:
        print(f"  [FAIL] {exc}")
        return 5

    # ---- 5. 可选启动 ----
    if args.launch:
        from lumen.core.launcher import GameProcess

        lines: list[str] = []
        exit_info: dict[str, float] = {}

        def _on_line(text: str) -> None:
            lines.append(text)

        def _on_exit(code: int, seconds: float) -> None:
            exit_info["code"] = code
            exit_info["elapsed"] = seconds

        process = GameProcess(prepared, on_line=_on_line, on_exit=_on_exit).start()
        deadline = time.time() + max(5, args.wait)
        while time.time() < deadline and process.running:
            time.sleep(0.5)
        alive = process.running
        if alive:
            print(f"\n游戏在 {args.wait}s 后仍在运行 → 启动成功（PID {process.pid}）")
            process.kill()
        else:
            print(f"\n游戏已退出：退出码 {exit_info.get('code')}")
        for line in lines[-12:]:
            print(f"    │ {line[:150]}")
        if not alive and "Setting user" not in "\n".join(lines):
            print("  [WARN] 未看到游戏初始化标志，请查看日志")

    if not args.keep:
        store.delete(instance)
        print(f"已清理测试实例：{instance.name}")

    print(f"\n{label} 安装链路：OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
