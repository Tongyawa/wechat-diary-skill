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

**平台型会话默认不导出**

`[daily_export].skip_official_accounts` 默认 `true`，`gh_` 公众号、`@openim` 企业微信与 `@opencustomerservicemsg` 客服会话连请求都不发。**产物里没有这些会话是预期行为，不是漏导。**

**手工调 API 排障时：日期参数只认 `YYYYMMDD`**

传 `2026-06-01` 这种带连字符的格式会被**静默忽略并返回全量历史**，不报错也不告警。CLI 内部已归一化，只有你自己直接调接口时要注意——否则会把「全量」误读成「过滤失效」，或反过来误判 CLI 漏数据。

**导出会话的目录名带类型前缀**

`{私聊|群聊}_<会话名>[_<日期>]`。前缀是契约的一部分，归档库靠它做会话连续性，别在中途重命名。
会话名优先取 `/contacts` 名册的当前 `displayName`（群聊同样适用）；名册没有该 username 时回退 `/sessions.displayName`，再缺失才用 wxid。

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

单会话失败只隔离它自己，不中断其余会话；失败记进工作区根 `.export-state.json`，连续多个导出日失败会在收尾列出待审查清单。群成员名册首次读取失败会先冷却 20 秒重试（总计最多 3 次请求，成功路径不等待）；重试耗尽也不算整会话失败：消息仍会落盘，发送者名称依次退到同一群既有 canonical raw 的最后已知名字、contacts，真的都没有才显示 wxid；控制台和失败 marker 只列最终降级，末尾降级数与健康信号也只统计重试耗尽的群。精确的“消息数据库未找到”只有在会话至少一年无活动且归档 raw 从未出现该 wxid 时才进入 `noLocalRecords`；证据不足仍按真失败。

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

群聊名册与日常链路使用同一冷却重试口径；三次请求仍未取得非空名册时，按需导出继续使用同群既有 canonical 名字、contacts、wxid 退路，不因辅助名字丢掉消息。

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

## 8. 批量 canonical raw 导出 `export_bulk_raw.py`

导出全部会话或一组指定会话的任意日期范围；只写显式输出根，不碰 live raw / processed / 归档，且不产 processed。先以 `media=false` 建可续跑的逐日消息数 plan，再按本地日历日组成目标大小的块带媒体导出；一个活跃会话日一个 canonical JSON，空日不造空文件。

```powershell
# 不写日期时使用 config.toml 的 [bulk_export]；空 end 每次滚动到昨天
python "$SkillRoot\scripts\export_bulk_raw.py" --out <raw输出根>

# 开跑前完全离线查看本次实际区间；stdout 只有一行 JSON
python "$SkillRoot\scripts\export_bulk_raw.py" --print-range --out <raw输出根>

# 全部会话；显式 CLI 日期走独立的 plan 冻结守卫
python "$SkillRoot\scripts\export_bulk_raw.py" --start 2026-07-02 --end 2026-08-05 --out <raw输出根>

# 续跑既有 plan：空 end 会补到昨天，已覆盖历史不重规划
python "$SkillRoot\scripts\export_bulk_raw.py" --out <同一个raw输出根>

# 指定一组会话；--session 可重复，接受 wxid 或唯一的显示名子串
python "$SkillRoot\scripts\export_bulk_raw.py" --start 2026-07-02 --end 2026-08-05 --out <raw输出根> --session <会话一> --session <会话二>
```

| 参数 | 说明 |
|---|---|
| `--start` / `--end` | 必须同时给或同时省略。显式给出时配置完全不参与；与 plan 不一致默认拒绝，只有配合 `--expand-range` 才能扩大。格式 `yyyy-mm-dd` 或 `yyyymmdd`，首尾都包含 |
| `--out` | 必填的 canonical raw 输出根；不会从 `[paths].raw` 猜落点 |
| `--session` | 可重复；wxid 或唯一显示名子串。省略即全部会话 |
| `--force` | 忽略已完成区间并重导；mapper 改进后刷新历史产物时使用 |
| `--chunk-target` | 每个导出块的目标消息数，默认 `10000`；单日超标时该日仍独立成块 |
| `--replan` | 强制以 `media=false` 重算本次范围的逐日消息数；只刷新 plan，不会重导已完成块 |
| `--expand-range` | 仅用于显式 CLI 日期：允许扩大既有 plan 区间；只能扩、不能缩。配置路径的扩区间不要求此开关 |
| `--retry-no-local-records` | 重新尝试已判定为「本机无记录」的会话；默认不再向这些会话发消息 API 请求 |
| `--log` | 把 driver 自身 stdout/stderr 逐行加 `[HH:mm:ss]` 后追加写入 UTF-8、LF 日志；控制台原文不变。与 `--print-range` 同给时忽略且不创建日志 |
| `--print-range` | 完全离线解析实际区间后退出；不构造 backend、不读会话名册、不写任何文件。成功时 stdout 恰好一行 JSON，含 `start`、`end`、`source`、`planRange`、`expands`；失败时 stdout 为空、中文错误走 stderr |
| `--config` | 配置文件路径 |

CLI 省略日期时，每次都读取 `[bulk_export]`：键缺省或空 `start` = `2010-01-01`，键缺省或空 `end` = 昨天；两者也可写具体日期。无 plan 时直接用配置创建首个区间；有 plan 时先拒绝显式固定边界造成的收缩，再以 `min(plan start, config start)` / `max(plan end, config end)` 钳位，所以区间只会不变或扩大。空 `end` 会滚动补到昨天；如果 plan 已到昨天或未来则静默沿用。想让区间冻结不再前移，就给 `[bulk_export].end` 写一个显式日期。任何实际扩大都会打印旧区间、新区间和需补规划的会话数。

逐日 plan 写在输出根同级的 `.<输出目录名>.bulk-export-plan.json`，状态写 `.<输出目录名>.bulk-export-state.json`（运行中有同名 `.journal` 追加账本），均不混进 canonical raw。plan/state/reconciliation 只存非零日消息数，另以完成区间证明其余天确实规划或导出为 0；覆盖缺一天时不会给出假分母。旧 v1 plan/state 会无损读取并在本轮压成 v2，不要求 `--replan`。

日常 `.export-state.json` 已判定的「本机无记录」会话会导入 plan 的 `noLocalRecords` 第三态；批量规划中新命中同一套四道证据判据的会话也进入该段。后续默认零请求、只在汇总显示数量；显式 `--retry-no-local-records` 才重新尝试。真失败仍逐轮重试、按错误指纹分组打印显示名；名字与 canonical 目录同样优先取 contacts 当前名，缺失才退 wxid。每完成一个块立即记账；若 canonical JSON 或它引用的媒体文件被删除，该块会自动失效并重导。每轮还会写 `.<输出目录名>.bulk-export-reconciliation.json`，逐会话列出稀疏的 plan/实际计数、覆盖区间与差异；控制台同时显示差异日与覆盖缺口。两遍之间的新消息会被报告为漂移，不作为导出失败。

退出码：`0` = 真失败为 0 且本次实际区间覆盖完整；已完成状态和「本机无记录」不算失败。`1` = 至少一个真失败、群名册坏状态守卫要求停止，或 reconciliation 的 `incompleteCoverageDays > 0`；`2` = 工作区或会话选择错误。汇总把「失败 N；本机无记录 M」分开打印。群成员名册三次请求仍失败时，当前消息块继续按同群既有 canonical 名字/contacts/wxid 退路落盘；若最近 5 个有消息群中至少 4 个名册重试耗尽，程序会先写紧凑 state、对账和汇总，再以退出码 `1` 停止，提示重启 WeFlow API 服务并用原命令续跑。平台型会话是否跳过仍服从 `[daily_export].skip_official_accounts`。若两个不同 wxid 经名称净化后会写进同一个 canonical 目录，程序会在第一条消息请求前拒绝整批，避免后写会话静默覆盖先写会话。

## 9. 离线群成员名字回填 `backfill_group_names.py`

扫描一个 canonical raw 根，用同一 `session.wxid` 下已经解析出的成员名字，回填仍显示为裸 wxid 的群消息。纯离线，不调用 WeFlow、不重取消息或媒体，也不生成状态文件。

```powershell
# 先预览，不写文件
python "$SkillRoot\scripts\backfill_group_names.py" --raw-root <canonical raw根>

# 确认计数后实际写入
python "$SkillRoot\scripts\backfill_group_names.py" --raw-root <canonical raw根> --apply
```

| 参数 | 说明 |
|---|---|
| `--raw-root` | 必填；要扫描和回填的 canonical raw 根 |
| `--apply` | 实际写入；省略时只打印可修复文件、消息、字段和仍无证据的数量 |

受影响集合由产物自描述：`senderDisplayName == senderUsername`，引用发送者对应 `quotedSenderDisplayName == quotedSender`。名字证据严格按 `session.wxid` 隔离，不信目录名前缀；只有命中该集合才会更新显示名，并仅在昵称/备注为空或仍等于 wxid 时补值。已有非 wxid 名字一律不覆盖。写入只替换单条消息里的 `senderDisplayName`、`senderNickname`、`senderRemark`、`quotedSenderDisplayName` 字符串；若文件不是现役 canonical 序列化形状，脚本会拒绝整文件重写。

该入口只修 raw。已经生成的 processed、Diary 和 insights 不会回溯更新；后续蒸馏读取修复后的归档 raw 才会受益。名字曾由旧 canonical 退路解析成功、但实际已经过期的部分降级，因字段不再等于 wxid 而无法从产物识别，本工具不会覆盖。
