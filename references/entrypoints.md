# 入口与参数详表

> `SKILL.md` 的路由表决定**走哪条**，本文件给**每条怎么调**。只在需要具体参数时读本文件。
>
> 约定：`$SkillRoot` = 本 skill 根目录；`$Workspace` = 含 `config.toml` 的工作区。所有 Python 入口默认从工作区当前目录读 `config.toml`，也可用 `--config` 显式指定。

## 1. 日常完整导出 `run_daily_export.ps1`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_daily_export.ps1" -Workspace "$Workspace" -NoPause
```

| 参数 | 说明 |
|---|---|
| `-Workspace` | 工作区路径。决定 config、`.runlog/`、`.export-state.json` 的落点 |
| `-NoPause` | 结尾不等回车。**Agent 调用必须带**，否则会挂住 |

前提：WeFlow 正在运行且已启用 API 服务。会归档轮转 live roots——**同一时间只在一个工作区跑**。

**退出码**：`0` = 全部成功。非 `0` 有两种，看控制台措辞区分：

- `Daily export completed with warnings.` → 聊天 diary 已正常产出，只是可选阶段（多为朋友圈）失败被跳过。列出的项可单独补跑，不必重跑整轮。
- `FAILED before export completed` → 导出没走完，产物不完整。

单会话失败只隔离它自己，记进工作区根 `.export-state.json`；连续多个导出日失败会在收尾列出待审查清单。

## 2. 处理已有 raw `process_existing_raw.py`

不启动 WeFlow、不联网，只消费已经在盘上的 raw。

```powershell
python "$SkillRoot\scripts\process_existing_raw.py" --raw-root <raw目录> --day <yyyy-mm-dd> --require-day
```

| 参数 | 说明 |
|---|---|
| `--raw-root` | 已有的 raw 根。省略则用 config `[paths].raw` |
| `--day` | 交给下游 skill 的日期。单一日期后缀的 raw 可自动推断 |
| `--require-day` | 推断不出唯一日期时直接失败，而不是猜。**批量/多日 raw 建议带上** |
| `--skip-voice-fallback` | 跳过 `[daily_export].voice_fallback_script` |
| `--config` | 配置文件路径 |

若当前 processed 非空，会先合并进归档 processed，再重建 diary 与 sidecar processed。**raw 原样保留、不轮转。**

## 3. 按需导出 `export_on_demand.py`

指定会话 + 日期范围，只写你给的输出目录，**不碰 live 的 raw / processed / 归档**。

```powershell
python "$SkillRoot\scripts\export_on_demand.py" --session <wxid或显示名子串> --start 2026-05-01 --end 2026-05-31 --out <输出目录>
```

| 参数 | 说明 |
|---|---|
| `--session` | wxid，或显示名子串 |
| `--list-sessions <关键词>` | 列出匹配候选并退出。**拿不准会话名时先用它** |
| `--start` / `--end` | 日期，`2026-05-01` 或 `20260501` 都接受 |
| `--out` | 本次导出的输出根目录 |
| `--merged` | 额外产出整段合并 markdown |
| `--group-window` | 群聊启用上下文窗口筛选。**默认关 = 保留全量** |
| `--no-asr` | 关闭语音转写 |
| `--no-media-copy` | 不把媒体复制到 markdown 旁 |
| `--config` | 配置文件路径 |

前提同 §1（需要 API 服务）。

## 4. 环境体检 `doctor.py`

只读诊断，不启动 WeFlow、不改文件。**排障先跑它，比读代码快。**

```powershell
python "$SkillRoot\scripts\doctor.py"
```

| 参数 | 说明 |
|---|---|
| `--config` | 配置文件路径 |
| `--json` | 输出结构化结果，供自动化消费 |

检查内容随后端而变：API 后端查 `/health`、token、对已知非空会话的消息语义探测（health 通 ≠ 数据可读）、语音 worker 就绪；legacy GUI 后端查可执行文件与 CDP。另外检查四个数据根。

## 5. 并入旧导出快照 `archive_exports.py`

把换机备份、历史手工导出等旧目录摄取进长期归档库。

```powershell
python "$SkillRoot\scripts\archive_exports.py" --raw-root <旧raw目录> --processed-root <旧processed目录>
```

| 参数 | 说明 |
|---|---|
| `--raw-root` | 要摄取的 raw 树 |
| `--processed-root` | 要摄取的 processed 树 |
| `--keep-source` | 保留源目录 |
| `--config` | 配置文件路径 |

归档是「同路径新覆盖旧」的合并语义，所以**多个快照必须按时间从旧到新依次摄取**，否则旧内容会盖掉新内容。

## 6. 打开产物 `Open-LatestInsights.ps1` / `Open-InsightsByDate.ps1`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\Open-LatestInsights.ps1" -Workspace "$Workspace"
```

| 参数 | 说明 |
|---|---|
| `-Workspace` | 工作区路径；insights 根从其 `config.toml` 的 `[paths].insights` 解析 |
| `-NoOpen` | 只打印「解析到的根 + 命中文件」，不启动编辑器。**排障与自动化验收入口** |
| `-NoPause` | 结尾不等回车 |

`Open-InsightsByDate.ps1` 参数相同，按日期挑选而非取最新。

## 7. 备份本地 git 仓 `Backup-PrivateRepo.ps1`

把任意本地 git 仓打成单文件 bundle（全 history 快照，`git clone <bundle>` 即可还原）。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\Backup-PrivateRepo.ps1" -RepoPath <仓路径> -Destination <落点目录> -Name <名字>
```

| 参数 | 说明 |
|---|---|
| `-RepoPath` | 要备份的 git 仓。默认当前目录 |
| `-Destination` | **必填**，bundle 落点目录 |
| `-Name` | bundle 名字前缀 |
| `-Keep` | 滚动保留份数，默认 5 |

路径与名字全走参数，不硬编码。

## 8. 内部脚本（不面向用户，路由表不收）

| 脚本 | 用途 |
|---|---|
| `print_config_path.py` | 供 PowerShell 侧读 config（复用 `load_config`）。**禁止在 ps1 里手写 TOML 解析** |
| `sensevoice_worker.py` | 语音转写常驻 worker，由导出链路自动拉起 |
| `init_worktree_config.py` | 开发用：为 git worktree 生成指回主工作区数据根的 config |
| `validate_weflow_automation.py` | legacy GUI 后端的校验脚本 |
| `process_existing_raw.ps1` | §2 的 PowerShell 包装 |
| `run_daily_export.py` | §1 的 Python 主体，由 ps1 调用 |
