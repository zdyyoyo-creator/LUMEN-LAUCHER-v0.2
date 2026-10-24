"""Modrinth 整合包（.mrpack）解析。

``modrinth.index.json`` 结构（formatVersion 1）::

    {
      "formatVersion": 1,
      "game": "minecraft",
      "versionId": "1.0.0",
      "name": "Example Pack",
      "dependencies": {"minecraft": "1.20.1", "fabric-loader": "0.15.0"},
      "files": [
        {"path": "mods/foo.jar", "hashes": {"sha1": "...", "sha512": "..."},
         "env": {"client": "required", "server": "optional"},
         "downloads": ["https://cdn.modrinth.com/..."], "fileSize": 12345}
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.errors import ModpackError
from ..utils.fsutil import read_zip_member

INDEX_NAME = "modrinth.index.json"
OVERRIDE_PREFIXES = ("overrides/", "client-overrides/")

LOADER_KEYS = {
    "fabric-loader": "fabric",
    "quilt-loader": "quilt",
    "forge": "forge",
    "neoforge": "neoforge",
}


@dataclass
class MrpackFile:
    path: str
    urls: list[str] = field(default_factory=list)
    sha1: str = ""
    sha512: str = ""
    size: int = 0
    env_client: str = "required"
    env_server: str = "required"

    @property
    def is_client_relevant(self) -> bool:
        return self.env_client != "unsupported"

    @property
    def label(self) -> str:
        return Path(self.path).name


@dataclass
class MrpackIndex:
    format_version: int
    game: str
    version_id: str
    name: str
    summary: str
    files: list[MrpackFile]
    dependencies: dict[str, str]
    has_overrides: bool = False

    @property
    def mc_version(self) -> str:
        return str(self.dependencies.get("minecraft", "")).strip()

    @property
    def loader_type(self) -> str:
        for key, kind in LOADER_KEYS.items():
            if key in self.dependencies:
                return kind
        return ""

    @property
    def loader_version(self) -> str:
        for key in LOADER_KEYS:
            if key in self.dependencies:
                return str(self.dependencies[key]).strip()
        return ""

    @property
    def client_files(self) -> list[MrpackFile]:
        return [item for item in self.files if item.is_client_relevant]

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.client_files)


def is_mrpack(archive: Path) -> bool:
    try:
        import zipfile

        with zipfile.ZipFile(archive) as zf:
            return INDEX_NAME in zf.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def read_mrpack(archive: Path) -> MrpackIndex | None:
    """解析 .mrpack；不是 mrpack 返回 None。"""
    archive = Path(archive)
    try:
        import zipfile

        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModpackError(f"无法读取压缩包：{exc}") from exc
    if INDEX_NAME not in names:
        return None
    try:
        payload = json.loads(read_zip_member(archive, INDEX_NAME).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ModpackError(f"modrinth.index.json 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise ModpackError("modrinth.index.json 不是对象")

    files: list[MrpackFile] = []
    for raw in payload.get("files") or []:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path") or "").replace("\\", "/").lstrip("/")
        if not path or ".." in path.split("/"):
            continue
        hashes = raw.get("hashes") or {}
        urls = [str(u) for u in (raw.get("downloads") or []) if u]
        if not urls:
            continue
        env = raw.get("env") or {}
        files.append(
            MrpackFile(
                path=path,
                urls=urls,
                sha1=str(hashes.get("sha1") or ""),
                sha512=str(hashes.get("sha512") or ""),
                size=int(raw.get("fileSize") or 0),
                env_client=str(env.get("client") or "required"),
                env_server=str(env.get("server") or "required"),
            )
        )

    dependencies = {
        str(key): str(value)
        for key, value in (payload.get("dependencies") or {}).items()
        if value is not None
    }
    has_overrides = any(name.startswith(OVERRIDE_PREFIXES) for name in names)
    return MrpackIndex(
        format_version=int(payload.get("formatVersion") or 1),
        game=str(payload.get("game") or "minecraft"),
        version_id=str(payload.get("versionId") or ""),
        name=str(payload.get("name") or "未命名整合包"),
        summary=str(payload.get("summary") or ""),
        files=files,
        dependencies=dependencies,
        has_overrides=has_overrides,
    )


def override_prefixes_in(archive: Path) -> list[str]:
    """返回实际存在的 overrides 前缀（顺序：overrides 先，client-overrides 后）。"""
    try:
        import zipfile

        with zipfile.ZipFile(archive) as zf:
            names = [n.replace("\\", "/") for n in zf.namelist()]
    except (OSError, zipfile.BadZipFile):
        return []
    prefixes: list[str] = []
    for prefix in OVERRIDE_PREFIXES:
        if any(name.startswith(prefix) for name in names):
            prefixes.append(prefix.rstrip("/"))
    return prefixes


def describe(index: MrpackIndex) -> dict[str, Any]:
    return {
        "name": index.name,
        "mc_version": index.mc_version,
        "loader": index.loader_type,
        "loader_version": index.loader_version,
        "files": len(index.client_files),
        "size": index.total_size,
        "has_overrides": index.has_overrides,
    }
