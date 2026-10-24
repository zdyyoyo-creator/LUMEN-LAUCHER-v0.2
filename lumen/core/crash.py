"""崩溃分析：把 Minecraft 日志翻译成「人话」原因与可执行建议。

不做玄学猜测：每条结论都对应日志里的具体特征串，并标注严重级别。
输入既可以是内存里的日志行（实时面板），也可以是实例 logs 目录下的文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..utils.log import get as get_logger

log = get_logger("core.crash")


@dataclass
class CrashFinding:
    title: str
    detail: str
    suggestion: str
    severity: str = "error"  # error | warning | info
    matched: str = ""


@dataclass(frozen=True)
class _Rule:
    pattern: str
    title: str
    detail: str
    suggestion: str
    severity: str = "error"


#: 特征串 → 结论。顺序即优先级（靠前的更具体）。
RULES: tuple[_Rule, ...] = (
    _Rule(
        r"UnsupportedClassVersionError[\s\S]{0,300}?class file version (\d+)",
        "Java 版本过低",
        "游戏或模组编译用的 class 版本（{v}）高于当前 Java 支持的版本。",
        "换用更高版本的 Java（1.17–1.20.4 需 Java 17，1.20.5+ 需 Java 21），可在「设置 → Java」里指定。",
    ),
    _Rule(
        r"(OutOfMemoryError|Java heap space)",
        "内存不足",
        "游戏申请的堆内存超过了分配给它的上限。",
        "在实例设置里调大内存（建议 4–8GB，且不超过物理内存的一半），或减少同时加载的模组。",
    ),
    _Rule(
        r"(Could not reserve enough space for .*object heap|Invalid maximum heap size|Error occurred during initialization of VM)",
        "JVM 内存参数无法满足",
        "分配的内存量超出系统可提供范围，或使用了 32 位 Java。",
        "把内存调小一些，并确认使用的是 64 位 Java。",
    ),
    _Rule(
        r"(UnsatisfiedLinkError|Found no lwjgl|Failed to load a library|no .* in java\.library\.path|Failed to locate library)",
        "natives 本地库缺失",
        "缺少平台本地库（LWJGL/natives），通常是没有解压或 classpath 不完整。",
        "重新安装该版本（会自动解压 natives），或检查杀毒软件是否误删了 versions/*/natives 下的 dll。",
    ),
    _Rule(
        r"(EXCEPTION_ACCESS_VIOLATION|hs_err_pid|A fatal error has been detected by the Java Runtime)",
        "JVM 级崩溃（驱动/内存）",
        "Java 虚拟机自身崩溃，多与显卡驱动、内存条或模组注入有关。",
        "更新显卡驱动；关闭 OptiFine 与光影组合测试；确认没有超频。",
    ),
    _Rule(
        r"(GLFW error|Failed to create window|Pixel format not accelerated|WGL: The driver does not appear to support OpenGL)",
        "显卡驱动 / OpenGL 不可用",
        "创建窗口失败，通常是显卡驱动过旧或远程桌面环境下没有硬件 OpenGL。",
        "更新显卡驱动；远程桌面下请在本机运行；尝试降低游戏画质或关闭光影。",
    ),
    _Rule(
        r"(DuplicateModsFoundException|Duplicate mods|Found multiple (?:mods|versions) (?:with|for) the same)",
        "模组重复",
        "mods 目录里存在同一个模组的多个版本。",
        "在「实例 → mods」里删除旧版本，只保留一个。",
    ),
    _Rule(
        r"(ModResolutionException|Missing or unsupported mandatory dependencies|Incompatible mod set|requires .* which is not installed)",
        "模组依赖缺失或冲突",
        "加载器无法满足模组之间的依赖关系。",
        "根据日志里列出的缺失模组补齐依赖，或删除冲突的模组。",
    ),
    _Rule(
        r"(Mixin (?:apply|transformer) failed|MixinTransformerError|InvalidInjectionException)",
        "Mixin 注入失败（模组不兼容）",
        "模组改写的目标代码与当前游戏版本不匹配。",
        "确认所有模组都是同一游戏版本；逐个禁用模组定位来源。",
    ),
    _Rule(
        r"(NoSuchMethodError|NoSuchFieldError|AbstractMethodError|IncompatibleClassChangeError)",
        "模组与游戏版本不兼容",
        "模组调用了不存在的类成员，通常是模组版本与 Minecraft 版本不匹配。",
        "更新到对应游戏版本的模组文件，或删除该模组。",
    ),
    _Rule(
        r"(ClassNotFoundException|NoClassDefFoundError)",
        "依赖类缺失",
        "运行需要的类不在 classpath 里。",
        "重新安装版本以补齐依赖库；若刚导入整合包，可能是下载不完整。",
    ),
    _Rule(
        r"(Could not find or load main class|Error: Could not find or load main class)",
        "主类加载失败",
        "启动参数里的主类不存在，通常版本文件损坏或加载器安装不完整。",
        "在版本管理里点「修复/重新安装」重建该版本。",
    ),
    _Rule(
        r"(Invalid session|Failed to verify username|AuthenticationException|Invalid credentials)",
        "账号验证失败",
        "服务器拒绝了当前账号的会话。",
        "离线账号无法进入正版服务器；请确认服务器是离线模式或改用正版账号。",
    ),
    _Rule(
        r"(StackOverflowError)",
        "栈溢出",
        "某个模组出现无限递归。",
        "调大 -Xss（如 -Xss4M），或移除可疑模组。",
    ),
    _Rule(
        r"(The game crashed whilst|A detailed walkthrough of the error)",
        "游戏崩溃",
        "游戏在初始化或运行中崩溃，日志里通常有更具体的 Caused by。",
        "查看 crash-reports 目录里的完整报告，或用「崩溃分析」逐条排查。",
        "warning",
    ),
)


def analyze_lines(
    lines: list[str],
    *,
    exit_code: int | None = None,
    java_major: int | None = None,
    required_java: int | None = None,
    memory_mb: int = 0,
) -> list[CrashFinding]:
    text = "\n".join(lines[-4000:])
    findings: list[CrashFinding] = []
    seen: set[str] = set()

    for rule in RULES:
        try:
            match = re.search(rule.pattern, text, re.IGNORECASE)
        except re.error:  # pragma: no cover - 规则写错时不拖垮 UI
            continue
        if not match or rule.title in seen:
            continue
        seen.add(rule.title)
        detail = rule.detail
        suggestion = rule.suggestion
        # 把捕获到的 class 版本换算成 Java 版本，让提示更具体
        if "class file version" in rule.pattern and match.groups():
            try:
                class_version = int(match.group(1))
                needed = {52: 8, 53: 9, 54: 10, 55: 11, 56: 12, 57: 13, 58: 14, 59: 15, 60: 16, 61: 17, 62: 18, 63: 19, 64: 20, 65: 21, 66: 22}.get(class_version)
                detail = rule.detail.format(v=class_version)
                if needed:
                    detail += f" 该 class 需要 Java {needed}+。"
                    if required_java and needed > required_java:
                        suggestion = f"该版本推荐 Java {required_java}，但模组需要 Java {needed}，请改用 Java {needed}。"
            except (ValueError, IndexError):
                pass
        findings.append(
            CrashFinding(
                title=rule.title,
                detail=detail,
                suggestion=suggestion,
                severity=rule.severity,
                matched=match.group(0)[:200],
            )
        )

    # 结构性诊断：Java 版本与需求不符（即使日志里没有明显异常也提示）
    if java_major and required_java and java_major < required_java:
        findings.insert(
            0,
            CrashFinding(
                title="Java 版本偏低",
                detail=f"当前使用 Java {java_major}，而该版本建议 Java {required_java}。",
                suggestion="在「设置 → Java」里换成推荐版本，或让启动器自动下载。",
                severity="warning",
            ),
        )
    if memory_mb and memory_mb < 1024:
        findings.append(
            CrashFinding(
                title="分配内存过小",
                detail=f"当前只分配了 {memory_mb} MB 堆内存。",
                suggestion="建议至少 2048 MB，大型整合包推荐 4096–8192 MB。",
                severity="warning",
            )
        )
    if exit_code is not None and exit_code != 0 and not findings:
        findings.append(
            CrashFinding(
                title=f"未识别的崩溃（退出码 {exit_code}）",
                detail="日志里没有匹配到常见错误特征。",
                suggestion="打开实例 logs 目录查看完整日志，或到 crash-reports 里找最近的报告。",
                severity="warning",
            ),
        )
    return findings[:8]


def analyze_log_file(path: Path, **context) -> list[CrashFinding]:
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        log.debug("读取日志失败：%s", exc)
        return []
    return analyze_lines(lines, **context)


def latest_crash_report(instance) -> Path | None:
    folder = instance.game_dir / "crash-reports"
    if not folder.is_dir():
        return None
    reports = sorted(folder.glob("crash-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def summarize(findings: list[CrashFinding]) -> str:
    if not findings:
        return "没有检测到明显的崩溃特征。"
    return "；".join(finding.title for finding in findings)
