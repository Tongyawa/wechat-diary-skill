---
name: wechat-diary
description: 拉取昨日 WeChat 消息（经 WeFlow 自动化），清洗后归档，再生成日记 / DoneList / 灵感 / 额外观察四份 Markdown。默认每天早上由脚本链触发；也支持 `/wechat-diary summarize <folder>` 子命令对单个会话做一次性总结。
---

# wechat-diary

> **Phase B Step 12 已完成**。二次加工 prompt（Diary / DoneList / Inspirations / ExtraNotes + Summarize）已落地，见下文 §二次加工 Prompt。

## 默认流程（无参数）

无参数调用时跑下面这套流程，给早晨脚本链使用。

0. **批量转文字（可选预处理）** —— 若 `config.toml [user].voice_transcribe_usernames` 非空，先调 `wechat_diary_core.weflow_automation.voice_transcribe.batch_transcribe_voices_for(usernames=...)`。WeFlow 内置的「批量语音处理 → 批量转文字 → 开始转写」流程会把指定联系人的「Silk 解码失败」语音消息转成文字，避免下一步导出出来后 processed md 被失败提示横刷。判完成走任务中心差分（新增「语音批量转写(<username>)」行 → 已完成）。空列表时跳过整步，不打开 WeFlow GUI 多余流程。
1. **导出** —— 调 `wechat_diary_core.weflow_automation.exporter.export_all_chats(date=yesterday)`。底层 driver 由 `config.toml [automation].driver` 决定（uia / cdp / template 三选一），驱动 WeFlow 走「打开任务中心 → 抓 baseline → 关弹窗 → 导出 → 自动化导出 → 立即执行 → 任务中心 → 等差分判完成 → 首页」流程。本 SKILL 不关心是哪一层 driver，全部走 `driver.click_by_name(...) / driver.set_text(...) / driver.wait_for(...)` 抽象接口。`yesterday` 按本地时区计算；默认 `cleanup="delete"` 在跑前清空 raw/processed（前一天的产出应已被二次加工归档到 `WeFlow-archived-exports/`）。若 `config.toml [daily_export].self_moments_usernames` 非空，runner 还会复用同一套 WeFlow 朋友圈导出逻辑导出自己的朋友圈。**详细步骤、判完成判据与异常分支以项目根 `CLAUDE.md` §5.0 为准**（修复或扩展导出流程前先读规范，不要根据 `exporter.py` 现有实现反推契约）。
2. **清洗** —— 调 `wechat_diary_core.preprocessing.run(raw_date_dir)`。
   - 空会话文件夹丢弃。
   - `media/emojis/` 目录整体跳过（不做 OCR，消息里仅留 `[表情]` 占位）。
   - 私聊：保留全量消息流。
   - 群聊：走上下文窗口过滤 —— 以自己发言、自己引用别人、别人引用自己、以及可选 `anchor_keywords` 字面量命中的消息为锚点；默认保留前 3 条 / 后 5 条 / 前后 15 分钟内的相邻消息，重叠区间合并。算法详见 `CLAUDE.md` §6.2。
   - WeFlow 群聊置顶协议消息本身丢弃；被置顶的真实消息保留，并在发送者后加 `【置顶消息】`。
   - 群聊拍一拍默认丢弃；若是我拍别人，或别人拍我，则保留为 `拍一拍：...`。
   - 邻近时间的连续消息：仅保留首条的时间戳，省体积。
   - `media/images/*` 走本地 OCR，识别文本以 `[OCR] ...` 后缀内联到对应消息里。
   - 「转文字失败」标记仅写警告日志，不阻塞流程。
3. **归档** —— 调 `wechat_diary_core.archiving.archive(processed_date_dir)`，按会话写出 `WeFlow-processed-exports/<session_dir>/<yyyy-mm-dd>.md` 极简聊天流（`session_dir` 去掉原始文件夹后缀的日期）。
4. **二次加工** —— 读 `WeFlow-processed-exports/**/<yesterday>.md`（**不读**子目录前缀以 `_` 开头的；下划线前缀目录是扩展组件 的旁路通道，diary 二次加工只扫顶层 session）。其中 `朋友圈_自己/<yesterday>.md` 是自己的朋友圈素材，格式不同于聊天流，但属于公开 diary 素材范围。在 `WeFlow-insights/` 下产出四份 Markdown：
   - `Diary/<yyyy>/<yyyy-mm-dd>.md` —— 第一人称当日日记。
   - `DoneList/<yyyy>/<yyyy-mm-dd>.md` —— 分类捕捉的 DoneList；优先把 `config.toml [user].self_wxids` 指定的「自己 / 文件传输助手」会话里以 `D：` 开头的条目升级为正式条目。
   - `Inspirations/<yyyy>/<yyyy-mm-dd>.md` —— 散落在各会话里的项目灵感与待办。
   - `ExtraNotes/<yyyy>/<yyyy-mm-dd>.md` —— Agent 主动挑出但我没注意到的值得关注的点。

5. **长期归档** —— 四份 Markdown 写完后调 `wechat_diary_core.promote_day_to_archive(yesterday_iso, config=cfg)`，把当日 `WeFlow-processed-exports/<session>/<yesterday>.md` 拷贝到 `WeFlow-archived-exports/<session>/<yesterday>.md`。明早 cron 的 `cleanup="delete"` 会清空 processed，archived 不会丢；月报 / 年报 skill 以后从 archived 读全历史。

> Prompt 详见下文 §二次加工 Prompt。

## 子命令：`/wechat-diary summarize <folder>`

对用户自行手动导出的某个会话做一次性总结。

1. 跳过自动导出步骤，直接把 `WeFlow-raw-exports/<folder>` 视为输入。
2. 跑同一套预处理流水线（私聊 / 群聊的分支根据 `session.type` 字段自动判定）。
3. 归档到 `WeFlow-processed-exports/<folder>/<date>.md`，规则与默认流程一致。
4. 输出单一总结：`WeFlow-insights/Summaries/<folder>__<run-timestamp>.md`。

> Prompt 详见下文 §二次加工 Prompt → Summarize。

## 输入 / 输出速查

| 来源 | 路径 |
|---|---|
| 原始导出 | `WeFlow-raw-exports/<yyyymmdd> 每日导出聊天记录示例/...`（或生产中的实际命名） |
| 当日归档 | `WeFlow-processed-exports/<session>/<yyyy-mm-dd>.md`（明早被 cleanup="delete" 清掉） |
| 自己朋友圈 | `WeFlow-processed-exports/朋友圈_自己/<yyyy-mm-dd>.md`（图片/视频路径指向 raw 导出媒体，供多模态读取） |
| 长期归档 | `WeFlow-archived-exports/<session>/<yyyy-mm-dd>.md`（二次加工后从 processed 复制过来）|
| 日产出 | `WeFlow-insights/{Diary,DoneList,Inspirations,ExtraNotes}/<yyyy>/<yyyy-mm-dd>.md` |
| 一次性总结 | `WeFlow-insights/Summaries/<folder>__<timestamp>.md` |

## 二次加工 Prompt

Agent 在步骤 4 依次执行以下 prompt。每段输出的 Markdown 结构是**刚性契约**——月报 / 年报 skill 会按标题和列表样式做 glob 聚合，不要随意改结构。

### Prompt 0：读取素材

```
你是用户的私人日记助手。以下是你的输入素材：昨日的微信聊天记录（已经过预处理的极简聊天流）。

1. **确定日期**：`yesterday` = 今天日期减 1 天（本地时区）。

2. **读取 config.toml**：获取 `[user].self_wxids`（用户自己的 wxid 列表，通常含 filehelper）。匹配这些 wxid 的会话是用户的「收集箱」——用户给自己 / 文件传输助手发的消息，是最直接的一手素材。

3. **读取 processed exports**：用 Glob 工具列出 `WeFlow-processed-exports/*/{yesterday}.md`。
   - **跳过**下划线前缀目录（`_gf/`、`_targets/` 等）——这些是其他 skill 的旁路通道，diary 不读。
   - 对每个文件用 Read 工具读取内容。
   - 文件名格式：`私聊_<联系人>/<yyyy-mm-dd>.md` 或 `群聊_<群名>/<yyyy-mm-dd>.md`。
   - 内容格式：极简聊天流，每条消息 `发送者：内容`，「我」= 用户本人。
   - `[图片：<OCR 文字>]` = 图片 OCR 识别结果；`[图片]` = OCR 不可用。
   - `[语音]` = 语音转写失败，跳过。
   - `[表情]` = 微信表情 / 表情包，忽略即可。

   `朋友圈_自己/{yesterday}.md` 是例外输入：它不是聊天流，而是自己的朋友圈流。每条朋友圈一个块：时间、正文、`[图片：WeFlow-raw-exports/...]` / `[视频：WeFlow-raw-exports/...]`、评论、位置。遇到图片/视频路径时，用 Read 工具读取真实媒体文件做多模态理解；不要把它当成 OCR 文字，也不要因为路径存在就写成「发了一张图片」。

4. **标记收集箱**：session 名包含 `self_wxids` 中任一值、或 session 名含「文件传输助手」的，标记为收集箱。收集箱里用户发给自己的消息通常是笔记、备忘、灵感碎片。

5. **标记自己朋友圈**：`朋友圈_自己` 标记为「公开自我记录素材」。它可用于 Diary 的关键时刻/情绪状态、Inspirations 的项目灵感、ExtraNotes 的额外观察；DoneList 只在朋友圈正文明确描述已完成事实时采纳，不把晒图或情绪表达强行改写成 done。

先读取以上文件，然后按 Prompt 1（Diary）、Prompt 2（DoneList）、Prompt 3（Inspirations）、Prompt 4（ExtraNotes）依次输出四份文件。

全部写完后，运行长期归档（步骤 5）：
python -c "from wechat_diary_core import promote_day_to_archive, load_config; promote_day_to_archive('{yesterday}', load_config())"
把 {yesterday} 替换为实际日期（如 2026-05-19）。

如果 processed 目录下没有任何昨日的 md 文件（排除下划线前缀目录后），写一段「昨日无聊天记录」并跳过后续。
```

### Prompt 1：Diary

```
## 输出文件
`WeFlow-insights/Diary/{yyyy}/{yyyy-mm-dd}.md`

用 Write 工具创建文件。目录不存在时先 mkdir -p。

## 输出结构

# {yyyy-mm-dd} 日记

## 今日概览
<!-- 3-5 句话，第一人称，概括今天的主要经历、心情和节奏。
从全部会话综合提炼，不要逐个会话列举。
收集箱里我发给自己的笔记/备忘是一手素材，可以直接采纳。 -->

## 关键时刻
<!-- 按时间顺序，挑出当天最值得记录的 3-8 个片段。格式：
- **HH:MM** 一句话描述发生了什么，涉及谁，我的态度/反应。
不要流水账复述每条消息，只保留有记忆价值的时刻。
可以引用原话（≤15 字，加引号），但不要大段搬运聊天记录。
群聊只保留跟我直接相关或我主动参与的讨论。 -->

## 今日收获
<!-- 今天学到的、想通的、发现的。1-3 条，每条一句话。
如果没有明确收获，删掉这个 section。 -->

## 情绪与状态
<!-- 用 1-2 个关键词概括今天的整体情绪基调，附一句简短理由。 -->

## 规则
- 全文第一人称。
- 语气自然，像真实日记，不要公文体或总结报告体。
- 不编造没有发生的事。不脑补消息背后的动机。
- 不要把 [表情] / [图片] / OCR 乱码当作正文内容。
- 如果当天聊天很少（总消息 < 10 条），概览从简，关键时刻可只写 1-2 条。
- 收集箱中以「D：」开头的条目让给 DoneList 处理，Diary 里不重复。
- 引用原话时标注来源人（不必标注会话名，除非有歧义）。
```

### Prompt 2：DoneList

```
## 输出文件
`WeFlow-insights/DoneList/{yyyy}/{yyyy-mm-dd}.md`

## 输出结构

# {yyyy-mm-dd} DoneList

## 正式条目
<!-- 收集箱（self_wxids 匹配的会话）中以「D：」开头的消息。
逐条提取，去掉「D：」前缀，保留原文。格式：
- 条目原文
如果没有「D：」开头的消息，删掉整个「正式条目」section。 -->

## 学习与技术
<!-- 今天在学习、编程、技术方面做了什么有意义的事。
从聊天记录中推断：讨论 bug 修复、写代码、调试、看论文、上课内容等。
每条一行，一句话描述。 -->

## 社交与沟通
<!-- 今天跟谁有了有意义的交流、帮了谁的忙、参加了什么活动。 -->

## 生活与日常
<!-- 运动、饮食、出行、购物、休息等生活类事项。 -->

## 规则
- 每个分类下 0-5 条。没有内容的分类直接删掉 section header。
- 条目必须是「做了」的事实，不是「打算做」或「想做」。
- 「正式条目」优先级最高：同一件事出现在「D：」里就不要在其他分类重复。
- 每条用一句话描述，不超过 30 字。
- 三个推断分类有交叉时，按最核心的归一个，不重复。
- 如果当天没有任何可提取的 done 事项，整个文件只写：
  `# {yyyy-mm-dd} DoneList\n\n今日无。`
```

### Prompt 3：Inspirations

```
## 输出文件
`WeFlow-insights/Inspirations/{yyyy}/{yyyy-mm-dd}.md`

## 输出结构

# {yyyy-mm-dd} 灵感与待办

## 项目灵感
<!-- 从聊天记录中捕捉到的项目想法、功能创意、技术方案。格式：
- **关键词**：一句话描述想法。来源：<会话名>
每条必须有可行动的方向，纯感想不算。 -->

## 待跟进
<!-- 聊天中提到但还没做的事：承诺回复某人、答应帮忙、约好的事、未关闭的讨论。格式：
- 内容描述。涉及：<联系人或群名>
必须有明确对象或隐含截止时间的才列入。 -->

## 值得深入的话题
<!-- 聊天中出现的有趣讨论或观点，值得之后花时间了解。格式：
- **话题**：一句话概括。来源：<会话名> -->

## 规则
- 每个 section 0-5 条。没有内容的 section 直接删掉 header。
- 如果当天没有任何灵感或待办，整个文件只写：
  `# {yyyy-mm-dd} 灵感与待办\n\n今日无。`
- 不要把 DoneList 里已完成的事项重复列为待办。
- 来源会话名从文件路径提取（例 `私聊_肖逸涵`、`群聊_小团体`）。
```

### Prompt 4：ExtraNotes

```
## 输出文件
`WeFlow-insights/ExtraNotes/{yyyy}/{yyyy-mm-dd}.md`

## 输出结构

# {yyyy-mm-dd} 额外观察

<!-- 你（Agent）在阅读聊天记录时主动发现的、用户可能没注意到的值得关注的点。
格式：
- **标签**：一句话描述。来源：<会话名>

可能的类型（不必全覆盖）：
- 社交信号：某人的情绪变化、求助暗示、关系动态
- 信息差：群里有人分享了重要信息但用户似乎没回应
- 时间敏感：提到的截止日期、需要确认的事项、即将到来的约定
- 异常模式：某人聊天频率突变、群讨论氛围异常等
-->

## 规则
- 0-5 条。如果没有值得提的，整个文件只写：
  `# {yyyy-mm-dd} 额外观察\n\n今日无特别发现。`
- 每条必须有具体依据（指向聊天中的某个片段），不要纯猜测。
- 不要重复 Diary / DoneList / Inspirations 已覆盖的内容。
- 保持中性客观，不做道德评判。
- 来源会话名从文件路径提取。
```

### Summarize Prompt（`/wechat-diary summarize <folder>` 子命令专用）

```
用户指定了一个会话文件夹做一次性总结。

## 步骤

1. **预处理**：运行
   python -c "from wechat_diary_core import archive; archive('WeFlow-raw-exports/{folder}')"
   把 {folder} 替换为用户指定的文件夹名。

2. **读取 processed**：预处理后产物在 `WeFlow-processed-exports/` 下，用 Glob + Read 读取。
   如果 processed 里已有该会话的 md 文件（用户可能已手动预处理过），直接读取，跳过步骤 1。

3. **写出总结**：用 Write 工具创建输出文件。
   输出路径：`WeFlow-insights/Summaries/{folder}__{timestamp}.md`
   其中 {timestamp} = 当前时间，格式 yyyymmdd-HHMMSS。

## 输出结构

# {folder} 总结

## 概要
<!-- 2-3 句话概括这个会话的主题、参与者和时间跨度。 -->

## 主题梳理
<!-- 按主题（不是按时间线）组织。每个主题一个三级标题：
### 主题名
- 要点 1
- 要点 2
提炼讨论的结论、决定和分歧。 -->

## 待办与后续
<!-- 对话中提取的行动项、未解决的问题、后续安排。
格式：
- 行动项描述。涉及：<谁>
没有则写「无」。 -->

## 规则
- 按主题组织，不要按时间线流水账。
- 优先提炼结论和决定，而非讨论过程。
- 每个主题下的要点用一句话概括。
- 不编造没有讨论过的内容。
```

---

## 不做的事

- 本 skill 不做单个联系人的私人画像或深度分析。涉及这类内容的逻辑都不在本开源 skill 范围内。
- 本 skill 不做跨天聚合。月报 / 年报 skill 以后另写，会读这些按日产出的 Markdown 文件。
