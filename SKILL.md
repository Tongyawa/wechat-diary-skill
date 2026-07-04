---
name: wechat-diary-skill
description: 用 WeFlow 导出并清洗微信聊天记录，生成日记、DoneList、灵感、额外观察，增量维护长期自我画像和年度主线；也支持对单个会话做一次性总结。用户说“跑 wechat-diary”“生成微信日记”“处理已有 raw”或“总结某个微信会话”时使用。
---

# WeChat Diary

## 运行边界

- 把包含 `config.toml` 的目录视为**工作区**。所有 `WeFlow-*` 数据路径都从该配置解析；相对路径相对于工作区，绝不相对于本 skill 目录。
- 把本文件所在目录视为**代码根**。调用脚本时使用代码根下的 `scripts/`，并显式传入工作区。
- 若当前目录没有 `config.toml`，先在用户给出的项目目录中查找；仍无法确定时再询问工作区位置。
- 真实 WeFlow GUI 自动化需要可见 Windows 桌面，Agent 执行时通常需要提权。

## 默认流程

1. 读取工作区 `config.toml`，确认 raw、processed、archived、insights 四个数据根。
2. 用户要求完整导出时，在工作区运行：

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File "<skill-root>\scripts\run_daily_export.ps1" -Workspace "<workspace>" -NoPause
   ```

3. 用户明确要求只处理现有 raw、不启动 WeFlow 时，在工作区运行：

   ```powershell
   python "<skill-root>\scripts\process_existing_raw.py" --raw-root WeFlow-raw-exports --day <yyyy-mm-dd> --require-day
   ```

   `--raw-root` 可省略并从配置读取。单一日期后缀可自动推断日期；区间导出或多日期混合时必须显式传 `--day`。

4. processed 生成后，完整读取 [references/prompt-daily.md](references/prompt-daily.md)，严格按 Prompt 0–6 依次生成四份日产物并增量维护画像与主线。以下划线开头的 sidecar 目录交给其他 skill，不纳入公开 diary。
5. 不手动执行长期归档；下一次 daily export 会把当前 raw/processed 合并进配置指定的 archived 根。

## 单会话总结

用户要求总结某个会话文件夹时，完整读取 [references/prompt-summarize.md](references/prompt-summarize.md)，跳过 WeFlow 自动导出，只处理工作区内指定的 raw 会话。

## 输入输出

| 类型 | 工作区配置对应路径 |
|---|---|
| 原始导出 | `[paths].raw` |
| 当日清洗结果 | `[paths].processed/<session>/<yyyy-mm-dd>.md` |
| 长期归档 | `[paths].archived/{raw,processed}/...` |
| 日产物 | `[paths].insights/{Diary,DoneList,Inspirations,ExtraNotes}/<yyyy>/...` |
| 长期产物 | `[paths].insights/Profile/自我画像.md`、`[paths].insights/Threads/<yyyy>.md` |

## 不做的事

- 只维护用户本人的长期画像，不为其他联系人建立画像。
- 不做跨天批量月报或年报；年度主线只是每天增量沉淀长程线索。
