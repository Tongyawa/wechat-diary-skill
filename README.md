# WeChat Diary

WeChat Diary 是一个本地微信日记工作流：通过 WeFlow 导出微信聊天记录，把原始导出清洗成更适合 Agent 阅读的 Markdown，再由 `wechat-diary` skill 生成日记、DoneList、灵感、额外观察，并增量维护长期自我画像和年度主线脉络。

这个 README 只是初稿。项目后续还会大改，当前说明以 `.claude/skills/wechat-diary/SKILL.md` 和现有目录结构为准。

## 解决什么问题

微信聊天记录里有大量个人经历、待办、灵感和长期线索，但原始导出不适合直接阅读或长期检索。本项目把它拆成两层：

1. **工程层**：自动化 WeFlow 导出、清洗、压缩群聊上下文、OCR 图片、归档 raw/processed 数据。
2. **Agent 层**：读取 processed Markdown，生成每日可读产物，并把长期信号沉淀进画像和主线文件。

最终目标不是备份微信，而是把每天散落在聊天、文件传输助手和朋友圈里的信息转成可回看的个人知识库。

## 当前主要产物

默认 `wechat-diary` skill 会在 `WeFlow-insights/` 下生成：

- `Diary/<yyyy>/...md`：第一人称日记。
- `DoneList/<yyyy>/...md`：当天完成事项。
- `Inspirations/<yyyy>/...md`：项目灵感、待跟进事项、值得深入的话题。
- `ExtraNotes/<yyyy>/...md`：Agent 额外观察到、但用户可能没注意到的点。
- `Profile/自我画像.md`：长期自我画像，增量维护。
- `Threads/<yyyy>.md`：年度主线脉络，增量维护。

## 目录结构

```text
.
├── .claude/skills/wechat-diary/   # 公开 diary skill，定义二次加工契约
├── docs/                          # 工程文档
├── scripts/                       # 日常入口、补跑入口、归档和校验脚本
├── tests/                         # 单元测试
├── wechat_diary_core/             # 导出、清洗、归档、配置等核心代码
├── config.example.toml            # 配置模板
├── requirements.txt               # Python 依赖
└── Start-DailyExport.bat          # 双击式每日导出入口
```

运行时还会用到这些 gitignored 数据目录，实际位置由 `config.toml [paths]` 决定：

```text
WeFlow-raw-exports/          # WeFlow 原始导出，下一轮运行前会合并进长期归档
WeFlow-processed-exports/    # 清洗后的当日 Markdown，供 skill 读取
WeFlow-archived-exports/     # 长期 raw/processed 归档库
WeFlow-insights/             # 日记、DoneList、灵感、画像、主线等最终产物
```

## 初次配置

1. 安装 Python 依赖：

   ```powershell
   python -m pip install -r requirements.txt
   ```

2. 复制配置模板：

   ```powershell
   Copy-Item config.example.toml config.toml
   ```

3. 编辑 `config.toml`，至少确认：

   - `[automation].weflow_exe`：本机 WeFlow 可执行文件路径。
   - `[paths]`：raw、processed、archived、insights 的落点。
   - `[user].self_wxids`：自己的 wxid / 文件传输助手，用于识别收集箱。
   - `[skills].daily`：默认包含 `wechat-diary`。

`config.toml` 不进 git，适合放本机路径、账号标识和其他私人配置。

## 常用用法

每日完整导出：

```powershell
.\Start-DailyExport.bat
```

Agent 或命令行环境更适合直接跑：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_export.ps1 -NoPause
```

只处理已经存在的 WeFlow raw 导出，不重新启动 WeFlow：

```powershell
python scripts/process_existing_raw.py --raw-root WeFlow-raw-exports --day 2026-06-15 --require-day
```

打开最近的 insights 产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Open-LatestInsights.ps1
```

按日期打开 insights 产物：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Open-InsightsByDate.ps1
```

## 二次加工怎么发生

当前仓库没有统一的一键二次加工脚本。日常导出完成后，由 Agent 在同一工作目录按 `config.toml [skills].daily` 顺序执行 skills。

公开默认配置是：

```toml
[skills]
daily = ["wechat-diary"]
```

`wechat-diary` 会读取 `WeFlow-processed-exports/*/<date>.md`，跳过下划线开头的 sidecar 目录，然后按 `.claude/skills/wechat-diary/SKILL.md` 里的 Prompt 契约生成 Markdown 产物。

## 测试

公开仓库单元测试：

```powershell
python -m unittest discover -s tests
```

一次性测试输出、日志和临时对照文件应放进 `tests/_artifacts/<yyyy-mm-dd>-<topic>/`，不要散落在仓库根目录。

## 注意事项

- GUI 自动化依赖 WeFlow 当前界面和 Windows 桌面环境；Agent 触发真实导出时通常需要提权运行。
- `WeFlow-*`、`config.toml`、最终 insights 产物都属于本地数据，不应提交进公开仓库。
- 群聊默认经过上下文窗口过滤，保留和用户相关的片段；需要完整语料时可将
  `preprocessing.group_context_window.enabled` 设为 `false`，此时群聊保留全量消息流。私聊始终保留全量消息流。
- 图片可走本地 OCR，微信表情目录会跳过，语音转文字失败默认只记录警告。
- 长期归档采用合并覆盖语义：相同相对路径下，新导出覆盖旧导出，用于避免重复运行产生重复文件。
