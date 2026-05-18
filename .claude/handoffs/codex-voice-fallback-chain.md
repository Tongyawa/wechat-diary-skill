# Handoff: codex-voice-fallback-chain

## 1. 意图与验收

问题：双击 `Start-DailyExport.bat` 的公开日常导出链路缺少“raw 导出完成后、processed 渲染前”的可选本地语音兜底步骤；私有流水线需要在这里修复 raw 中的语音转写失败，再让后续 diary/sidecar processed 自然消费修复后的 raw。

完成 = `[daily_export].voice_fallback_script` 为空时公开默认行为不变；非空时 runner 在 raw 稳定后、`archive_diary_processed` 前调用该脚本；单测覆盖默认跳过和调用顺序。

## 2. 范围

改了：公开配置结构、示例配置、daily export runner、runner 单测。

没改：没有把私有脚本路径硬编码进公开仓库；没有引入 whisper 依赖到公开 `requirements.txt`；没有改变已有 `archive()` / `archive_chats_for()` 分日逻辑。

## 3. 改动

`wechat_diary_core/config.py`：`DailyExportConfig` 增加 `voice_fallback_script: Path | None`，默认空字符串解析为 `None`；非空路径按 config 所在目录解析为绝对路径。

`config.example.toml`：`[daily_export]` 增加 `voice_fallback_script = ""`，注释说明这是本地可选脚本，公开默认留空。

`scripts/run_daily_export.py`：`DailyExportDeps` 增加 `run_voice_fallback_script` 便于测试注入；新增 `run_voice_fallback_script(script_path, cfg)`，用当前 Python 执行脚本并传 `--raw-root <cfg.paths.raw>`，若有 `target_usernames` 则逐个传 `--target-wxid`。

`scripts/run_daily_export.py`：在 `export_all_chats` / `export_target_moments` 后的 raw-stable 检查完成之后、`archive_diary_processed` 之前插入 `voice_fallback` stage；未配置时打印 `voice_fallback skipped: no configured script.`。

`tests/test_run_daily_export_script.py`：补默认配置解析、注释保留、target 为空跳过、target 存在调用顺序等测试。

本机未入库配置：根 `config.toml` 已把 `voice_fallback_script` 指向本地私有脚本路径，该路径与脚本均被 gitignore，公开 repo 不感知。

## 4. 决策与假设

含糊点：语音兜底为什么放在 archive 前。

选择：放在 raw 导出稳定后、任何 processed markdown 渲染前。

依据：语音兜底的真实数据源是 raw JSON + `media/voices`；只要先修 raw，后续 diary processed 和 sidecar processed 都使用同一份修复结果，不需要对 markdown 做二次替换。

何时重选：如果未来只想修 sidecar，不想影响 diary 流，可改成只在 `archive_target_chats` 前生成 sidecar 专用临时 processed；当前需求是修 raw。

含糊点：公开仓库是否应该知道私有脚本位置。

选择：公开仓库只暴露中性 `voice_fallback_script` 配置项，不写任何私有路径或用途名。

依据：公开核心包必须话题中性；路径由本机 `config.toml` 决定。

何时重选：如果未来开源通用 voice fallback，可把脚本移入公开 `wechat_diary_core` 并改为正式依赖；当前不做。

## 5. 验证

命令：`python -m unittest tests.test_run_daily_export_script tests.test_config`

结果：通过，11 tests OK。

命令：`python -m unittest discover -s tests`

结果：通过，78 tests OK。注意 `python -m unittest` 在本 repo 默认发现 0 个测试，需用 discovery。

命令：`python .claude/worktrees/codex-voice-fallback/voice_fallback.py --raw-root WeFlow-raw-exports --target-wxid <target> --dry-run`

结果：`scanned_exports=1`、`matched_exports=1`、`failures=0`，证明真实 raw 已修复。

环境前提：公开链路不需要安装 whisper；只有配置了实际 fallback 脚本的本机才需要该脚本自己的依赖。

未覆盖：未通过双击 `Start-DailyExport.bat` 跑完整 GUI 链路；GUI 导出耗时且会重启 WeFlow，当前只测 Python runner 编排。

## 6. 风险与评审重点

重点查：公开代码是否保持话题中性；`voice_fallback_script` 为空时行为是否完全兼容；非空路径执行失败是否能通过 `DailyExportStageError(stage="voice_fallback")` 暴露。

薄弱点：`run_voice_fallback_script` 当前只传 `--raw-root` 和可选 `--target-wxid`，其他参数由 fallback 脚本自己的 config/默认值决定；如果某脚本需要额外参数，需扩展公开配置。

未验证启发式：多个 target 时逐个调用脚本；如果脚本本身支持多 target，当前不是最高效，但行为清晰。

## 7. 状态

分支/worktree：`codex-voice-fallback-chain` / `E:\.100_Code\Github\Wechat_Diary\.claude\worktrees\codex-voice-fallback-chain`

base SHA：`82475f2e622c2451c896f7c71b9a1ce0659ffb1d`

提交：未提交

已rebase：否

已push：否

## 8. 待办与移交

下一个Agent：review 后提交公开链路分支；等私有 `voice_fallback.py` 合回主私有目录后，再双击 `Start-DailyExport.bat` 做一次端到端验收。

阻塞：私有脚本尚未合入其私有 repo 的主分支，因此本机 `config.toml` 指向的最终路径目前在主 worktree 还不存在；合并后再做一次端到端验收。

待人决策：是否把 `voice_fallback_script` 的执行参数扩展为配置项，例如 `voice_fallback_args`。

---

## 评审

结论：待评审

查了&命令：见第 5 节。

没查：未跑真实 GUI/BAT 全链路。

问题：none

最小修复：若 review 认为公开配置项过窄，增加 `voice_fallback_args: list[str]` 并在 runner 中拼接。
