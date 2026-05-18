# Handoff: codex-daily-export-script

## 1. 意图与验收

问题：把“每日导出到 processed 层”的手动流程做成 Windows 双击可运行脚本，并填平本轮真实运行暴露的问题：缺少 `config.toml`、无 target 时不应强制引导、WeFlow 语音转文字 CDP 等待误判、PowerShell 中文乱码/NativeCommandError、旧 raw/processed 重跑前清理、WeFlow UI 显示完成但 raw 文件仍在落盘导致 processed 为空。

完成 = 双击/等价运行 `Start-DailyExport.bat` 后，脚本自动 stop WeFlow -> rotate raw/processed -> start/wait CDP -> 可选语音转文字 -> 全部聊天导出 -> 可选 target 朋友圈导出 -> 等 raw 稳定 -> diary processed -> 可选 target sidecar processed，并在失败时输出阶段、原因、下一步。

## 2. 范围

改了：新增双击入口与 daily runner；新增中性的 `[daily_export]` 配置；补充 config 自动生成/补全；强化 workspace rotation 处理 ReadOnly 文件；修正语音转写任务中心等待、失败重试和多余 UI 跳转；增加 raw 文件树稳定等待；修复 PowerShell UTF-8 日志和 native stderr 误判；补充单测。

没改：不调用 Claude/Agent 二次加工生成 Diary/DoneList/私有复盘；不 push；不把本机私有 `config.toml` 入库；不改变 WeFlow 自身转写失败的底层能力，只做有限重试。

## 3. 改动

`Start-DailyExport.bat`：新增根目录双击入口，使用 `powershell -ExecutionPolicy Bypass` 调 `scripts/run_daily_export.ps1`。

`scripts/run_daily_export.ps1`：从 `*>&1 | Tee-Object` 改为 `cmd /d /c ... 2>&1` 加 UTF-8 环境变量和 UTF-8 日志写入，避免 WeFlow stderr 触发 PowerShell `NativeCommandError`，并修复中文乱码。

`scripts/run_daily_export.py`：实现 daily export 主流程；`config.toml` 不存在时从 example 生成；无 target 时只跑 diary processed；有 target 时默认用 target 填补空的 `voice_transcribe_usernames`；导出后新增 `wait_for_raw_exports_stable()`，避免 processed 归档抢跑；输出 processed 文件与 rotation 位置。

`config.example.toml` / `wechat_diary_core/config.py`：新增 `DailyExportConfig` 和 `[daily_export]` 示例字段：`target_usernames`、`target_processed_subroot`、`cleanup_mode`、`restart_weflow`。

`wechat_diary_core/workspace.py`：rotation archive/delete 对 ReadOnly 文件先 chmod 再清理，避免旧媒体文件导致重跑前 raw/processed 清理失败。

`wechat_diary_core/weflow_automation/voice_transcribe.py`：语音转写先抓任务中心 baseline；`批量转文字` 按按钮/标签点击而非 checkbox；等待标题从 `语音批量转写(<username>)` 改为通用 `语音批量转写`；任务失败/取消时有限重试，默认最多 3 次；删除完成后额外跳回聊天清搜索的无效步骤。

`wechat_diary_core/weflow_automation/cdp_driver.py` / `driver.py`：新增 `TaskFailed`；`wait_for_new_task_completion()` 遇到新任务行 `失败` / `已取消` 立即抛错，避免等到超时；仍用 baseline 排除旧任务行。

`tests/*`：覆盖 config 缺失/无 target、有 target voice 默认值、runner 顺序、ReadOnly rotation、PowerShell wrapper、raw 稳定等待、语音失败重试、CDP 失败任务行。

## 4. 决策与假设

含糊点：公开脚本是否必须引导 target。选择：不强制。依据：公开 diary skill 不需要指定联系人；target 是本机自用可选 sidecar。

含糊点：导出任务中心显示“已完成”后何时归档。选择：再等 raw 文件树稳定，默认 8 秒 quiet window、180 秒超时。依据：真实运行中 WeFlow 18:08:35 已显示完成，但 raw 目录 18:08:56-18:09:04 仍在写，导致 processed 为空。何时重选：如果后续仍出现 processed 缺文件，调大 quiet window 或增加更强的 raw 完整性判据。

含糊点：语音转写失败是否无限循环。选择：有限重试，默认 3 次。依据：真实观察中一次失败后手动重跑可补齐；无限循环会掩盖 WeFlow 真故障。何时重选：如果连续失败是常态，应暴露配置项或跳过语音转写继续导出。

含糊点：语音转写完成后是否清搜索框。选择：删除多余“回聊天清搜索”步骤。依据：下一轮会重新定位搜索框并覆盖输入；单联系人日常流程不需要该 UI 抖动。

## 5. 验证

命令：`python -m unittest discover -s tests`

结果：通过；Ran 76 tests in 2.475s, OK。最后一次确认在当前最终代码上通过；输出中包含预期的 voice retry 单测日志。

命令：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_export.ps1 -NoPause`

结果：通过。最近一次真实运行输出：`Daily export completed`；`Diary processed files: 12`；`Sidecar chat files: 1`；`Sidecar moments files: 1`。

环境前提：Windows；本机 `config.toml` 存在且 gitignored；WeFlow 可由配置路径启动；CDP driver 使用 `electron_cdp_port=9222`；当前本机 target sidecar subroot 配为 `_gf`。

真实产物：`E:\.100_Code\Github\Wechat_Diary\WeFlow-processed-exports\` 下生成 14 个 `2026-05-17.md`，其中 diary 12 个、`_gf\chats\2026-05-17.md` 1 个、`_gf\moments\2026-05-17.md` 1 个。

旧产物归档：最近一次真实运行归档到 `E:\.100_Code\Github\Wechat_Diary\其他\test\test_archive\20260518-183127-daily_export`。

未覆盖：最终代码未再用鼠标双击 `.bat` 做一次人工启动，只用等价 PowerShell 命令；真实 WeFlow 最新一次没有复现“语音任务失败后自动重试”，该路径由单测模拟覆盖；没有验证多 target 并发/连续联系人场景。

## 6. 风险与评审重点

重点查：`scripts/run_daily_export.py` 的流程顺序是否符合“到 processed”目标；`wait_for_raw_exports_stable()` 的文件树签名是否足够稳；`voice_transcribe.py` 的失败重试是否会误吞真正的 CDP/定位异常。

薄弱点：任务中心 row 解析仍是 DOM 文本启发式；WeFlow UI 文案或 DOM 结构变化会影响 baseline 和 status 检测；PowerShell wrapper 通过 `cmd /d /c` 汇合 stderr/stdout，退出码依赖 `$LASTEXITCODE`。

未验证启发式：8 秒 raw quiet window；`失败` / `已取消` 作为终止失败状态；`语音批量转写` 通用标题匹配不会误匹配旧任务行，依赖 baseline 排除旧行。

## 7. 状态

分支/worktree：`codex-daily-export-script` / `E:\.100_Code\Github\Wechat_Diary\.claude\worktrees\codex-daily-export-script`

base SHA：`f877bed5b62c285720f2092d86b0407aba7e78fb`

提交：`10ebd67 feat: add double-click daily export runner`；`a4e1275 fix: harden daily export automation`；本 handoff 文件为最后一个提交。

已rebase：否

已push：否

## 8. 待办与移交

下一个 Agent：读本 handoff 后复跑 `python -m unittest discover -s tests`；如需直观验收，再运行 `powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run_daily_export.ps1 -NoPause` 或双击 `Start-DailyExport.bat`，观察 processed counts 与 `.runlog`。

阻塞：无。

待人决策：是否把 `wait_for_raw_exports_stable()` 的 quiet window / 语音重试次数暴露进 `config.toml`；是否把真实双击 `.bat` 作为合入前人工验收项。

---

## 评审

结论：通过（带 2 处修复）。

查了：Claude Opus 4.7 静态审阅 `git diff main..codex-daily-export-script` 全部 15 个文件。

命令：`python -m unittest discover -s tests`（修复后 77 个全过，含新增 1 个）。

没查：未独立复跑 `.bat`（用户已盯过真实产物）；未穷举 `_set_toml_value` 对多行字符串/表数组的鲁棒性（当前涉及的键都是简单 string/bool/字符串数组）。

问题1：`ensure_local_config` 每次 daily run 都无条件 `_set_toml_value` 写回 4 个 `[daily_export]` 键，而 `_set_toml_value` 用 `^[ \t]*{key}[ \t]*=.*$` 整行替换，会吞掉行尾 `# comment`。后果：从 `config.example.toml` 继承过来的注释在首次 daily run 后永久消失。

修复：改为「key 缺失才插入」；present 的键不再 normalize。`voice_transcribe_usernames` 自动填充因为是真值变更（[] → target_usernames）保持原样，inline 注释丢失视为该 feature 的代价。

问题2：首次双击 `.bat` 时 `input("WeFlow.exe path: ")` 的 prompt 走 python stdout → `cmd /d /c 2>&1` → PowerShell `ForEach-Object` 管道，partial 行（无换行）被块缓冲卡住、用户看不到提示就以为窗口卡死。

修复：prompt 前显式 `print(..., flush=True)`，`input()` 用空 prompt 参数。

新增测试：`test_ensure_local_config_preserves_inline_comments_on_existing_keys` 钉死问题1的回归。

未修（建议后续）：
- `TASK_FAILURE_STATUSES` 用子串匹配，「失败重试中」会误触发重试；3 次上限兜底。
- PowerShell wrapper 的 `cmd /d /c "python" "{0}" 2>&1` 引号脱壳依赖当前路径无空格；clone 到带空格目录可能脱错。
- `archive_moments_for(None, ...)` 当前因 rotation 先跑而安全；传 `target_usernames` 语义更紧。
