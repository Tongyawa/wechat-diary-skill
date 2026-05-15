---
name: wechat-diary
description: 拉取昨日 WeChat 消息（经 WeFlow 自动化），清洗后归档，再生成日记 / DoneList / 灵感 / 额外观察四份 Markdown。默认每天早上由脚本链触发；也支持 `/wechat-diary summarize <folder>` 子命令对单个会话做一次性总结。
---

# wechat-diary

> **Phase A 骨架**。本文件是契约 —— 函数名、路径、产物结构。真正的二次加工 prompt 与底层 Python 实现都在 Phase B 落地。

## 默认流程（无参数）

无参数调用时跑下面这套流程，给早晨脚本链使用。

1. **导出** —— 调 `wechat_diary_core.weflow_automation.exporter.export_all_chats(date=yesterday)`。底层 driver 由 `config.toml [automation].driver` 决定（uia / cdp / template 三选一），驱动 WeFlow 走「导出 → 自动化导出 → 立即执行 → 任务中心 → 已完成 → 首页」流程。本 SKILL 不关心是哪一层 driver，全部走 `driver.click_by_name(...) / driver.set_text(...) / driver.wait_for(...)` 抽象接口。`yesterday` 按本地时区计算。
2. **清洗** —— 调 `wechat_diary_core.preprocessing.run(raw_date_dir)`。
   - 空会话文件夹丢弃。
   - `media/emojis/` 目录整体跳过（不做 OCR，消息里仅留 `[表情]` 占位）。
   - 私聊：保留全量消息流。
   - 群聊：走上下文窗口过滤 —— 保留所有 `isSend == 1` 的消息，加上窗口前 K 条 / 后 K 条 / 时间窗内的相邻消息，重叠区间合并。算法详见 `CLAUDE.md` §6.2。
   - 邻近时间的连续消息：仅保留首条的时间戳，省体积。
   - `media/images/*` 走本地 OCR，识别文本以 `[OCR] ...` 后缀内联到对应消息里。
   - 「转文字失败」标记仅写警告日志，不阻塞流程。
3. **归档** —— 调 `wechat_diary_core.archiving.archive(processed_date_dir)`，按会话写出 `WeFlow-processed-exports/<session_dir>/<yyyy-mm-dd>.json`（`session_dir` 去掉原始文件夹后缀的日期）。
4. **二次加工** —— 读 `WeFlow-processed-exports/**/<yesterday>.json`。在 `WeFlow-insights/` 下产出四份 Markdown：
   - `Diary/<yyyy>/<yyyy-mm-dd>.md` —— 第一人称当日日记。
   - `DoneList/<yyyy>/<yyyy-mm-dd>.md` —— 分类捕捉的 DoneList；优先把 `config.toml [user].self_wxids` 指定的「自己 / 文件传输助手」会话里以 `D：` 开头的条目升级为正式条目。
   - `Inspirations/<yyyy>/<yyyy-mm-dd>.md` —— 散落在各会话里的项目灵感与待办。
   - `ExtraNotes/<yyyy>/<yyyy-mm-dd>.md` —— Agent 主动挑出但我没注意到的值得关注的点。

> **Phase B TODO**：在此处补四段产物的具体 prompt，每段作为独立的 fenced 代码块，prompt 内严格规定输出结构（章节、列表样式），方便后续月报 / 年报 skill 按 glob 聚合。

## 子命令：`/wechat-diary summarize <folder>`

对用户自行手动导出的某个会话做一次性总结。

1. 跳过自动导出步骤，直接把 `WeFlow-raw-exports/<folder>` 视为输入。
2. 跑同一套预处理流水线（私聊 / 群聊的分支根据 `session.type` 字段自动判定）。
3. 归档到 `WeFlow-processed-exports/<folder>/<date>.json`，规则与默认流程一致。
4. 输出单一总结：`WeFlow-insights/Summaries/<folder>__<run-timestamp>.md`。

> **Phase B TODO**：在此处补总结 prompt；强调按主题 / 决定 / 后续动作组织，而不是按时间线流水账。

## 输入 / 输出速查

| 来源 | 路径 |
|---|---|
| 原始导出 | `WeFlow-raw-exports/<yyyymmdd> 每日导出聊天记录示例/...`（或生产中的实际命名） |
| 归档 | `WeFlow-processed-exports/<session>/<yyyy-mm-dd>.json` |
| 日产出 | `WeFlow-insights/{Diary,DoneList,Inspirations,ExtraNotes}/<yyyy>/<yyyy-mm-dd>.md` |
| 一次性总结 | `WeFlow-insights/Summaries/<folder>__<timestamp>.md` |

## 不做的事

- 本 skill 不分析任何单个联系人的情绪状态或关系动态。涉及这类内容的逻辑都不在本开源 skill 范围内。
- 本 skill 不做跨天聚合。月报 / 年报 skill 以后另写，会读这些按日产出的 Markdown 文件。
