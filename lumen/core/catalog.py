"""资源目录客户端：Modrinth（公开 API）与 CurseForge（需要 API Key）。

只负责「查」与「给出下载信息」，不负责落盘；下载统一交给 ``core.downloader``，
整合包导入交给 ``modpack.importer``，这样 UI 层可以复用同一条链路。

Modrinth 文档：https://docs.modrinth.com/api/
CurseForge 文档：https://docs.curseforge.com/rest-api/
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..utils.errors import LumenError, NetworkError
from ..utils.log import get as get_logger
from ..utils.net import get_json

log = get_logger("core.catalog")

MODRINTH_API = "https://api.modrinth.com/v2"
CURSEFORGE_API = "https://api.curseforge.com/v1"
CURSEFORGE_GAME_ID = 432

#: CurseForge classId
CURSEFORGE_CLASS = {"mod": 6, "modpack": 4471, "resourcepack": 12, "shader": 6552}
#: CurseForge modLoaderType
CURSEFORGE_LOADER = {"forge": 1, "fabric": 4, "quilt": 5, "neoforge": 6}
#: CurseForge 作者关闭 API 直链时的 CDN 兜底
CURSEFORGE_CDN = "https://edge.forgecdn.net/files/{a}/{b}/{name}"

SOURCE_LABELS = {"modrinth": "Modrinth", "curseforge": "CurseForge"}
TYPE_LABELS = {"mod": "模组", "modpack": "整合包", "resourcepack": "资源包", "shader": "光影"}


@dataclass
class CatalogHit:
    """搜索结果的统一表示（两个平台字段差异都收敛到这里）。"""

    source: str
    project_id: str
    slug: str = ""
    title: str = ""
    description: str = ""
    author: str = ""
    downloads: int = 0
    follows: int = 0
    icon_url: str = ""
    categories: list[str] = field(default_factory=list)
    game_versions: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    project_type: str = "mod"

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    @property
    def downloads_text(self) -> str:
        value = float(self.downloads)
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 1_000:
            return f"{value / 1_000:.1f}K"
        return str(int(value))


@dataclass
class CatalogVersion:
    """某个资源的一个具体文件版本（可直接下载）。"""

    version_id: str
    name: str = ""
    version_number: str = ""
    game_versions: list[str] = field(default_factory=list)
    loaders: list[str] = field(default_factory=list)
    date: str = ""
    download_url: str = ""
    filename: str = ""
    file_size: int = 0
    sha1: str = ""
    release_type: str = "release"
    project_type: str = "mod"

    @property
    def label(self) -> str:
        bits = [self.version_number or self.name or self.version_id]
        if self.game_versions:
            bits.append("MC " + "/".join(self.game_versions[:2]))
        if self.loaders:
            bits.append("/".join(self.loaders))
        return " · ".join(bits)


class CatalogError(LumenError):
    """目录接口返回了不可用的结果（需要 API Key、被限流等）。"""


# --------------------------------------------------------------------------- #
# Modrinth
# --------------------------------------------------------------------------- #
class ModrinthClient:
    source = "modrinth"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = api_key
        self.headers = {"User-Agent": "LumenLauncher/1.0 (modrinth catalog)"}

    # ------------------------------------------------------------------ #
    def search(
        self,
        query: str,
        *,
        project_type: str = "mod",
        loader: str = "",
        game_version: str = "",
        limit: int = 20,
        offset: int = 0,
        sort: str = "relevance",
    ) -> list[CatalogHit]:
        facets: list[list[str]] = [[f"project_type:{project_type}"]]
        if loader:
            facets.append([f"categories:{loader}"])
        if game_version:
            facets.append([f"versions:{game_version}"])
        params = {
            "query": query,
            "limit": max(1, min(50, limit)),
            "offset": max(0, offset),
            "index": sort,
            "facets": json.dumps(facets, separators=(",", ":")),
        }
        try:
            payload = get_json(f"{MODRINTH_API}/search", params=params, headers=self.headers, retries=1)
        except NetworkError as exc:
            raise CatalogError(f"Modrinth 搜索失败：{exc}", hint=exc.hint) from exc
        hits: list[CatalogHit] = []
        for raw in payload.get("hits") or []:
            if not isinstance(raw, dict):
                continue
            hits.append(
                CatalogHit(
                    source="modrinth",
                    project_id=str(raw.get("project_id") or ""),
                    slug=str(raw.get("slug") or ""),
                    title=str(raw.get("title") or ""),
                    description=str(raw.get("description") or ""),
                    author=str(raw.get("author") or ""),
                    downloads=int(raw.get("downloads") or 0),
                    follows=int(raw.get("follows") or 0),
                    icon_url=str(raw.get("icon_url") or ""),
                    categories=[str(c) for c in (raw.get("display_categories") or raw.get("categories") or [])],
                    game_versions=[str(v) for v in (raw.get("versions") or [])],
                    loaders=[str(v) for v in (raw.get("categories") or [])],
                    project_type=str(raw.get("project_type") or project_type),
                )
            )
        return hits

    # ------------------------------------------------------------------ #
    def versions(
        self,
        project_id: str,
        *,
        loader: str = "",
        game_version: str = "",
        limit: int = 60,
    ) -> list[CatalogVersion]:
        params: dict[str, Any] = {}
        if loader:
            params["loaders"] = json.dumps([loader], separators=(",", ":"))
        if game_version:
            params["game_versions"] = json.dumps([game_version], separators=(",", ":"))
        try:
            payload = get_json(
                f"{MODRINTH_API}/project/{project_id}/version",
                params=params or None,
                headers=self.headers,
                retries=1,
                expect=list,
            )
        except NetworkError as exc:
            raise CatalogError(f"Modrinth 版本列表获取失败：{exc}", hint=exc.hint) from exc
        versions: list[CatalogVersion] = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            files = raw.get("files") or []
            primary = next((f for f in files if isinstance(f, dict) and f.get("primary")), files[0] if files else None)
            if not isinstance(primary, dict):
                continue
            hashes = primary.get("hashes") or {}
            versions.append(
                CatalogVersion(
                    version_id=str(raw.get("id") or ""),
                    name=str(raw.get("name") or ""),
                    version_number=str(raw.get("version_number") or ""),
                    game_versions=[str(v) for v in (raw.get("game_versions") or [])],
                    loaders=[str(v) for v in (raw.get("loaders") or [])],
                    date=str(raw.get("date_published") or ""),
                    download_url=str(primary.get("url") or ""),
                    filename=str(primary.get("filename") or ""),
                    file_size=int(primary.get("size") or 0),
                    sha1=str(hashes.get("sha1") or ""),
                    release_type=str(raw.get("version_type") or "release"),
                )
            )
            if len(versions) >= limit:
                break
        return versions

    # ------------------------------------------------------------------ #
    def project(self, project_id: str) -> dict:
        try:
            payload = get_json(
                f"{MODRINTH_API}/project/{project_id}", headers=self.headers, retries=1
            )
        except NetworkError as exc:
            raise CatalogError(f"Modrinth 项目信息获取失败：{exc}", hint=exc.hint) from exc
        return payload


# --------------------------------------------------------------------------- #
# CurseForge
# --------------------------------------------------------------------------- #
class CurseForgeClient:
    source = "curseforge"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = (api_key or "").strip()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise CatalogError(
                "CurseForge 需要 API Key",
                hint="到「设置 → 整合包」填入 CurseForge API Key 后即可搜索与下载（Modrinth 无需 Key）。",
            )
        return {"x-api-key": self.api_key, "Accept": "application/json"}

    def _request(self, path: str, *, params: dict | None = None, expect=dict):
        try:
            return get_json(
                f"{CURSEFORGE_API}{path}",
                params=params,
                headers=self._headers(),
                retries=1,
                expect=expect,
            )
        except NetworkError as exc:
            message = str(exc)
            if "403" in message or "401" in message:
                raise CatalogError(
                    "CurseForge API Key 被拒绝",
                    hint="请检查 Key 是否填写正确、是否被限流（403/401）。",
                ) from exc
            raise CatalogError(f"CurseForge 请求失败：{message}", hint=exc.hint) from exc

    # ------------------------------------------------------------------ #
    def search(
        self,
        query: str,
        *,
        project_type: str = "mod",
        loader: str = "",
        game_version: str = "",
        limit: int = 20,
        offset: int = 0,
        sort: str = "popularity",
    ) -> list[CatalogHit]:
        params: dict[str, Any] = {
            "gameId": CURSEFORGE_GAME_ID,
            "classId": CURSEFORGE_CLASS.get(project_type, 6),
            "searchFilter": query,
            "pageSize": max(1, min(50, limit)),
            "index": max(0, offset),
            "sortField": 2 if sort == "popularity" else 6,  # 2=Popularity 6=TotalDownloads
            "sortOrder": "desc",
        }
        if game_version:
            params["gameVersion"] = game_version
        if loader and loader in CURSEFORGE_LOADER:
            params["modLoaderType"] = CURSEFORGE_LOADER[loader]
        payload = self._request("/mods/search", params=params)
        hits: list[CatalogHit] = []
        for raw in payload.get("data") or []:
            if not isinstance(raw, dict):
                continue
            logo = raw.get("logo") or {}
            categories = [str(c.get("name")) for c in (raw.get("categories") or []) if isinstance(c, dict)]
            hits.append(
                CatalogHit(
                    source="curseforge",
                    project_id=str(raw.get("id") or ""),
                    slug=str(raw.get("slug") or ""),
                    title=str(raw.get("name") or ""),
                    description=str(raw.get("summary") or ""),
                    author="",
                    downloads=int(raw.get("downloadCount") or 0),
                    icon_url=str(logo.get("thumbnailUrl") or logo.get("url") or ""),
                    categories=categories,
                    game_versions=[str(v) for v in (raw.get("gameVersionLatestFiles") or []) if isinstance(v, str)][:8],
                    loaders=[loader] if loader else [],
                    project_type=project_type,
                )
            )
        return hits

    # ------------------------------------------------------------------ #
    def versions(
        self,
        project_id: str,
        *,
        loader: str = "",
        game_version: str = "",
        limit: int = 60,
    ) -> list[CatalogVersion]:
        params: dict[str, Any] = {"pageSize": min(50, limit)}
        if game_version:
            params["gameVersion"] = game_version
        if loader and loader in CURSEFORGE_LOADER:
            params["modLoaderType"] = CURSEFORGE_LOADER[loader]
        payload = self._request(f"/mods/{project_id}/files", params=params)
        versions: list[CatalogVersion] = []
        for raw in payload.get("data") or []:
            if not isinstance(raw, dict):
                continue
            download_url = str(raw.get("downloadUrl") or "")
            filename = str(raw.get("fileName") or "")
            file_id = int(raw.get("id") or 0)
            if not download_url and filename:
                download_url = CURSEFORGE_CDN.format(a=file_id // 1000, b=file_id % 1000, name=filename)
            sha1 = ""
            for entry in raw.get("hashes") or []:
                if int(entry.get("algo") or 0) == 1:
                    sha1 = str(entry.get("value") or "")
            versions.append(
                CatalogVersion(
                    version_id=str(file_id),
                    name=str(raw.get("displayName") or ""),
                    version_number=str(raw.get("displayName") or filename),
                    game_versions=[str(v) for v in (raw.get("gameVersions") or [])],
                    loaders=[loader] if loader else [],
                    date=str(raw.get("fileDate") or ""),
                    download_url=download_url,
                    filename=filename,
                    file_size=int(raw.get("fileLength") or 0),
                    sha1=sha1,
                    release_type={1: "release", 2: "beta", 3: "alpha"}.get(int(raw.get("releaseType") or 1), "release"),
                )
            )
        return versions[:limit]


# --------------------------------------------------------------------------- #
def make_client(source: str, *, curseforge_key: str = "") -> ModrinthClient | CurseForgeClient:
    if source == "curseforge":
        return CurseForgeClient(curseforge_key)
    return ModrinthClient()


def pick_best_version(
    versions: list[CatalogVersion],
    *,
    loader: str = "",
    game_version: str = "",
) -> CatalogVersion | None:
    """从版本列表里挑一个最适合当前实例的（稳定版优先、版本号靠前）。"""

    def score(version: CatalogVersion) -> tuple:
        stable = 0 if version.release_type == "release" else 1
        loader_ok = 0 if (not loader or loader in version.loaders or not version.loaders) else 1
        mc_ok = 0 if (not game_version or game_version in version.game_versions or not version.game_versions) else 1
        return (loader_ok, mc_ok, stable, -_date_rank(version.date))

    usable = [v for v in versions if v.download_url]
    if not usable:
        return None
    return min(usable, key=score)


def _date_rank(text: str) -> float:
    """把 ISO 时间转成可比较的数字（越大越新）。"""
    digits = "".join(ch for ch in str(text) if ch.isdigit())
    try:
        return float(digits[:14] or 0)
    except ValueError:
        return 0.0
