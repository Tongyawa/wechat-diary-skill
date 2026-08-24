---
name: wechat-diary-skill
description: 用 WeFlow 导出并清洗微信聊天记录，生成日记、DoneList、灵感、额外观察，增量维护长期自我画像和年度主线；也支持按需导出指定会话与时间段、对单个会话做一次性总结、环境体检与归档摄取。用户说“跑 wechat-diary”“生成微信日记”“处理已有 raw”“导出某个会话”“总结某个微信会话”时使用。
---

# WeChat Diary

微信聊天记录 → 可长期回看的个人知识库。本文件是**路由表**：判断用户要什么、走哪条路径、该读哪份细节。参数详表和 prompt 都在 `references/`，按需加载，不要一次性全读。

## 运行边界

- **工作区** = 含 `config.toml` 的目录。所有 `WeFlow-*` 数据路径都从该配置解析；相对路径相对于工作区，**绝不相对于本 skill 目录**。入口统一按“显式 `--config` / `-Workspace` → 当前目录 → `WECHAT_DIARY_WORKSPACE` 指向的目录”发现配置；显式目标不存在时直接报错，不会降级到别的工作区。唯一例外是完整导出的显式工作区，它会从模板创建首份配置并进入首启引导。仍找不到时会列出探测路径和可执行的下一步，不会静默使用默认配置。
- **代码根** = 本文件所在目录。调用脚本一律用代码根下的 `scripts/`，并显式传入工作区。
- **默认导出后端是 WeFlow 本地 HTTP API**：需要 WeFlow 正在运行且已启用 API 服务，**不点 GUI、不需要提权**。只有 legacy GUI 后端（仅 WeFlow ≤4.x）才需要可见 Windows 桌面与提权。
- **动手前先看 `references/entrypoints.md` §0「共同前提与常见坑」**：token 语义、公众号默认跳过、API 日期格式、会话目录名契约——这几条踩过就会浪费一整轮排查。
- 跑不起来先跑 ④ 体检，别先读代码。

## 路由表

| 用户想要 | 走这条 | 细节读 |
|---|---|---|
| 跑日常 / 生成昨天的日记 | ① 完整导出 | `references/entrypoints.md` §1 → `references/prompt-daily.md` |
| 处理已有 raw / 重新生成某天 | ② 消费已有 raw | `references/entrypoints.md` §2 → `references/prompt-daily.md` |
| 总结某个会话（记录已在盘上） | ② 消费已有 raw → 总结 | `references/entrypoints.md` §2 → `references/prompt-summarize.md` |
| 总结某个会话（记录还没导出） | ③ 按需导出 → 总结 | `references/entrypoints.md` §3 → `references/prompt-summarize.md` |
| 只要某会话某时段的语料，不要总结 | ③ 按需导出 | `references/entrypoints.md` §3 |
| 跑不起来 / 环境体检 | ④ doctor | `references/entrypoints.md` §4 |
| 把旧的导出快照并进长期归档 | ⑤ 归档摄取 | `references/entrypoints.md` §5 |
| 打开某天的产物 | ⑥ 打开产物 | `references/entrypoints.md` §6 |
| 在 Git worktree 做实机验收前，让数据根指向主工作区 | ⑦ 初始化 worktree 配置 | `references/entrypoints.md` §7 |

**读 processed 内容之前，先读 `references/processed-format.md`**——它是占位符（引用 / 媒体 / 表情 / 文件 / 链接…）的唯一事实源，路径 ①②③ 都要用。

## 各路径的最小命令

约定 `$SkillRoot` = 代码根，`$Workspace` = 工作区。完整参数见 `references/entrypoints.md`。

**① 完整导出**（会归档轮转 live roots，同一时间只在一个工作区跑）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$SkillRoot\scripts\run_daily_export.ps1" -Workspace "$Workspace" -NoPause
```

**② 消费已有 raw**（不启动 WeFlow、不联网、raw 原样保留）

```powershell
python "$SkillRoot\scripts\process_existing_raw.py" --raw-root <raw目录> --day <yyyy-mm-dd> --require-day
```

**③ 按需导出**（只写 `--out` 指定的目录，不碰 live 数据）

```powershell
python "$SkillRoot\scripts\export_on_demand.py" --session <wxid或显示名子串> --start <起> --end <止> --out <输出目录>
```

周期性追加到已有会话夹：改用 `--merge-into <已有会话夹>`；起始日可从已有分日产物自动推导，完整参数见 `references/entrypoints.md` §3。

**④ 体检** `python "$SkillRoot\scripts\doctor.py"`
**⑤ 归档摄取** `python "$SkillRoot\scripts\archive_exports.py" --raw-root <旧raw> --processed-root <旧processed>`（默认复制保源；倒序覆盖会在整批写入前拒绝，移动与人工确认参数见 §5）
**⑥ 打开产物** `powershell ... -File "$SkillRoot\scripts\Open-LatestInsights.ps1" -Workspace "$Workspace"`（按日期挑用 `Open-InsightsByDate.ps1`）
**⑦ 初始化 worktree 配置**（在目标 worktree 根目录运行）`python "$SkillRoot\scripts\init_worktree_config.py"`

## 二次加工

路径 ① / ② 产出 processed 之后：完整读 `references/prompt-daily.md`，严格按 Prompt 0–6 依次生成四份日产物，并增量维护自我画像与年度主线。

**下划线开头的 sidecar 目录交给其他 skill，公开 diary 不读。**

## 收尾：提交产物

写完产物后，若 insights 根是 git 仓（存在 `.git`），提交本 skill 写的那部分：

```powershell
git -C "<insights根>" add Diary DoneList Inspirations ExtraNotes Profile Threads
git -C "<insights根>" commit -m "diary: <yyyy-mm-dd> 产物"
```

- **只 add 上面这些自己的产物目录，不要 `add -A`**——下划线 sidecar 属于其他 skill，由它们各自提交。
- 无改动时 `commit` 以「nothing to commit」退出，属正常。
- insights 根没有 `.git` 时整步跳过，不提示、不创建仓库。
- 目的是给长线产物一份本地版本历史与「哪天变了什么」的增量标记。是否建仓、是否加远端由用户决定。

## 输入输出

| 类型 | 工作区配置对应路径 |
|---|---|
| 原始导出 | `[paths].raw` |
| 当日清洗结果 | `[paths].processed/<session>/<yyyy-mm-dd>.md` |
| 长期归档 | `[paths].archived/{raw,processed}/...` |
| 日产物 | `[paths].insights/{Diary,DoneList,Inspirations,ExtraNotes}/<yyyy>/...` |
| 长期产物 | `[paths].insights/Profile/自我画像.md`、`[paths].insights/Threads/<yyyy>.md` |
| 单会话总结 | `[paths].insights/Summaries/<会话>__<时间戳>/` |

长期归档**不需要手动执行**：下一次完整导出会把当前 raw/processed 合并进 archived 根。

## 不做的事

- 只维护用户本人的长期画像，不为其他联系人建立画像。
- 不做跨天批量月报或年报；年度主线只是每天增量沉淀长程线索。
- 不把过程产物（逐条 LLM 调用记录、中间态）写进 insights——那里只放成品。
