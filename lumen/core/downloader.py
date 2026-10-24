"""多线程下载器：断点续传、SHA1 校验、指数退避重试、暂停/取消、进度聚合。

使用方式（在后台线程里调用，UI 只接收回调）::

    downloader = Downloader(selector, max_workers=12)
    report = downloader.run(tasks, progress=lambda snap: ..., label="安装 1.21.4")

并发模型：
* ``ThreadPoolExecutor`` 负责文件级并发；
* 一个「进度心跳线程」每 250ms 聚合快照，避免几万次小文件回调把 GUI 压垮；
* pause/cancel 通过 ``threading.Event`` 广播，worker 在 chunk 之间检查。
"""

from __future__ import annotations

import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..utils.errors import DownloadError, IntegrityError, LumenError, NetworkError, OperationCancelled
from ..utils.fsutil import human_speed, sha1_file
from ..utils.log import get as get_logger
from ..utils.net import DownloadCancelled, download_to_part

log = get_logger("core.downloader")

PROGRESS_INTERVAL = 0.25
MAX_BACKOFF = 8.0


@dataclass(frozen=True)
class DownloadTask:
    """一个待下载文件。``url`` 是官方地址，镜像改写由下载器负责。"""

    url: str
    path: Path
    sha1: str | None = None
    size: int | None = None
    label: str = ""
    optional: bool = False

    @property
    def display_name(self) -> str:
        return self.label or Path(self.path).name


@dataclass
class DownloadProgress:
    label: str
    total_files: int
    done_files: int
    failed_files: int
    total_bytes: int
    done_bytes: int
    speed: float
    active: tuple[str, ...]
    elapsed: float
    message: str = ""

    @property
    def percent(self) -> float:
        if self.total_files <= 0:
            return 0.0
        if self.total_bytes > 0:
            return max(0.0, min(1.0, self.done_bytes / self.total_bytes))
        return max(0.0, min(1.0, self.done_files / self.total_files))

    @property
    def percent_by_files(self) -> float:
        if self.total_files <= 0:
            return 0.0
        return max(0.0, min(1.0, self.done_files / self.total_files))


@dataclass
class DownloadReport:
    label: str = ""
    ok: int = 0
    skipped: int = 0
    failed: int = 0
    bytes_written: int = 0
    elapsed: float = 0.0
    errors: list[tuple[str, str]] = field(default_factory=list)
    cancelled: bool = False

    @property
    def total(self) -> int:
        return self.ok + self.skipped + self.failed

    @property
    def success(self) -> bool:
        return self.failed == 0 and not self.cancelled

    def summary(self) -> str:
        parts = [f"完成 {self.ok} 个"]
        if self.skipped:
            parts.append(f"跳过 {self.skipped} 个（已校验）")
        if self.failed:
            parts.append(f"失败 {self.failed} 个")
        return "，".join(parts)


class _ProgressState:
    """线程安全的进度累加器。"""

    def __init__(self, label: str) -> None:
        self.label = label
        self.lock = threading.RLock()
        self.total_files = 0
        self.done_files = 0
        self.failed_files = 0
        self.total_bytes = 0
        self.done_bytes = 0
        self.active: dict[str, float] = {}
        self.started = time.monotonic()
        self.message = "准备中"
        # 速度滑动窗口：(时间, 累计字节)
        self._samples: list[tuple[float, int]] = [(self.started, 0)]

    def add_file(self, display_name: str) -> None:
        with self.lock:
            self.active[display_name] = time.monotonic()

    def remove_file(self, display_name: str) -> None:
        with self.lock:
            self.active.pop(display_name, None)

    def add_bytes(self, count: int) -> None:
        with self.lock:
            self.done_bytes += max(0, count)

    def add_size(self, size: int) -> None:
        with self.lock:
            self.total_bytes += max(0, size)

    def complete_file(self, *, skipped: bool = False) -> None:
        with self.lock:
            self.done_files += 1

    def fail_file(self) -> None:
        with self.lock:
            self.failed_files += 1

    def snapshot(self) -> DownloadProgress:
        with self.lock:
            now = time.monotonic()
            self._samples.append((now, self.done_bytes))
            # 保留最近 6 秒的样本
            self._samples = [(t, b) for t, b in self._samples if now - t <= 6.0]
            speed = 0.0
            if len(self._samples) >= 2:
                oldest_t, oldest_b = self._samples[0]
                span = max(now - oldest_t, 1e-3)
                speed = max(0.0, (self.done_bytes - oldest_b) / span)
            active = tuple(sorted(self.active)[:4])
            return DownloadProgress(
                label=self.label,
                total_files=self.total_files,
                done_files=self.done_files,
                failed_files=self.failed_files,
                total_bytes=self.total_bytes,
                done_bytes=min(self.done_bytes, self.total_bytes) if self.total_bytes else self.done_bytes,
                speed=speed,
                active=active,
                elapsed=now - self.started,
                message=self.message,
            )


class Downloader:
    def __init__(
        self,
        selector,
        *,
        max_workers: int = 12,
        retries: int = 3,
        timeout: tuple[float, float] = (10.0, 30.0),
        verify_hash: bool = True,
    ) -> None:
        self.selector = selector
        self.max_workers = max(1, min(32, int(max_workers)))
        self.retries = max(0, int(retries))
        self.timeout = timeout
        self.verify_hash = verify_hash
        self._cancel = threading.Event()
        self._pause = threading.Event()
        self._state: _ProgressState | None = None
        self._progress_cb: Callable[[DownloadProgress], None] | None = None
        self._heartbeat: threading.Thread | None = None
        self._stop_heartbeat = threading.Event()

    # ------------------------------------------------------------------ #
    # 控制
    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancel.set()
        self._pause.clear()

    def pause(self) -> None:
        self._pause.set()
        if self._state is not None:
            with self._state.lock:
                self._state.message = "已暂停"

    def resume(self) -> None:
        self._pause.clear()
        if self._state is not None:
            with self._state.lock:
                self._state.message = ""

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def _wait_if_paused(self) -> None:
        while self._pause.is_set():
            if self._cancel.is_set():
                raise OperationCancelled("下载已取消")
            time.sleep(0.1)

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def run(
        self,
        tasks: Iterable[DownloadTask],
        *,
        progress: Callable[[DownloadProgress], None] | None = None,
        label: str = "下载",
    ) -> DownloadReport:
        task_list = list(tasks)
        report = DownloadReport(label=label)
        started = time.monotonic()
        state = _ProgressState(label)
        self._state = state
        self._progress_cb = progress
        self._cancel.clear()
        self._pause.clear()

        # 统计总量（size 已知时用于总进度）
        for task in task_list:
            if task.size and task.size > 0:
                state.add_size(int(task.size))
            # 断点续传：已存在的 .part 字节要算作「已完成」，否则进度条会从 0 重新走
            part = Path(task.path).with_name(Path(task.path).name + ".part")
            if part.exists() and not Path(task.path).exists():
                try:
                    resume_bytes = part.stat().st_size
                    if task.size:
                        resume_bytes = min(resume_bytes, int(task.size))
                    state.add_bytes(resume_bytes)
                except OSError:
                    pass
        state.total_files = len(task_list)
        state.message = f"共 {len(task_list)} 个文件"

        if not task_list:
            report.elapsed = time.monotonic() - started
            return report

        self._start_heartbeat()
        try:
            with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="lumen-dl") as pool:
                futures = {pool.submit(self._download_one, task): task for task in task_list}
                try:
                    for future in as_completed(futures):
                        task = futures[future]
                        try:
                            result = future.result()
                        except OperationCancelled:
                            report.cancelled = True
                            self._cancel.set()
                            raise
                        except Exception as exc:  # noqa: BLE001 - 单个文件失败不终止整体
                            report.failed += 1
                            state.fail_file()
                            message = str(exc) or exc.__class__.__name__
                            report.errors.append((task.display_name, message))
                            if task.optional:
                                log.debug("可选文件下载失败：%s（%s）", task.display_name, message)
                            else:
                                log.warning("文件下载失败：%s（%s）", task.display_name, message)
                            continue
                        if result == "ok":
                            report.ok += 1
                            report.bytes_written += int(task.size or 0)
                        elif result == "skipped":
                            report.skipped += 1
                        state.complete_file(skipped=(result == "skipped"))
                except OperationCancelled:
                    report.cancelled = True
                    for future in futures:
                        future.cancel()
                    raise
        finally:
            self._stop_heartbeat.set()
            heartbeat = self._heartbeat
            if heartbeat is not None:
                heartbeat.join(timeout=2.0)
            self._emit(final=True)
            report.elapsed = time.monotonic() - started

        if report.failed and not report.cancelled:
            log.warning("%s：%s", label, report.summary())
        return report

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _start_heartbeat(self) -> None:
        self._stop_heartbeat.clear()

        def _beat() -> None:
            while not self._stop_heartbeat.wait(PROGRESS_INTERVAL):
                self._emit()

        self._heartbeat = threading.Thread(target=_beat, name="lumen-dl-progress", daemon=True)
        self._heartbeat.start()

    def _emit(self, *, final: bool = False) -> None:
        state, callback = self._state, self._progress_cb
        if state is None or callback is None:
            return
        try:
            callback(state.snapshot())
        except Exception as exc:  # pragma: no cover - UI 回调异常不能影响下载
            log.debug("进度回调异常：%s", exc)

    def _needs_download(self, task: DownloadTask) -> bool:
        path = Path(task.path)
        if not path.exists():
            return True
        if task.size is not None and task.size >= 0 and path.stat().st_size != task.size:
            return True
        if self.verify_hash and task.sha1:
            try:
                return sha1_file(path) != str(task.sha1).lower()
            except OSError:
                return True
        return False

    def _download_one(self, task: DownloadTask) -> str:
        state = self._state
        display = task.display_name
        if state is not None:
            state.add_file(display)
        try:
            path = Path(task.path)
            part = path.with_name(path.name + ".part")
            path.parent.mkdir(parents=True, exist_ok=True)

            if path.exists() and not self._needs_download(task):
                if state is not None:
                    # 跳过的文件也计入已完成字节，进度条不会倒退
                    if task.size:
                        state.add_bytes(int(task.size))
                return "skipped"

            if self.verify_hash and task.sha1 and path.exists():
                log.debug("校验失败，重新下载：%s", display)

            candidates = self.selector.candidates(task.url)
            last_error: Exception | None = None
            for attempt in range(self.retries + 1):
                self._wait_if_paused()
                if self._cancel.is_set():
                    raise OperationCancelled("下载已取消")
                for candidate in candidates:
                    self._wait_if_paused()
                    if self._cancel.is_set():
                        raise OperationCancelled("下载已取消")
                    try:
                        self._fetch(candidate, part, task, state)
                        self._finalize(path, part, task)
                        return "ok"
                    except OperationCancelled:
                        raise
                    except (NetworkError, IntegrityError, DownloadError, DownloadCancelled) as exc:
                        last_error = exc
                        log.debug("下载源失败（%s）：%s", display, exc)
                        # 校验失败说明内容有问题，删掉半成品，换源重来
                        if isinstance(exc, IntegrityError):
                            _safe_unlink(part)
                        continue
                    except Exception as exc:  # noqa: BLE001
                        last_error = exc
                        log.debug("下载异常（%s）：%s", display, exc)
                        continue
                if attempt < self.retries:
                    delay = min(MAX_BACKOFF, 0.5 * (2**attempt))
                    if state is not None:
                        with state.lock:
                            state.message = f"重试中（{attempt + 1}/{self.retries}）：{display}"
                    time.sleep(delay)
            raise DownloadError(
                f"下载失败：{display}",
                hint=str(last_error) if last_error else "所有下载源均不可用",
            )
        finally:
            if state is not None:
                state.remove_file(display)

    def _fetch(
        self,
        url: str,
        part: Path,
        task: DownloadTask,
        state: _ProgressState | None,
    ) -> None:
        def _on_chunk(count: int) -> None:
            # 暂停：阻塞在 chunk 之间（连接保留，恢复后继续），不丢弃已下载内容
            while self._pause.is_set():
                if self._cancel.is_set():
                    raise OperationCancelled("下载已取消")
                time.sleep(0.1)
            if self._cancel.is_set():
                raise OperationCancelled("下载已取消")
            if state is not None:
                state.add_bytes(count)

        download_to_part(
            url,
            str(part),
            expected_size=task.size,
            progress=_on_chunk,
            cancel_check=lambda: self._cancel.is_set(),
            timeout=self.timeout,
        )

    def _finalize(self, path: Path, part: Path, task: DownloadTask) -> None:
        if not part.exists():
            # 极端情况：Range 416 且本地文件已存在
            if path.exists():
                return
            raise DownloadError(f"下载未产生文件：{task.display_name}")
        if task.size is not None and task.size >= 0:
            actual = part.stat().st_size
            if actual != task.size:
                _safe_unlink(part)
                raise IntegrityError(
                    f"大小不符：{task.display_name}", hint=f"期望 {task.size} 实际 {actual}"
                )
        if self.verify_hash and task.sha1:
            actual_sha = sha1_file(part)
            if actual_sha != str(task.sha1).lower():
                _safe_unlink(part)
                raise IntegrityError(
                    f"SHA1 校验失败：{task.display_name}",
                    hint=f"期望 {task.sha1} 实际 {actual_sha}",
                )
        try:
            os.replace(part, path)
        except OSError:
            shutil.move(str(part), str(path))


def _safe_unlink(path: Path) -> None:
    try:
        Path(path).unlink()
    except OSError:
        pass


def download_many(
    selector,
    tasks: Iterable[DownloadTask],
    *,
    label: str = "下载",
    progress: Callable[[DownloadProgress], None] | None = None,
    max_workers: int = 12,
    downloader: Downloader | None = None,
) -> DownloadReport:
    """便捷函数：一次性下载一组文件。"""
    worker = downloader or Downloader(selector, max_workers=max_workers)
    return worker.run(tasks, progress=progress, label=label)


def format_progress(progress: DownloadProgress) -> str:
    from ..utils.fsutil import human_size

    return (
        f"{progress.done_files}/{progress.total_files} 文件 · "
        f"{human_size(progress.done_bytes)}/{human_size(progress.total_bytes)} · "
        f"{human_speed(progress.speed)}"
    )
