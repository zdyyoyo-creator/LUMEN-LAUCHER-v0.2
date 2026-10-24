"""后台任务封装：所有耗时操作（网络/解压/安装/启动探测）都走 QThread。

约定：
* worker 函数只做纯逻辑，不与 widget 直接交互；
* 结果通过信号回到主线程（``succeeded`` / ``failed`` / ``cancelled``）；
* 进度回调已做节流（默认 100ms），避免高频信号把 UI 压垮；
* 任何未捕获异常都会被转成可读文案，绝不让进程静默崩溃。
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal

from ..utils.errors import LumenError, OperationCancelled
from ..utils.log import get as get_logger

log = get_logger("ui.worker")

PROGRESS_THROTTLE = 0.1


class Worker(QThread):
    """在后台线程执行 ``fn``，可注入 progress / cancel_check。"""

    succeeded = Signal(object)
    failed = Signal(str, str)  # (message, hint)
    cancelled = Signal()
    progressed = Signal(object)

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        with_progress: bool = False,
        with_cancel: bool = False,
        parent: QObject | None = None,
        label: str = "",
    ) -> None:
        super().__init__(parent)
        self._fn = fn
        self._with_progress = with_progress
        self._with_cancel = with_cancel
        self._label = label or getattr(fn, "__name__", "任务")
        self._cancel_event = threading.Event()
        self._last_emit = 0.0
        self._result: Any = None
        self._error: tuple[str, str] | None = None

    # ------------------------------------------------------------------ #
    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_event.is_set()

    # ------------------------------------------------------------------ #
    def _emit_progress(self, payload: object) -> None:
        now = time.monotonic()
        if now - self._last_emit < PROGRESS_THROTTLE:
            return
        self._last_emit = now
        self.progressed.emit(payload)

    def run(self) -> None:  # noqa: D401 - QThread 入口
        kwargs: dict[str, Any] = {}
        if self._with_progress:
            kwargs["progress"] = self._emit_progress
        if self._with_cancel:
            kwargs["cancel_check"] = self._cancel_event.is_set
        try:
            result = self._fn(**kwargs)
        except OperationCancelled:
            log.debug("后台任务已取消：%s", self._label)
            self.cancelled.emit()
            return
        except LumenError as exc:
            log.warning("后台任务失败（%s）：%s", self._label, exc.message)
            self.failed.emit(exc.message, exc.hint)
            return
        except Exception as exc:  # noqa: BLE001 - 兜底，不让异常穿透线程
            detail = traceback.format_exc(limit=6)
            log.error("后台任务异常（%s）：%s\n%s", self._label, exc, detail)
            self.failed.emit(f"{exc.__class__.__name__}: {exc}", detail.strip().splitlines()[-1] if detail else "")
            return
        self.succeeded.emit(result)


class TaskManager(QObject):
    """持有运行中的 worker 引用，回收线程，并在窗口关闭时统一取消。"""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._workers: set[Worker] = set()

    def start(self, worker: Worker) -> Worker:
        worker.finished.connect(lambda: self._cleanup(worker))
        self._workers.add(worker)
        worker.start()
        return worker

    def _cleanup(self, worker: Worker) -> None:
        self._workers.discard(worker)
        worker.deleteLater()

    def shutdown(self, timeout_ms: int = 3000) -> None:
        for worker in list(self._workers):
            worker.cancel()
        for worker in list(self._workers):
            if worker.isRunning():
                worker.wait(timeout_ms)
        self._workers.clear()

    @property
    def busy(self) -> bool:
        return any(worker.isRunning() for worker in self._workers)
