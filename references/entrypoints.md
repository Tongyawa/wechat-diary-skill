# 入口与参数详表

> `SKILL.md` 的路由表决定**走哪条**，本文件给**每条怎么调**。只在需要具体参数时读本文件。
>
> 约定：`$SkillRoot` = 本 skill 根目录；`$Workspace` = 含 `config.toml` 的工作区。所有 Python 入口默认从工作区当前目录读 `config.toml`，也可用 `--config` 显式指定。

## 0. 共同前提与常见坑

只收「跑流程会踩到」的运行事实。工程规格、版本考据、待办不在此处。

**API 服务（路径 ①③ 的硬前提）**

- WeFlow 必须在跑，且**手动开过一次**「设置 → API 服务」。
- **Access Token 必须固定为非空**。「清空 token」那条路有 bug：UI 提示「已清除、允许无鉴权访问」，但服务仍返回 401，重启软件和 API 都无效。
- **token 改动不热更**，改完要**重启 WeFlow 的 API 服务**才生效。
- 症状不明先跑 `doctor.py`（§4）——它会区分「服务不通」和「服务通但读不到数据」。

**公众号默认不导出**

`[daily_export].skip_official_accounts` 默认 `true`，`gh_` 开头的会话连请求都不发。**产物里没有公众号是预期行为，不是漏导。**

**手工调 API 排障时：日期参数只认 `YYYYMMDD`**

传 `2026-06-01` 这种带连字符的格式会被**静默忽略并返回全量历史**，不报错也不告警。CLI 内部已归一化，只有你自己直接调接口时要注意——否则会把「全量」误读成「过滤失效」，或反过来误判 CLI 漏数据。

**导出会话的目录名带类型前缀**

`{私聊|群聊}_<会话名>[_<日期>]`。前缀是契约的一部分，归档库靠它做会话连续性，别在中途重命名。

**同一时间只在一个工作区跑完整导出**（路径 ① 会归档轮转 live roots）。

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

- `Daily export completed with warnings.` → 整轮走完了，但**有阶段失败被跳过**。
- `FAILED before export completed` → 导出没走完，产物不完整。

⚠️ **「with warnings」不等于「只是朋友圈失败」。** 失败清单里可能包含**几十个聊天会话**，也可能 sidecar 产出为 0。判断产物是否够用，**看控制台打印的实际计数**（`Diary processed files: N`、各类 sidecar 计数）和失败项清单，**不要凭「completed」三个字就假定聊天记录完整**。

单会话失败只隔离它自己，不中断其余会话；失败记进工作区根 `.export-state.json`，连续多个导出日失败会在收尾列出待审查清单。

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

> 🔴 **默认是「移动」，摄取成功后会 `rmtree` 删掉源目录。**
> 不确定、或源目录是唯一副本时，**先加 `--keep-source` 跑一遍**（改为复制，源树原样保留），确认归档结果无误再决定要不要真的移动。

```powershell
# 安全起步：先复制，不动源
python "$SkillRoot\scripts\archive_exports.py" --raw-root <旧raw目录> --processed-root <旧processed目录> --keep-source
```

| 参数 | 说明 |
|---|---|
| `--raw-root` | 要摄取的 raw 树 |
| `--processed-root` | 要摄取的 processed 树 |
| `--keep-source` | **复制而非移动，源树原样保留。省略即为移动 + 删源** |
| `--config` | 配置文件路径 |

两条语义要记牢：

1. 归档是「同路径新覆盖旧」的合并，所以**多个快照必须按时间从旧到新依次摄取**，否则旧内容会盖掉新内容。
2. raw 摄取遇校验失败时会保留源目录（不删），这是兜底、不是可依赖的保护——**别拿它当 `--keep-source` 用**。

## 6. 打开产物 `Open-LatestInsights.ps1` / `Open-InsightsByDate.ps1`

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\Open-LatestInsights.ps1" -Workspace "$Workspace"
```

`Open-LatestInsights.ps1` 参数：

| 参数 | 说明 |
|---|---|
| `-Workspace` | 工作区路径；insights 根从其 `config.toml` 的 `[paths].insights` 解析 |
| `-NoOpen` | 只打印「解析到的根 + 命中文件」，不启动编辑器。**排障与自动化验收入口** |
| `-NoPause` | 结尾不等回车 |

`Open-InsightsByDate.ps1` **参数并不相同**，多两个：

| 参数 | 说明 |
|---|---|
| `-Date` | 要打开的日期 |
| `-InsightsRoot` | 直接指定 insights 根，**绕过 config 解析**（config 不可用或想临时指向别处时用） |
| `-Workspace` / `-NoOpen` / `-NoPause` | 同上 |

## 7. 备份单个 git 仓 `Backup-GitRepo.ps1`

把**任意**本地 git 仓打成单文件 bundle（全 history 快照，`git clone <bundle>` 即可还原，不依赖任何服务器）。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\Backup-GitRepo.ps1" -RepoPath <仓路径> -Destination <落点目录> -Name <名字>
```

| 参数 | 说明 |
|---|---|
| `-RepoPath` | 要备份的 git 仓。默认当前目录 |
| `-Destination` | **必填**，bundle 落点目录 |
| `-Name` | bundle 名字前缀。默认取仓目录名 |
| `-Keep` | 滚动保留份数，默认 5 |
| `-Slot N` | 写 `<Name>-slot-N.bundle` 并**原地覆盖**；跳过日期命名与滚动清理 |

两种命名模式：

- **默认**（不传 `-Slot`）：`<Name>-<yyyyMMdd>.bundle`，保留最新 `-Keep` 份。适合手工打一次性快照。
- **槽位**（`-Slot N`）：`<Name>-slot-N.bundle`，固定文件名、原地覆盖。§8 的编排用这个，理由见那节。

路径与名字全走参数，不硬编码。把 `-Destination` 指向**另一块物理盘或云同步目录**才算真正的离机备份；和仓在同一块盘只防误删、不防盘坏。

## 8. 批量 bundle 冷备 `Invoke-BundleBackup.ps1`

按 `config.toml` 的 `[backup]` 段逐个备份配置里的仓，并把结果写进 `<bundle_dest>/last-run.json`。适合交给计划任务每天跑一次。

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\Invoke-BundleBackup.ps1" -Workspace "$Workspace"
```

| 参数 | 说明 |
|---|---|
| `-Workspace` | 工作区目录（含 `config.toml`）。默认当前目录 |
| `-Config` | 直接指定 config 路径，优先于 `-Workspace` |
| `-NoPopup` | 失败时不弹报告窗口。**无人值守/自动化用**；退出码与状态文件照常反映失败 |

行为要点：

- **成功不弹窗（CLI 下仍打印一行结果），失败弹出一个停留的报告窗口**。理由：一个成功和失败长得一模一样的闪窗只会训练人忽略它——旧计划任务正是这样连续失败一个月没被发现。交互调用该有反馈；计划任务里不闪窗，靠注册时的 `-WindowStyle Hidden` 保证。
- **`[backup]` 配置有误时拒绝运行**，退出码 2 并逐条列出。判据：缺 `path`、缺 `bundle_dest`、`name` 重复（**含仅大小写不同**——Windows 视为同一文件）、`name` 含非法文件名字符。不会「备份一部分再报成功」。
- **单仓失败不影响其余**，但整体退出码非零。
- **配置里的仓路径不存在 = 硬失败**，不会静默跳过（这正是旧任务失效的原因）。
- 真正未配置（既无 `repos` 也无 `bundle_dest`）时打一行 skip 后正常退出——这是唯一允许静默的情形。

### 槽位轮换：为什么文件名里没有日期

每个仓写 `<仓名>-slot-1.bundle` … `-slot-<keep>.bundle`，**固定文件名、原地覆盖**，而不是每天新增一个带日期的文件。

原因是云同步：**本地的滚动清理不一定会作为「删除」同步到云端**（实测有本地已删的 bundle 仍留在云上）。日期命名下，每天每仓新增一个永不消失的文件名——四个仓跑一年就是约 1460 个文件、近 1 GB，且只增不减。固定槽位把云端占用变成常数。

选哪个槽位：**先补空缺，再覆盖最旧的一个**。刻意不从 `last-run.json` 读「上次写了哪个」——那样状态文件一丢就不知道该写哪；按磁盘实际情况推断可以自愈。

代价是文件名不再带日期，所以 **`last-run.json` 里维护一份 `slotIndex`**，跨轮累积记录「每个仓的每个槽位分别是什么时候写的」：

```json
"slotIndex": { "<仓名>": { "1": "2026-08-05T21:00:00+08:00", "2": "2026-08-08T21:00:00+08:00" } }
```

**还原步骤**：读 `last-run.json` 的 `slotIndex` 挑时间最新的槽位 → `git clone <bundle_dest>\<仓名>-slot-N.bundle <目标目录>`。不需要解压，bundle 直接就是可 clone 的仓。

配置样例见 `config.example.toml` 的 `[backup]` 段。备份是否还在跑，`doctor.py`（§4）和日常导出（§1）收尾都会检查并在陈旧时给出补跑命令。

## 9. 内部脚本（不面向用户，路由表不收）

| 脚本 | 用途 |
|---|---|
| `print_config_path.py` | 供 PowerShell 侧读 config 路径（复用 `load_config`）。**禁止在 ps1 里手写 TOML 解析** |
| `print_backup_config.py` | 同上，以 JSON 输出 `[backup]` 段，供 §8 使用 |
| `sensevoice_worker.py` | 语音转写常驻 worker，由导出链路自动拉起 |
| `init_worktree_config.py` | 开发用：为 git worktree 生成指回主工作区数据根的 config |
| `validate_weflow_automation.py` | legacy GUI 后端的校验脚本 |
| `process_existing_raw.ps1` | §2 的 PowerShell 包装 |
| `run_daily_export.py` | §1 的 Python 主体，由 ps1 调用 |
