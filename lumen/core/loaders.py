"""模组加载器：Fabric / Quilt（meta API 直装）与 Forge / NeoForge（installer 引导）。

Fabric / Quilt 的 profile json 就是一份「继承原版」的 version.json，
可以直接与本地的 vanilla json 合并成独立版本，不需要运行任何安装器。

Forge / NeoForge 没有官方 profile API，只能下载 installer.jar 后调用
``--installClient`` 让官方安装器自己生成版本文件（因此需要已就绪的 Java）。
"""

from __future__ import annotations

import re
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from ..utils.errors import InstallError, NetworkError
from ..utils.log import get as get_logger
from ..utils.net import get_bytes, get_json

log = get_logger("core.loaders")

FABRIC_META = "https://meta.fabricmc.net"
QUILT_META = "https://meta.quiltmc.org"
FORGE_MAVEN = "https://maven.minecraftforge.net"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases"

LOADER_LABELS = {
    "fabric": "Fabric",
    "quilt": "Quilt",
    "forge": "Forge",
    "neoforge": "NeoForge",
}

#: 安装器输出里的进度特征：优先百分比，其次 done/total
_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")
_RATIO_RE = re.compile(r"(\d{1,6})\s*/\s*(\d{1,6})")


def parse_installer_progress(text: str) -> float | None:
    """从 Forge/NeoForge 安装器的一行输出里尽量解析出 0..1 的进度。

    安装器的进度条会刷新在同一行（``\\r``），所以取最后一个匹配；
    解析不到返回 None，由调用方退化为「耗时渐近」进度。
    """
    if not text:
        return None
    percents = _PERCENT_RE.findall(text)
    if percents:
        try:
            value = int(percents[-1])
        except ValueError:
            return None
        if 0 <= value <= 100:
            return value / 100.0
    ratios = _RATIO_RE.findall(text)
    if ratios:
        done_text, total_text = ratios[-1]
        try:
            done, total = int(done_text), int(total_text)
        except ValueError:
            return None
        if total > 0 and 0 <= done <= total:
            return done / total
    return None


@dataclass
class LoaderVersion:
    version: str
    stable: bool = True
    game_version: str = ""

    @property
    def label(self) -> str:
        return f"{self.version}{'' if self.stable else ' (beta)'}"


# --------------------------------------------------------------------------- #
# Fabric / Quilt
# --------------------------------------------------------------------------- #
def fabric_loader_versions(selector, mc_version: str, *, stable_only: bool = False) -> list[LoaderVersion]:
    url = f"{FABRIC_META}/v2/versions/loader/{mc_version}"
    data = _get_json_via_mirror(selector, url)
    if not isinstance(data, list):
        raise InstallError(f"Fabric 元数据格式异常（{mc_version}）")
    result: list[LoaderVersion] = []
    for item in data:
        loader = (item or {}).get("loader") or {}
        version = str(loader.get("version") or "")
        if not version:
            continue
        stable = bool(loader.get("stable", True))
        if stable_only and not stable:
            continue
        result.append(LoaderVersion(version=version, stable=stable, game_version=mc_version))
    return result


def quilt_loader_versions(selector, mc_version: str, *, stable_only: bool = False) -> list[LoaderVersion]:
    url = f"{QUILT_META}/v3/versions/loader/{mc_version}"
    data = _get_json_via_mirror(selector, url)
    if not isinstance(data, list):
        raise InstallError(f"Quilt 元数据格式异常（{mc_version}）")
    result: list[LoaderVersion] = []
    for item in data:
        loader = (item or {}).get("loader") or {}
        version = str(loader.get("version") or "")
        if not version:
            continue
        stable = bool(loader.get("stable", True))
        if stable_only and not stable:
            continue
        result.append(LoaderVersion(version=version, stable=stable, game_version=mc_version))
    return result


def fabric_profile(selector, mc_version: str, loader_version: str) -> dict:
    url = f"{FABRIC_META}/v2/versions/loader/{mc_version}/{loader_version}/profile/json"
    data = _get_json_via_mirror(selector, url)
    if "libraries" not in data or "mainClass" not in data:
        raise InstallError(f"Fabric profile 结构异常（{mc_version} + {loader_version}）")
    return data


def quilt_profile(selector, mc_version: str, loader_version: str) -> dict:
    url = f"{QUILT_META}/v3/versions/loader/{mc_version}/{loader_version}/profile/json"
    data = _get_json_via_mirror(selector, url)
    if "libraries" not in data or "mainClass" not in data:
        raise InstallError(f"Quilt profile 结构异常（{mc_version} + {loader_version}）")
    return data


def latest_loader(kind: str, versions: list[LoaderVersion]) -> str | None:
    for item in versions:
        if item.stable:
            return item.version
    return versions[0].version if versions else None


# --------------------------------------------------------------------------- #
# Forge / NeoForge
# --------------------------------------------------------------------------- #
def _maven_metadata(selector, base: str, group_path: str, artifact: str) -> list[str]:
    """拉 maven-metadata.xml 并解析出版本列表（官方失败自动走镜像候选）。"""
    url = f"{base}/{group_path}/{artifact}/maven-metadata.xml"
    raw: bytes | None = None
    last_error: Exception | None = None
    for candidate in selector.candidates(url):
        try:
            raw = get_bytes(candidate, retries=1)
            break
        except Exception as exc:  # noqa: BLE001 - 换下一个源
            last_error = exc
            log.debug("maven-metadata 源失败（%s）：%s", artifact, exc)
    if raw is None:
        raise NetworkError(
            f"无法获取 {artifact} 的 maven-metadata.xml",
            hint=str(last_error) if last_error else url,
        )
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise NetworkError(f"{artifact} 的 maven-metadata.xml 解析失败") from exc
    versions: list[str] = []
    for node in root.iter():
        if node.tag.endswith("version") and node.text:
            versions.append(node.text.strip())
    return versions


def forge_versions(selector, mc_version: str) -> list[LoaderVersion]:
    """返回形如 ``1.20.1-47.2.0`` 的 Forge 版本号列表（新→旧）。"""
    try:
        # 注意：group_path 到 groupId 为止，artifact 才是最后一层目录
        raw = _maven_metadata(selector, FORGE_MAVEN, "net/minecraftforge", "forge")
    except NetworkError as exc:
        raise InstallError("无法获取 Forge 版本列表", hint=str(exc)) from exc
    prefix = f"{mc_version}-"
    result = [
        LoaderVersion(version=item, game_version=mc_version)
        for item in raw
        if item.startswith(prefix) and item.count("-") == 1
    ]
    result.reverse()
    return result


def neoforge_versions(selector, mc_version: str) -> list[LoaderVersion]:
    """NeoForge 版本号规则：MC 1.21.1 → NeoForge 21.1.x。"""
    major_minor = _neoforge_prefix(mc_version)
    if not major_minor:
        return []
    try:
        raw = _maven_metadata(selector, NEOFORGE_MAVEN, "net/neoforged", "neoforge")
    except NetworkError as exc:
        raise InstallError(
            "无法获取 NeoForge 版本列表",
            hint=(
                "maven.neoforged.net 当前不可达（该站点在部分网络下被限制）。"
                "NeoForge 的安装器也从这个地址下载，建议配置代理后重试，"
                "或改用 Forge / Fabric。"
            ),
        ) from exc
    result = [
        LoaderVersion(version=item, game_version=mc_version)
        for item in raw
        if item.startswith(major_minor + ".")
    ]
    result.reverse()
    return result


def _neoforge_prefix(mc_version: str) -> str:
    parts = str(mc_version).split(".")
    if len(parts) < 2 or not parts[0].isdigit():
        return ""
    major = parts[1]
    minor = parts[2] if len(parts) > 2 and parts[2].isdigit() else "0"
    return f"{major}.{minor}"


def forge_installer_url(mc_version: str, forge_version: str, *, kind: str = "forge") -> str:
    if kind == "neoforge":
        coordinate = forge_version
        return f"{NEOFORGE_MAVEN}/net/neoforged/neoforge/{coordinate}/neoforge-{coordinate}-installer.jar"
    coordinate = forge_version if forge_version.startswith(f"{mc_version}-") else f"{mc_version}-{forge_version}"
    return f"{FORGE_MAVEN}/net/minecraftforge/forge/{coordinate}/forge-{coordinate}-installer.jar"


def run_forge_installer(
    java_path: str,
    installer_jar,
    game_dir,
    *,
    cancel_check=None,
    timeout: float = 1800.0,
    on_output=None,
    on_progress=None,
) -> str:
    """以 headless 模式运行 Forge/NeoForge 官方安装器。

    * ``on_output(line)``：逐行回调安装器输出（向导里的实时日志面板）；
    * ``on_progress(value)``：尽力解析的 0..1 进度，解析不到时回调 ``None``；
    * ``cancel_check()``：返回 True 时立即结束子进程并抛 ``InstallError``。

    返回输出尾部（失败时用于提示）。
    """
    command = [str(java_path), "-jar", str(installer_jar), "--installClient"]
    log.info("运行加载器安装器：%s", " ".join(command))
    try:
        process = subprocess.Popen(
            command,
            cwd=str(game_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
    except OSError as exc:
        raise InstallError(f"无法启动安装器：{exc}", hint="请检查 Java 路径是否有效") from exc

    lines: list[str] = []
    assert process.stdout is not None
    try:
        for raw_line in process.stdout:
            text = raw_line.rstrip("\r\n")
            lines.append(text)
            if len(lines) > 600:
                lines.pop(0)
            if on_output is not None:
                try:
                    on_output(text)
                except Exception:  # noqa: BLE001 - UI 回调异常不能中断安装
                    pass
            if on_progress is not None:
                try:
                    on_progress(parse_installer_progress(text))
                except Exception:  # noqa: BLE001
                    pass
            if cancel_check is not None and cancel_check():
                process.kill()
                raise InstallError("安装器已被取消")
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        raise InstallError(f"安装器运行超时（{int(timeout / 60)} 分钟）") from exc
    if code != 0:
        tail = "\n".join(lines[-12:]) or "无输出"
        raise InstallError(
            f"加载器安装器返回非零退出码：{code}",
            hint=tail,
        )
    return "\n".join(lines[-40:])


def available_loader_versions(selector, kind: str, mc_version: str) -> list[LoaderVersion]:
    if kind == "fabric":
        return fabric_loader_versions(selector, mc_version)
    if kind == "quilt":
        return quilt_loader_versions(selector, mc_version)
    if kind == "forge":
        return forge_versions(selector, mc_version)
    if kind == "neoforge":
        return neoforge_versions(selector, mc_version)
    raise InstallError(f"不支持的加载器类型：{kind}")


def loader_requires_java(kind: str) -> bool:
    """Forge/NeoForge 安装需要本地 Java；Fabric/Quilt 不需要。"""
    return kind in ("forge", "neoforge")


def _get_json_via_mirror(selector, url: str) -> dict | list:
    """meta API 也支持镜像改写（Fabric 有 fabric-meta 映射）。

    Fabric/Quilt 的版本列表接口返回数组，profile 接口返回对象，所以 expect 两者都接受。
    """
    last: Exception | None = None
    for candidate in selector.candidates(url):
        try:
            return get_json(candidate, retries=1, expect=(dict, list))
        except Exception as exc:  # noqa: BLE001
            last = exc
            continue
    raise NetworkError(f"无法获取元数据：{url}", hint=str(last))
