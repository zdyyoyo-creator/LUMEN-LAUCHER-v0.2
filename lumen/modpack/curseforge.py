"""CurseForge 整合包（manifest.json）解析。

manifest 里只有 ``projectID`` / ``fileID``，必须通过 CurseForge 官方 API 换取下载直链：
``GET https://api.curseforge.com/v1/mods/{projectID}/files/{fileID}``（需要 x-api-key）。
没有 API Key 时：仍可解析版本与加载器、解压 overrides，并把无法自动下载的文件列成清单，
由用户手动补齐——这是「优雅降级」，而不是直接失败。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..utils.errors import ModpackError
from ..utils.fsutil import read_zip_member
from ..utils.log import get as get_logger
from ..utils.net import get_json

log = get_logger("modpack.curseforge")

MANIFEST_NAME = "manifest.json"
CF_API_BASE = "https://api.curseforge.com/v1"
CF_CDN_FALLBACK = "https://edge.forgecdn.net/files/{a}/{b}/{name}"

LOADER_MAP = {
    "forge": "forge",
    "fabric": "fabric",
    "quilt": "quilt",
    "neoforge": "neoforge",
}


@dataclass
class CurseFile:
    project_id: int
    file_id: int
    required: bool = True
    #: 解析后填入
    url: str = ""
    file_name: str = ""
    sha1: str = ""
    size: int = 0
    resolved: bool = False
    note: str = ""

    @property
    def label(self) -> str:
        return self.file_name or f"{self.project_id}/{self.file_id}"


@dataclass
class CurseManifest:
    name: str
    version: str
    mc_version: str
    loader_type: str
    loader_version: str
    files: list[CurseFile] = field(default_factory=list)
    overrides_prefix: str = "overrides"
    author: str = ""

    @property
    def unresolved(self) -> list[CurseFile]:
        return [item for item in self.files if not item.resolved and item.url]

    @property
    def missing(self) -> list[CurseFile]:
        return [item for item in self.files if not item.url]


def is_curse_manifest(archive: Path) -> bool:
    try:
        import zipfile

        with zipfile.ZipFile(archive) as zf:
            names = set(zf.namelist())
        if MANIFEST_NAME not in names:
            return False
        # 必须是 CurseForge 的 manifestType，避免把任意 manifest.json 当成整合包
        payload = json.loads(read_zip_member(archive, MANIFEST_NAME).decode("utf-8"))
        return str(payload.get("manifestType", "")).lower() == "minecraftmodpack"
    except (OSError, ValueError, UnicodeDecodeError, KeyError):
        return False


def read_curse_manifest(archive: Path) -> CurseManifest | None:
    archive = Path(archive)
    try:
        import zipfile

        with zipfile.ZipFile(archive) as zf:
            if MANIFEST_NAME not in zf.namelist():
                return None
    except (OSError, zipfile.BadZipFile) as exc:
        raise ModpackError(f"无法读取压缩包：{exc}") from exc
    try:
        payload = json.loads(read_zip_member(archive, MANIFEST_NAME).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ModpackError(f"manifest.json 解析失败：{exc}") from exc
    if not isinstance(payload, dict) or str(payload.get("manifestType", "")).lower() != "minecraftmodpack":
        return None

    minecraft = payload.get("minecraft") or {}
    loaders = minecraft.get("modLoaders") or []
    loader_type = ""
    loader_version = ""
    primary = next((item for item in loaders if item.get("primary")), loaders[0] if loaders else None)
    if isinstance(primary, dict):
        raw_id = str(primary.get("id") or "")
        if "-" in raw_id:
            prefix, version = raw_id.split("-", 1)
            loader_type = LOADER_MAP.get(prefix.lower(), prefix.lower())
            loader_version = version

    files: list[CurseFile] = []
    for raw in payload.get("files") or []:
        if not isinstance(raw, dict):
            continue
        try:
            project_id = int(raw.get("projectID"))
            file_id = int(raw.get("fileID"))
        except (TypeError, ValueError):
            continue
        files.append(CurseFile(project_id=project_id, file_id=file_id, required=bool(raw.get("required", True))))

    overrides = str(payload.get("overrides") or "overrides").strip("/")
    return CurseManifest(
        name=str(payload.get("name") or "未命名整合包"),
        version=str(payload.get("version") or ""),
        mc_version=str(minecraft.get("version") or ""),
        loader_type=loader_type,
        loader_version=loader_version,
        files=files,
        overrides_prefix=overrides or "overrides",
        author=str(payload.get("author") or ""),
    )


def has_api_key(key: str | None) -> bool:
    return bool((key or "").strip())


def resolve_files(manifest: CurseManifest, api_key: str, *, timeout: float = 20.0) -> CurseManifest:
    """用 CurseForge API 补全每个文件的下载直链。缺少 key 时原样返回。"""
    key = (api_key or "").strip()
    if not key:
        for item in manifest.files:
            item.note = "需要 CurseForge API Key 才能自动下载"
        return manifest
    headers = {"x-api-key": key, "Accept": "application/json"}
    for item in manifest.files:
        url = f"{CF_API_BASE}/mods/{item.project_id}/files/{item.file_id}"
        try:
            payload = get_json(url, headers=headers, retries=1, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不影响整体
            item.note = f"接口请求失败：{exc}"
            log.debug("CurseForge 文件解析失败 %s/%s：%s", item.project_id, item.file_id, exc)
            continue
        data = payload.get("data") or {}
        download_url = str(data.get("downloadUrl") or "")
        item.file_name = str(data.get("fileName") or "")
        hashes = data.get("hashes") or []
        for entry in hashes:
            if int(entry.get("algo") or 0) == 1:  # 1 = SHA1
                item.sha1 = str(entry.get("value") or "")
        item.size = int(data.get("fileLength") or 0)
        if download_url:
            item.url = download_url
            item.resolved = True
            continue
        # 作者禁止第三方分发：用 CDN 兜底（fileID 拆成两段路径）+ 文件名
        if item.file_name:
            first = item.file_id // 1000
            second = item.file_id % 1000
            item.url = CF_CDN_FALLBACK.format(a=first, b=second, name=item.file_name)
            item.note = "作者关闭了 API 直链，已用 CDN 兜底，若 403 请手动下载"
            item.resolved = True
        else:
            item.note = "作者禁止第三方下载，请手动添加"
    return manifest


def describe(manifest: CurseManifest) -> dict:
    return {
        "name": manifest.name,
        "mc_version": manifest.mc_version,
        "loader": manifest.loader_type,
        "loader_version": manifest.loader_version,
        "files": len(manifest.files),
        "resolved": sum(1 for f in manifest.files if f.url),
        "manual": [f"{f.project_id}/{f.file_id}" for f in manifest.missing][:50],
        "version": manifest.version,
        "author": manifest.author,
    }
