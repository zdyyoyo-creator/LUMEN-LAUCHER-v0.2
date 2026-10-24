"""版本清单（version_manifest_v2）与 version.json 的完整解析。

职责边界：
* 只负责「取数据 + 解析 + 规则判定 + 生成下载清单」，不落盘、不下载文件本体；
* 下载统一交给 ``core.downloader``，安装编排交给 ``core.installer``。

解析覆盖：
* libraries：rules（os/arch/features）、Maven 坐标、classifiers、natives、
  无 ``downloads`` 字段时按 ``url`` 基址拼地址（Fabric/Forge profile 常见）；
* arguments：1.13+ 的 arguments.game / arguments.jvm（带 rules），
  以及 1.12- 的 minecraftArguments 字符串；
* assetIndex：objects / virtual / map_to_resources；
* javaVersion：用于 Java 版本匹配。
"""

from __future__ import annotations

import json
import platform
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..config import AppPaths
from ..utils.errors import ManifestError
from ..utils.log import get as get_logger
from ..utils.net import get_json
from ..utils.versions import classify, maven_os_name, os_tag, java_arch_tag

log = get_logger("core.manifest")

MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
MANIFEST_TTL = 600
LIBRARIES_BASE = "https://libraries.minecraft.net/"
ASSETS_BASE = "https://resources.download.minecraft.net/"


# --------------------------------------------------------------------------- #
# 规则判定（libraries / arguments 通用）
# --------------------------------------------------------------------------- #
def _norm_arch(machine: str | None = None) -> str:
    value = (machine or platform.machine() or "").lower()
    if value in ("amd64", "x86_64", "x64"):
        return "x86_64"
    if value in ("aarch64", "arm64"):
        return "arm64"
    if value in ("x86", "i386", "i686", "x86_32"):
        return "x86"
    return value


def _os_matches(rule_os: dict, *, os_name: str, arch: str, os_version: str) -> bool:
    name = rule_os.get("name")
    if name and str(name).lower() != os_name:
        return False
    rule_arch = rule_os.get("arch")
    if rule_arch:
        wanted = str(rule_arch).lower()
        if wanted == "x86" and arch != "x86":
            return False
        if wanted == "arm64" and arch != "arm64":
            return False
        if wanted not in ("x86", "arm64") and wanted != arch:
            return False
    version_regex = rule_os.get("version")
    if version_regex:
        try:
            if not re.search(str(version_regex), os_version):
                return False
        except re.error:
            log.debug("无法解析 os.version 正则：%s", version_regex)
            return False
    return True


def features_match(rule_features: dict, features: dict[str, bool] | None) -> bool:
    if not rule_features:
        return True
    active = features or {}
    for key, expected in rule_features.items():
        if bool(active.get(key, False)) != bool(expected):
            return False
    return True


def evaluate_rules(
    rules: list[dict] | None,
    *,
    features: dict[str, bool] | None = None,
    os_name: str | None = None,
    arch: str | None = None,
    os_version: str | None = None,
) -> bool:
    """按官方 rules 语义判定是否生效。无 rules 视为允许。"""
    if not rules:
        return True
    current_os = os_name or maven_os_name()
    current_arch = arch or _norm_arch()
    if os_version is None:
        os_version = platform.version()
    allowed = False
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        action = rule.get("action")
        # 官方取值是 allow / disallow；部分第三方清单会写 deny，一并兼容
        if action not in ("allow", "disallow", "deny"):
            continue
        if "os" in rule and not _os_matches(
            rule.get("os") or {}, os_name=current_os, arch=current_arch, os_version=os_version
        ):
            continue
        if "features" in rule and not features_match(rule.get("features") or {}, features):
            continue
        allowed = action == "allow"
    return allowed


# --------------------------------------------------------------------------- #
# Maven 坐标
# --------------------------------------------------------------------------- #
def maven_to_path(coordinate: str) -> str:
    """``group:artifact:version[:classifier][@ext]`` → 相对 maven 仓库路径。"""
    raw = str(coordinate).strip()
    ext = "jar"
    if "@" in raw:
        raw, ext = raw.rsplit("@", 1)
    parts = raw.split(":")
    if len(parts) < 3:
        raise ManifestError(f"非法的 Maven 坐标：{coordinate}")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else ""
    filename = f"{artifact}-{version}" + (f"-{classifier}" if classifier else "") + f".{ext}"
    return "/".join(group.replace(".", "/").split("/") + [artifact, version, filename])


def maven_url(coordinate: str, base: str = LIBRARIES_BASE) -> str:
    return base.rstrip("/") + "/" + maven_to_path(coordinate)


def is_native_coordinate(coordinate: str) -> bool:
    parts = str(coordinate).split(":")
    return len(parts) >= 4 and parts[3].startswith("natives-")


def native_classifier_matches(coordinate: str, arch: str | None = None) -> bool:
    """判断 natives 分类器是否适用于当前 CPU 架构。

    1.19+ 的 version.json 里，x64 / arm64 / x86 三个 natives 库的 ``rules`` 完全一样
    （只判断 ``os.name``）。如果只按 rules 过滤，会把三份都下载并解压到同一目录，
    后解压的（32 位 x86）覆盖前者，最终报 ``Can't load IA 32-bit .dll on AMD 64-bit``。
    因此必须再按 classifier 后缀过滤：

    * ``natives-windows`` / ``natives-linux`` / ``natives-macos``  → x86_64
    * ``natives-*-arm64``                                        → arm64
    * ``natives-*-x86``                                          → x86（32 位 JVM）
    """
    parts = str(coordinate).split(":")
    if len(parts) < 4:
        return True
    classifier = parts[3].lower()
    if not classifier.startswith("natives-"):
        return True
    current = (arch or _norm_arch()).lower()
    if classifier.endswith("-arm64"):
        return current == "arm64"
    if classifier.endswith("-arm32"):
        return current in ("arm32", "arm")
    if classifier.endswith("-x86"):
        return current == "x86"
    return current == "x86_64"


# --------------------------------------------------------------------------- #
# 下载描述
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RemoteFile:
    """一个待下载文件（URL 为「官方地址」，镜像改写由下载器完成）。"""

    url: str
    path: Path
    sha1: str | None = None
    size: int | None = None
    label: str = ""
    executable: bool = False

    def __str__(self) -> str:
        return self.label or self.url


@dataclass
class LibraryFile:
    coordinate: str
    relative_path: str
    url: str
    sha1: str | None = None
    size: int | None = None
    is_native: bool = False


def library_files(
    libraries: list[dict],
    *,
    features: dict[str, bool] | None = None,
    os_name: str | None = None,
) -> tuple[list[LibraryFile], list[LibraryFile]]:
    """把 version.json 的 libraries 解析成 (classpath 库, natives 库)。

    * 旧式 natives：``lib["natives"][os]`` + ``downloads.classifiers``
    * 新式 natives：坐标里带 ``natives-windows`` / ``natives-linux`` 分类器
    * 缺少 downloads 字段（Fabric/Forge profile）：用 ``lib["url"]`` 拼地址
    """
    current_os = os_name or maven_os_name()
    arch_suffix = "64" if _norm_arch() in ("x86_64", "arm64") else "32"
    classpath: list[LibraryFile] = []
    natives: list[LibraryFile] = []

    for lib in libraries or []:
        if not isinstance(lib, dict):
            continue
        if not evaluate_rules(lib.get("rules"), features=features, os_name=current_os):
            continue
        coordinate = str(lib.get("name") or "")
        if not coordinate:
            continue
        downloads = lib.get("downloads") or {}

        # 旧式 natives
        native_map = lib.get("natives") or {}
        if native_map:
            classifier_name = native_map.get(current_os)
            if not classifier_name:
                continue
            classifier_key = classifier_name.replace("${arch}", arch_suffix)
            entry = (downloads.get("classifiers") or {}).get(classifier_key) or {}
            url = entry.get("url")
            if not url:
                continue
            relative = entry.get("path") or maven_to_path(f"{coordinate}:{classifier_key}")
            natives.append(
                LibraryFile(
                    coordinate=f"{coordinate}:{classifier_key}",
                    relative_path=relative,
                    url=str(url),
                    sha1=entry.get("sha1"),
                    size=entry.get("size"),
                    is_native=True,
                )
            )
            continue

        artifact = downloads.get("artifact") or {}
        url = artifact.get("url")
        relative = artifact.get("path")
        if not url:
            base = str(lib.get("url") or LIBRARIES_BASE)
            relative = maven_to_path(coordinate)
            url = base.rstrip("/") + "/" + relative
        entry = LibraryFile(
            coordinate=coordinate,
            relative_path=str(relative or maven_to_path(coordinate)),
            url=str(url),
            sha1=artifact.get("sha1"),
            size=artifact.get("size"),
            is_native=is_native_coordinate(coordinate),
        )
        if entry.is_native:
            if not native_classifier_matches(coordinate):
                # 架构不匹配的 natives（如 32 位 x86 / arm64 版本）必须跳过，
                # 否则多个 dll 会解压到同一目录互相覆盖。
                continue
            natives.append(entry)
        else:
            classpath.append(entry)
    return classpath, natives


def argument_list(
    arguments: dict | None,
    *,
    legacy: str | None = None,
    features: dict[str, bool] | None = None,
) -> tuple[list[str], list[str]]:
    """返回 (jvm 参数, game 参数) 的原始字符串列表（未做变量替换）。"""

    def _collect(items: list | None) -> list[str]:
        collected: list[str] = []
        for item in items or []:
            if isinstance(item, str):
                collected.append(item)
            elif isinstance(item, dict):
                if evaluate_rules(item.get("rules"), features=features):
                    value = item.get("value")
                    if isinstance(value, str):
                        collected.append(value)
                    elif isinstance(value, list):
                        collected.extend(str(v) for v in value)
        return collected

    if arguments and isinstance(arguments, dict):
        return _collect(arguments.get("jvm")), _collect(arguments.get("game"))
    if legacy:
        return [], [token for token in str(legacy).split(" ") if token]
    return [], []


# --------------------------------------------------------------------------- #
# 远程清单
# --------------------------------------------------------------------------- #
@dataclass
class RemoteVersion:
    id: str
    type: str
    url: str
    time: str = ""
    release_time: str = ""
    sha1: str = ""
    compliance_level: int = 0

    @property
    def category(self) -> str:
        """release / snapshot / old / unknown（优先用清单原始 type）。"""
        raw = (self.type or "").lower()
        if raw in ("release", "snapshot", "old_beta", "old_alpha"):
            if raw == "release":
                return "release"
            if raw == "snapshot":
                return "snapshot"
            return "old"
        return classify(self.id)

    @property
    def is_release(self) -> bool:
        return self.category == "release"

    @property
    def is_snapshot(self) -> bool:
        return self.category == "snapshot"

    @property
    def is_old(self) -> bool:
        return self.category == "old"

    @property
    def date(self) -> str:
        stamp = self.release_time or self.time
        return stamp[:10] if stamp else ""


class VersionManifest:
    def __init__(self, data: dict, *, fetched_at: float | None = None) -> None:
        if not isinstance(data, dict) or "versions" not in data:
            raise ManifestError("版本清单结构异常（缺少 versions 字段）")
        self.raw = data
        self.fetched_at = fetched_at or time.time()
        latest = data.get("latest") or {}
        self.latest: dict[str, str] = {
            "release": str(latest.get("release", "")),
            "snapshot": str(latest.get("snapshot", "")),
        }
        self.versions: list[RemoteVersion] = []
        for item in data.get("versions") or []:
            try:
                self.versions.append(
                    RemoteVersion(
                        id=str(item["id"]),
                        type=str(item.get("type", "release")),
                        url=str(item["url"]),
                        time=str(item.get("time", "")),
                        release_time=str(item.get("releaseTime", "")),
                        sha1=str(item.get("sha1", "")),
                        compliance_level=int(item.get("complianceLevel", 0) or 0),
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.debug("跳过异常的版本条目：%s", exc)
        self._by_id = {version.id: version for version in self.versions}

    # ------------------------------------------------------------------ #
    def __len__(self) -> int:
        return len(self.versions)

    def get(self, version_id: str) -> RemoteVersion | None:
        return self._by_id.get(version_id)

    def filtered(self, *, snapshots: bool = False, old: bool = False) -> list[RemoteVersion]:
        result: list[RemoteVersion] = []
        for version in self.versions:
            category = version.category
            if category == "release":
                result.append(version)
            elif category == "snapshot" and snapshots:
                result.append(version)
            elif category == "old" and old:
                result.append(version)
        return result

    def latest_of(self, category: str = "release") -> RemoteVersion | None:
        version_id = self.latest.get(category)
        return self._by_id.get(version_id) if version_id else None

    def counts(self) -> dict[str, int]:
        counter = {"release": 0, "snapshot": 0, "old": 0, "unknown": 0}
        for version in self.versions:
            counter[version.category] = counter.get(version.category, 0) + 1
        return counter


def _manifest_cache_paths(paths: AppPaths) -> tuple[Path, Path]:
    base = paths.meta_cache_dir
    return base / "version_manifest_v2.json", base / "version_manifest_v2.meta.json"


def load_manifest(
    selector,  # MirrorSelector，延迟导入避免循环
    paths: AppPaths,
    *,
    force: bool = False,
    ttl: int = MANIFEST_TTL,
) -> VersionManifest:
    """拉取（或读缓存）版本清单。任意候选源可用即成功。"""
    cache_file, meta_file = _manifest_cache_paths(paths)
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    if not force and cache_file.exists():
        try:
            age = time.time() - cache_file.stat().st_mtime
            if age < ttl:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                return VersionManifest(data, fetched_at=time.time() - age)
        except (OSError, ValueError):
            log.debug("版本清单缓存损坏，将重新下载")

    last_error: Exception | None = None
    for candidate in selector.candidates(MANIFEST_URL):
        try:
            log.debug("拉取版本清单：%s", candidate)
            data = get_json(candidate, retries=1)
            manifest = VersionManifest(data)
            try:
                cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                meta_file.write_text(
                    json.dumps({"url": candidate, "fetched_at": time.time()}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError as exc:  # pragma: no cover
                log.debug("写入清单缓存失败：%s", exc)
            log.info("版本清单已更新：共 %d 个版本（含快照/远古版本）", len(manifest))
            return manifest
        except Exception as exc:  # noqa: BLE001 - 换源继续
            last_error = exc
            log.debug("清单源失败，尝试下一个：%s", exc)
    raise ManifestError(
        "无法获取 Minecraft 版本清单",
        hint=f"官方源与镜像均不可用：{last_error}",
    )


# --------------------------------------------------------------------------- #
# version.json
# --------------------------------------------------------------------------- #
def local_version_json(paths: AppPaths, instance_id: str, version_id: str) -> Path:
    from ..core.instances import instance_version_json  # 延迟导入避免循环

    return instance_version_json(paths, instance_id, version_id)


def fetch_version_json(
    selector,
    paths: AppPaths,
    remote: RemoteVersion,
    *,
    force: bool = False,
) -> dict:
    """下载并缓存某个版本的 version.json（按清单里的 sha1 校验缓存）。"""
    cache_file = paths.meta_cache_dir / "versions" / f"{remote.id}.json"
    cache_file.parent.mkdir(parents=True, exist_ok=True)

    if cache_file.exists() and not force:
        try:
            raw = cache_file.read_bytes()
            if not remote.sha1:
                return json.loads(raw)
            import hashlib

            if hashlib.sha1(raw).hexdigest() == remote.sha1:
                return json.loads(raw)
        except (OSError, ValueError):
            log.debug("version.json 缓存失效：%s", remote.id)

    last_error: Exception | None = None
    for candidate in selector.candidates(remote.url):
        try:
            data = get_json(candidate, retries=1)
            if not isinstance(data.get("libraries"), list) and not data.get("mainClass"):
                raise ManifestError(f"{remote.id} 的 version.json 结构异常")
            try:
                cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
            return data
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            log.debug("version.json 源失败（%s）：%s", remote.id, exc)
    raise ManifestError(f"无法获取 {remote.id} 的 version.json", hint=str(last_error))


class VersionDetail:
    """version.json 的语义化包装。"""

    def __init__(self, data: dict, *, version_id: str | None = None) -> None:
        if not isinstance(data, dict):
            raise ManifestError("version.json 不是对象")
        self.data = data
        self.id = str(version_id or data.get("id") or "unknown")

    # ------------------------------------------------------------------ #
    def get(self, key: str, default=None):
        return self.data.get(key, default)

    @property
    def type(self) -> str:
        return str(self.data.get("type", "release"))

    @property
    def main_class(self) -> str:
        value = str(self.data.get("mainClass") or "").strip()
        if not value:
            raise ManifestError(f"{self.id} 缺少 mainClass 字段")
        return value

    @property
    def libraries(self) -> list[dict]:
        return list(self.data.get("libraries") or [])

    @property
    def assets(self) -> str:
        return str(self.data.get("assets") or "")

    @property
    def asset_index(self) -> dict:
        return dict(self.data.get("assetIndex") or {})

    @property
    def downloads(self) -> dict:
        return dict(self.data.get("downloads") or {})

    @property
    def client_download(self) -> dict:
        return dict(self.downloads.get("client") or {})

    @property
    def java_version(self) -> dict:
        return dict(self.data.get("javaVersion") or {})

    @property
    def java_major(self) -> int | None:
        try:
            value = self.java_version.get("majorVersion")
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def inherits_from(self) -> str | None:
        value = self.data.get("inheritsFrom")
        return str(value) if value else None

    @property
    def arguments(self) -> dict | None:
        value = self.data.get("arguments")
        return value if isinstance(value, dict) else None

    @property
    def minecraft_arguments(self) -> str | None:
        value = self.data.get("minecraftArguments")
        return str(value) if value else None

    @property
    def release_time(self) -> str:
        return str(self.data.get("releaseTime") or "")

    def classpath_and_natives(self, features: dict[str, bool] | None = None):
        return library_files(self.libraries, features=features)

    def resolved_arguments(self, features: dict[str, bool] | None = None) -> tuple[list[str], list[str]]:
        return argument_list(self.arguments, legacy=self.minecraft_arguments, features=features)


def library_conflict_key(lib: dict) -> str:
    """合并 inheritsFrom 时用于判断「同一个库」的键：``group:artifact[:classifier]``。

    官方启动器的规则是：子版本（Forge/NeoForge）对同名 artifact 有更高优先级，
    而不是保留不同版本。Forge 的 version.json 会显式写它需要的库版本
    （例如 guava 32.1.2-jre），而原版 1.21.4 用 33.3.1-jre；
    如果两个版本都进入 classpath，Java 模块系统会因为同名模块/重复包直接崩溃
    （``ResolutionException: ... export package ...``）。
    """
    name = str(lib.get("name") or "")
    parts = name.split(":")
    if len(parts) >= 4:
        # group:artifact:version:classifier → group:artifact:classifier
        return f"{parts[0]}:{parts[1]}:{parts[3]}"
    return ":".join(parts[:2])


def merge_version_data(parent: dict, child: dict, *, version_id: str | None = None) -> dict:
    """合并 inheritsFrom 复合版本（Forge/NeoForge 常见）。

    * libraries：父在前、**子覆盖同 group:artifact[:classifier] 的库**；
    * arguments：父在前、子追加；
    * 其余标量字段：子优先，child 缺失则继承父。
    """
    merged: dict = json.loads(json.dumps(parent))
    for key, value in child.items():
        if key == "libraries":
            continue
        if key == "arguments" and isinstance(value, dict):
            parent_args = merged.get("arguments") if isinstance(merged.get("arguments"), dict) else {}
            combined: dict[str, list] = {}
            for group in ("game", "jvm"):
                combined[group] = list(parent_args.get(group) or []) + list(value.get(group) or [])
            merged["arguments"] = combined
            continue
        merged[key] = json.loads(json.dumps(value)) if isinstance(value, (dict, list)) else value

    ordered: list[dict] = []
    index_by_key: dict[str, int] = {}
    for lib in merged.get("libraries") or []:
        if not isinstance(lib, dict):
            continue
        key = library_conflict_key(lib)
        if key in index_by_key:
            ordered[index_by_key[key]] = lib
        else:
            index_by_key[key] = len(ordered)
            ordered.append(lib)
    for lib in child.get("libraries") or []:
        if not isinstance(lib, dict):
            continue
        key = library_conflict_key(lib)
        if key in index_by_key:
            ordered[index_by_key[key]] = lib
        else:
            index_by_key[key] = len(ordered)
            ordered.append(lib)
    merged["libraries"] = ordered
    if version_id:
        merged["id"] = version_id
    return merged


def asset_index_files(index_data: dict, assets_dir: Path) -> list[RemoteFile]:
    """把 asset index 的 objects 展开成下载任务。"""
    objects = (index_data or {}).get("objects") or {}
    files: list[RemoteFile] = []
    for name, entry in objects.items():
        if not isinstance(entry, dict):
            continue
        digest = str(entry.get("hash") or "")
        if len(digest) < 2:
            continue
        target = assets_dir / "objects" / digest[:2] / digest
        files.append(
            RemoteFile(
                url=f"{ASSETS_BASE}{digest[:2]}/{digest}",
                path=target,
                sha1=digest,
                size=entry.get("size"),
                label=f"asset:{name}",
            )
        )
    return files


def client_library_files(paths: AppPaths, detail: VersionDetail, *, features=None) -> list[RemoteFile]:
    """libraries 中 classpath 库的下载任务。"""
    classpath, _natives = detail.classpath_and_natives(features=features)
    return [
        RemoteFile(
            url=lib.url,
            path=paths.libraries_dir / lib.relative_path,
            sha1=lib.sha1,
            size=lib.size,
            label=f"library:{lib.coordinate}",
        )
        for lib in classpath
    ]


def native_library_files(paths: AppPaths, detail: VersionDetail, *, features=None) -> list[RemoteFile]:
    _classpath, natives = detail.classpath_and_natives(features=features)
    return [
        RemoteFile(
            url=lib.url,
            path=paths.libraries_dir / lib.relative_path,
            sha1=lib.sha1,
            size=lib.size,
            label=f"native:{lib.coordinate}",
        )
        for lib in natives
    ]


def total_asset_size(index_data: dict) -> int:
    objects = (index_data or {}).get("objects") or {}
    return sum(int((entry or {}).get("size", 0) or 0) for entry in objects.values() if isinstance(entry, dict))
