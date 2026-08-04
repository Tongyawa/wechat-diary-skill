from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from ...config import Config, load_config
from ...workspace import CleanupMode, RotationResult, rotate_export_workspace
from .cdp_driver import CdpDriver
from .driver import (
    Driver,
    DriverCommand,
    DriverUnavailable,
    ExporterContext,
    describe_driver_command,
    run_driver_command,
)
from .launcher import ensure_weflow_running


@dataclass(frozen=True)
class ExportRun:
    kind: str
    date: date
    output_dir: Path
    commands: list[DriverCommand]
    rotation: RotationResult | None = None


def export_all_chats(
    date: date | str | None = None,
    config: Config | None = None,
    driver: Driver | None = None,
    output_dir: str | Path | None = None,
    cleanup: CleanupMode = "delete",
) -> ExportRun:
    """Export every chat for the given date via WeFlow's automation pipeline.

    ``cleanup`` controls how the raw + processed roots get wiped first so today's
    WeFlow output never mixes with yesterday's:

    - ``"delete"``: rmtree both roots without archiving.
    - ``"archive"`` (daily default): merge both roots into the ``paths.archived``
      library first (per-session, newer file wins), so nothing is lost.
    - ``"skip"``: do nothing (tests / partial reruns).
    """
    cfg = config or load_config()
    day = _coerce_date(date)
    destination = Path(output_dir) if output_dir is not None else cfg.paths.raw
    rotation = rotate_export_workspace(cfg, label="all_chats", mode=cleanup)
    commands = _all_chats_commands(cfg)
    _run_export(commands, cfg, driver)
    return ExportRun(kind="all_chats", date=day, output_dir=destination, commands=commands, rotation=rotation)


def export_moments_for(
    usernames: list[str],
    date: date | str | None = None,
    config: Config | None = None,
    driver: Driver | None = None,
    output_dir: str | Path | None = None,
) -> ExportRun:
    """Export the given contacts' Moments for the date via WeFlow.

    Moments is downstream of ``export_all_chats`` in the daily flow and writes
    into the same raw root, so it must not rotate the workspace itself.
    """
    cfg = config or load_config()
    day = _coerce_date(date)
    destination = Path(output_dir) if output_dir is not None else cfg.paths.raw
    commands = _moments_commands(usernames, cfg)
    _run_export(commands, cfg, driver)
    return ExportRun(kind="moments", date=day, output_dir=destination, commands=commands, rotation=None)


def wait_for_export_tasks_idle(
    config: Config | None = None,
    driver: Driver | None = None,
    *,
    title_contains: str = "",
) -> list[DriverCommand]:
    """Wait for WeFlow's task center to have no active export tasks.

    This is intentionally separate from raw-tree stability: the filesystem
    check protects readers from half-written files, while this protects the
    next GUI workflow from WeFlow's background task consuming the UI thread.
    """
    cfg = config or load_config()
    completion_timeout = max(1800.0, cfg.automation.poll_export_interval_sec * 30)
    poll_interval = max(1.0, cfg.automation.poll_export_interval_sec)
    commands = _task_center_idle_commands(title_contains, completion_timeout, poll_interval)
    _run_export(commands, cfg, driver)
    return commands


def create_driver(config: Config | None = None) -> Driver:
    cfg = config or load_config()
    session = ensure_weflow_running(cfg)
    if cfg.automation.driver == "cdp":
        if not session.cdp_endpoint:
            raise DriverUnavailable("CDP endpoint was not available after launching WeFlow.")
        return _connect_cdp_with_retry(session.cdp_endpoint, cfg.automation.cdp_busy_timeout_sec)
    if cfg.automation.driver == "uia":
        from .uia_driver import UiaDriver

        return UiaDriver()
    if cfg.automation.driver == "template":
        from .template_driver import TemplateDriver

        return TemplateDriver()
    raise DriverUnavailable(f"Unsupported driver: {cfg.automation.driver}")


def _connect_cdp_with_retry(endpoint: str, timeout: float, *, poll_interval: float = 1.0) -> CdpDriver:
    """Connect to CDP, retrying while WeFlow is briefly unresponsive.

    Two layers of the same "busy != dead" tolerance, both budgeted by
    ``timeout`` (= ``automation.cdp_busy_timeout_sec``):

    - *Connect layer*: ``CdpDriver.connect`` -> ``fetch_cdp_targets`` can raise
      ``DriverUnavailable`` when WeFlow is pegged at connect time. Retry over the
      window instead of failing on the first miss; always one attempt minimum.
    - *Per-evaluate layer*: ``ws_timeout`` makes each subsequent
      ``Runtime.evaluate`` wait out a transient renderer freeze rather than
      aborting the GUI step with a 10s ``socket.timeout`` (the moments
      date-range dialog failures). Floor of 10s keeps the prior behaviour when
      the busy budget is configured very low.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    ws_timeout = max(10.0, timeout)
    while True:
        try:
            return CdpDriver.connect(endpoint, ws_timeout=ws_timeout)
        except DriverUnavailable:
            if time.monotonic() >= deadline:
                raise
            time.sleep(poll_interval)


def _run_export(commands: Iterable[DriverCommand], config: Config, driver: Driver | None = None) -> None:
    own_driver = driver is None
    active_driver = driver or create_driver(config)
    context = ExporterContext()
    try:
        for command in commands:
            try:
                run_driver_command(active_driver, command, context=context)
            except Exception:
                # Dump the live dialog's interactive elements before the driver
                # closes, so a GUI step failure (e.g. moments date-range 确认 not
                # enabling) is diagnosable from the runlog instead of guesswork.
                _dump_failure_diagnostics(active_driver, command)
                raise
    finally:
        close = getattr(active_driver, "close", None)
        if own_driver and callable(close):
            close()


def _dump_failure_diagnostics(driver: Driver, command: DriverCommand) -> None:
    """Best-effort: log visible interactive elements after a failed command.

    Lines are prefixed ``[weflow]`` so the PowerShell wrapper keeps them out of
    the console but still writes them to the runlog. Never raises.
    """
    dump = getattr(driver, "visible_elements", None)
    if not callable(dump):
        return
    try:
        elements = dump()
    except Exception as exc:  # diagnostics must never mask the real failure
        print(f"[weflow] failure diagnostics unavailable: {exc}", file=sys.stderr)
        return
    print(f"[weflow] DUMP after failed command {describe_driver_command(command)}:", file=sys.stderr)
    shown = 0
    for element in elements:
        if not isinstance(element, dict):
            continue
        text = str(element.get("text") or "").strip()
        if not text:
            continue
        print(
            f"[weflow]   tag={element.get('tag')} role={element.get('role')!r} "
            f"enabled={element.get('enabled')} text={text!r}",
            file=sys.stderr,
        )
        shown += 1
    if shown == 0:
        print("[weflow]   (no visible interactive elements captured)", file=sys.stderr)


def _all_chats_commands(config: Config) -> list[DriverCommand]:
    completion_timeout = max(1800.0, config.automation.poll_export_interval_sec * 30)
    poll_interval = max(1.0, config.automation.poll_export_interval_sec)
    return [
        DriverCommand("close_any_modal", timeout=5),
        # Capture the task-center baseline before triggering 立即执行 so we can tell
        # today's run apart from yesterday's still-visible 已完成 row.
        DriverCommand("wait_for_enabled", "任务中心", timeout=30),
        DriverCommand("click", "任务中心"),
        DriverCommand("capture_task_baseline", "all_chats"),
        DriverCommand("close_current_modal", timeout=5),
        DriverCommand("click", "导出"),
        DriverCommand("wait_for", "自动化导出", timeout=30),
        DriverCommand("click", "自动化导出"),
        DriverCommand("wait_for_enabled", "立即执行", timeout=30),
        DriverCommand("click", "立即执行"),
        DriverCommand("close_current_modal", timeout=5),
        DriverCommand("wait_for_enabled", "任务中心", timeout=30),
        DriverCommand("click", "任务中心"),
        DriverCommand(
            "wait_for_new_task_completion",
            "all_chats",
            value="自动化导出",
            timeout=completion_timeout,
            poll_interval=poll_interval,
        ),
        DriverCommand("close_current_modal", timeout=5),
        DriverCommand("click", "首页"),
    ]


def _task_center_idle_commands(
    title_contains: str,
    completion_timeout: float,
    poll_interval: float,
) -> list[DriverCommand]:
    return [
        DriverCommand("close_any_modal", timeout=5),
        DriverCommand("wait_for_enabled", "任务中心", timeout=30),
        DriverCommand("click", "任务中心"),
        DriverCommand(
            "wait_for_task_center_idle",
            title_contains,
            timeout=completion_timeout,
            poll_interval=poll_interval,
        ),
        DriverCommand("close_current_modal", timeout=5),
        DriverCommand("click", "首页"),
    ]


def _moments_commands(usernames: list[str], config: Config) -> list[DriverCommand]:
    commands = [
        DriverCommand("close_any_modal", timeout=5),
        DriverCommand("wait_for_absent", "导出格式", timeout=5),
        DriverCommand("wait_for", "朋友圈", timeout=30),
        DriverCommand("click_after_anchor", "聊天", value="朋友圈", timeout=15),
        DriverCommand("wait_for", "查找联系人", timeout=30),
        DriverCommand("click", "查找联系人"),
    ]
    for username in usernames:
        commands.append(DriverCommand("set_text", "查找联系人", value=username))
        if _looks_like_wxid(username):
            commands.extend(
                [
                    DriverCommand("wait_for_moments_contact_ready", username, timeout=30),
                    DriverCommand("click_after_anchor", "全选", value="选择", timeout=30),
                ]
            )
        else:
            commands.extend(
                [
                    DriverCommand("wait_for_text_sequence", username, value="条", timeout=30),
                    DriverCommand("ensure_selected", username, timeout=30),
                ]
            )
    commands.extend(
        [
            DriverCommand("ensure_action_available", "下载所选", value="全选", timeout=30),
            DriverCommand("click", "下载所选"),
            DriverCommand("wait_for", "导出格式", timeout=30),
            *_contact_verification_commands(usernames),
            DriverCommand("click", "JSON"),
            DriverCommand("click", "点击选择输出目录"),
            DriverCommand("confirm_native_dialog", "选择导出目录", value="选择文件夹", timeout=30),
            DriverCommand("click", "全部时间"),
            DriverCommand("wait_for", "时间范围设置", timeout=10),
            DriverCommand("click", "昨天"),
            DriverCommand("wait_for_enabled", "确认", timeout=10),
            DriverCommand("click", "确认"),
            DriverCommand("wait_for_absent", "时间范围设置", timeout=10),
            DriverCommand("wait_for", "昨天", timeout=10),
            DriverCommand("ensure_checked", "图片", timeout=10),
            DriverCommand("ensure_checked", "实况图", timeout=10),
            DriverCommand("ensure_checked", "视频", timeout=10),
            *_contact_verification_commands(usernames),
            DriverCommand("wait_for_enabled", "开始导出", timeout=30),
            DriverCommand("click", "开始导出"),
            DriverCommand("wait_for", "完成", timeout=max(300, config.automation.poll_export_interval_sec * 10)),
            DriverCommand("click", "完成"),
            DriverCommand("close_current_modal", timeout=5),
            DriverCommand("click", "首页"),
        ]
    )
    return commands


def _coerce_date(value: date | str | None) -> date:
    if value is None:
        return datetime.now().date() - timedelta(days=1)
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def _looks_like_wxid(value: str) -> bool:
    return value.strip().startswith(("wxid_", "gh_"))


def _contact_verification_commands(usernames: list[str]) -> list[DriverCommand]:
    commands: list[DriverCommand] = []
    for username in usernames:
        if _looks_like_wxid(username):
            commands.append(DriverCommand("wait_for", "联系人", timeout=30))
        else:
            commands.append(DriverCommand("wait_for_text_sequence", "联系人", value=username, timeout=30))
    return commands
