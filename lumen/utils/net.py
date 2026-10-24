"""HTTP 基础能力：线程独立 Session、JSON 拉取、HEAD 测速、流式断点续传。

设计取舍：
* requests.Session 不是严格线程安全的，这里按线程各持一个连接池；
* 重试策略放在调用方（下载器/清单）显式控制，便于区分「可重试」与「永久失败」；
* 所有异常统一包装成 ``NetworkError``，携带 URL 与状态码，UI 可直接展示。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Callable
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter

from .. import USER_AGENT
from .errors import NetworkError, OperationCancelled
from .log import get as get_logger

log = get_logger("utils.net")

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 30.0
JSON_TIMEOUT = 20.0
CHUNK_SIZE = 256 * 1024

#: 停滞检测：连续 STALL_WINDOW 秒写入不足 STALL_MIN_BYTES 就判定为「连接假死」，换源重试
STALL_WINDOW = 15.0
STALL_MIN_BYTES = 24 * 1024

_thread_local = threading.local()


def session() -> requests.Session:
    """每个线程一个 Session（连接池复用，避免跨线程共享问题）。"""
    sess: requests.Session | None = getattr(_thread_local, "session", None)
    if sess is None:
        sess = requests.Session()
        sess.headers.update({"User-Agent": USER_AGENT, "Accept": "*/*"})
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=32, max_retries=0)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _thread_local.session = sess
    return sess


def close_thread_session() -> None:
    sess: requests.Session | None = getattr(_thread_local, "session", None)
    if sess is not None:
        try:
            sess.close()
        except Exception:  # pragma: no cover
            pass
        _thread_local.session = None


def _wrap(exc: Exception, url: str) -> NetworkError:
    if isinstance(exc, requests.Timeout):
        return NetworkError(f"请求超时：{url}", hint="网络较慢或被墙，可切换到镜像源后重试")
    if isinstance(exc, requests.ConnectionError):
        return NetworkError(f"无法连接：{url}", hint="请检查网络、代理或防火墙设置")
    if isinstance(exc, requests.HTTPError):
        return NetworkError(f"服务器返回错误：{exc}", hint=url)
    return NetworkError(f"请求失败：{exc}", hint=url)


def get_json(
    url: str,
    *,
    timeout: float = JSON_TIMEOUT,
    retries: int = 2,
    headers: dict[str, str] | None = None,
    backoff: float = 0.8,
    expect: type | tuple[type, ...] = dict,
    params: dict | None = None,
) -> dict | list:
    """拉取 JSON，带指数退避重试。

    ``expect`` 指定顶层类型：Mojang 清单是对象（dict），Fabric/Quilt 的
    meta API 是数组（list），调用方按需指定，避免把「结构不符」误判成网络错误。
    ``params`` 用于 GET 查询串（Modrinth facets / CurseForge 搜索参数）。
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = session().get(
                url, timeout=(CONNECT_TIMEOUT, timeout), headers=headers, params=params
            )
            if response.status_code in (401, 403):
                raise NetworkError(
                    f"接口拒绝访问（{response.status_code}）：{url}",
                    hint="可能需要 API Key，或 Key 无效/被限流",
                )
            if response.status_code == 404:
                raise NetworkError(f"资源不存在（404）：{url}")
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise NetworkError(f"响应不是合法 JSON：{url}", hint=str(exc)) from exc
            if not isinstance(data, expect):
                raise NetworkError(f"响应结构异常（期望 {getattr(expect, '__name__', expect)}）：{url}")
            return data
        except NetworkError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一转换
            last = exc
            wait = backoff * (2**attempt)
            if attempt < retries:
                log.debug("请求失败，%.1fs 后重试（%s/%s）：%s", wait, attempt + 1, retries, url)
                time.sleep(wait)
    raise _wrap(last or RuntimeError("unknown"), url)


def get_bytes(
    url: str,
    *,
    timeout: float = JSON_TIMEOUT,
    retries: int = 1,
    headers: dict[str, str] | None = None,
    limit: int = 64 * 1024 * 1024,
) -> bytes:
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            response = session().get(url, timeout=(CONNECT_TIMEOUT, timeout), headers=headers)
            response.raise_for_status()
            if len(response.content) > limit:
                raise NetworkError(f"响应体过大：{url}")
            return response.content
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries:
                time.sleep(0.6 * (2**attempt))
    raise _wrap(last or RuntimeError("unknown"), url)


def head_probe(url: str, *, timeout: float = 8.0) -> tuple[float, int, int]:
    """HEAD 测速：返回 (耗时秒, HTTP 状态码, Content-Length)。

    有些镜像对 HEAD 返回 405，此时退化为 Range: bytes=0-0 的小 GET。
    """
    started = time.monotonic()
    try:
        response = session().head(url, timeout=(CONNECT_TIMEOUT, timeout), allow_redirects=True)
        elapsed = time.monotonic() - started
        if response.status_code == 405 or response.status_code == 501:
            return _range_probe(url, timeout=timeout)
        size = int(response.headers.get("Content-Length") or 0)
        return elapsed, response.status_code, size
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, url) from exc


def _range_probe(url: str, *, timeout: float = 8.0) -> tuple[float, int, int]:
    started = time.monotonic()
    response = session().get(
        url, timeout=(CONNECT_TIMEOUT, timeout), headers={"Range": "bytes=0-0"}, stream=True
    )
    try:
        elapsed = time.monotonic() - started
        total = 0
        content_range = response.headers.get("Content-Range", "")
        if "/" in content_range:
            try:
                total = int(content_range.rsplit("/", 1)[1])
            except ValueError:
                total = 0
        return elapsed, response.status_code, total
    finally:
        response.close()


class DownloadCancelled(Exception):
    """内部使用：下载被用户取消。"""


def download_to_part(
    url: str,
    part_path: str,
    *,
    expected_size: int | None = None,
    progress: Callable[[int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    headers: dict[str, str] | None = None,
    timeout: tuple[float, float] = (CONNECT_TIMEOUT, READ_TIMEOUT),
) -> int:
    """流式下载到 ``.part`` 文件，支持 Range 续传与取消。

    返回本次写入的字节数。调用方负责校验与改名。
    """
    existing = os.path.getsize(part_path) if os.path.exists(part_path) else 0
    if expected_size is not None and existing > expected_size:
        # 本地残留比期望还大，直接丢弃重下
        try:
            os.remove(part_path)
        except OSError:
            pass
        existing = 0

    req_headers = dict(headers or {})
    if existing > 0:
        req_headers["Range"] = f"bytes={existing}-"

    try:
        response = session().get(url, headers=req_headers, stream=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        raise _wrap(exc, url) from exc

    with response:
        if response.status_code == 416:
            # Range 超出文件范围：多半是本地文件已完整，交给调用方校验
            return 0
        if response.status_code not in (200, 206):
            raise NetworkError(
                f"下载失败（HTTP {response.status_code}）：{url}",
                hint=urlparse(url).netloc,
            )

        mode = "ab" if response.status_code == 206 and existing > 0 else "wb"
        written = 0
        os.makedirs(os.path.dirname(os.path.abspath(part_path)) or ".", exist_ok=True)
        # 停滞看门狗：某些 CDN 节点会保持连接但不发数据（read timeout 不触发），
        # 这里按「时间窗口内的字节增量」判定，主动放弃并让下载器换源。
        last_mark_time = time.monotonic()
        last_mark_bytes = 0
        try:
            with open(part_path, mode) as fh:
                for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
                    if cancel_check is not None and cancel_check():
                        raise DownloadCancelled()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(len(chunk))
                    now = time.monotonic()
                    if now - last_mark_time >= STALL_WINDOW:
                        delta = written - last_mark_bytes
                        if delta < STALL_MIN_BYTES:
                            raise NetworkError(
                                f"下载停滞（{STALL_WINDOW:.0f} 秒仅收到 {delta} 字节）",
                                hint=f"已下载 {existing + written} / {expected_size or '?'} 字节，将换源重试",
                            )
                        last_mark_time = now
                        last_mark_bytes = written
        except DownloadCancelled as exc:
            raise OperationCancelled("下载已取消") from exc
        except requests.RequestException as exc:
            raise _wrap(exc, url) from exc
        return written


def looks_like_json_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".json")


def safe_json_loads(text: str) -> dict | None:
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None
