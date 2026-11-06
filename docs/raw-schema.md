# Canonical Raw Schema v1

`schema_version: 1` 是本项目定义的 canonical raw 契约版本。当前 WeFlow 1.x 导出的聊天 JSON 与朋友圈 JSON，经本页列出的字段子集解释后，视为 raw schema v1。原始 JSON 文件本身不要求写入 `schema_version` 字段。

这份契约只固化下游实际消费的字段子集。未来替换导出基底时，适配器只需要产出本页目录树与字段语义；`preprocessing/`、`archiving.py`、`chat_flow.py` 不应再跟具体导出工具耦合。

## 目录树约定

```text
<raw-root>/
  {私聊|群聊}_<会话名>_<YYYYMMDD>/
    {私聊|群聊}_<会话名>_<YYYYMMDD>.json
    media/
      images/
      voices/
      emojis/
      ...
  {私聊|群聊}_<会话名>_<YYYYMMDD-YYYYMMDD>/
    {私聊|群聊}_<会话名>_<YYYYMMDD-YYYYMMDD>.json
    media/
      images/
      voices/
      emojis/
      ...
  朋友圈导出_*.json
  media/
    ...
```

约定：

- 导出会话目录名必须以会话类型开头并带日期后缀：`{私聊|群聊}_<会话名>_<YYYYMMDD>` 或 `{私聊|群聊}_<会话名>_<YYYYMMDD-YYYYMMDD>`。类型前缀是命名契约。长期归档的单日聊天按 `session.wxid` 与 JSON 文件名日期解析既有会话目录；全新会话才按导出目录去日期后缀建目录，重新写入的文件名为 `<归档目录名>_<YYYYMMDD>.json`。多日聊天仍是可读取的 schema v1 产物，但不由单日摄取器拆分，批量摄取会明确阻断。
- 聊天媒体路径始终以会话 JSON 所在目录为基准解析。历史产物可为 `media/images/a.jpg`；新 HTTP API 产物的 `weflow.mediaScheme` 为 `cas-v1`，引用形态为 `../_media/<class>/<hh>/<md5>.<ext>`。
- 朋友圈 canonical 位置是 raw 根级 `朋友圈导出_*.json`；新产物的 `posts[].media[].localPath` 以 raw 根为基准引用 `_media/<class>/<hh>/<md5>.<ext>`，Live Photo 动态部分写在同一媒体项的 `livePhoto.localPath`。媒体解密失败时该项可保留 URL 而不写 `localPath`；历史 `media/` 布局继续可读、不迁移。
- 长期归档库中的 `archived/raw/<会话>/...json` 仍是 schema v1；根级朋友圈 JSON 与根级 `media/` 也按原名归档。
- 归档计划、会话头索引、改名扫描、历史导出身份扫描与群成员历史回退均接受 UTF-8 BOM；归档仍保存 incoming 原字节，不因解码重新序列化。BOM 不改变同目录混入不同 wxid 的拒绝判据。
- 日常导出收尾会扫描 `archived/raw` 的一级会话目录；同一 `session.wxid` 出现在多个目录时，以本轮 canonical raw 的 `session.displayName` 为目标目录逐文件合并。没有本轮 raw 的会话不猜测目标目录而停留报告；碰撞判据复用历史摄取护栏。相同被拒/停留项在零文件动作时由工作区本地状态去重，明细或目标改变、状态删除或实际移动才重新报告；状态与报告均在工作区本地，且不影响导出退出码。
- 改名纯搬家不回溯归一文件名。所有将移出 raw 的旧路径都纳入计划并先持久备份；共享媒体仍被旧聊天需要时保留旧源。同日逐字节相同的副本可用字节相等证明覆盖，未知代际不冒充同代证明；其它同日文件保留并报告，等待该日重写时证明覆盖并收敛。备份位于 `archived/raw-folded/`，不作为 raw 输入；JSON 本身不重新序列化。
- 轮转、改名合并与批量摄取在计划定稿后统一检查最终目标及退休列表，取消日常按名免证明。同字节 no-op；同 wxid、同日且消息覆盖通过的日常替换可不备份；同身份坏旧正文只能在日常先备份复读再替换，非法 incoming 不得覆盖旧聊天。非聊天替换沿用碰撞规则，非聊天退休也必须先备份；朋友圈区间按原顺序在虚拟归档上规划，包含本批先写后退休的对象。其生产者与 SHA 进入计划，退休前仍备份复读；同路径多版本通过 manifest 的 `backupPath` 区分，旧 manifest 无该字段时回退 `path`。范围计划字段和执行前提见 [入口与参数详表](../references/entrypoints.md)。

## 聊天 JSON

保留约定同时适用于聊天与朋友圈：daily 的保留输入、引用 CAS（含 Live Photo 动态部分）及同组附件，以原字节移入 archived 同级的 `<archived名>-retained/<时间戳>/`。每份文件先复制、复读校验再删 live 原件；失败则保留原件并报告。子目录沿用 live 相对布局，manifest 在子目录外同级，批量入口可直接读取该子目录。归档旧目录残留原地不动，bulk 与 rename 不建立隔离根。本轮 processed 目标从隔离副本计算，跳过受触及的会话日文件以及归档已有同路径的多对一日文件；没有同路径时可并入并报告。执行约定见 [入口与参数详表](../references/entrypoints.md)，不改变 canonical raw 字段或 schema 版本。

隔离须保全输入里原本存在的引用 CAS，包括计划期受阻的区间，以及发布后因旧区间删除失败才进入保留的输入。相关 live CAS 延迟到发布结果明确后再删；若从归档补取，须同时符合文件名中的 md5 和本次输入的原字节指纹，否则该输入留在 live 并报告。输入原本悬空的引用不自动补造，也不在 planner 增加缺媒体阻断。隔离中的编码或值错误按文件处理，不中断其它文件；apply 新产生的保留原因须进入报告与 manifest。

根对象：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `session` | object | 是 | 当前会话元信息。 | `load_chat_export()` 报错，不进入 preprocessing。 |
| `messages` | array | 是 | 原始消息列表，按 WeFlow 导出顺序排列。 | `load_chat_export()` 报错。空数组合法，但 `preprocess_run()` 不产出 processed export。 |
| `weflow` | object | 否 | WeFlow 元信息；归档摄取护栏读取下表中的血统与快照时间字段。 | 缺失时无法判断导出血统，异内容碰撞会保守拒绝。 |
| `avatars` | object/array | 否 | 头像元信息。当前下游不读取。 | 忽略。 |

`weflow`（字段均为可选；validator 不要求）：

| 字段 | 类型 | 语义 | 摄取护栏行为 |
|---|---|---|---|
| `generator` | string | legacy GUI 生成器；真机值为 `WeFlow`。 | 精确值 `WeFlow` 标识 legacy 血统。未知值不猜测。 |
| `version` | string | legacy GUI 版本，如 `1.0.3`。 | 不参与血统判定，版本变化不会被误判成跨代。 |
| `exportedAt` | integer/string | legacy GUI 快照导出 Unix 时间戳。 | 双方均为有效正整数时，在消息集合检查通过后比较快照新旧；incoming 较旧为不可强制的回退证据。 |
| `source` | string | 导出来源；HTTP API 真机值为 `http_api`。 | 精确值 `http_api` 标识 HTTP API 血统。 |
| `format` | string | HTTP API 输出格式；当前为 `json`。 | 不参与血统或新旧判定。 |

`session`：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `wxid` | string | 是 | 会话 wxid；目标会话过滤候选。 | validator 报错；过去会导致 `archive_chats_for()` 目标匹配能力下降。 |
| `nickname` | string | 是 | 会话昵称；目标会话过滤候选。 | validator 报错；过去会少一个匹配候选。 |
| `remark` | string | 是 | 会话备注；目标会话过滤候选，可为空字符串。 | validator 报错；过去会少一个匹配候选。 |
| `displayName` | string | 是 | 会话显示名；目标会话过滤候选。 | validator 报错；过去会少一个匹配候选。 |
| `type` | string | 是 | 会话类型，如 `私聊`、`群聊`；群聊在 `group_context_window.enabled = true` 时触发上下文窗口过滤。 | validator 报错；过去会跳过群聊上下文过滤。 |
| `username` | string | 否 | 旧样本或适配器可提供的别名；目标会话过滤候选。 | 少一个匹配候选。 |
| `messageCount` | integer | 否 | 导出时消息数；preprocessing 后会重写为清洗后的消息数。 | 忽略，之后由 preprocessing 写回。 |
| `avatar` / `lastTimestamp` | string/integer | 否 | WeFlow 元数据。当前下游不读取。 | 忽略。 |

`messages[]`：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `localId` | integer/string | 是 | 本地消息 id；用于语音失败日志与压缩消息 id 列表。 | validator 报错；过去日志会退化到其他 id。 |
| `createTime` | integer/string | 是 | Unix 秒级时间；用于排序、时间窗、压缩间隔、分日兜底和时间标记。 | validator 报错；过去会退化为 `0` 或当天计算错误。 |
| `formattedTime` | string | 是 | 可读时间，如 `2026-05-15 18:13:23`；优先用于分日和显示时间。 | validator 报错；过去会退回 `createTime` 或显示未知时间。 |
| `type` | string | 是 | 消息类型，如 `文本消息`、`引用消息`、`语音消息`、`图片消息`、`动画表情`、`其他消息`。 | validator 报错；过去会按普通文本路径处理。 |
| `content` | string | 否 | 文本内容、图片相对路径、语音转写结果、引用 XML 或系统协议文本。 | 渲染为空并影响清洗规则。 |
| `source` | string | 是 | 媒体/表情/语音相关源字段，可为空字符串。 | validator 报错；过去媒体与语音失败识别会少一个来源。 |
| `isSend` | integer/boolean/string | 是 | 是否本人发送；`1` 表示“我”。 | validator 报错；过去会默认对方消息。 |
| `senderUsername` | string | 是 | 发送者 wxid；群聊上下文、压缩、目标会话兜底过滤使用。 | validator 报错；过去会影响 self 识别、压缩和目标过滤。 |
| `senderDisplayName` | string | 是 | 发送者显示名派生缓存；群聊由本项目按 `senderRemark > senderGroupNickname > senderNickname > 既有 canonical 名 > contacts > wxid` 计算，私聊继续使用 contact `displayName`。 | validator 报错；消费方按事实字段与 wxid 兜底。 |
| `platformMessageId` | string/integer | 否 | 平台消息 id；引用解析和压缩 id 列表使用。 | 引用解析会失效；压缩 id 列表少该消息 id。 |
| `senderRemark` | string | 否 | 名册 `remark` 原值；群成员备注事实。 | 显示名继续退回下一候选。 |
| `senderGroupNickname` | string | 否 | 名册 `groupNickname` 原值；仅新群聊 canonical 保证写出，旧产物可缺。 | 显示名继续退回微信昵称；validator 不拒绝旧形状。 |
| `senderNickname` | string | 否 | 名册 `nickname` 原值；新群聊 canonical 不再混入群昵称。⚠️ 旧产物可能在此字段存了群昵称。 | 显示名继续退回下一候选。 |
| `replyToMessageId` | string/integer | 否 | 当前消息引用的 `platformMessageId`。 | 不解析 `replyContext`，只用原始引用字段或不显示引用。 |
| `quotedContent` | string | 否 | WeFlow 原始引用内容。 | 引用兜底内容为空时显示 `[消息]` 或不显示引用。 |
| `quotedSender` | string | 否 | WeFlow 原始引用发送者。 | 引用发送者退回其他字段或“未知”。 |
| `quotedSenderDisplayName` | string | 否 | WeFlow 原始引用发送者显示名。 | 引用发送者退回 `quotedSender` 或“未知”。 |
| `voiceEmotion` | object | 否 | 本地 ASR 从语音中抽出的标签，形如 `{ "emotion": ["HAPPY"], "events": ["Speech"] }`；本期只在 canonical raw 留档。 | 现有 preprocessing 与渲染忽略，不影响语音正文。 |
| `emojiName` | string | 否 | 动画表情 `rawContent` 的 `<emoji desc="…">` 解码出的作者名称原文，不是专辑名。仅成功解码且有字时写出。 | 名称缺失或仅空白时渲染 `[表情]`；旧 raw 仍可读。 |
| `emojiDesc` | string | 否 | 动画表情原始 base64 `desc`，最多 8192 个字符；保留供后续重解。无 desc、超限或 XML 不合法时不写；有限长但坏 base64/protobuf 仍可留此字段，不产生名字。 | 不尝试从 content 或 emojiattr 猜名，渲染 `[表情]`。 |

两个表情字段均为 schema v1 可选字符串，存在但类型错误时 validator 拒绝。只对新导出的动画表情映射，不回填旧 raw，不保留完整 `rawContent`（其中可能含媒体密钥/URL）。`desc` 的实测结构为外层 repeated field 1、内层 field 1 语言和 field 2 UTF-8 名称；语言取首个非空的 `zh_cn > default > zh_tw > 其他语言原顺序`，同语言取首个非空名称。这是本项目确定的选择顺序，不是上游保证。非法 base64、截断 protobuf 或非法 UTF-8 均不生成名字；解析 XML 上限 65536 个字符，未知 protobuf 字段可跳过，损坏尾部不接受部分名称。

processed 将 `emojiName` 渲染为 `〔名字〕`（U+3014/U+3015），不改变 raw 原文：空白/控制字符压为空格、半角竖线改 `｜`、名称内的 `〔〕` 改 `（）`，显示名最多 80 个 Unicode 字符（超长取前 79 个再加 `…`），无可显示字符时回退 `[表情]`。cleaner、聊天流及图片语境使用同一规则；同发送者且同 isSend、在既有时间间隔内的动画表情可与文字/引用按 ` | ` 合并，其它类型仍不合并。

`messages[].replyContext` 是可选嵌套对象。当前 preprocessing 会根据 `replyToMessageId` 生成它，raw 适配器通常不必直接提供；若提供，下游会消费这些字段：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `isSend` | integer/boolean/string | 否 | 被引用消息是否本人发送。 | 引用发送者按对方处理。 |
| `senderDisplayName` | string | 否 | 被引用消息发送者显示名。 | 退回 `senderUsername` 或“未知”。 |
| `senderUsername` | string | 否 | 被引用消息发送者 wxid。 | 退回“未知”。 |
| `content` | string | 否 | 被引用消息内容。 | 退回 `quotedContent` 或 `[消息]`。 |
| `type` | string | 否 | 被引用消息类型，用于图片/语音引用压缩。 | 按普通文本引用处理。 |
| `platformMessageId` | string/integer | 否 | 被引用消息平台 id。 | 仅少一个追踪字段。 |
| `image_ocr` | array | 否 | preprocessing 派生的图片 OCR 行。 | 图片引用无 OCR 文本时显示 `[图片]`。 |
| `image_ocr_inline` | string | 否 | preprocessing 派生的图片 OCR 摘要或保留路径。 | 退回 `image_ocr` 或从 `content` 提取。 |
| `image_vision` | array | 否 | preprocessing 派生的逐图视觉描述。 | 仅开启图片外传功能时出现。 |
| `image_vision_inline` | string | 否 | preprocessing 派生的单行视觉描述。 | 渲染优先于 OCR；不属于 canonical raw。 |
| `transcribe_failed` | boolean | 否 | preprocessing 派生的语音转写失败标记。 | 语音失败可能按原文本渲染。 |
| `replyToMessageId` / `quotedContent` / `quotedSender` / `quotedSenderDisplayName` / `quotedIsSelf` / `replyContext` | mixed | 否 | 嵌套引用链路的兜底信息。 | 嵌套引用降级或不显示。 |

## 朋友圈 JSON

根对象：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `filters` | object | 是 | 导出过滤条件；`filters.usernames` 用于区分 target/self stream。 | validator 报错；过去会失去导出归属兜底。 |
| `filters.usernames` | array | 是 | 本次朋友圈导出的目标用户名/显示名列表。 | validator 报错；过去会影响空 post-level 匹配时的归属判断。 |
| `posts` | array | 是 | 朋友圈动态列表。 | validator 报错。 |
| `exportTime` | string | 否 | WeFlow 导出时间；发现器标记之一。 | 只影响旧发现启发式；核心渲染不读。 |
| `totalPosts` | integer | 否 | WeFlow 导出总数；发现器标记之一。 | 只影响旧发现启发式；核心渲染不读。 |

`posts[]`：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `username` | string | 是 | 动态作者 wxid；target/self 过滤使用。 | validator 报错；过去会退回昵称匹配。 |
| `nickname` | string | 是 | 动态作者显示名；渲染与过滤使用。 | validator 报错；过去会退回 username 或“未知”。 |
| `createTime` | integer/string | 是 | Unix 秒级时间；排序与分日兜底使用。 | validator 报错；过去会排到 `0` 或 unknown day。 |
| `createTimeStr` | string | 是 | WeFlow 可读时间，如 `2026/05/15 10:00:00`；优先用于显示与分日。 | validator 报错；过去会退回 `createTime`。 |
| `contentDesc` | string | 是 | 朋友圈正文。 | validator 报错；过去会渲染为空正文。 |
| `media` | array | 是 | 媒体列表。 | validator 报错；过去无媒体行。 |
| `comments` | array | 是 | 评论列表。 | validator 报错；过去无评论行。 |
| `location` | object | 是 | 地点对象。 | validator 报错；过去无地点行。 |
| `id` | string/integer | 否 | WeFlow 动态 id；当前下游不读取。 | 忽略。 |
| `type` | integer/string | 否 | WeFlow 动态类型；当前下游不读取。 | 忽略。 |
| `likes` | array | 否 | 点赞列表；当前刻意不渲染。 | 忽略。 |

`posts[].media[]`：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `localPath` | string | 否 | 图片/视频本地路径；相对 raw 导出目录定位并复制到 processed sidecar。 | 该媒体项被跳过。 |
| `thumb` / `url` | string | 否 | WeFlow 元数据。当前下游不读取。 | 忽略。 |

`posts[].comments[]`：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `nickname` | string | 否 | 评论者显示名。 | 显示“未知”。 |
| `content` | string | 否 | 评论正文。 | 正文为空时可能按表情兜底。 |
| `refNickname` | string | 否 | 被回复者显示名；空字符串表示普通评论。 | 按普通评论渲染。 |
| `emoji` | object | 否 | 单个表情元数据。 | `content` 为空时少一个 `[表情]` 兜底信号。 |
| `emojis` | array | 否 | 多表情元数据。 | `content` 为空时少一个 `[表情]` 兜底信号。 |
| `id` / `refCommentId` | string/integer | 否 | WeFlow 评论 id。当前下游不读取。 | 忽略。 |

`posts[].location`：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `poiName` | string/number | 否 | POI 名称，地点显示优先字段。 | 退回 `address`，否则无地点行。 |
| `address` | string/number | 否 | 地址兜底字段。 | 无地点行。 |
| `cityName` | string/number | 否 | 城市名。 | 地点行不显示城市前缀。 |
| `latitude` / `longitude` / `country` / `city` / `poiAddress` / `poiAddressName` | mixed | 否 | WeFlow 地理元数据。当前下游不读取。 | 忽略。 |

## Validator

模块：`wechat_diary_core.raw_schema`

- `validate_session_json(data)`：校验聊天 raw JSON 的必填字段与基础类型。
- `validate_moments_json(data)`：校验朋友圈 raw JSON 的必填字段与基础类型。
- 失败抛 `RawSchemaError`，错误信息列出所有缺失或类型错误字段，例如 `messages[0].createTime`、`posts[0].contentDesc`。
- validator 不做严格全量校验：可选字段缺失不报错，未知字段不报错。

接线点：

- `preprocessing.cleaner.load_chat_export()`：聊天 raw 校验失败时抛 `InvalidExportError`，消息包含具体文件路径与字段名。
- `preprocessing.moments.load_moments_export()`：朋友圈 raw 校验失败时抛 `InvalidExportError`，消息包含具体文件路径与字段名。
- `scripts/archive_exports.py`：摄取 raw 根前逐个 JSON 校验；坏 JSON 跳过，其余文件继续归档，结尾汇总失败清单并返回非零退出码。写入前对解析得到的同 wxid、同日旧件做快照倒退检查，不限于同路径（同代按 `(createTime, localId)`、跨代按非空 `platformMessageId` 的多重集合；身份缺失、时间水位倒退、会话身份错配一律拒绝整批）。所有将被替换或退休的旧聊天（含目标同名件）都须校验正文 schema、证明被覆盖并先备份；人工强放不能跳过这三项。⚠️ `platformMessageId` 不是必填字段、也不保证唯一，摄取护栏只在跨代比较时用它并在有空值时 fail-closed，**不要据此把它升级为 schema 主键**。processed 与其它没有聊天身份契约的同路径异字节冲突默认拒绝，仅可对允许人工确认的类型强放；legacy 会话媒体与朋友圈区间 CAS 的异字节碰撞为硬拒绝。朋友圈区间 JSON 使用动态及媒体覆盖判据决定计划目标和退休列表，随后也通过最终目标终检与备份执行。

## 字段梳理清单

范围：`wechat_diary_core/preprocessing/{cleaner,context_window,moments,image_ocr,time_compress}.py`、`wechat_diary_core/archiving.py`、`wechat_diary_core/chat_flow.py`。

| 文件 | 消费字段 |
|---|---|
| `preprocessing/cleaner.py` | root: `session`, `messages`; session: `type`; message: `platformMessageId`, `replyToMessageId`, `senderUsername`, `senderDisplayName`, `isSend`, `content`, `type`, `source`, `localId`, `createTime`, `formattedTime`, `senderRemark`, `senderGroupNickname`, `senderNickname`, `quotedSenderDisplayName`, `quotedSender`, `quotedContent`, `quotedIsSelf`, `replyContext`; derived writes: `replyContext`, `is_self_related_pat`, `is_chatroom_top_message`, `transcribe_failed`, `session.messageCount` |
| `preprocessing/context_window.py` | message: `platformMessageId`, `is_self_related_pat`, `isSend`, `senderUsername`, `replyToMessageId`, `content`, `quotedContent`, `source`, `createTime` |
| `preprocessing/moments.py` | root: `posts`, `filters`; filters: `usernames`; post: `createTime`, `nickname`, `username`, `contentDesc`, `media`, `comments`, `location`, `createTimeStr`; media: `localPath`; comment: `nickname`, `refNickname`, `content`, `emojis`, `emoji`; location: `poiName`, `address`, `cityName` |
| `preprocessing/image_ocr.py` | message: `content`, `source`; OCR engine result fields are external engine outputs, not raw schema fields |
| `preprocessing/time_compress.py` | message: `type`, `senderUsername`, `isSend`, `createTime`, `endCreateTime`, `content`, `compressed_count`, `compressed_local_ids`, `localId`, `compressed_platform_message_ids`, `platformMessageId`, `compressed_segments`; `endCreateTime` and `compressed_*` are preprocessing 派生字段 |
| `archiving.py` | processed root: `messages`, `session`; session: `username`, `wxid`, `displayName`, `nickname`, `remark`; message: `senderUsername`, `isSend`, `formattedTime`, `createTime` |
| `chat_flow.py` | message: `createTime`, `formattedTime`, `is_chatroom_top_message`, `is_self_related_pat`, `isSend`, `senderDisplayName`, `senderRemark`, `senderGroupNickname`, `senderNickname`, `senderUsername`, `compressed_segments`, `type`, `content`, `image_ocr`, `image_ocr_inline`, `transcribe_failed`, `compressed_local_ids`, `replyContext`, `replyToMessageId`, `quotedContent`, `quotedSenderDisplayName`, `quotedSender`, `quotedIsSelf`; replyContext: `isSend`, `senderDisplayName`, `senderUsername`, `type`, `content`, `quotedContent`, `image_ocr`, `image_ocr_inline`, `transcribe_failed` |
