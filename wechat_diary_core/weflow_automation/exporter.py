from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from ..config import Config, load_config
from .cdp_driver import CdpDriver
from .driver import Driver, DriverCommand, DriverUnavailable, run_driver_command
from .launcher import ensure_weflow_running


@dataclass(frozen=True)
class ExportRun:
    kind: str
    date: date
    output_dir: Path
    commands: list[DriverCommand]


def export_all_chats(
    date: date | str | None = None,
    config: Config | None = None,
    driver: Driver | None = None,
    output_dir: str | Path | None = None,
) -> ExportRun:
    cfg = config or load_config()
    day = _coerce_date(date)
    destination = Path(output_dir) if output_dir is not None else cfg.paths.raw
    commands = _all_chats_commands(cfg)
    _run_export(commands, cfg, driver)
    return ExportRun(kind="all_chats", date=day, output_dir=destination, commands=commands)


def export_moments_for(
    usernames: list[str],
    date: date | str | None = None,
    config: Config | None = None,
    driver: Driver | None = None,
    output_dir: str | Path | None = None,
) -> ExportRun:
    cfg = config or load_config()
    day = _coerce_date(date)
    destination = Path(output_dir) if output_dir is not None else cfg.paths.raw
    commands = _moments_commands(usernames, cfg)
    _run_export(commands, cfg, driver)
    return ExportRun(kind="moments", date=day, output_dir=destination, commands=commands)


def create_driver(config: Config | None = None) -> Driver:
    cfg = config or load_config()
    session = ensure_weflow_running(cfg)
    if cfg.automation.driver == "cdp":
        if not session.cdp_endpoint:
            raise DriverUnavailable("CDP endpoint was not available after launching WeFlow.")
        return CdpDriver.connect(session.cdp_endpoint)
    if cfg.automation.driver == "uia":
        from .uia_driver import UiaDriver

        return UiaDriver()
    if cfg.automation.driver == "template":
        from .template_driver import TemplateDriver

        return TemplateDriver()
    raise DriverUnavailable(f"Unsupported driver: {cfg.automation.driver}")


def _run_export(commands: Iterable[DriverCommand], config: Config, driver: Driver | None = None) -> None:
    own_driver = driver is None
    active_driver = driver or create_driver(config)
    try:
        for command in commands:
            run_driver_command(active_driver, command)
    finally:
        close = getattr(active_driver, "close", None)
        if own_driver and callable(close):
            close()


def _all_chats_commands(config: Config) -> list[DriverCommand]:
    return [
        DriverCommand("click_if_present", "关闭任务中心", timeout=2),
        DriverCommand("click_if_present", "关闭自动化导出", timeout=2),
        DriverCommand("click", "导出"),
        DriverCommand("wait_for", "自动化导出", timeout=30),
        DriverCommand("click", "自动化导出"),
        DriverCommand("wait_for_enabled", "立即执行", timeout=30),
        DriverCommand("click", "立即执行"),
        DriverCommand("click_if_present", "关闭自动化导出", timeout=5),
        DriverCommand("wait_for_enabled", "任务中心", timeout=30),
        DriverCommand("click", "任务中心"),
        DriverCommand("wait_for", "已完成", timeout=max(300, config.automation.poll_export_interval_sec * 10)),
        DriverCommand("click_if_present", "关闭任务中心", timeout=5),
        DriverCommand("click", "首页"),
    ]


def _moments_commands(usernames: list[str], config: Config) -> list[DriverCommand]:
    commands = [
        DriverCommand("click_if_present", "关闭任务中心", timeout=2),
        DriverCommand("click_if_present", "关闭时间范围设置", timeout=2),
        DriverCommand("click_if_present", "完成", timeout=2),
        DriverCommand("click_if_present", "取消", timeout=2),
        DriverCommand("click", "朋友圈"),
        DriverCommand("wait_for", "查找联系人", timeout=30),
    ]
    for username in usernames:
        commands.extend(
            [
                DriverCommand("set_text", "查找联系人", value=username),
                DriverCommand("wait_for_enabled", f"选择 {username}", timeout=30),
                DriverCommand("click", f"选择 {username}"),
            ]
        )
    commands.extend(
        [
            DriverCommand("wait_for_enabled", "导出朋友圈", timeout=30),
            DriverCommand("click", "导出朋友圈"),
            DriverCommand("wait_for", "导出格式", timeout=30),
            DriverCommand("click", "JSON"),
            DriverCommand("click", "点击选择输出目录"),
            DriverCommand("confirm_native_dialog", "选择导出目录", value="选择文件夹", timeout=30),
            DriverCommand("click_if_present", "关闭时间范围设置", timeout=2),
            DriverCommand("click_if_present", "全部时间", timeout=2),
            DriverCommand("click_if_present", "昨天", timeout=2),
            DriverCommand("click_if_present", "昨天", timeout=2),
            DriverCommand("click_if_present", "关闭时间范围设置", timeout=2),
            DriverCommand("wait_for_enabled", "开始导出", timeout=30),
            DriverCommand("click", "开始导出"),
            DriverCommand("wait_for", "完成", timeout=max(300, config.automation.poll_export_interval_sec * 10)),
            DriverCommand("click_if_present", "完成", timeout=5),
            DriverCommand("click_if_present", "关闭任务中心", timeout=5),
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
