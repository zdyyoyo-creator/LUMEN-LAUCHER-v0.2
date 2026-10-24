"""统一的异常层次。

设计原则：底层只抛带上下文信息的语义化异常，UI 层捕获 ``LumenError`` 并翻译成
人话提示；任何未预期异常也不能让进程直接退出（见 ``ui/workers.py``）。
"""

from __future__ import annotations


class LumenError(Exception):
    """所有 Lumen 自定义异常的基类。"""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - 仅格式化
        return self.message


class NetworkError(LumenError):
    """网络请求失败（连接、超时、HTTP 状态码异常）。"""


class DownloadError(LumenError):
    """下载失败（重试耗尽、写入磁盘失败）。"""


class IntegrityError(DownloadError):
    """文件完整性校验失败（大小或 SHA1 不符）。"""


class ManifestError(LumenError):
    """版本清单 / version.json 结构异常。"""


class InstallError(LumenError):
    """安装流程失败（缺少必要字段、解压失败等）。"""


class LaunchError(LumenError):
    """启动器无法组装出可执行的启动命令。"""


class JavaError(LumenError):
    """Java 探测 / 下载 / 版本匹配失败。"""


class ModpackError(LumenError):
    """整合包解析或导入失败。"""


class ConfigError(LumenError):
    """配置文件损坏或无法写入。"""


class OperationCancelled(LumenError):
    """用户主动取消（不视为错误，UI 静默处理）。"""

    def __init__(self, message: str = "操作已取消") -> None:
        super().__init__(message)
