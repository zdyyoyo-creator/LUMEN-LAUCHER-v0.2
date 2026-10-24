"""启动参数组装与游戏进程管理。

职责：
1. 把 version.json 的 arguments / minecraftArguments 展开成最终命令行；
2. 组装 JVM 参数（内存、natives、品牌标识、用户自定义参数）与 classpath；
3. 启动进程、把 stdout/stderr 重定向到实例 logs 目录、回调给 UI 的日志面板。

参数顺序遵循官方启动器约定：
    java  [-Xmx -Xmn]  [-D...]  <json jvm args>  <自定义 jvm>  -cp <classpath>  <mainClass>  <game args>
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .. import APP_DISPLAY_NAME, APP_VERSION
from ..auth.account import Account
from ..config import AppPaths
from ..utils.errors import InstallError, LaunchError, OperationCancelled
from ..utils.fsutil import ensure_dir
from ..utils.log import get as get_logger
from .instances import Instance
from .manifest import VersionDetail

log = get_logger("core.launcher")

_VAR_RE = re.compile(r"\$\{([a-zA-Z0-9_]+)\}")


@dataclass
class LaunchOptions:
    java_path: str
    memory_mb: int = 4096
    jvm_args: str = ""
    width: int = 0
    height: int = 0
    fullscreen: bool = False
    extra_game_args: list[str] = field(default_factory=list)
    demo: bool = False
    server: str = ""
    port: str = ""


@dataclass
class PreparedLaunch:
    command: list[str]
    cwd: Path
    version_id: str
    detail: VersionDetail
    log_file: Path
    notes: list[str] = field(default_factory=list)
    classpath: str = ""
    java_path: str = ""
    memory_mb: int = 0

    def command_preview(self, max_len: int = 2000) -> str:
        text = " ".join(f'"{part}"' if " " in part else part for part in self.command)
        return text if len(text) <= max_len else text[:max_len] + " …"


def split_user_args(raw: str) -> list[str]:
    """把用户填的 JVM/游戏参数拆成列表，兼容引号包裹的路径。"""
    if not raw or not raw.strip():
        return []
    try:
        return shlex.split(raw, posix=False) if sys.platform == "win32" else shlex.split(raw)
    except ValueError:
        # 引号不配对时退化为按空格切分
        return [part for part in raw.split() if part]


def _norm_args(args: list[str]) -> list[str]:
    """去掉 Windows shlex(posix=False) 残留的引号。"""
    fixed: list[str] = []
    for arg in args:
        if len(arg) >= 2 and arg[0] == arg[-1] and arg[0] in ('"', "'"):
            fixed.append(arg[1:-1])
        else:
            fixed.append(arg)
    return fixed


def substitute(tokens: list[str], variables: dict[str, str]) -> list[str]:
    result: list[str] = []
    for token in tokens:
        def _replace(match: re.Match) -> str:
            key = match.group(1)
            return variables.get(key, match.group(0))

        value = _VAR_RE.sub(_replace, token)
        # 只有整段就是变量且值为空时才丢弃（避免产生空参数）
        if value == "" and token.startswith("${") and token.endswith("}"):
            continue
        result.append(value)
    return result


def _features_for(options: LaunchOptions) -> dict[str, bool]:
    return {
        "is_demo_user": bool(options.demo),
        "has_custom_resolution": bool(options.width and options.height),
        "has_quick_plays_support": False,
        "is_quick_play_singleplayer": False,
        "is_quick_play_multiplayer": bool(options.server),
        "is_quick_play_realms": False,
    }


def resolve_client_jar(instance: Instance, version_id: str, detail: VersionDetail) -> Path:
    """加载器版本通常复用原版客户端 jar。"""
    jar = instance.version_jar(version_id)
    if jar.exists():
        return jar
    parent = detail.inherits_from or version_id
    parent_jar = instance.version_jar(parent)
    if parent_jar.exists():
        return parent_jar
    # 最后兜底：从 `1.20.1-fabric` 里猜出 `1.20.1`
    guess = version_id.split("-")[0]
    guess_jar = instance.version_jar(guess)
    if guess_jar.exists():
        return guess_jar
    raise InstallError(
        f"找不到客户端 jar：{jar.name}",
        hint="请重新安装该版本以补齐文件",
    )


def build_classpath(instance: Instance, version_id: str, detail: VersionDetail, paths: AppPaths) -> str:
    classpath_files = []
    for lib in detail.classpath_and_natives()[0]:
        path = paths.libraries_dir / lib.relative_path
        classpath_files.append(str(path))
    jar = resolve_client_jar(instance, version_id, detail)
    classpath_files.append(str(jar))
    return os.pathsep.join(classpath_files)


def load_version_json(instance: Instance, version_id: str) -> dict:
    """读取版本 json 并沿 inheritsFrom 合并（Forge/NeoForge 必需）。"""
    from .version_store import resolve_version

    return resolve_version(instance, version_id)


def prepare_launch(
    paths: AppPaths,
    instance: Instance,
    account: Account,
    options: LaunchOptions,
    *,
    version_id: str | None = None,
) -> PreparedLaunch:
    """组装完整启动命令（不启动进程）。"""
    version_id = version_id or instance.version_id
    data = load_version_json(instance, version_id)
    detail = VersionDetail(data, version_id=version_id)
    notes: list[str] = []

    java_path = options.java_path
    if not java_path or not Path(java_path).exists():
        raise LaunchError(
            "Java 路径无效",
            hint="请在设置或实例配置里选择一个有效的 Java 可执行文件",
        )

    natives_dir = instance.natives_dir(version_id)
    # 加载器版本的 natives 可能复用原版目录
    if not natives_dir.is_dir():
        parent_natives = instance.natives_dir(detail.inherits_from or instance.mc_version)
        if parent_natives.is_dir():
            natives_dir = parent_natives
        else:
            notes.append("natives 目录不存在，启动后可能报 UnsatisfiedLinkError")

    classpath = build_classpath(instance, version_id, detail, paths)
    features = _features_for(options)
    jvm_tokens, game_tokens = detail.resolved_arguments(features=features)
    if not game_tokens:
        raise LaunchError("版本文件里没有可用的游戏参数", hint="version.json 缺少 arguments.game")

    asset_index_id = str(detail.asset_index.get("id") or detail.assets or "")
    assets_root = paths.assets_dir
    if str(detail.data.get("assets") or "").lower() == "legacy":
        virtual_dir = assets_root / "virtual" / "legacy"
        if virtual_dir.is_dir():
            assets_root = virtual_dir
        else:
            notes.append("该版本需要 legacy 资源，但虚拟资源目录缺失")

    variables = {
        "auth_player_name": account.username,
        "version_name": version_id,
        "game_directory": str(instance.game_dir),
        "assets_root": str(paths.assets_dir),
        "game_assets": str(assets_root),
        "assets_index_name": asset_index_id,
        "auth_uuid": account.uuid,
        "auth_access_token": account.access_token,
        "auth_session": account.access_token,
        "clientid": account.client_id,
        "auth_xuid": account.xuid,
        "user_type": account.user_type,
        "user_properties": account.properties_json,
        "version_type": str(detail.type),
        "natives_directory": str(natives_dir),
        "launcher_name": APP_DISPLAY_NAME.replace(" ", ""),
        "launcher_version": APP_VERSION,
        "classpath": classpath,
        "classpath_separator": os.pathsep,
        "library_directory": str(paths.libraries_dir),
        "resolution_width": str(options.width),
        "resolution_height": str(options.height),
        "quickPlayPath": "",
        "quickPlaySingleplayer": "",
        "quickPlayMultiplayer": options.server or "",
    }

    jvm_args = substitute(jvm_tokens, variables)
    game_args = substitute(game_tokens, variables)

    has_cp = any(token in ("-cp", "-classpath") for token in jvm_args)
    has_brand = any(token.startswith("-Dminecraft.launcher.brand") for token in jvm_args)
    has_natives = any(token.startswith("-Djava.library.path") for token in jvm_args)

    memory = max(512, int(options.memory_mb))
    command: list[str] = [java_path]
    command.append(f"-Xmx{memory}M")
    command.append(f"-Xmn{max(256, memory // 4)}M")
    if not has_natives:
        command.append(f"-Djava.library.path={natives_dir}")
    if not has_brand:
        command.append(f"-Dminecraft.launcher.brand={APP_DISPLAY_NAME.replace(' ', '')}")
        command.append(f"-Dminecraft.launcher.version={APP_VERSION}")
    command.append("-Dfile.encoding=UTF-8")
    command.extend(jvm_args)
    custom_jvm = _norm_args(split_user_args(options.jvm_args))
    command.extend(custom_jvm)
    if not has_cp:
        command.extend(["-cp", classpath])
    command.append(detail.main_class)
    command.extend(game_args)

    if options.width and options.height and "--width" not in command:
        command.extend(["--width", str(options.width), "--height", str(options.height)])
    if options.fullscreen and "--fullscreen" not in command:
        command.append("--fullscreen")
    command.extend(options.extra_game_args)

    ensure_dir(instance.logs_dir)
    log_file = instance.logs_dir / f"launch-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"

    prepared = PreparedLaunch(
        command=command,
        cwd=instance.game_dir,
        version_id=version_id,
        detail=detail,
        log_file=log_file,
        notes=notes,
        classpath=classpath,
        java_path=java_path,
        memory_mb=memory,
    )
    log.info("启动命令已生成（%d 个参数），日志：%s", len(command), log_file)
    log.debug("命令预览：%s", prepared.command_preview(1200))
    return prepared


class GameProcess:
    """游戏进程包装：日志重定向 + 退出回调 + 强制结束。"""

    def __init__(
        self,
        prepared: PreparedLaunch,
        *,
        on_line: Callable[[str], None] | None = None,
        on_exit: Callable[[int, float], None] | None = None,
    ) -> None:
        self.prepared = prepared
        self.on_line = on_line
        self.on_exit = on_exit
        self.process: subprocess.Popen | None = None
        self.started_at = 0.0
        self._lock = threading.RLock()
        self._lines: list[str] = []
        self.exit_code: int | None = None
        self._reader: threading.Thread | None = None
        self._log_handle = None

    # ------------------------------------------------------------------ #
    def start(self) -> "GameProcess":
        prepared = self.prepared
        ensure_dir(prepared.cwd)
        try:
            self._log_handle = open(prepared.log_file, "w", encoding="utf-8", errors="replace")
        except OSError as exc:
            raise LaunchError(f"无法创建日志文件：{exc}", hint=str(prepared.log_file)) from exc
        self._log_handle.write(
            f"# Lumen Launcher {APP_VERSION}\n# {datetime.now().isoformat(timespec='seconds')}\n"
            f"# 版本：{prepared.version_id}  内存：{prepared.memory_mb}MB\n"
            f"# 命令：{prepared.command_preview(4000)}\n\n"
        )
        self._log_handle.flush()

        creation_flags = 0
        if sys.platform == "win32":
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        try:
            self.process = subprocess.Popen(
                prepared.command,
                cwd=str(prepared.cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=creation_flags,
                startupinfo=startupinfo,
            )
        except FileNotFoundError as exc:
            self._close_log()
            raise LaunchError(
                f"无法执行 Java：{prepared.java_path}",
                hint="Java 路径可能已失效，请在设置中重新选择",
            ) from exc
        except OSError as exc:
            self._close_log()
            raise LaunchError(f"启动失败：{exc}", hint=exc.__class__.__name__) from exc

        self.started_at = time.monotonic()
        self._reader = threading.Thread(target=self._pump, name="lumen-game-log", daemon=True)
        self._reader.start()
        log.info("游戏进程已启动，PID=%s", self.process.pid)
        return self

    def _pump(self) -> None:
        assert self.process is not None
        stream = self.process.stdout
        if stream is not None:
            for line in stream:
                text = line.rstrip("\r\n")
                self._remember(text)
                if self.on_line is not None:
                    try:
                        self.on_line(text)
                    except Exception:  # pragma: no cover
                        pass
        try:
            code = self.process.wait()
        except Exception:  # pragma: no cover
            code = -1
        elapsed = time.monotonic() - self.started_at
        self.exit_code = code
        self._remember(f"# 进程退出，退出码 {code}，运行 {elapsed:.1f}s")
        self._close_log()
        if self.on_exit is not None:
            try:
                self.on_exit(code, elapsed)
            except Exception:  # pragma: no cover
                pass
        log.info("游戏进程结束：退出码 %s，运行 %.1fs", code, elapsed)

    def _remember(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)
            if len(self._lines) > 5000:
                self._lines = self._lines[-4000:]
        handle = self._log_handle
        if handle is not None:
            try:
                handle.write(line + "\n")
                handle.flush()
            except (OSError, ValueError):
                pass

    def _close_log(self) -> None:
        handle, self._log_handle = self._log_handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def pid(self) -> int | None:
        return self.process.pid if self.process else None

    def recent_lines(self, limit: int = 400) -> list[str]:
        with self._lock:
            return list(self._lines[-limit:])

    def wait(self, timeout: float | None = None) -> int:
        if self.process is None:
            return -1
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return -1

    def kill(self) -> None:
        if self.process is None:
            return
        try:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        except OSError:
            pass


def start_game(
    paths: AppPaths,
    instance: Instance,
    account: Account,
    options: LaunchOptions,
    *,
    version_id: str | None = None,
    on_line: Callable[[str], None] | None = None,
    on_exit: Callable[[int, float], None] | None = None,
) -> GameProcess:
    prepared = prepare_launch(paths, instance, account, options, version_id=version_id)
    instance.touch_played()
    try:
        instance.save()
    except Exception:  # pragma: no cover
        pass
    process = GameProcess(prepared, on_line=on_line, on_exit=on_exit)
    return process.start()
