from __future__ import annotations

from dataclasses import dataclass

from ..config import Config, load_config
from .driver import Driver, DriverCommand, ExporterContext, run_driver_command
from .exporter import create_driver


@dataclass(frozen=True)
class BatchTranscribeRun:
    username: str
    commands: list[DriverCommand]


def batch_transcribe_voices_for(
    usernames: list[str],
    config: Config | None = None,
    driver: Driver | None = None,
) -> list[BatchTranscribeRun]:
    """Run WeFlow's built-in batch voice→text pipeline for each username.

    For each ``usernames[i]`` the driver walks the 13-step GUI sequence
    described in the project root CLAUDE.md §5.0.3:

      1. close_any_modal
      2. click 聊天 page
      3. set_text search box, type the username
      4. wait for 联系人 section to appear and click the contact there
         (NOT the 聊天记录 entry above it)
      5. open the 批量语音处理 dialog
      6. ensure 批量转文字 option is checked
      7. capture task-center baseline
      8. click 开始转写 (the dialog dismisses on its own)
      9. switch to 导出 page, open 任务中心
     10. wait_for_new_task_completion(baseline, title contains 语音批量转写)
     11. close 任务中心, return to 首页

    Empty ``usernames`` is a no-op (no driver created, no GUI touched).
    """
    if not usernames:
        return []

    cfg = config or load_config()
    poll_interval = max(1.0, float(cfg.automation.poll_export_interval_sec))
    completion_timeout = max(1800.0, cfg.automation.poll_export_interval_sec * 30)

    runs: list[BatchTranscribeRun] = []
    own_driver = driver is None
    active_driver = driver or create_driver(cfg)
    context = ExporterContext()
    try:
        for username in usernames:
            commands = _voice_transcribe_commands(username, poll_interval, completion_timeout)
            for command in commands:
                run_driver_command(active_driver, command, context=context)
            runs.append(BatchTranscribeRun(username=username, commands=commands))
    finally:
        close = getattr(active_driver, "close", None)
        if own_driver and callable(close):
            close()
    return runs


def _voice_transcribe_commands(
    username: str,
    poll_interval: float,
    completion_timeout: float,
) -> list[DriverCommand]:
    baseline_key = f"voice_transcribe::{username}"
    return [
        DriverCommand("close_any_modal", timeout=5),
        DriverCommand("click", "聊天"),
        DriverCommand("wait_for", "搜索", timeout=15),
        DriverCommand("set_text", "搜索", value=username),
        DriverCommand("wait_for_text_sequence", "联系人", value=username, timeout=15),
        DriverCommand("click_after_anchor", "联系人", value=username, timeout=15),
        DriverCommand("wait_for", "批量语音处理", timeout=15),
        DriverCommand("click", "批量语音处理"),
        DriverCommand("ensure_checked", "批量转文字", timeout=10),
        DriverCommand("capture_task_baseline", baseline_key),
        DriverCommand("wait_for_enabled", "开始转写", timeout=15),
        DriverCommand("click", "开始转写"),
        DriverCommand("close_current_modal", timeout=5),
        DriverCommand("click", "导出"),
        DriverCommand("wait_for_enabled", "任务中心", timeout=30),
        DriverCommand("click", "任务中心"),
        DriverCommand(
            "wait_for_new_task_completion",
            baseline_key,
            value=f"语音批量转写({username})",
            timeout=completion_timeout,
            poll_interval=poll_interval,
        ),
        DriverCommand("close_current_modal", timeout=5),
        DriverCommand("click", "首页"),
        # Clear the search box so the next username starts from a clean state.
        DriverCommand("click", "聊天"),
        DriverCommand("set_text", "搜索", value=""),
    ]
