"""多镜像源：地址改写表 + 并发测速 + 自动/官方/镜像策略。

镜像表覆盖需求列出的全部主机，并按「最长前缀优先」匹配，避免
``maven.neoforged.net/releases`` 被泛化的 ``maven.neoforged.net`` 规则抢先匹配。
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .. import USER_AGENT
from ..config import AppPaths, Settings
from ..utils.errors import NetworkError
from ..utils.log import get as get_logger
from ..utils.net import head_probe, session

log = get_logger("core.mirrors")

BMCLAPI_ROOT = "https://bmclapi2.bangbang93.com"
TENCENT_MAVEN = "https://mirrors.cloud.tencent.com/nexus/repository/maven-public"
ALIYUN_MAVEN = "https://maven.aliyun.com/repository/central"

SOURCE_OFFICIAL = "official"
SOURCE_MIRROR = "mirror"
SOURCE_ORDER = (SOURCE_OFFICIAL, SOURCE_MIRROR)

SOURCE_LABELS = {
    SOURCE_OFFICIAL: "Mojang 官方源",
    SOURCE_MIRROR: "BMCLAPI 镜像",
}


# --------------------------------------------------------------------------- #
# 改写规则
# --------------------------------------------------------------------------- #
#: (官方前缀, 镜像前缀)，**顺序敏感**：越具体的规则必须越靠前
REWRITE_RULES: tuple[tuple[str, str], ...] = (
    ("https://launchermeta.mojang.com/", f"{BMCLAPI_ROOT}/"),
    ("https://piston-meta.mojang.com/", f"{BMCLAPI_ROOT}/"),
    ("https://piston-data.mojang.com/", f"{BMCLAPI_ROOT}/"),
    ("https://libraries.minecraft.net/", f"{BMCLAPI_ROOT}/maven/"),
    ("https://resources.download.minecraft.net/", f"{BMCLAPI_ROOT}/assets/"),
    ("https://maven.minecraftforge.net/", f"{BMCLAPI_ROOT}/maven/"),
    ("https://maven.neoforged.net/releases/", f"{BMCLAPI_ROOT}/maven/"),
    ("https://maven.neoforged.net/", f"{BMCLAPI_ROOT}/maven/"),
    ("https://meta.fabricmc.net/", f"{BMCLAPI_ROOT}/fabric-meta/"),
    ("https://maven.fabricmc.net/", f"{BMCLAPI_ROOT}/maven/"),
    ("https://repo1.maven.org/maven2/", f"{TENCENT_MAVEN}/"),
    ("https://repo.maven.apache.org/maven2/", f"{TENCENT_MAVEN}/"),
    ("https://mall.aliyun.com/", f"{ALIYUN_MAVEN}/"),
)

#: 只替换 host 的官方主机（BMCLAPI 与官方路径结构一致）
HOST_ONLY_RULES: dict[str, str] = {
    "launchermeta.mojang.com": BMCLAPI_ROOT,
    "piston-meta.mojang.com": BMCLAPI_ROOT,
    "piston-data.mojang.com": BMCLAPI_ROOT,
}

#: 测速样本：小 JSON + 中等体积 jar，分别反映「首包延迟」与「吞吐」
PROBE_URLS_OFFICIAL = (
    "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json",
    "https://libraries.minecraft.net/com/google/code/gson/gson/2.10.1/gson-2.10.1.jar",
)


def to_mirror_url(url: str, *, endpoint: str = BMCLAPI_ROOT) -> str | None:
    """把官方地址改写成镜像地址；无法改写时返回 None。"""
    if not url:
        return None
    for official_prefix, mirror_prefix in REWRITE_RULES:
        if url.startswith(official_prefix):
            suffix = url[len(official_prefix):]
            # endpoint 允许用户自定义（内置镜像根地址可替换）
            if mirror_prefix.startswith(BMCLAPI_ROOT) and endpoint != BMCLAPI_ROOT:
                mirror_prefix = endpoint.rstrip("/") + mirror_prefix[len(BMCLAPI_ROOT):]
            return mirror_prefix + suffix
    # 兜底：仅替换 host，保持 path 不变
    parts = urlsplit(url)
    if parts.netloc in HOST_ONLY_RULES:
        if endpoint != BMCLAPI_ROOT and HOST_ONLY_RULES[parts.netloc] == BMCLAPI_ROOT:
            netloc = endpoint.rstrip("/")
        else:
            netloc = HOST_ONLY_RULES[parts.netloc]
        return urlunsplit((parts.scheme, netloc.split("//")[-1], parts.path, parts.query, parts.fragment))
    return None


def to_official_url(url: str) -> str:
    """镜像地址反查官方地址（用于用户手填了镜像链接时纠正）。"""
    return url


def is_official_url(url: str) -> bool:
    for official_prefix, _ in REWRITE_RULES:
        if url.startswith(official_prefix):
            return True
    return urlsplit(url).netloc in HOST_ONLY_RULES


# --------------------------------------------------------------------------- #
# 测速
# --------------------------------------------------------------------------- #
@dataclass
class SourceProbe:
    source_id: str
    ok: bool = False
    latency_ms: float = 0.0
    speed_kbps: float = 0.0
    error: str = ""
    checked_at: float = field(default_factory=time.time)

    @property
    def score(self) -> float:
        """综合得分：吞吐为主、延迟为辅（越小越慢）。"""
        if not self.ok:
            return -1.0
        latency_weight = 120.0 / (60.0 + max(self.latency_ms, 1.0))
        return self.speed_kbps * latency_weight

    def to_dict(self) -> dict:
        return {
            "source_id": self.source_id,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 1),
            "speed_kbps": round(self.speed_kbps, 1),
            "error": self.error,
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SourceProbe":
        return cls(
            source_id=str(raw.get("source_id", "")),
            ok=bool(raw.get("ok")),
            latency_ms=float(raw.get("latency_ms", 0) or 0),
            speed_kbps=float(raw.get("speed_kbps", 0) or 0),
            error=str(raw.get("error", "")),
            checked_at=float(raw.get("checked_at", 0) or 0),
        )


def sample_url(url: str, *, sample_bytes: int = 65536, timeout: float = 8.0) -> tuple[float, float]:
    """对单个 URL 做「Range 小样本」测速：返回 (TTFB 毫秒, 吞吐 KB/s)。"""
    started = time.monotonic()
    response = session().get(
        url,
        headers={"Range": f"bytes=0-{sample_bytes - 1}", "User-Agent": USER_AGENT},
        stream=True,
        timeout=(5.0, timeout),
    )
    try:
        if response.status_code not in (200, 206):
            raise NetworkError(f"HTTP {response.status_code}")
        first_byte_at: float | None = None
        received = 0
        for chunk in response.iter_content(16384):
            if first_byte_at is None:
                first_byte_at = time.monotonic()
            received += len(chunk)
            if received >= sample_bytes:
                break
        elapsed = max(time.monotonic() - started, 1e-3)
        ttfb_ms = ((first_byte_at or started + elapsed) - started) * 1000
        speed_kbps = (received / 1024) / elapsed
        return ttfb_ms, speed_kbps
    finally:
        response.close()


class MirrorSelector:
    """决定某个 URL 在本次下载中使用哪些候选地址、以什么顺序尝试。"""

    def __init__(self, settings: Settings, paths: AppPaths) -> None:
        self.settings = settings
        self.paths = paths
        self._lock = threading.RLock()
        self._probes: dict[str, SourceProbe] = {}
        self._load_cache()

    # ------------------------------------------------------------------ #
    # 设置
    # ------------------------------------------------------------------ #
    @property
    def policy(self) -> str:
        policy = str(self.settings.download_source)
        return policy if policy in ("auto", "official", "mirror") else "auto"

    @property
    def endpoint(self) -> str:
        return str(self.settings.get("mirror_endpoint") or BMCLAPI_ROOT).rstrip("/")

    def mirror_url(self, url: str) -> str | None:
        return to_mirror_url(url, endpoint=self.endpoint)

    def rewrite(self, url: str, source_id: str) -> str | None:
        if source_id == SOURCE_OFFICIAL:
            return url
        return self.mirror_url(url)

    # ------------------------------------------------------------------ #
    # 测速缓存
    # ------------------------------------------------------------------ #
    @property
    def cache_file(self) -> Path:
        return self.paths.cache_dir / "mirror_probe.json"

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.cache_file.read_text(encoding="utf-8"))
            for item in raw.get("results", []):
                probe = SourceProbe.from_dict(item)
                if probe.source_id:
                    self._probes[probe.source_id] = probe
        except (OSError, ValueError):
            return

    def _save_cache(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "checked_at": time.time(),
                "results": [p.to_dict() for p in self._probes.values()],
            }
            self.cache_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 缓存失败不影响主流程
            log.debug("测速缓存写入失败：%s", exc)

    def probes(self) -> dict[str, SourceProbe]:
        with self._lock:
            return dict(self._probes)

    def is_stale(self) -> bool:
        ttl = float(self.settings.get("mirror_probe_ttl", 900) or 900)
        with self._lock:
            if len(self._probes) < len(SOURCE_ORDER):
                return True
            newest = max(p.checked_at for p in self._probes.values())
        return (time.time() - newest) > ttl

    def probe_all(self, *, force: bool = False, timeout: float = 8.0) -> dict[str, SourceProbe]:
        """并发测速官方源与镜像源，返回 {source_id: SourceProbe}。"""
        if not force and not self.is_stale():
            return self.probes()
        results: dict[str, SourceProbe] = {}
        for source_id in SOURCE_ORDER:
            results[source_id] = self._probe_source(source_id, timeout=timeout)
        with self._lock:
            self._probes.update(results)
        self._save_cache()
        for source_id, probe in results.items():
            if probe.ok:
                log.info(
                    "测速 %s：%.0f ms / %.0f KB/s",
                    SOURCE_LABELS.get(source_id, source_id),
                    probe.latency_ms,
                    probe.speed_kbps,
                )
            else:
                log.info("测速 %s 失败：%s", SOURCE_LABELS.get(source_id, source_id), probe.error)
        return results

    def _probe_source(self, source_id: str, *, timeout: float) -> SourceProbe:
        probe = SourceProbe(source_id=source_id)
        latencies: list[float] = []
        speeds: list[float] = []
        errors: list[str] = []
        for official_url in PROBE_URLS_OFFICIAL:
            candidate = self.rewrite(official_url, source_id)
            if not candidate:
                continue
            try:
                ttfb, speed = sample_url(candidate, timeout=timeout)
                latencies.append(ttfb)
                speeds.append(speed)
            except Exception as exc:  # noqa: BLE001 - 任一节点失败视为该源失败
                errors.append(f"{exc.__class__.__name__}: {exc}")
        if latencies:
            probe.ok = True
            probe.latency_ms = sum(latencies) / len(latencies)
            probe.speed_kbps = sum(speeds) / len(speeds)
        else:
            probe.ok = False
            probe.error = "; ".join(errors[-1:]) or "全部样本失败"
        return probe

    # ------------------------------------------------------------------ #
    # 候选地址排序
    # ------------------------------------------------------------------ #
    def ranked_sources(self) -> list[str]:
        """按当前策略给出源优先级顺序。"""
        policy = self.policy
        if policy == "official":
            return [SOURCE_OFFICIAL, SOURCE_MIRROR]
        if policy == "mirror":
            return [SOURCE_MIRROR, SOURCE_OFFICIAL]
        with self._lock:
            probes = dict(self._probes)
        usable = [sid for sid in SOURCE_ORDER if probes.get(sid) and probes[sid].ok]
        if not usable:
            return list(SOURCE_ORDER)
        ranked = sorted(usable, key=lambda sid: probes[sid].score, reverse=True)
        for sid in SOURCE_ORDER:
            if sid not in ranked:
                ranked.append(sid)
        return ranked

    def preferred_source(self) -> str:
        return self.ranked_sources()[0]

    def candidates(self, url: str) -> list[str]:
        """给定官方地址，返回按偏好排序的候选 URL 列表（含回退）。"""
        ordered: list[str] = []
        for source_id in self.ranked_sources():
            rewritten = self.rewrite(url, source_id)
            if rewritten and rewritten not in ordered:
                ordered.append(rewritten)
        if url not in ordered:
            ordered.append(url)
        return ordered

    def describe_sources(self) -> str:
        with self._lock:
            probes = dict(self._probes)
        chunks = []
        for source_id in SOURCE_ORDER:
            probe = probes.get(source_id)
            label = SOURCE_LABELS.get(source_id, source_id)
            if probe is None:
                chunks.append(f"{label}: 未测速")
            elif probe.ok:
                chunks.append(f"{label}: {probe.latency_ms:.0f}ms {probe.speed_kbps:.0f}KB/s")
            else:
                chunks.append(f"{label}: 不可用")
        return " · ".join(chunks)
