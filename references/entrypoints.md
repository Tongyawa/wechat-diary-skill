# 入口与参数详表

> `SKILL.md` 的路由表决定**走哪条**，本文件给**每条怎么调**。只在需要具体参数时读本文件。
>
> 约定：`$SkillRoot` = 本 skill 根目录；`$Workspace` = 含 `config.toml` 的工作区。入口统一按“显式 `--config` / `-Workspace` → 当前目录 → `WECHAT_DIARY_WORKSPACE` 指向的目录”发现 `config.toml`。显式路径是硬约束，写错时不会降级到 CWD 或环境变量；只有完整导出的显式目标允许配置尚不存在，以便从模板创建并进入首启引导。全部落空时直接报错并列出探测路径，不会静默使用默认配置。

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

# 周期性追加到已有会话夹；省略 --start 时从目标夹最新分日 md 的日期起重导
python "$SkillRoot\scripts\export_on_demand.py" --session <wxid或显示名子串> --merge-into <已有会话夹> [--end 2026-05-31]
```

| 参数 | 说明 |
|---|---|
| `--session` | wxid，或显示名子串 |
| `--list-sessions <关键词>` | 列出匹配候选并退出。**拿不准会话名时先用它** |
| `--start` / `--end` | 日期，`2026-05-01` 或 `20260501` 都接受。合并模式省略 `--start` 时取目标夹最新分日 md 的日期（边界日会重导），省略 `--end` 时取运行当天；一次性 `--out` 模式仍要求两者都提供 |
| `--out` | 本次导出的输出根目录；与 `--merge-into` 互斥，一次性产物继续使用带日期范围的目录名 |
| `--merge-into` | 增量合并进已有会话目录；目标必须已存在，分日 md 与媒体并入该显式目录，`_raw/` 仍按日期范围累积 |
| `--merged` | 额外产出整段合并 markdown。合并模式会按目标夹全部分日 md 重建；若同名 merged md 已存在，即使未给此参数也会重建 |
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

把换机备份、`--out` 的一次性产物等旧目录摄取进长期归档库。**默认只复制、不动源树。**

```powershell
python "$SkillRoot\scriptsrchive_exports.py" --raw-root <旧raw目录> --processed-root <旧processed目录>
```

| 参数 | 说明 |
|---|---|
| `--raw-root` | 要摄取的 raw 树 |
| `--processed-root` | 要摄取的 processed 树 |
| `--move-source` | 摄取成功后删除源树；不传即复制保源 |
| `--keep-source` | 旧命令兼容参数，现在是 no-op |
| `--force-overwrite` | 只放行「机器判不出新旧」的同路径冲突；越不过严格子集 / 时间水位倒退 / 会话身份错配 |
| `--config` | 配置文件路径 |

写入前整批预检：判定 incoming 更旧就**拒绝整批、退出 `1`、一个文件都不写**（报告最多列 12 条），坏 JSON 逐个跳过并在结尾汇总。判据实现在 `scripts/archive_exports.py`，元数据形态与设计理由在工程 `CLAUDE.md`。

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

## 7. 初始化 Git worktree 配置 `init_worktree_config.py`

在**目标 worktree 根目录**运行。脚本自动从当前 Git worktree 发现主工作区，并生成本 worktree 的 gitignored `config.toml`：代码继续取当前 worktree，四个数据根与可选本地 voice fallback 路径则全部指向主工作区。不要在这之后顺手跑完整导出；完整导出会轮转主工作区的真实数据。

```powershell
python "$SkillRoot\scripts\init_worktree_config.py"
```

| 参数 | 说明 |
|---|---|
| `--main-root` | 主工作区根目录。自动发现失败或需要指定其他主工作区时才传 |
| `--force` | 已有本 worktree 的 `config.toml` 时覆盖它 |
