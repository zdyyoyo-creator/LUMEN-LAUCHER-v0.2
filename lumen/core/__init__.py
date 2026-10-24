"""core 包：版本清单、镜像源、下载器、安装、启动与崩溃分析。"""

from .downloader import DownloadReport, Downloader, DownloadTask  # noqa: F401
from .manifest import RemoteVersion, VersionDetail, VersionManifest  # noqa: F401
from .mirrors import MirrorSelector, SourceProbe  # noqa: F401

__all__ = [
    "Downloader",
    "DownloadTask",
    "DownloadReport",
    "MirrorSelector",
    "SourceProbe",
    "VersionManifest",
    "VersionDetail",
    "RemoteVersion",
]
