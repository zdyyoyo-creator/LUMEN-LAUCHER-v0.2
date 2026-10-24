"""版本文件读取与 inheritsFrom 链合并。

Forge / NeoForge 安装器生成的版本是「继承原版」的：本地 json 里只有
``inheritsFrom: "1.20.1"`` 和自己的库/主类，原版的 libraries、arguments、
assetIndex 需要沿链合并后才能用于启动与完整性校验。

这里把合并逻辑收敛到一个模块，launcher / preflight / installer 共用，
避免出现「启动时用一个 json、校验时用另一个 json」的不一致。
"""

from __future__ import annotations

import json

from ..utils.errors import InstallError
from ..utils.log import get as get_logger
from .instances import Instance
from .manifest import merge_version_data

log = get_logger("core.version_store")

#: 防止损坏的 manifest 造成无限递归
MAX_CHAIN = 8


def read_raw_json(instance: Instance, version_id: str) -> dict:
    path = instance.version_json(version_id)
    if not path.exists():
        raise InstallError(
            f"缺少版本文件：{path.name}",
            hint="请先在版本管理里安装该版本",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise InstallError(f"版本文件损坏：{exc}", hint=str(path)) from exc
    if not isinstance(data, dict):
        raise InstallError(f"版本文件结构异常：{path.name}")
    return data


def resolve_version(
    instance: Instance,
    version_id: str,
    *,
    _depth: int = 0,
    _chain: tuple[str, ...] = (),
) -> dict:
    """读取版本 json 并沿 ``inheritsFrom`` 合并父版本，返回可直接使用的完整数据。"""
    data = read_raw_json(instance, version_id)
    parent_id = data.get("inheritsFrom")
    if not parent_id:
        return data

    parent_id = str(parent_id)
    if _depth >= MAX_CHAIN:
        raise InstallError(
            f"版本继承链过深：{version_id}",
            hint="manifest 里可能存在异常的 inheritsFrom",
        )
    if parent_id in _chain or parent_id == version_id:
        raise InstallError(
            f"版本继承链存在循环：{' → '.join(_chain + (version_id, parent_id))}",
        )

    parent = resolve_version(
        instance,
        parent_id,
        _depth=_depth + 1,
        _chain=_chain + (version_id,),
    )
    merged = merge_version_data(parent, data, version_id=version_id)
    log.debug("已合并版本 %s ← %s（libraries=%d）", version_id, parent_id, len(merged.get("libraries") or []))
    return merged


def inherited_chain(instance: Instance, version_id: str) -> list[str]:
    """返回从当前版本到根的 id 列表（排障用）。"""
    chain: list[str] = []
    current = version_id
    for _ in range(MAX_CHAIN):
        chain.append(current)
        try:
            data = read_raw_json(instance, current)
        except InstallError:
            break
        parent = data.get("inheritsFrom")
        if not parent:
            break
        current = str(parent)
    return chain
