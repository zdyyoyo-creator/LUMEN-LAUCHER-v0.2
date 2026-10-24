"""Lumen Launcher —— 跨平台 Minecraft Java 版第三方启动器。

模块划分（详见 ARCHITECTURE.md）：
    utils     通用工具：日志、网络、文件、版本号解析
    core      版本清单、镜像源、下载器、安装、启动、崩溃分析
    java      Java 探测与 Mojang 运行时自动获取
    auth      账号（离线 / 微软正版预留）
    modpack   整合包解析与导入（mrpack / CurseForge / 普通 zip）
    ui        PySide6 界面层（主题、组件、页面、对话框）
"""

APP_ID = "LumenLauncher"
APP_DISPLAY_NAME = "Lumen Launcher"
APP_VERSION = "1.0.0"
APP_CHANNEL = "stable"

#: 所有出站请求统一 UA，便于镜像站统计与排障
USER_AGENT = f"{APP_ID}/{APP_VERSION} (+https://example.invalid/lumen)"

__all__ = ["APP_ID", "APP_DISPLAY_NAME", "APP_VERSION", "APP_CHANNEL", "USER_AGENT"]
