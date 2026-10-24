# 架构说明

## 1. 分层

```
┌──────────────────────────────────────────────────────────────┐
│  ui/         PySide6：主题、组件、页面、对话框、后台任务包装     │
│              （只读 ctx，不发网络请求；耗时操作一律丢给 Worker）  │
├──────────────────────────────────────────────────────────────┤
│  core/       清单解析 · 镜像选源 · 下载器 · 安装 · 启动 · 崩溃   │
│  java/       Java 扫描 / 运行时下载                              │
│  modpack/    整合包解析与导入编排                                 │
│  auth/       离线账号 / 微软设备码骨架                            │
├──────────────────────────────────────────────────────────────┤
│  utils/      日志 · HTTP · 文件/Zip · 版本号 · 内存探测           │
│  config.py   路径解析 + Settings(JSON)                          │
└──────────────────────────────────────────────────────────────┘
```

依赖方向严格单向：`ui → core/java/modpack/auth → utils/config`。
`core` 内部不 import `ui`，因此核心能力可以脱离 GUI 单独测试（`tests/selftest.py`、`tools/e2e_check.py`）。

## 2. 模块职责

| 模块 | 关键文件 | 职责 |
| --- | --- | --- |
| 配置与路径 | `lumen/config.py` | 按「参数 > 环境变量 > 便携目录 > 用户目录」解析数据根；`Settings` 线程安全读写 JSON，损坏时自动备份重建 |
| 通用工具 | `utils/{log,net,fsutil,versions,sysinfo}.py` | 滚动日志 + 内存环形缓冲；按线程复用的 requests Session；防 Zip Slip 的安全解压；MC/Java 版本比较与 Java 需求推断；物理内存探测 |
| 镜像选源 | `core/mirrors.py` | 官方→镜像改写表（最长前缀优先）；对两源做 Range 采样测速（TTFB+吞吐）；按策略给出候选 URL 列表（含失败回退） |
| 清单解析 | `core/manifest.py` | version_manifest_v2 拉取与缓存；version.json 的 rules（os/arch/features）、libraries（含旧式 natives/classifiers）、arguments、assetIndex、inheritsFrom 合并 |
| 下载器 | `core/downloader.py` | 线程池文件级并发；每个文件：跳过校验 → 候选源轮询 → `.part` 续传 → 大小/SHA1 校验 → 改名；指数退避；250ms 聚合进度心跳；暂停/取消 Event |
| 安装编排 | `core/installer.py` | 阶段化安装（准备/客户端/依赖库/资源索引/资源/natives/收尾），阶段权重合成总进度；legacy 资源实体化；加载器安装入口；完整性校验 `verify_version` |
| 加载器 | `core/loaders.py` | Fabric/Quilt 走 meta API 生成 profile；Forge/NeoForge 下载 installer 并 `--installClient` 安装；逐行回调安装器输出并解析进度 |
| 版本解析 | `core/version_store.py` | 读取版本 json 并沿 `inheritsFrom` 链合并（Forge/NeoForge 必需），带循环与深度保护 |
| 实例 | `core/instances.py` | 实例元数据与目录布局；CRUD；mods 启停（`.jar` ↔ `.jar.disabled`）；`version_id_override` 记录安装器生成的真实版本目录 |
| 启动 | `core/launcher.py` | 变量替换、JVM/游戏参数拼装、classpath 分隔符、natives/资源目录、自定义参数解析（支持引号）、子进程与日志重定向 |
| 自检 | `core/preflight.py` | 启动前 6 类检查 + Java 解析（实例级 > 全局 > 扫描 > 自动下载） |
| 崩溃分析 | `core/crash.py` | 16 条特征规则 → 「原因 + 可执行建议」 |
| Java | `java/detect.py`, `java/runtime.py` | 多来源扫描 + `java -version` 探测；Mojang all.json → component manifest → 逐文件安装，Adoptium 兜底 |
| 账号 | `auth/account.py`, `auth/microsoft.py` | 离线 UUID（RFC4122 v3）；微软设备码请求/轮询已实现，换票留扩展点 |
| 整合包 | `modpack/{mrpack,curseforge,importer}.py` | 格式识别 → 计划（文件清单/覆盖目录/手动清单）→ 建实例 → 装游戏 → 解压 overrides → 下载文件 |
| 资源目录 | `core/catalog.py` | Modrinth（公开 API）/ CurseForge（需 Key）的搜索、版本列表与下载信息；`pick_best_version` 按加载器/版本/稳定性择优 |
| 界面 | `ui/*` | 主题 QSS（一套 token 两套主题）、自绘线性图标、卡片/环形进度/骨架屏/空状态/Toast/步骤条、五页面（版本/资源/整合包/实例/设置）+ 八个对话框、`Worker`(QThread) 封装 |

## 3. 三条主流程

### 3.1 版本列表刷新

```
UI(刷新) → Worker(QThread) → ctx.ensure_manifest(force)
    → load_manifest(): 读缓存(10min) 或 逐候选源 GET 清单
    → VersionManifest 解析 920+ 条 → 写 cache/meta/*.json
← succeeded 信号 → 页面按「类型 + 搜索」过滤 → 分页渲染 80 条
```

### 3.2 安装一个版本

```
InstallDialog → VersionInstaller.install()
  1. load_manifest + fetch_version_json         （准备，2%）
  2. Downloader.run([client jar])               （客户端，6%）
  3. libraries + natives 下载                   （依赖库，12%）
  4. assetIndex 下载 → objects 展开 → 下载资源   （资源，73%）
  5. natives 解压到 versions/<v>/natives         （4%）
  6. legacy 资源实体化（1.6 及以前）
  7. instance.save()                            （收尾，3%）
每次下载：已存在且 SHA1 通过 → skip；失败换候选源 → 退避重试 → 抛语义化错误
```

### 3.3 Forge / NeoForge 安装向导

```
用户选 Forge/NeoForge → InstallDialog 转交 ForgeWizardDialog（四步）
  ① 确认参数：实例 / 加载器版本（maven-metadata 拉取）/ Java，三项自检
  ② 准备原版：0.02–0.68（复用 VersionInstaller.install 的完整原版流程）
  ③ 安装加载器：0.68–0.78 下载 installer；0.78–0.96 运行 installer
       ├─ 临时补齐 launcher_profiles.json + versions/<mc>/<mc>.json（官方安装器的前置校验）
       ├─ 子进程 stdout 逐行 → Signal → 主线程批量刷新日志面板（120ms 一批）
       ├─ 解析 "xx%" / "done/total" 驱动进度环；解析不到用 1-exp(-t/90) 渐近
       └─ 结束后清理临时文件
  ④ 完成：0.96–1.00 搬 libraries/versions、解压 natives、写实例 version_id_override
       结果页可打开目录 / 启动游戏 / 失败重试
```

关键点：Forge 生成的版本是 `1.21.4-forge-54.1.22`（由安装器命名），
实例用 `version_id_override` 记住它，启动与校验统一走 `version_store.resolve_version`。

### 3.4 资源下载（模组 / 整合包）

```
资源页搜索 → Worker → catalog.search(loader+mc 由目标实例决定)
  → 结果卡片（图标异步加载：后台取字节 → 主线程转 QPixmap）
  → 点「安装」→ Worker 拉版本列表 → pick_best_version（加载器/版本/稳定版）
    ├─ 模组：Downloader → 实例 mods/（SHA1 校验）→ Toast + 刷新实例
    └─ 整合包：下载 .mrpack 到 cache/packs → inspect_modpack → ImportDialog（复用导入链路）
```

「从文件添加模组」是纯本地路径：多选 `.jar` → `shutil.copy2` 进 `mods/`（重名跳过）。

### 3.5 启动游戏

```
点击「启动游戏」
  → Worker(ctx.build_plan)                      启动前自检（可能自动下载 Java）
  → 有问题 → IssueDialog（人话提示，错误阻止 / 警告可继续）
  → finish_prepare(): 变量替换 + classpath + 参数拼装
  → GameSession.start(): Popen + 日志泵线程
    ├─ on_line → Qt 信号 → 日志面板（跨线程排队）
    └─ on_exit → 崩溃分析 → 建议对话框
```

## 4. 并发模型

- **网络/磁盘**：`Downloader` 内部 `ThreadPoolExecutor`（文件级并发），每个线程独立 `requests.Session`。
- **UI**：主线程只做信号处理；`Worker(QThread)` 承载单个长任务，进度回调节流 100ms。
- **取消/暂停**：`threading.Event` 广播；下载 chunk 之间检查，`OperationCancelled` 一路上抛到 Worker，
  由 UI 静默处理（保留 `.part` 以便续传）。
- **进程**：游戏 stdout 由独立线程泵出，逐行写日志文件并发信号；`closeEvent` 会取消所有 Worker 并 `wait`。

## 5. 关键取舍

| 决策 | 原因 |
| --- | --- |
| 不做 `asyncio` | Qt 事件循环与 asyncio 桥接成本高；下载是 I/O 密集但并发度有限（≤32），线程池足够且易于取消/续传 |
| 共享 `libraries/assets`，实例内独立 `versions/mods/saves` | 资源体积大，重复存浪费磁盘；隔离性由 gameDir 保证 |
| 图标用 QPainter 自绘而非 SVG/字体 | 免打包资源、随主题取色、无缺字风险 |
| QSS 而非 QML | 更强的「原生控件 + 精细样式」可控性，代码量小，跨平台一致 |
| version.json 存到实例而非共享 | 不同实例可装不同加载器/版本，互不影响 |
| 下载校验严格 SHA1 | 镜像站偶发错误内容；宁可重试也不能把坏文件当成功 |
| `inheritsFrom` 按 `group:artifact` 去重（子版本优先） | Forge 的 json 会显式锁定库版本（如 guava 32.1.2），若按完整坐标去重会让 vanilla 的 33.3.1 一起进 classpath；Forge 1.20.5+ 的 Java 模块系统会因重复包直接 `ResolutionException` |

## 6. 扩展点

- **微软正版登录**：`auth/microsoft.py` 的 `complete_sign_in()` 补 Xbox Live → XSTS → Minecraft 换票，
  UI 占位在设置页；`Account` 已含 `access_token/user_type/refresh_token` 字段。
- **更多镜像**：`core/mirrors.py` 的 `REWRITE_RULES` 追加前缀即可（改完跑 `tests/selftest.py`）。
- **更多加载器**：`core/loaders.py` 增加 `available_loader_versions()` 分支 + `VersionInstaller` 对应方法。
- **CurseForge 完整下载**：已有 `resolve_files()`，只差在设置里填 API Key。

## 7. 已知限制

- Forge/NeoForge 依赖官方 installer 的 headless 模式：安装器要求目标目录是「已安装过 MC 的
  .minecraft」（`launcher_profiles.json` + `versions/<mc>/<mc>.json`），启动器会临时补齐并在结束后清理。
- 安装器运行时无法「暂停」（子进程不支持），只能取消；取消会结束子进程并保留已下载文件。
- 微软正版登录未完成换票链路，当前仅离线账号可用（UI 已注明）。
- 资源下载是「全量补齐」，不做按需懒加载；首次安装 1.21.x 约 600MB。
- 老版本（1.6-）的 legacy 资源通过实体副本兼容，未实现完整的 virtual 符号链接。
