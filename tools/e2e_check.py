"""端到端验证：真实拉清单 → 测速 → 安装原版 → 校验 → 启动进程 → 打印日志。

这是「最小可用主流程」的自动化验收脚本，不需要 GUI：

    python tools/e2e_check.py --data-dir D:\\LumenLauncher\\data --version 1.21.4 --launch

它会：
1. 拉取版本清单并测速（官方 vs 镜像）；
2. 创建（或复用）实例，安装客户端 / 依赖库 / 资源 / natives；
3. 调用 ``verify_version`` 检查完整性；
4. 用真实 Java 组装命令并启动，采集日志 30–90 秒后结束进程；
5. 打印结论：启动成功 / 崩溃原因（自动分析）。
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
from lumen.core.crash import analyze_lines  # noqa: E402
from lumen.core.instances import InstanceStore  # noqa: E402
from lumen.core.installer import VersionInstaller, verify_version  # noqa: E402
from lumen.core.launcher import GameProcess, LaunchOptions, prepare_launch  # noqa: E402
from lumen.core.manifest import load_manifest  # noqa: E402
from lumen.core.mirrors import MirrorSelector  # noqa: E402
from lumen.java.detect import pick_java, scan_java_runtimes  # noqa: E402
from lumen.utils import log as log_utils  # noqa: E402
from lumen.utils.fsutil import human_size, human_speed  # noqa: E402
from lumen.utils.versions import required_java_major  # noqa: E402

log = log_utils.get("tools.e2e")


_last_print = [0.0, ""]


def _progress(snapshot) -> None:
    """节流输出：每 1.5 秒或阶段变化时打印，避免管道阻塞拖慢下载。"""
    now = time.monotonic()
    stage = getattr(snapshot, "stage", "")
    if now - _last_print[0] < 1.5 and stage == _last_print[1]:
        return
    _last_print[0] = now
    _last_print[1] = stage
    percent = getattr(snapshot, "percent", 0.0)
    message = getattr(snapshot, "message", "")
    speed = getattr(snapshot, "speed", 0.0)
    done = getattr(snapshot, "files_done", 0)
    total = getattr(snapshot, "files_total", 0)
    print(
        f"[{percent * 100:5.1f}%] {stage:<12} {message[:36]:<38} {done}/{total} {human_speed(speed)}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Lumen Launcher 端到端验证")
    parser.add_argument("--data-dir", default=None, help="数据目录（默认用环境变量或程序目录 data）")
    parser.add_argument("--version", default="1.21.4", help="要安装的 Minecraft 版本")
    parser.add_argument("--instance", default="", help="实例名（默认 <version> 原版）")
    parser.add_argument("--launch", action="store_true", help="安装完成后尝试真实启动")
    parser.add_argument("--no-assets", action="store_true", help="跳过资源文件下载（快速验证安装链路）")
    parser.add_argument("--wait", type=int, default=45, help="启动后观察秒数")
    parser.add_argument("--source", choices=["auto", "official", "mirror"], default="auto")
    args = parser.parse_args()

    paths = AppPaths(data_root=resolve_data_root(args.data_dir)).ensure()
    log_utils.setup(paths.logs_dir, debug=True)
    settings = Settings(paths)
    settings.download_source = args.source
    selector = MirrorSelector(settings, paths)
    store = InstanceStore(paths)

    print(f"数据目录：{paths.data_root}")
    print("=" * 100)

    # ---- 1. 测速 + 清单 ----
    probes = selector.probe_all(force=True)
    print(f"测速：{selector.describe_sources()}  → 优选 {selector.preferred_source()}")
    manifest = load_manifest(selector, paths, force=True)
    remote = manifest.get(args.version)
    if remote is None:
        print(f"版本不存在：{args.version}")
        return 2
    print(f"清单：{len(manifest)} 个版本，目标 {remote.id}（{remote.type}，{remote.date}）")

    # ---- 2. 实例 ----
    instance_name = args.instance or f"{args.version} 原版"
    instance = next((item for item in store.list_instances() if item.name == instance_name), None)
    if instance is None:
        instance = store.create(instance_name, args.version)
        print(f"已创建实例：{instance.name}（{instance.game_dir}）")
    else:
        print(f"复用实例：{instance.name}")

    # ---- 3. 安装 ----
    installer = VersionInstaller(selector, paths, store, instance)
    started = time.time()
    try:
        result = installer.install(args.version, progress=_progress, with_assets=not args.no_assets)
    except Exception as exc:  # noqa: BLE001
        print(f"\n安装失败：{exc}")
        return 3
    elapsed = time.time() - started
    print(f"\n安装完成：{result.summary()}  用时 {elapsed:.1f}s")
    if result.errors:
        print(f"  错误（前 3 条）：{result.errors[:3]}")
    if result.warnings:
        print(f"  警告（前 3 条）：{result.warnings[:3]}")

    # ---- 4. 完整性 ----
    problems = verify_version(instance, args.version, paths)
    if problems:
        print("完整性检查未通过：")
        for item in problems:
            print(f"  - {item}")
        if not args.launch:
            return 4
    else:
        print("完整性检查：通过（版本文件 / 客户端 jar / 依赖库 / natives 齐全）")

    # ---- 5. 启动 ----
    if not args.launch:
        print("（未指定 --launch，跳过启动验证）")
        return 0

    runtimes = scan_java_runtimes(paths)
    required = required_java_major(args.version)
    java = pick_java(runtimes, required)
    print(f"Java：{len(runtimes)} 个可用，选中 {java.major if java else '无'}（需要 {required}）")
    if java is None:
        print("没有可用 Java，无法启动")
        return 5

    account = offline_account("E2ETester")
    options = LaunchOptions(java_path=str(java.path), memory_mb=2048)
    prepared = prepare_launch(paths, instance, account, options, version_id=args.version)
    print(f"启动命令：{prepared.command_preview(400)}")
    print(f"日志文件：{prepared.log_file}")

    lines: list[str] = []
    exit_info: dict[str, float] = {}

    def _on_line(text: str) -> None:
        lines.append(text)
        if len(lines) % 20 == 0:
            sys.stdout.write(f"\r  已接收 {len(lines)} 行日志…（最近：{text[:60]}）".ljust(140))
            sys.stdout.flush()

    def _on_exit(code: int, elapsed_seconds: float) -> None:
        exit_info["code"] = code
        exit_info["elapsed"] = elapsed_seconds

    process = GameProcess(prepared, on_line=_on_line, on_exit=_on_exit)
    try:
        process.start()
    except Exception as exc:  # noqa: BLE001
        print(f"\n进程启动失败：{exc}")
        return 6

    deadline = time.time() + max(5, args.wait)
    while time.time() < deadline and process.running:
        time.sleep(0.5)
    alive = process.running
    if alive:
        print(f"\n进程在 {args.wait}s 后仍在运行 → 启动成功（PID {process.pid}）")
        process.kill()
        time.sleep(1.0)
    else:
        print(f"\n进程已退出：退出码 {exit_info.get('code')}，运行 {exit_info.get('elapsed', 0):.1f}s")

    print("\n日志尾部（最后 25 行）：")
    for line in lines[-25:]:
        print(f"  {line[:160]}")

    findings = analyze_lines(
        lines,
        exit_code=int(exit_info.get("code", -1)) if exit_info else None,
        required_java=required,
    )
    if findings:
        print("\n崩溃分析：")
        for finding in findings:
            print(f"  - {finding.title}：{finding.suggestion}")
    else:
        print("\n崩溃分析：没有发现常见错误特征")

    # 判定：进程存活，或日志里出现游戏初始化成功标志
    success_markers = (
        "Setting user:",
        "Backend library:",
        "LWJGL",
        "OpenAL",
        "Sound engine started",
        "Created:",
        "Starting Minecraft",
    )
    joined = "\n".join(lines)
    matched = [marker for marker in success_markers if marker in joined]
    if alive or matched:
        print(f"\n结论：启动链路 OK（命中标志：{matched[:3] or '进程存活'}）")
        return 0
    print("\n结论：启动失败（未进入游戏初始化阶段）")
    return 7


if __name__ == "__main__":
    sys.exit(main())
