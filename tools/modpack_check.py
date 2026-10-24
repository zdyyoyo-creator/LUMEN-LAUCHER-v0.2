"""整合包导入验证：现场构造一个最小 .mrpack → 解析 → 导入 → 校验落盘结果。

    python tools/modpack_check.py --data-dir D:\\LumenLauncher\\data

为了不依赖外部整合包，脚本用 Minecraft 官方库里的一个小 jar 充当「mod」，
并把 overrides 目录、instance.json、mods 启停都验证一遍。导入完成后会删除测试实例。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests  # noqa: E402

from lumen.config import AppPaths, Settings, resolve_data_root  # noqa: E402
from lumen.core.instances import InstanceStore  # noqa: E402
from lumen.core.mirrors import MirrorSelector  # noqa: E402
from lumen.modpack.importer import import_modpack, inspect_modpack  # noqa: E402
from lumen.utils import log as log_utils  # noqa: E402
from lumen.utils.errors import LumenError  # noqa: E402
from lumen.utils.fsutil import human_size  # noqa: E402

DEMO_URL = "https://libraries.minecraft.net/com/google/code/gson/gson/2.10.1/gson-2.10.1.jar"
DEMO_NAME = "gson-2.10.1.jar"


def build_mrpack(target: Path, *, sha1: str, size: int) -> None:
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": "1.0.0-smoke",
        "name": "Lumen 冒烟整合包",
        "summary": "由 tools/modpack_check.py 自动生成",
        "dependencies": {"minecraft": "1.21.4"},
        "files": [
            {
                "path": f"mods/{DEMO_NAME}",
                "hashes": {"sha1": sha1, "sha512": "0" * 128},
                "env": {"client": "required", "server": "required"},
                "downloads": [DEMO_URL],
                "fileSize": size,
            }
        ],
    }
    with zipfile.ZipFile(target, "w") as zf:
        zf.writestr("modrinth.index.json", json.dumps(index, ensure_ascii=False))
        zf.writestr("overrides/config/lumen-smoke.toml", 'enabled = true\nname = "lumen"\n')
        zf.writestr("overrides/mods/overrides-note.txt", "overrides extracted ok\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--keep", action="store_true", help="保留测试实例便于人工检查")
    args = parser.parse_args()

    paths = AppPaths(data_root=resolve_data_root(args.data_dir)).ensure()
    log_utils.setup(paths.logs_dir)
    settings = Settings(paths)
    selector = MirrorSelector(settings, paths)
    store = InstanceStore(paths)
    print(f"数据目录：{paths.data_root}")

    # 1) 取一个真实小文件作为 mod，并算出 sha1
    work = Path(tempfile.mkdtemp(prefix="lumen-mrpack-"))
    demo = work / DEMO_NAME
    print(f"下载示例文件：{DEMO_URL}")
    response = requests.get(DEMO_URL, timeout=(10, 60))
    response.raise_for_status()
    demo.write_bytes(response.content)
    sha1 = hashlib.sha1(response.content).hexdigest()
    size = len(response.content)
    print(f"  {size} 字节，sha1={sha1}")

    # 2) 构造 mrpack
    archive = work / "lumen-smoke.mrpack"
    build_mrpack(archive, sha1=sha1, size=size)
    print(f"已生成测试整合包：{archive.name}（{human_size(archive.stat().st_size)}）")

    # 3) 解析
    plan = inspect_modpack(archive)
    print("解析结果：", plan.summary())
    assert plan.source == "mrpack", plan.source
    assert plan.mc_version == "1.21.4", plan.mc_version
    assert plan.file_count == 1, plan.file_count
    assert "overrides" in plan.override_prefixes, plan.override_prefixes
    print("  [PASS] mrpack 解析")

    # 4) 导入
    instance = None
    try:
        report = import_modpack(
            selector,
            paths,
            store,
            plan,
            mc_version=plan.mc_version,
            loader_type="",
            loader_version="",
            instance_name="冒烟整合包（可删除）",
            progress=lambda stage: print(f"  [{stage.percent * 100:5.1f}%] {stage.stage:<10} {stage.message}")
            if int(stage.percent * 100) % 20 == 0
            else None,
        )
        instance = report.instance
        print(f"导入完成：{instance.name} → {instance.game_dir}")

        # 5) 校验
        config_file = instance.game_dir / "config" / "lumen-smoke.toml"
        note_file = instance.game_dir / "mods" / "overrides-note.txt"
        mod_file = instance.game_dir / "mods" / DEMO_NAME
        assert config_file.exists(), f"缺少 overrides 文件：{config_file}"
        assert note_file.exists(), f"缺少 overrides 文件：{note_file}"
        assert mod_file.exists(), f"缺少下载文件：{mod_file}"
        assert hashlib.sha1(mod_file.read_bytes()).hexdigest() == sha1, "mod 文件 sha1 不匹配"
        print("  [PASS] overrides 解压 + 文件下载 + SHA1")
        meta = json.loads((instance.root / "instance.json").read_text(encoding="utf-8"))
        assert meta["modpack"]["source"] == "mrpack", meta.get("modpack")
        print("  [PASS] instance.json 记录了整合包来源")

        # 6) mods 启停
        mods = store.list_mods(instance)
        assert len(mods) >= 1, mods
        store.toggle_mod(mods[0])
        assert not store.list_mods(instance)[0].enabled
        store.toggle_mod(store.list_mods(instance)[0])
        print("  [PASS] mods 启用/禁用")
    except (AssertionError, LumenError) as exc:
        print(f"  [FAIL] {exc.__class__.__name__}: {exc}")
        return 1
    finally:
        if instance is not None and not args.keep:
            store.delete(instance)
            print(f"已清理测试实例：{instance.name}")
        import shutil

        shutil.rmtree(work, ignore_errors=True)

    print("\n整合包导入链路：OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
