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

- 会话目录名必须以会话类型开头并带日期后缀：`{私聊|群聊}_<会话名>_<YYYYMMDD>` 或 `{私聊|群聊}_<会话名>_<YYYYMMDD-YYYYMMDD>`。类型前缀用于保证不同导出后端写入同一套长期会话目录；进入长期归档库时会去掉日期后缀，JSON 文件名保留原日期。
- 聊天媒体路径以会话目录为基准解析，例如 `media/images/a.jpg`。
- 朋友圈 canonical 位置是 raw 根级 `朋友圈导出_*.json`，朋友圈媒体在 raw 根级 `media/` 下。当前发现器仍递归兼容旧位置，但新适配器应按根级布局产出。
- 长期归档库中的 `archived/raw/<会话>/...json` 仍是 schema v1；根级朋友圈 JSON 与根级 `media/` 也按原名归档。

## 聊天 JSON

根对象：

| 字段 | 类型 | 必填 | 语义 | 缺失时下游行为 |
|---|---|---:|---|---|
| `session` | object | 是 | 当前会话元信息。 | `load_chat_export()` 报错，不进入 preprocessing。 |
| `messages` | array | 是 | 原始消息列表，按 WeFlow 导出顺序排列。 | `load_chat_export()` 报错。空数组合法，但 `preprocess_run()` 不产出 processed export。 |
| `weflow` | object | 否 | WeFlow 元信息。当前下游不读取。 | 忽略。 |
| `avatars` | object/array | 否 | 头像元信息。当前下游不读取。 | 忽略。 |

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
| `senderDisplayName` | string | 是 | 发送者显示名；渲染姓名和本人名字推断使用。 | validator 报错；过去会退回备注、昵称、wxid 或“未知”。 |
| `platformMessageId` | string/integer | 否 | 平台消息 id；引用解析和压缩 id 列表使用。 | 引用解析会失效；压缩 id 列表少该消息 id。 |
| `senderRemark` | string | 否 | 发送者备注；显示名兜底。 | 显示名继续退回下一候选。 |
| `senderNickname` | string | 否 | 发送者昵称；显示名兜底。 | 显示名继续退回下一候选。 |
| `replyToMessageId` | string/integer | 否 | 当前消息引用的 `platformMessageId`。 | 不解析 `replyContext`，只用原始引用字段或不显示引用。 |
| `quotedContent` | string | 否 | WeFlow 原始引用内容。 | 引用兜底内容为空时显示 `[消息]` 或不显示引用。 |
| `quotedSender` | string | 否 | WeFlow 原始引用发送者。 | 引用发送者退回其他字段或“未知”。 |
| `quotedSenderDisplayName` | string | 否 | WeFlow 原始引用发送者显示名。 | 引用发送者退回 `quotedSender` 或“未知”。 |
| `voiceEmotion` | object | 否 | 本地 ASR 从语音中抽出的标签，形如 `{ "emotion": ["HAPPY"], "events": ["Speech"] }`；本期只在 canonical raw 留档。 | 现有 preprocessing 与渲染忽略，不影响语音正文。 |

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
- `scripts/archive_exports.py`：摄取 raw 根前逐个 JSON 校验；坏 JSON 跳过，其余文件继续归档，结尾汇总失败清单并返回非零退出码。

## 字段梳理清单

范围：`wechat_diary_core/preprocessing/{cleaner,context_window,moments,image_ocr,time_compress}.py`、`wechat_diary_core/archiving.py`、`wechat_diary_core/chat_flow.py`。

| 文件 | 消费字段 |
|---|---|
| `preprocessing/cleaner.py` | root: `session`, `messages`; session: `type`; message: `platformMessageId`, `replyToMessageId`, `senderUsername`, `senderDisplayName`, `isSend`, `content`, `type`, `source`, `localId`, `createTime`, `formattedTime`, `senderRemark`, `senderNickname`, `quotedSenderDisplayName`, `quotedSender`, `quotedContent`, `quotedIsSelf`, `replyContext`; derived writes: `replyContext`, `is_self_related_pat`, `is_chatroom_top_message`, `transcribe_failed`, `session.messageCount` |
| `preprocessing/context_window.py` | message: `platformMessageId`, `is_self_related_pat`, `isSend`, `senderUsername`, `replyToMessageId`, `content`, `quotedContent`, `source`, `createTime` |
| `preprocessing/moments.py` | root: `posts`, `filters`; filters: `usernames`; post: `createTime`, `nickname`, `username`, `contentDesc`, `media`, `comments`, `location`, `createTimeStr`; media: `localPath`; comment: `nickname`, `refNickname`, `content`, `emojis`, `emoji`; location: `poiName`, `address`, `cityName` |
| `preprocessing/image_ocr.py` | message: `content`, `source`; OCR engine result fields are external engine outputs, not raw schema fields |
| `preprocessing/time_compress.py` | message: `type`, `senderUsername`, `isSend`, `createTime`, `endCreateTime`, `content`, `compressed_count`, `compressed_local_ids`, `localId`, `compressed_platform_message_ids`, `platformMessageId`, `compressed_segments`; `endCreateTime` and `compressed_*` are preprocessing 派生字段 |
| `archiving.py` | processed root: `messages`, `session`; session: `username`, `wxid`, `displayName`, `nickname`, `remark`; message: `senderUsername`, `isSend`, `formattedTime`, `createTime` |
| `chat_flow.py` | message: `createTime`, `formattedTime`, `is_chatroom_top_message`, `is_self_related_pat`, `isSend`, `senderDisplayName`, `senderRemark`, `senderNickname`, `senderUsername`, `compressed_segments`, `type`, `content`, `image_ocr`, `image_ocr_inline`, `transcribe_failed`, `compressed_local_ids`, `replyContext`, `replyToMessageId`, `quotedContent`, `quotedSenderDisplayName`, `quotedSender`, `quotedIsSelf`; replyContext: `isSend`, `senderDisplayName`, `senderUsername`, `type`, `content`, `quotedContent`, `image_ocr`, `image_ocr_inline`, `transcribe_failed` |
