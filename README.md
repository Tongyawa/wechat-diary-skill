# WeChat Diary

> 🏅 2026-10-31，本仓提交 [`7a8656c`](https://github.com/Tongyawa/wechat-diary-skill/commit/7a8656cc80dc34cbec15dee3324966f8c0c5d50f)（按需导出支持省略日期及可选文件附件）被开源 AI 搜索引擎 [XERJ](https://github.com/xerj-org/xerj) 选中，收录于其 [Corpus Hub](https://hub.xerj.org/corpus/tongyawa--wechat-diary-skill)——供 AI 编程智能体动手前参考的公开优秀实现索引。
>
> [![XERJ Corpus Hub contributor](https://hub.xerj.org/contributors/Tongyawa/badge.svg)](https://hub.xerj.org/contributors/Tongyawa)

WeChat Diary 是一个本地微信日记工作流：通过 WeFlow 导出微信聊天记录，把原始导出清洗成更适合 Agent 阅读的 Markdown，再由 `wechat-diary-skill` 生成日记、DoneList、灵感、额外观察，并增量维护长期自我画像和年度主线脉络。

代码仓与数据工作区相互独立：本仓只放可分发代码和根级 `SKILL.md`；`config.toml`、`WeFlow-*` 数据和工作区入口保留在用户自己的工作区。

## 解决什么问题

微信聊天记录里有大量个人经历、待办、灵感和长期线索，但原始导出不适合直接阅读或长期检索。本项目把它拆成两层：

1. **工程层**：自动化 WeFlow 导出、清洗、压缩群聊上下文、OCR 图片、归档 raw/processed 数据。
2. **Agent 层**：读取 processed Markdown，生成每日可读产物，并把长期信号沉淀进画像和主线文件。

最终目标不是备份微信，而是把每天散落在聊天、文件传输助手和朋友圈里的信息转成可回看的个人知识库。

## 当前主要产物

默认 `wechat-diary-skill` 会在配置指定的 insights 根下生成：

- `Diary/<yyyy>/...md`：第一人称日记。
- `DoneList/<yyyy>/...md`：当天完成事项。
- `Inspirations/<yyyy>/...md`：项目灵感、待跟进事项、值得深入的话题。
- `ExtraNotes/<yyyy>/...md`：Agent 额外观察到、但用户可能没注意到的点。
- `Profile/自我画像.md`：长期自我画像，增量维护。
- `Threads/<yyyy>.md`：年度主线脉络，增量维护。

日产物的文件名带当天标题与关键词（形如 `2026-06-15 <当天标题> #关键词1 #关键词2.md`），便于在文件管理器里直接扫读和检索，不必逐个打开。

## 目录结构

```text
.
├── SKILL.md                       # 根级 skill 入口与工作流
├── references/                    # 按需加载的二次加工 Prompt
├── docs/                          # 工程文档
├── scripts/                       # 日常入口、补跑入口、归档和校验脚本
├── tests/                         # 单元测试
├── wechat_diary_core/             # 导出、清洗、归档、配置等核心代码
├── requirements.txt               # Python 依赖
└── config.example.toml            # 工作区配置模板
```

运行时还会用到这些 gitignored 数据目录，实际位置由 `config.toml [paths]` 决定：

```text
WeFlow-raw-exports/          # WeFlow 原始导出，下一轮运行前会合并进长期归档
WeFlow-processed-exports/    # 清洗后的当日 Markdown，供 skill 读取
WeFlow-archived-exports/     # 长期 raw/processed 归档库
WeFlow-insights/             # 日记、DoneList、灵感、画像、主线等最终产物
```

## 初次配置

先把本仓安装到 Skill 目录，再创建一个独立的数据工作区。下面用 `$SkillRoot` 和 `$Workspace` 区分二者：

1. 安装 Python 依赖：

   ```powershell
   python -m pip install -r "$SkillRoot\requirements.txt"
   ```

2. 复制配置模板：

   ```powershell
   New-Item -ItemType Directory -Force $Workspace
   Copy-Item "$SkillRoot\config.example.toml" "$Workspace\config.toml"
   ```

3. 编辑 `$Workspace\config.toml`，至少确认：

   - `[export_backend].backend`：默认 `weflow_api`（WeFlow 5.x 本地 HTTP API）；已有 canonical raw、只想离线处理时可设为 `manual`。`weflow` 仅保留给 WeFlow ≤4.x 的 legacy GUI 自动化。
   - `[export_backend.weflow_api]`：确认 `base_url`，并在 WeFlow「设置 → API 服务」生成一个固定非空 Access Token 写入 `access_token`。token 不热更新，修改后要重启 API 服务；首次启用 API 服务仍需手动打开一次。
     超时按路径区分：`request_timeout_sec`（控制面，探活与会话/联系人/朋友圈列表，默认 120）；`message_request_timeout_sec`（消息数据面，默认 600，跨月全量可调到 900）；`moments_request_timeout_sec`（朋友圈媒体导出数据面，默认 3600）。朋友圈新键缺省即生效，不受现役控制面低值影响。
   - `[export_backend.weflow].weflow_exe`：API 不可达时可用于普通启动 WeFlow，也是 legacy GUI 后端的可执行文件路径；API 后端不会停止用户自己打开的 WeFlow。旧 `[automation]` 配置仍兼容，运行时会提示迁移。
   - `[asr].engine`：空字符串表示关闭并写明确的语音转写失败占位；设为 `sensevoice` 时，还要让 `worker_python` 指向独立 uv 项目的 Python。模型在首次语音时才由常驻 worker 加载；路径缺失或 worker 崩溃只降级语音，不阻断聊天导出，也不会向全局 Python 安装重依赖。
   - `[daily_export].skip_official_accounts`：默认 `true`，跳过 `gh_` 公众号、`@openim` 企业微信与 `@opencustomerservicemsg` 客服会话，连请求都不发。这些平台型会话对日记没有价值；想要就设 `false`。**导出结果里找不到它们是预期行为，不是漏导。**
   - `[paths]`：raw、processed、archived、insights 的落点。
   - `[user].self_wxids`：自己的 wxid / 文件传输助手，用于识别收集箱。
   - `[skills].daily`：默认包含 `wechat-diary-skill`。

`config.toml` 不进 git，适合放本机路径、账号标识和其他私人配置。

首次运行或环境变化后，可先执行只读体检：

```powershell
Push-Location $Workspace
python "$SkillRoot\scripts\doctor.py"
Pop-Location
```

doctor 会按后端检查数据入口：`weflow_api` 覆盖 `/health`、固定 token、已知非空会话的消息语义探测和 SenseVoice worker 配置；legacy `weflow` 检查可执行文件与 CDP。此外还检查四个数据根。它不会启动 WeFlow、worker 或修改文件。自动化联动可给同一命令追加 `--json` 获取结构化结果。

## 常用用法

每日完整导出由工作区薄壳调用本仓脚本；Agent 可直接运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_daily_export.ps1" -Workspace $Workspace -NoPause
```

只处理已经存在的 WeFlow raw 导出，不重新启动 WeFlow：

```powershell
Push-Location $Workspace
python "$SkillRoot\scripts\process_existing_raw.py" --raw-root WeFlow-raw-exports --day 2026-06-15 --require-day
Pop-Location
```

### 按需导出单个会话

日常导出面向「昨天的全部会话」。想单独拿某个会话的某段历史（复盘一次合作、回顾一段时间的联系），用按需通道——它只写你指定的输出目录，**不碰日常的 raw / processed / 归档**：

```powershell
Push-Location $Workspace
python "$SkillRoot\scripts\export_on_demand.py" --session 张三 --start 2026-05-01 --end 2026-05-31 --out .\临时导出
Pop-Location
```

`--session` 接受 wxid 或显示名子串；拿不准就先 `--list-sessions 关键词` 列候选。省略日期即本机可读的全部历史，只给一边日期则只限制该边。日期两种写法都行（`2026-05-01` / `20260501`）。常用开关：`--merged` 额外产出整段合并 markdown，`--group-window` 对群聊启用上下文窗口筛选（默认全量），`--no-asr` 跳过语音转写，`--no-media-copy` 不把媒体复制到 markdown 旁。

文件附件默认随导出保存：日常、批量、补跑和按需都会把本机已下载、且 MD5 与大小都对得上的文件存进 `_media/files/`，并在消息里记下链接；客户端没下载过的文件只留标签，不影响退出码。总开关和来源目录在配置 `[file_attachments]`，详见 [文件附件](references/file-attachments.md)。按需导出另在会话目录生成 `files/README.md`（保留原名，可直接点开）和逐条 `manifest.json`；本次不要文件加 `--no-files`；要求文件必须齐全时加 `--include-files`，缺任何一个退出码 3。`--files-root` 临时指定来源目录。不自动下载过期或尚未下载的文件，独立于 `--no-media-copy`。

老格式图片可能因 API 不支持而缺少路径，字节仍可能在本地。可在本机 `config.toml` 配置一个支持 `--canonical-dir`、`--config`、`--apply`、`--report-json` 的 Python 补数脚本：

```toml
[image_backfill]
script = ""  # 默认关闭；填写本机脚本路径，相对路径以 config 所在目录为准
```

脚本是可选本地扩展，本公开仓不分发补数算法。配置后，按需导出在生成 Markdown 前补本次 raw 中的空图片 `source`，不覆盖已有图片。结果区分补回、按现役规则未找到候选、格式不支持、无法关联、结构或解码失败、ffmpeg 不可用和未检查；“未找到候选”不代表已证实字节消失。未配置或工具失败时导出照常继续，保留 `[图片]` 并给出配置指引；配置的脚本不存在时指出缺失路径并引导修正 `[image_backfill].script`，不打印手工命令；其他补数失败或仍有剩余时给出使用最终 raw 路径的手工补数命令。

`--no-media-copy` 不影响 raw 补数；`media_localize=false` 时不自动补。此配置目前只由按需入口消费，日常、批量与归档摄取行为不变。手工补 raw 后，processed / merged 仍需重建；离线入口 `scripts/process_existing_raw.py` 会使用 config 的 processed / archived 落点，应先准备指向本次独立输出的隔离配置。

日常、按需与批量导出都会检查 API 的媒体类别与同页撞号：图片和表情按消息自身的媒体标识确认归属，无法确认的引用保留既有缺媒体占位，不影响其余消息导出。控制台分别显示“碰撞组归属不符或无法判定”与“媒体类别不符”的条数；按需入口显示的是补图前诊断。合法的 GIF 图片仍正常保留。此检查只覆盖本次 API 响应，历史产物与上游语音持久缓存不在修复范围内。

### 批量导出全部会话的一段历史

想一次性把**所有会话**的某段历史（乃至全部历史）落成 canonical raw 时，用批量通道。它同样只写你指定的输出目录，**不碰日常的 raw / processed / 归档，也不轮转**：

```powershell
Push-Location $Workspace
python "$SkillRoot\scripts\export_bulk_raw.py" --out .\批量导出
Pop-Location
```

`--out` 始终必填。省略 `--start` / `--end` 时，每次都按 config.toml 的 `[bulk_export]` 解析区间：`start` 默认 `2010-01-01`，空 `end` 默认滚动到昨天；已有 plan 只补新增日期，不重规划已覆盖历史。配置只能让 plan 区间不变或扩大，显式写出的内缩边界会被拒绝；想停止向前滚动，就给 `[bulk_export].end` 写一个具体日期。CLI 同时显式给两个日期时仍走独立守卫：与 plan 不一致默认拒绝，确认扩大才加 `--expand-range`，且永远不能缩。任何实际扩区间都会打印旧区间、新区间和需补规划的会话数。`--session` 可重复，用来只导一组会话；省略即全部会话。

只想在开跑前确认实际区间，可加 `--print-range`。它完全离线，只读 config 与 plan，向 stdout 输出一行可直接解析的 JSON，不读取会话名册、不写 plan/state/journal；即使同时给 `--log` 也不会创建日志。

**它分两遍跑，这是它能中断续跑的原因**：

1. **规划**：先以「不取媒体」的方式扫一遍选定范围，按会话记下**有消息的日期与条数**，并用完成区间证明其余日期确实为 0。产出 `.<输出目录名>.bulk-export-plan.json`；旧版全量逐日 plan 会无损压成稀疏格式，不需重扫。
2. **导出**：按上一步的计数把日期装箱成若干「块」，每块目标 `--chunk-target` 条消息（默认 10000），逐块取数落盘，**每块一完成就记账**。

中断之后只需对同一个 `--out` 重跑即可续跑：已有规划会被复用，已完成的块直接跳过。日常链路已确认的「本机无记录」会话会进入 plan 第三态，默认不再请求；要复查时显式加 `--retry-no-local-records`。若某个已记账的 canonical JSON 或它引用的媒体文件被删除，那一块会自动失效并重导。`--replan` 只强制重扫计数、不重导已完成的块。需要持久日志时加 `--log <路径>`，driver 会逐行加时间戳追加写入且不改变控制台输出。

每轮还会写 `.<输出目录名>.bulk-export-reconciliation.json`，逐会话列出规划与实际的逐日消息数和差异——两遍之间新到的消息会被报告为差异，但**不算导出失败**。

几个值得知道的行为：

- 一个活跃会话日一个 canonical JSON，没有消息的日子不造空文件。
- 单日消息数超过 `--chunk-target` 时，那一天自己独占一块——按天落文件是下限，无法再切细。
- 规划阶段会把「真失败」「本机无记录」「确实一条都没有」分开记录；退出码由真失败、主动守卫停止或对账覆盖缺口驱动，只有完整覆盖才可能 exit 0。
- 若两个不同会话经名称净化后会写进同一个目录，程序在发出第一条消息请求前就拒绝整批，避免后写的静默覆盖先写的。

### 批量导出本人朋友圈历史

使用同一个批量入口，显式加 `--self-moments`：

```powershell
python "$SkillRoot\scripts\export_bulk_raw.py" --self-moments --out <raw输出根> --config <config.toml>
```

只导 `[daily_export].self_moments_usernames` 中的本人账号；名单为空时报错，不做聊天导出，也不读取或改写聊天 plan/state/对账。区间优先使用成对 `--start` / `--end`，省略时直接用 `[bulk_export]`（缺省 2010-01-01 到昨天）；控制台会打印请求区间。输出是一份根级区间朋友圈 canonical JSON 与 `_media` CAS，文件名取动态实际最早、最晚的本地日期，零动态用 `区间_空`，区间名称不撞日常单日文件。新文件发布后，同账号集合下旧动态和已落地媒体都被完整覆盖的旧区间自动删除；评论、点赞、昵称变化不阻止替换。同名但覆盖不足时，新文件加唯一保留后缀，旧文件原样保留。摘要列出按年数量、最早/最晚动态及媒体发布/缺失计数。退出码 0 表示 JSON 发布成功且无旧区间待审查；3 表示新文件已发布，但旧区间有缺动态、缺媒体、不可核验或删除失败，须看报告。缺媒体会明确警告并计数。完整规则见 [入口与参数详表](references/entrypoints.md)。

动态来自本地 SNS 库缓存；已有动态的媒体可能通过 CDN 下载。未缓存的更早动态无法经 WeFlow HTTP API 主动补拉。可先在 PC 微信中打开本人朋友圈并向下浏览，再用原命令重跑；是否补齐应核对按年数量和最早时间。

### 把历史导出并入长期归档

手上有旧的导出快照（换机备份、以前手工导出的目录）时，可以直接摄取进归档库：

```powershell
Push-Location $Workspace
python "$SkillRoot\scripts\archive_exports.py" --raw-root <旧raw目录> --processed-root <旧processed目录>
Pop-Location
```

入口默认复制并保留源树；确需在成功后删除源树时，显式加 `--move-source`。旧 `--keep-source` 参数继续兼容。

本人朋友圈区间摄取使用与导出相同的覆盖判据：先发布新文件，再清理完全覆盖的旧区间。`--force-overwrite` 不跳过这条判据；退出码 3 时保留 raw 源树供审查。

同名单日朋友圈也使用同一份动态与媒体覆盖证明：文件名账号 digest 和正文账号集合须一致，新文件须包含每个旧动态及其已落地媒体。证明通过就以 incoming 原字节替换，不备份旧单日 JSON；证明失败时日常轮转隔离 incoming，报告保留具体 post 身份或媒体引用。单日文件不另造第二个同日副本，区间发布器仍只处理区间文件。

单日聊天按 `session.wxid` 和文件名日期归档到既有会话目录，重新写入时统一文件名前缀。批量摄取中，每份将被替换或退休的旧聊天都须由 incoming 覆盖全部消息身份，包括目标同名文件；原件先写入 archived 下的 `raw-folded/` 并校验，再发布新文件、退休旧路径。写入前仍整批检查严格子集、时间水位和会话身份；legacy 快照还用 `exportedAt` 判断共同消息内容变化。`--force-overwrite` 只放行人工已确认的“机器无法判断”冲突，不能越过覆盖失败、旧聊天损坏或目标解析阻断。多个快照可重复传 `--raw-root`，同键多个 incoming 会被拒绝。

先加 `--print-plan` 可只读查看目标、旧副本、覆盖证明、阻断原因，以及预计保留的旧目录和媒体；显式 `--archive-raw-root` 可让预览不依赖本机配置。预览不创建任何文件，也不代表授权摄取。参数与输出说明见 [入口与参数详表](references/entrypoints.md)。多日、无日期、根级或嵌套聊天 JSON 暂不支持批量摄取；不要把整个 archived 根当作 raw 输入。

### 打开当天产物

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\Open-LatestInsights.ps1" -Workspace $Workspace
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\Open-InsightsByDate.ps1" -Workspace $Workspace
```

insights 根从 `-Workspace` 指向的 `config.toml` 的 `[paths].insights` 解析。加 `-NoOpen` 只打印「解析到的根 + 命中文件」而不启动编辑器，用于排障。

## 一轮跑完之后：怎么读结果

退出码 `0` 表示全部阶段成功。非 `0` 有两种，控制台措辞可以区分：

- **`Daily export completed with warnings.`** —— 聊天 diary 已经正常产出，只是某些可选阶段（多为朋友圈）失败并被跳过。列出的项可以在修复后单独补跑，不必重跑整轮。
- **`FAILED before export completed`** —— 导出本身没走完，产物不完整。

单个会话导出失败**只隔离它自己**，不会中断其余几百个会话。失败会记进工作区根的 `.export-state.json`（按导出目标日期去重计数，同一天重跑不累加）；某个会话连续多个导出日都失败时，收尾阶段会列出待审查清单，交互式运行会问你要不要忽略它，忽略名单也存在同一个文件里。只有精确命中“消息数据库未找到”、至少一年无活动且历史从未产出 canonical raw 的会话，才会进入 `noLocalRecords`，不再冒充失败；证据不足时仍按真失败。会话重新导出成功后会自动清除相应状态。

日常导出按 `/contacts` 名册中的当前 `displayName` 建目录（含群聊）；名册缺少该会话时才回退 `/sessions.displayName`，再缺失则回退 wxid。归档轮转共用单日目标解析。计划定稿后统一终检最终目标与退休列表，按名路径和只差大小写的路径都没有免证明通道。同字节直接复用；日常替换合法聊天要求同 wxid、同日且覆盖全部消息，可不备份；旧件移出 raw 前必须先持久备份并复读。解析失败、覆盖不足或旧正文损坏仍可退回原按名路径，但退路同样经过终检：身份不同、未知或覆盖不足时保留 incoming；同身份同日坏正文先备份复读再修复。非法 incoming 不得替换最终目标上的聊天，空目标保留原字节归档。Windows 日常目录匹配按文件系统的大小写规则处理。

报告按会话和日期累积，日常与批量分别维护自己的条目；无关运行不会清空旧问题。日常保留件及其实际引用的 CAS、同组附件复制并复读校验后，从 live 移入 archived 同级的 `<archived名>-retained/<时间戳>/`，同秒批次加序号。内部保持 live 相对布局；原路径、SHA256、身份、原因和时间记录在旁边的 `<时间戳>.manifest.json`。复制、删除或编码失败时该原件留在 live 并报告，其余文件继续处理，归档旧目录残留不搬。`doctor.py` 显示派生隔离根及批次数，不新增配置键。archived 下的 `raw-fold-report.json` 保留机器明细，Markdown 列出隔离子目录与处理开场词；逐项核对后可用批量入口直接摄取该子目录，或删除它。待处理批次以现存子目录为准，下一次轮转清除已不存在的批次条目，孤儿 manifest 只提示。日常入口只在报告新增或变化时打开报告；清单解决后保留空态。引用判据包括聊天媒体和朋友圈 `media[].localPath`、`livePhoto.localPath`；描述、元数据中的路径不算依赖。截断输入只恢复有效前缀中的完整媒体字段，不作为合法 raw 或覆盖证明。被引用的 live CAS 延迟到发布结果明确后再清理，使后续保留的区间也能带走原有媒体；输入本来缺失的对象不补造。其余正常附件在目标不存在时仍优先同卷移动；隔离一律复制、校验后再删源。

日常轮转发现报告 JSON 或条目结构损坏时，将原字节改名封存为 `raw-fold-report.corrupt-<时间戳>.json`，打印警告并建立新账本；报告读写或 Markdown 渲染异常只警告，不中止本次 processed 合并与安全清源。被终检保留的 raw 不再参与聊天、朋友圈或 sidecar 渲染；就绪输入在 live 正常生成最新内容。合并会跳过保留聊天对应的会话日 Markdown；它还会跳过被保留输入触及、且归档已有同路径的 sidecar／朋友圈日文件，保持归档旧版原字节。归档没有同路径的多对一日文件可以并入，报告列出相关保留源与贡献的不确定性。跳过文件留在 live，下次渲染由正常清目录删除。

三个渲染器处理 live raw 中的现存输入，不读取保留账本，也没有输入排除集。轮转的 processed 合并只看本次保留集：受触及的会话日文件跳过；受触及的多对一日文件在归档已有同路径时跳过，否则并入并报告。跳过的文件留在 live，下一次渲染正常清掉。处理已有 raw、manual 后端与批量 `--processed-root` 均按原有整树合并行为运行，不读取保留账本、不按保留状态跳过。批量仍先完成所有碰撞预检，任何拒绝发生在首次写入之前。

报告的「processed 影响」分别列出被跳过的会话文件、多对一文件，以及无同路径而并入的文件。归档 processed 是历史快照；这些报告行不参与后续并入判定，不承诺跨轮内容单调增加，也不触发自动重生。需要与归档 raw 同步时手工按日重生；按日全集工具就绪前，**不要用 `process_existing_raw` 单目录补 sidecar／朋友圈日文件，它会以单会话覆盖整日**。接口边界见 [入口与参数详表](references/entrypoints.md)。

若发现同一 wxid 已落在多个归档目录，收尾会把旧目录逐文件并到**本轮实际导出的 displayName**目录，纯搬家保留原文件名。同日逐字节相同的副本先备份再退休；不同内容保留并报告“待下次重写时收敛”，同路径冲突仍复用原摄取护栏。首次发现的拒绝/停留、明细或目标变化、以及实际文件移动，才会写 `.session-rename-report.md` 并尝试用 Notepad 打开；相同拒绝且本轮零动作不会重复弹出。本轮未导出的会话因没有可信新名而停留。状态仍在工作区 `.session-rename-state.json`，报告和状态不进 git，不改变导出退出码。

控制台只显示阶段和核心错误，完整日志在工作区根的 `.runlog/`。

## 二次加工怎么发生

当前仓库没有统一的一键二次加工脚本。日常导出完成后，由 Agent 在同一工作目录按 `config.toml [skills].daily` 顺序执行 skills。

公开默认配置是：

```toml
[skills]
daily = ["wechat-diary-skill"]
```

`wechat-diary-skill` 会从工作区配置解析 processed/insights 根，跳过下划线开头的 sidecar 目录，然后按根级 `SKILL.md` 与 `references/prompt-daily.md` 的契约生成 Markdown 产物。

## 测试

图片视觉转写默认关闭。开启 `[preprocessing.image_vision].enabled` 后，daily、按需与补跑共用 img-v4 的打标、成组、装包、缓存与渲染：聊天图只使用清洗及群聊窗口过滤后的同一份 raw，前后各 3 条消息；动态使用本条正文，评论默认不传。标签层另将长边最多 512px 的缩略图送 Codex Luna/low，不带语境。打标判为敏感且未解封的图只做本地占位，不进入正式转写；打标失败时按无打标继续。可在私有配置按 sha256 或会话解封，相邻普通图片的正常语境仍可能包含共享聊天文字。

`backend="cheap-api"` 兼容现有 provider/model 配置；选 `backend="codex"` 时使用具体模型名（如 `gpt-6.1-sol`）和 `effort`，并配置已有的原生 CLI 或 JS 入口 `codex_command`。缺少 CLI 时会告警降级，不会自动安装。两种接入都只接受经过 JSON、附件数和 slot 校验的描述；失败不写成功缓存，包失败按组重试。此版本不包含后台续跑、agy、自动路由或开机自启。

缓存默认在 `[paths].archived/_image-descriptions/`，自定义 `cache_dir` 也必须位于 archived 下；图片字节、组语境、提示词版本与模型身份共同决定命中。Codex 身份包含具体模型与强度；cheap-api 使用目录解析后的服务 ID，现有 CLI 不传显式 effort。命中直接渲染；同图不同语境可借旧记录并标记待补。组内部分失败后的补跑保留完整组重新发送，保留已有强命中；弱命中成功重转后升级为当前语境的强命中，旧语境缓存保持原样。近重复 pHash 只记录信号，不合并或跳过图片；依赖不可用时单次告警并继续转写。显式旧配置会盖过新默认值，启用前应核对 `context_messages`、超时与 token 预算，详见 `config.example.toml`。

公开仓库单元测试：

```powershell
python -m unittest discover -s tests
```

一次性测试输出、日志和临时对照文件应放进 `tests/_artifacts/<yyyy-mm-dd>-<topic>/`，不要散落在仓库根目录。

## 注意事项

- 默认 HTTP API 后端不做 GUI/CDP 点按；朋友圈日常导出会调用 `POST /sns/export` 并固定使用 `exportMedia: true`，让 WeFlow 在 staging 中按需解密媒体，再以稳定的日期+目标哈希文件名发布。解密失败的动态保留 URL 并产生警告，不会中断聊天导出。动态仍须已经进入 WeFlow 的朋友圈库；久未打开 WeFlow 时的数据新鲜度需要结合实际导出检查。legacy GUI 后端仍依赖 Windows 可见桌面，Agent 触发时通常需要提权运行。
- `WeFlow-*`、`config.toml`、最终 insights 产物都属于本地数据，不应提交进公开仓库。
- 群聊默认经过上下文窗口过滤，保留和用户相关的片段；需要完整语料时可将
  `preprocessing.group_context_window.enabled` 设为 `false`，此时群聊保留全量消息流。私聊始终保留全量消息流。
- 图片可走本地 OCR，微信表情目录会跳过，语音转文字失败默认只记录警告。
- 非文本消息按**证据**渲染，不按类型硬猜：附件类消息只有在原始数据里确实带文件名字段时才渲染成 `[文件：<名称> (<大小>)]`，没有该字段的就保持中性占位，不会编出一个不存在的文件名。引用消息会同时保留被引用的原文与本次回复。
- 单日聊天采用上述身份解析与覆盖证明。其它归档文件也经过最终目标碰撞检查；同字节复用，非聊天异字节按既有碰撞规则处理。本人朋友圈区间仍以动态与媒体覆盖决定是否退休，计划按原发布顺序模拟本批新增和退休对象，实际目标与退休列表先确定，执行不重新选路径。本批先发布、后退休的文件也先备份复读；同一路径多个字节版本分别保留，恢复时按 manifest 的 `backupPath` 取文件。`raw-folded` 是原件备份，不应作为 raw 数据源再次摄取或清洗。
- 导出映射是按当前 WeFlow 5.0.x 的响应契约实现的。升级 WeFlow 之后，建议先跑一次 `doctor.py`，再挑一个已归档的日期重导一遍、和归档里的旧产物比对，确认字段与顺序没有漂移，然后再恢复日常链路。

## 许可证

[MIT](LICENSE)
