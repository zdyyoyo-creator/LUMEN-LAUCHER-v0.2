# Lumen Launcher（流明启动器）

一款从零实现的 **Minecraft Java 版第三方启动器**：版本管理、镜像加速下载、整合包导入、
Java 运行时自动获取、Forge/NeoForge 安装向导、多实例隔离、浅色/深色双主题。
跨平台（Windows 优先，macOS / Linux 可运行）。

> 技术路线：Python 3.10+ / PySide6（Qt 6）。不依赖任何 Minecraft 相关 SDK，
> 全部按官方协议与社区镜像规范自行实现。

![版本管理（深色）](docs/screenshot-dark-versions.png)

| 实例（深色） | 设置（浅色） |
| --- | --- |
| ![实例](docs/screenshot-dark-instances.png) | ![设置](docs/screenshot-light-settings.png) |

| Forge/NeoForge 安装向导 | 整合包 |
| --- | --- |
| ![Forge 向导](docs/screenshot-forge-wizard.png) | ![整合包](docs/screenshot-dark-modpacks.png) |

| 资源下载（Modrinth / CurseForge） | CurseForge 需要 Key 的引导 |
| --- | --- |
| ![资源](docs/screenshot-browse.png) | ![CurseForge](docs/screenshot-browse-curseforge.png) |

---

## 0. 我该打开哪个文件？（入口与分发）

整个项目**只需一个入口文件**：

```bash
python main.py          # ← 启动启动器（唯一入口）
```

其余 `.py` 都是它 import 的模块，不需要单独运行。按你想要的用法选一种：

| 方式 | 怎么做 | 适合 |
| --- | --- | --- |
| **源码运行**（推荐开发/改代码） | `pip install -r requirements.txt` 后 `python main.py` | 日常使用、二次开发 |
| **单文件包**（整合成一个文件） | `python tools/make_zipapp.py` → 得到 `dist/LumenLauncher.pyz`，运行 `python dist/LumenLauncher.pyz` | 放到任意文件夹、发给别人；仍需目标机装 Python + 依赖 |
| **独立 exe**（免 Python） | `pip install pyinstaller` → `python tools/build.py` → `dist/LumenLauncher/` | 分发给没有 Python 的朋友 |
| **上传 GitHub** | `python tools/make_repo_zip.py` → `dist/LumenLauncher-src.zip`，解压后整个目录 `git init` + push | 建仓库、开 PR |

**代码导览**（想改哪块就看哪块）：

| 你想改 | 打开文件 |
| --- | --- |
| 程序入口 / 命令行参数 | `main.py` |
| 主窗口、侧栏、底部启动条 | `lumen/ui/main_window.py` |
| 四个页面（版本/整合包/实例/设置） | `lumen/ui/pages/*.py` |
| 安装向导、安装进度、日志、mods 管理 | `lumen/ui/dialogs/*.py` |
| 配色与 QSS 主题 | `lumen/ui/theme.py` |
| 下载器（并发/续传/校验/重试） | `lumen/core/downloader.py` |
| 镜像表与测速选源 | `lumen/core/mirrors.py` |
| 版本清单与 version.json 解析 | `lumen/core/manifest.py` |
| 原版安装、Forge/Fabric 安装编排 | `lumen/core/installer.py` |
| Forge/NeoForge 安装器调用 | `lumen/core/loaders.py` |
| 启动参数组装、进程与日志 | `lumen/core/launcher.py` |
| 路径/设置/实例元数据 | `lumen/config.py`、`lumen/core/instances.py` |
| 整合包解析（mrpack / CurseForge） | `lumen/modpack/` |

---

## 1. 环境要求

| 项目 | 要求 |
| --- | --- |
| Python | 3.10 及以上（开发环境验证于 3.13） |
| 操作系统 | Windows 10/11（优先）、macOS 12+、Linux（X11/Wayland） |
| 依赖 | `PySide6-Essentials >= 6.6`、`requests >= 2.31` |
| 磁盘 | 原版 1.21.x 约 600MB；Java 运行时约 50MB；整合包视规模而定 |
| 网络 | 可访问 Mojang 官方或 BMCLAPI 镜像（内网/代理环境可在设置中自定义镜像根地址） |

## 2. 安装与运行

```bash
# 1) 安装依赖（推荐虚拟环境）
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

# 2) 启动 GUI
python main.py

# 可选参数
python main.py --data-dir "D:\LumenLauncher\data"   # 指定数据目录（便携/多开）
python main.py --debug                              # 输出调试日志
python main.py --theme light                        # 临时覆盖主题
```

**数据放在哪？** 启动器不写死任何绝对路径，按以下优先级解析数据目录：

1. `--data-dir` 参数（最高优先级）；
2. 环境变量 `LUMEN_DATA_DIR`；
3. **便携模式**：程序目录下 `data/` 可写时直接使用（把整个目录放到 D 盘，数据就落在 D 盘）；
4. 兜底：用户目录（Windows `%LOCALAPPDATA%\LumenLauncher`，macOS `~/Library/Application Support`，Linux `~/.local/share/lumenlauncher`）。

运行时生成的目录结构见第 5 节。

## 3. 功能一览

### 3.1 版本管理
- 从 `https://piston-meta.mojang.com/mc/game/version_manifest_v2.json` 拉取**全部版本**（正式版 / 快照 / Old Alpha / Old Beta），本地缓存 10 分钟，可手动「刷新」。
- 完整解析 `version.json`：`mainClass`、`arguments`（含 rules）、`libraries`（含 natives/classifiers）、`assetIndex`、`downloads.client`、`javaVersion`、`inheritsFrom`。
- 列表默认只显示正式版，可勾选「快照 / 远古版本」；支持搜索；与本地已安装版本合并显示（`已安装` 徽章 + 一键启动）。
- 版本类型标签：正式版（绿）/ 快照（黄）/ 远古（灰）/ 最新（蓝）。
- **新建实例时就能选加载器**：「实例」页 → 新建实例，加载器可选
  **原版 / Fabric / Quilt / Forge / NeoForge**，选择结果会带进后续安装流程
  （选 Forge/NeoForge 会直接打开安装向导）。
- 安装对话框同样支持这五种目标：
  - Fabric、Quilt 通过 meta API 直接生成本地版本文件，无需运行安装器；
  - **Forge / NeoForge 走独立安装向导**（`lumen/ui/dialogs/forge_wizard.py`）：
    - ① 确认参数：目标实例、加载器版本（自动拉取并标注推荐）、运行安装器用的 Java，
      并做 Java / 磁盘空间 / 版本一致性三项自检；
    - ② 准备原版：客户端、依赖库、资源、natives，已校验的文件自动跳过；
    - ③ 安装加载器：下载官方安装器 → 运行 → **实时滚动安装器输出**，
      并解析其百分比驱动进度环；解析不到时按耗时渐近推进，避免进度条假死；
      显示运行计时，可随时取消（会结束子进程）；
    - ④ 完成：结果摘要 + 打开实例目录 / 直接启动游戏，失败可一键重试。
  - Forge/NeoForge 生成的版本是 `inheritsFrom` 结构，启动器会沿继承链合并
    vanilla 的 libraries/arguments 后再启动与校验（见 `lumen/core/version_store.py`）；
    合并时按 `group:artifact` 去重、**子版本优先**——Forge 1.20.5+ 使用 Java 模块化启动，
    若 guava 32.1.2(Forge) 与 33.3.1(vanilla) 同时进 classpath，JVM 会直接
    `ResolutionException: ... export package ...`（已实测修复）。

### 3.2 镜像加速下载
- 内置两个源：**Mojang 官方** 与 **BMCLAPI 镜像**（`https://bmclapi2.bangbang93.com`）。
- 官方 → 镜像地址改写表（见 `lumen/core/mirrors.py`）：

  | 官方主机 | 镜像地址 |
  | --- | --- |
  | `launchermeta / piston-meta / piston-data.mojang.com` | `bmclapi2.bangbang93.com/<原路径>` |
  | `libraries.minecraft.net` | `bmclapi2.bangbang93.com/maven/` |
  | `resources.download.minecraft.net` | `bmclapi2.bangbang93.com/assets/` |
  | `maven.minecraftforge.net`、`maven.neoforged.net` | `bmclapi2.bangbang93.com/maven/` |
  | `meta.fabricmc.net` | `bmclapi2.bangbang93.com/fabric-meta/` |
  | `maven.fabricmc.net` | `bmclapi2.bangbang93.com/maven/` |
  | `repo1.maven.org/maven2` | 腾讯云 Maven 镜像 `mirrors.cloud.tencent.com/nexus/repository/maven-public` |

- **自动选源**：对官方源与镜像源并发做「Range 小样本」测速（首个字节延迟 + 吞吐），
  综合得分择优；失败自动回退到另一个源。支持手动锁定 `自动 / 官方 / 镜像`。
- 下载器特性：多线程并发（可配 1–32）、**断点续传**（`.part` + HTTP Range）、
  **SHA1 校验**（对照 version.json）、**指数退避重试**、已存在且校验通过直接跳过、
  进度显示（总进度 / 速度 / 剩余文件数）、**暂停 / 取消**。

### 3.3 资源下载（Modrinth / CurseForge）
左导航「资源」页：搜索并下载**模组**与**整合包**。
- **Modrinth**（公开 API，无需 Key）：搜索、按加载器 / Minecraft 版本筛选、版本列表、下载；
- **CurseForge**（需要 API Key）：同样支持搜索与下载，没填 Key 时页面会给出可点击的引导；
- 目标实例决定筛选条件：默认按实例的加载器 + Minecraft 版本过滤，下载的模组直接落进该实例 `mods/`，
  并做 SHA1 校验；整合包下载到缓存后自动弹出导入对话框（复用同一导入链路）；
- 「选择版本」可以手动挑具体文件版本，默认自动选择「稳定版 + 匹配加载器 + 匹配 MC 版本」的那一个；
- 入口不止一个：实例卡片的「mods」→「从 Modrinth / CurseForge 下载」、
  整合包页「在线浏览」都会跳到资源页并预设条件。

### 3.4 整合包导入
- **Modrinth `.mrpack`**：解析 `modrinth.index.json`（formatVersion、game、dependencies、files、hashes），
  下载到实例目录，解压 `overrides/` 与 `client-overrides/`，按 `env.client` 过滤仅服务端文件。
- **CurseForge**：解析 `manifest.json`（minecraft.version、modLoaders、files[projectID/fileID]），
  通过官方 API `api.curseforge.com/v1/mods/{projectID}/files/{fileID}` 换下载直链；
  **没有 API Key 时优雅降级**：照常解压 overrides、生成 `MANUAL_DOWNLOADS.txt` 清单，提示手动补齐。
- **通用 zip**：无元数据时按「解压到实例目录」处理，由用户指定 Minecraft 版本。
- 导入时自动识别所需 Minecraft 版本与加载器并安装；可接受拖拽（把文件拖到页面虚线区）。
- 也可以不下载整合包文件，直接用「资源」页在线下载（见 3.3）。

### 3.4 Java 运行时
- 自动扫描：`PATH`、`JAVA_HOME/JDK_HOME/GRAALVM_HOME`、Windows 注册表
  （JavaSoft / Eclipse Adoptium / Microsoft / Zulu）、常见安装目录（`Program Files` 下的
  Java、Adoptium、Microsoft、Corretto、Zulu、`~/.jdks`、Mojang 官方启动器 runtime 等）、
  启动器托管的 `runtimes/`。
- 执行 `java -version` 解析主版本号（兼容 `1.8.0_402` 与 `21.0.1` 两种风格），失败时回退读 `release` 文件。
- 按版本匹配：≤1.16.5 → Java 8；1.17–1.20.4 → Java 17；1.20.5+ → Java 21
  （优先使用 `version.json` 里的 `javaVersion.majorVersion`）。
- 缺 Java 时自动从 **Mojang Java 运行时清单**（`all.json` → component manifest → 逐文件下载）安装到
  `runtimes/<component>/`；清单不可用时回退 **Adoptium Temurin** 的 JRE zip。
- 支持手动指定 `java.exe`，可保存为实例级或全局默认。

### 3.5 启动与进程管理
- **离线账号**：`uuid = md5("OfflinePlayer:" + name)`（RFC4122 v3），`userType=legacy`；
  微软正版登录的设备码流程接口已预留（`lumen/auth/microsoft.py`，UI 有占位说明）。
- 参数组装：`-Xmx/-Xmn`、`-Djava.library.path`（natives）、
  `-Dminecraft.launcher.brand/version`、`-Dfile.encoding=UTF-8`、完整 classpath
  （Windows 用 `;`，其它平台 `:`）、`mainClass`、`--username/--version/--gameDir/--assetsDir/--assetIndex/--uuid/--accessToken/--userType`。
- 兼容两种参数体系：1.13+ 的 `arguments.jvm/game`（含 rules 判定）与 1.12- 的 `minecraftArguments`。
- **启动前自检**：Java 是否匹配、文件是否完整、路径是否含中文/空格、内存是否超限、
  资源索引是否存在 —— 全部翻译成人话提示，可「仍然启动」。
- 启动后 stdout/stderr 重定向到 `logs/launch-<时间>.log`，界面提供**实时日志面板**与
  **崩溃分析**（Java 版本错、内存不足、natives 缺失、mod 重复/冲突、显卡驱动、Mixin 失败等 16 类特征）。

### 3.6 多实例与 mods 管理
- 每个实例独立 `minecraft/` gameDir（mods / config / saves / resourcepacks / shaderpacks 全隔离），
  独立 `versions/`；只共享只读的 `shared/libraries` 与 `shared/assets` 以节省磁盘。
- 卡片列表展示版本号、加载器类型与版本、最后游玩时间、mod/存档数量。
- 支持新建、复制、重命名、删除（删除需输入实例名二次确认）、打开目录。
- **给已有实例加模组**：实例卡片 →「mods」→
  - 「从文件添加…」：多选本地 `.jar` 复制进 `mods/`（同名自动跳过）；
  - 「从 Modrinth / CurseForge 下载」：跳到资源页，自动带上该实例的加载器与 MC 版本做筛选；
  - 列表里可以勾选启用/禁用（`.jar` ↔ `.jar.disabled`）、删除、全部启停。

### 3.7 界面
- 侧栏 92px + 主区 + 底部 76px 启动条；卡片式扁平布局，圆角统一 8px。
- 浅色 / 深色双主题一键切换（侧栏底部月亮/太阳按钮，或设置页）。
- 语义色：成功 `#3BA55D` / 警告 `#D9A02B` / 危险 `#DF5A5F`，主色 `#5B7CFA`（深）/ `#4C6FEA`（浅）。
- 所有耗时操作走后台线程（`QThread` + 信号），UI 只接收进度快照；下载/安装/导入可暂停取消。
- 空状态、骨架屏、环形进度、Toast 提示均已实现，无花哨装饰动画。

## 4. 自检与测试

所有脚本都只用标准库 + requests + PySide6，可在 CI 里直接跑：

```bash
# 离线自检（不联网，12 项：版本解析/镜像表/rules/启动参数/崩溃分析/mrpack/实例/设置/下载器续传）
python tests/selftest.py

# 含联网自检（15 项：测速、版本清单、真实下载 SHA1、Mojang 运行时清单）
python tests/selftest.py --online

# 端到端：真实安装一个版本并启动（需要网络与 Java；--no-assets 可跳过资源加速验证）
python tools/e2e_check.py --data-dir "D:\LumenLauncher\data" --version 1.21.4 --launch

# 整合包导入链路：现场构造 .mrpack → 解析 → 导入 → 校验 overrides/mods
python tools/modpack_check.py --data-dir "D:\LumenLauncher\data"

# 加载器链路：Fabric/Quilt 的 profile 合并、库下载、natives 解压、启动自检
python tools/fabric_check.py --data-dir "D:\LumenLauncher\data" --mc 1.21.4

# Forge/NeoForge 链路：真实下载并运行官方安装器，校验 inheritsFrom 合并结果（约 2–5 分钟）
python tools/forge_check.py --data-dir "D:\LumenLauncher\data" --mc 1.21.4 --kind forge

# 资源目录链路：Modrinth 搜索 → 选版本 → 真实下载并校验 SHA1（含整合包搜索）
python tools/catalog_check.py --data-dir "D:\LumenLauncher\data" --query sodium

# UI 冒烟：离屏构造全部对话框并截图（不需要显示器）
python tools/ui_smoke.py

# 界面截图：Forge 向导四步 / 资源页
python tools/wizard_preview.py
python tools/browse_preview.py

# 打包：单文件 .pyz / 可上传 GitHub 的源码 zip / 独立 exe
python tools/make_zipapp.py
python tools/make_repo_zip.py
python tools/build.py          # 需要 pyinstaller
```

GUI 冒烟测试（离屏渲染并截图，便于在无显示器环境验证界面）：

```bash
set QT_QPA_PLATFORM=offscreen
python main.py --smoke --screenshot shot.png --page versions
```

> 离屏模式下如果中文显示为方块，设置 `QT_QPA_FONTDIR=C:\Windows\Fonts`
> （启动器内部也会尝试加载系统微软雅黑，正常情况下无需手动设置）。

## 5. 数据目录结构

```
<data>/
├── config.json                 启动器设置（下载源/主题/内存/账号…）
├── instances/
│   └── <实例>/                 一个实例 = 一套独立游戏目录
│       ├── instance.json       实例元数据（版本、加载器、内存、上次游玩…）
│       ├── minecraft/          gameDir：mods、config、saves、resourcepacks、shaderpacks、logs
│       ├── versions/<版本>/    <版本>.json、<版本>.jar、natives/
│       └── logs/               每次启动的游戏日志
├── shared/
│   ├── libraries/              所有实例共享的依赖库（按 Maven 路径）
│   └── assets/{indexes,objects,virtual}
├── runtimes/<component>/       启动器托管的 Java 运行时
├── cache/                      版本清单/version.json/测速结果缓存
└── logs/launcher.log           启动器自身日志（2MB×3 滚动）
```

## 6. 打包

```bash
pip install pyinstaller
python tools/make_icon.py                     # 生成 assets/icon.ico
pyinstaller --noconfirm --windowed --name LumenLauncher ^
  --icon assets\icon.ico ^
  --add-data "lumen;lumen" ^
  main.py
```

产物在 `dist/LumenLauncher/`。首次运行会把 `data/` 建在 exe 同级目录（便携），
无需管理员权限；也可用 `--data-dir` 指定别处。

## 7. 常见问题

**下载很慢 / 卡住？**
设置页点「测速」看两源延迟，或手动锁定更快的源；也可以在「镜像根地址」填自建 BMCLAPI 兼容服务。
下载支持断点续传，取消后重新安装会从 `.part` 继续。

**提示「缺少可用的 Java」？**
到「设置 → Java」点「管理 Java → 自动获取 Java 21」，或手动指定已安装的 `java.exe`。
1.16.5 及更早的版本需要 Java 8。

**启动后立刻退出（退出码 1）？**
点底栏「日志」→「崩溃分析」，常见原因（Java 版本错 / 内存不足 / natives 缺失 / mod 冲突）会被直接翻译成建议。

**整合包导入后 mods 是空的？**
如果是 CurseForge 包且没有 API Key，文件不会自动下载：到设置页填入 CurseForge API Key 后重新导入，
或按实例目录里的 `MANUAL_DOWNLOADS.txt` 手动补齐。

**「资源」页搜不到 CurseForge 内容？**
CurseForge 的搜索/文件接口要求 API Key（403）。到「设置 → 整合包」填入 Key 即可；
不填也能用 Modrinth（无需 Key），两者数据互不影响。

**为什么下载的模组没生效？**
模组只对「带对应加载器」的实例生效。如果是原版实例，先给实例装 Fabric/Forge/NeoForge；
资源页的提示行会明确告诉你目标实例当前的加载器，安装时会自动按加载器 + MC 版本筛选文件。

**为什么共享 libraries/assets，而不是每个实例一份？**
体积优先：资源文件动辄几百 MB，重复存放浪费磁盘。游戏隔离性由 `minecraft/` gameDir 与 `mods/saves` 保证，
共享目录只读。

**首次安装会下载多少？**
1.21.x 约 600MB（客户端 28MB + 依赖库 50MB + 资源 500MB + 索引）。测试环境实测 12 并发可达 2–4 MiB/s，
因此首次通常 3–8 分钟；资源文件下载完成后再次安装会全部命中校验直接跳过。

## 8. 已知限制

- **Forge / NeoForge**：依赖官方 installer 的 headless 模式，首次安装需要本机 Java 且耗时较长；极旧版本可能弹 GUI。
- **微软正版登录**：设备码请求已实现，Xbox Live / Minecraft 换票链路留作扩展（见 `lumen/auth/microsoft.py`）。
- **CurseForge**：无 API Key 时只能解压 overrides 并生成手动下载清单。
- **legacy 资源**（1.6 及更早）：用实体副本兼容，未实现符号链接方案。

## 9. 免责声明

本项目为学习/自用性质的第三方启动器，与 Mojang Studios / Microsoft 无关。
使用它下载的游戏与资源仍受 Minecraft 最终用户许可协议约束；请支持正版。
