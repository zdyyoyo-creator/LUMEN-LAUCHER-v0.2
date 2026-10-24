"""日志基础设施。

三个落点：
1. 控制台（开发期）
2. ``logs/launcher.log``（滚动 2MB×3，长跑排障）
3. 内存环形缓冲（UI 日志面板直接读取，不碰磁盘）
"""

from __future__ import annotations

import collections
import logging
import logging.handlers
import sys
import threading
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

_ring_lock = threading.RLock()
_ring: "collections.deque[tuple[str, str, str]]" = collections.deque(maxlen=4000)
_initialized = False


class RingBufferHandler(logging.Handler):
    """把日志压进内存环形缓冲，供 UI 日志窗口消费。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
            with _ring_lock:
                _ring.append((record.levelname, record.name, text))
        except Exception:  # pragma: no cover - 日志永远不能拖垮主流程
            pass


def setup(log_dir: Path, *, debug: bool = False) -> Path:
    """初始化根 logger，返回主日志文件路径。可重复调用（幂等）。"""
    global _initialized
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "launcher.log"

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    if _initialized:
        return log_file

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # 文件：按大小滚动，避免长期运行把磁盘写满
    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    # 控制台：Windows 下 stdout 可能被 PyInstaller windowed 模式吃掉
    if sys.stderr is not None:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(formatter)
        console.setLevel(logging.DEBUG if debug else logging.INFO)
        root.addHandler(console)

    ring = RingBufferHandler()
    ring.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    ring.setLevel(logging.DEBUG)
    root.addHandler(ring)

    # 压掉第三方库的噪音
    for noisy in ("urllib3", "requests", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _initialized = True
    logging.getLogger("lumen").debug("日志系统就绪：%s", log_file)
    return log_file


def get(name: str) -> logging.Logger:
    return logging.getLogger(name if name.startswith("lumen") else f"lumen.{name}")


def recent(limit: int = 400) -> list[str]:
    """取最近若干条已格式化日志（供 UI 展示）。"""
    with _ring_lock:
        items = list(_ring)[-limit:]
    return [text for _level, _name, text in items]


def snapshot() -> str:
    """把当前环形缓冲导出为一段文本（用于「复制日志」）。"""
    with _ring_lock:
        return "\n".join(text for _level, _name, text in _ring)


def clear() -> None:
    with _ring_lock:
        _ring.clear()


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")
